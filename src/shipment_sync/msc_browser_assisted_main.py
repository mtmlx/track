from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
import requests
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from dotenv import load_dotenv

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.main import _filter_shipments, _print_preview_plan
from shipment_sync.msc_browser_assisted import (
    build_queue,
    MscBrowserCapture,
    consolidate_browser_statuses,
    is_msc_line,
    read_import_batch,
    status_from_browser_capture,
    write_queue,
)
from shipment_sync.terminal import terminal_safe_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and validate operator-assisted MSC tracking captures")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export-queue", type=Path, help="Write an MSC browser review queue as JSON. No ClickUp writes.")
    mode.add_argument("--preview-capture", type=Path, help="Parse copied MSC result text and preview its ClickUp projection.")
    mode.add_argument(
        "--import-batch",
        type=Path,
        help="Validate browser captures/failures from JSON and preview their ClickUp actions.",
    )
    mode.add_argument(
        "--import-batch-url",
        help="Download a private browser-review batch URL for this one import, then delete the temporary copy.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply a validated --import-batch to ClickUp. Requires explicit operator-copied MSC results.",
    )
    parser.add_argument("--task-id", help="Restrict queue or preview to one internal ClickUp task ID.")
    parser.add_argument("--booking", help="Restrict queue or preview to one booking number.")
    parser.add_argument("--container", help="Restrict queue or preview to one container number.")
    parser.add_argument("--report", type=Path, help="Append per-task outcomes to a private JSONL progress report.")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional runtime environment file. This is read locally and never included in an import batch.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file) if args.env_file else load_dotenv()
    settings = Settings.from_env()
    import_batch = args.import_batch
    temporary_batch: Path | None = None
    if args.import_batch_url:
        temporary_batch = _download_import_batch(args.import_batch_url)
        import_batch = temporary_batch

    client = ClickUpClient(settings)

    if args.apply and not import_batch:
        raise ValueError("--apply requires --import-batch or --import-batch-url")

    if args.export_queue:
        # This queue is carrier-scoped by design. Require the server-side
        # ClickUp filter so large non-MSC lists are never loaded into the
        # operator workflow.
        shipments = _filter_shipments(
            client.list_shipments(require_carrier_prefilter=True),
            task_id=args.task_id,
            booking=args.booking,
            container=args.container,
        )
        msc_shipments = [shipment for shipment in shipments if is_msc_line(shipment.shipping_line)]
        items = build_queue(msc_shipments)
        write_queue(args.export_queue, items)
        print(terminal_safe_text(f"Wrote {len(items)} MSC browser review item(s) to {args.export_queue}"))
        return

    if import_batch:
        try:
            captures, failures = read_import_batch(import_batch)
            task_ids = [item.task_id for item in [*captures, *failures]]
            # The batch came from the production discovery scope. Resolve that
            # bounded list set first, then read only the reviewed task IDs.
            allowed_list_ids = set(client._resolve_target_lists())
            msc_shipments = [
                shipment
                for shipment in client.get_shipments_by_task_ids(task_ids, allowed_list_ids=allowed_list_ids)
                if is_msc_line(shipment.shipping_line)
            ]
            _import_batch(client, msc_shipments, captures, failures, apply=args.apply, report_path=args.report)
        finally:
            if temporary_batch:
                temporary_batch.unlink(missing_ok=True)
        return

    shipments = _filter_shipments(
        client.list_shipments(require_carrier_prefilter=True),
        task_id=args.task_id,
        booking=args.booking,
        container=args.container,
    )
    msc_shipments = [shipment for shipment in shipments if is_msc_line(shipment.shipping_line)]
    if len(msc_shipments) != 1:
        raise ValueError("Preview capture requires exactly one matching MSC shipment")
    capture_text = args.preview_capture.read_text(encoding="utf-8")
    capture = MscBrowserCapture(msc_shipments[0].task_id, capture_text, args.container)
    status = consolidate_browser_statuses(
        msc_shipments[0], [(capture, status_from_browser_capture(capture_text))]
    )
    plan = client.plan_shipment_update(msc_shipments[0], status)
    _print_preview_plan(msc_shipments[0], plan)
    print("No ClickUp write was made.")


