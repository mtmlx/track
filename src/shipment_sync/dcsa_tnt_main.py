from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from shipment_sync.dcsa_event_ledger import DcsaEventLedger
from shipment_sync.dcsa_tnt import DcsaTntEvent, parse_dcsa_tnt_event
from shipment_sync.terminal import terminal_safe_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and shadow-ingest DCSA TNT 2.2/2.3 event payloads without carrier or ClickUp calls."
    )
    parser.add_argument("--payload-file", required=True, help="JSON object, array, or object containing events/data.")
    parser.add_argument("--carrier", required=True, help="Carrier name used in the canonical ledger.")
    parser.add_argument("--tnt-version", required=True, help="Supported values: 2.2, 2.2.0, 2.3, or 2.3.0.")
    parser.add_argument("--ledger-db", help="SQLite ledger path. Required unless --dry-run is supplied.")
    parser.add_argument("--task-id", help="Optional ClickUp task identifier for shadow reconciliation only.")
    parser.add_argument("--source-url", help="Optional carrier endpoint or tracking URL retained as event provenance.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the event summary without creating a ledger entry.")
    args = parser.parse_args()

    payload = _read_json(Path(args.payload_file))
    raw_events = _extract_events(payload)
    events = [
        parse_dcsa_tnt_event(
            item,
            carrier=args.carrier,
            tnt_version=args.tnt_version,
            source_url=args.source_url,
        )
        for item in raw_events
    ]

    if args.dry_run:
        print(json.dumps(_summary(events, created=0, duplicates=0, mode="dry-run"), sort_keys=True))
        return
    if not args.ledger_db:
        parser.error("--ledger-db is required unless --dry-run is supplied.")

    ledger = DcsaEventLedger(args.ledger_db)
    writes = [ledger.record(event, task_id=args.task_id) for event in events]
    print(
        json.dumps(
            _summary(
                events,
                created=sum(1 for write in writes if write.created),
                duplicates=sum(1 for write in writes if not write.created),
                mode="shadow-ingested",
            ),
            sort_keys=True,
        )
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Payload file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Payload file is not valid JSON: {path}") from exc


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_events = payload
    elif isinstance(payload, dict):
        wrapped = payload.get("events", payload.get("data"))
        raw_events = wrapped if isinstance(wrapped, list) else [payload]
    else:
        raise ValueError("Payload must be a JSON object or array.")
    if not raw_events:
        raise ValueError("Payload contains no events.")
    events: list[dict[str, Any]] = []
    for index, item in enumerate(raw_events):
        if not isinstance(item, dict):
            raise ValueError(f"Event at index {index} must be a JSON object.")
        events.append(item)
    return events


def _summary(events: list[DcsaTntEvent], *, created: int, duplicates: int, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "events": len(events),
        "created": created,
        "duplicates": duplicates,
        "halted": sum(1 for event in events if event.initial_projection_state == "halted"),
        "requires_review": sum(1 for event in events if event.initial_projection_state == "requires_review"),
        "event_keys": [event.idempotency_key for event in events],
    }


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except ValueError as exc:
        raise SystemExit(terminal_safe_text(exc)) from exc