def _record_outcome(path, task_id, outcome, **details):
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"task_id": task_id, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(), **details}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _import_batch(client: ClickUpClient, shipments, captures, failures, *, apply: bool, report_path=None) -> None:
    shipments_by_id = {shipment.task_id: shipment for shipment in shipments}
    requested_ids = {item.task_id for item in [*captures, *failures]}
    unknown_ids = sorted(requested_ids - set(shipments_by_id))
    if unknown_ids:
        for task_id in unknown_ids:
            print(terminal_safe_text(f"MSC task={task_id} skipped: outside current eligible MSC scope"))
            _record_outcome(report_path, task_id, "ineligible")

    updated = 0
    unchanged = 0
    failure_comments = 0
    captures_by_task: dict[str, list] = {}
    for item in captures:
        captures_by_task.setdefault(item.task_id, []).append(item)

    for task_id, task_captures in captures_by_task.items():
        if task_id not in shipments_by_id:
            continue
        shipment = shipments_by_id[task_id]
        evidence_hash = hashlib.sha256(json.dumps([item.capture for item in task_captures]).encode()).hexdigest()
        try:
            parsed = [(item, status_from_browser_capture(item.capture)) for item in task_captures]
            status = consolidate_browser_statuses(shipment, parsed)
        except ValueError as exc:
            error = str(exc)
            _record_outcome(report_path, task_id, "rejected", evidence_hash=evidence_hash, reason=error)
            print(
                terminal_safe_text(
                    f"- {shipment.task_name} | task={shipment.task_id} | MSC container review rejected | error={error}"
                )
            )
            if apply:
                try:
                    if client.report_msc_container_review_issue(shipment, error=error):
                        failure_comments += 1
                except requests.RequestException:
                    _record_outcome(report_path, task_id, "diagnostic_uncertain")
                    print(terminal_safe_text(f"MSC task={task_id}: diagnostic delivery uncertain"))
            continue
        plan = client.plan_shipment_update(shipment, status)
        _print_preview_plan(shipment, plan)
        if apply:
            _record_outcome(report_path, task_id, "write_started", evidence_hash=evidence_hash)
            try:
                result = client.update_shipment_status(shipment, status)
            except requests.RequestException:
                # A timeout can occur after a partial write. Do not automatically
                # replay or stamp success; re-read ClickUp before resuming it.
                _record_outcome(report_path, task_id, "write_uncertain", evidence_hash=evidence_hash)
                print(terminal_safe_text(f"MSC task={task_id}: write uncertain; verify ClickUp before retrying"))
                continue
            _record_outcome(report_path, task_id, "write_accepted", evidence_hash=evidence_hash, changed=result.changed)
            if result.changed:
                updated += 1
            else:
                unchanged += 1
        else:
            _record_outcome(report_path, task_id, "preview", evidence_hash=evidence_hash, changed=plan.changed)

    for item in failures:
        if item.task_id not in shipments_by_id:
            continue
        shipment = shipments_by_id[item.task_id]
        print(
            terminal_safe_text(
                f"- {shipment.task_name} | task={shipment.task_id} | MSC failure | "
                f"reference={item.reference} | error={item.error}"
            )
        )
        _record_outcome(report_path, item.task_id, "carrier_failure", reference=item.reference)
        if apply:
            try:
                if client.report_msc_tracking_failure(shipment, reference=item.reference, error=item.error):
                    failure_comments += 1
            except requests.RequestException:
                _record_outcome(report_path, item.task_id, "diagnostic_uncertain")
                print(terminal_safe_text(f"MSC task={item.task_id}: diagnostic delivery uncertain"))

    if apply:
        print(
            terminal_safe_text(
                f"MSC browser import applied. Updated: {updated}, unchanged: {unchanged}, "
                f"failure comments posted: {failure_comments}."
            )
        )
    else:
        print("No ClickUp write was made. Re-run with --apply to execute this validated batch.")


def _download_import_batch(url: str) -> Path:
    """Download one reviewed batch without retaining it after the import exits."""
    with urlopen(url, timeout=30) as response:  # nosec B310 - URL is supplied by the controlled ECS invocation
        body = response.read()
    if not body:
        raise ValueError("MSC import batch URL returned an empty response")
    with NamedTemporaryFile(prefix="msc-browser-batch-", suffix=".json", delete=False) as handle:
        handle.write(body)
        return Path(handle.name)


if __name__ == "__main__":
    main()
