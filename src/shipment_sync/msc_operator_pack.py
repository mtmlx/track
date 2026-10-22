"""Offline, single-shipment MSC operator handoff. No API calls or writeback."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

from shipment_sync.destination import same_port
from shipment_sync.models import ShipmentRef
from shipment_sync.msc_browser_assisted import (
    MscBrowserCapture,
    consolidate_browser_statuses,
    status_from_browser_capture,
)


def _fresh_timestamp(value: object, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Review and capture timestamps must be timezone-qualified ISO timestamps")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or not timedelta(0) <= now - stamp <= timedelta(hours=6):
        raise ValueError("Review and capture timestamps must be in the past six hours")
    return stamp


def build_package(manifest: dict, captures_dir: Path, *, now: datetime | None = None) -> tuple[dict, dict]:
    now = now or datetime.now(timezone.utc)
    if manifest.get("schema_version") != 1:
        raise ValueError("Expected manifest schema_version 1")
    for field in ("task_id", "task_name", "list_id", "booking_no", "destination_port", "operator"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            raise ValueError(f"Manifest requires {field}")
    if not re.fullmatch(r"[a-z0-9]+", manifest["task_id"]):
        raise ValueError("Use the internal ClickUp task ID, not the MTMLXGT custom ID")
    reviewed = _fresh_timestamp(manifest.get("task_reviewed_at"), now)
    captured = _fresh_timestamp(manifest.get("captured_at"), now)
    if captured < reviewed:
        raise ValueError("Capture must follow the approved task review")
    containers = manifest.get("containers")
    if not isinstance(containers, list) or not containers or any(
        not isinstance(c, str) or not re.fullmatch(r"[A-Z]{4}\d{7}", c) for c in containers
    ):
        raise ValueError("Manifest requires the complete uppercase container list")
    if len(set(containers)) != len(containers):
        raise ValueError("Duplicate container in manifest")
    actual_files = {p.name for p in captures_dir.glob("*.txt")}
    if actual_files != {f"{c}.txt" for c in containers}:
        raise ValueError("Capture directory must contain exactly one .txt file per approved container")
    entries, batch, hashes = [], [], {}
    for container in containers:
        path = captures_dir / f"{container}.txt"
        if path.is_symlink() or path.stat().st_size > 2_000_000:
            raise ValueError("Capture must be a regular local text file no larger than 2 MB")
        data = path.read_bytes()
        capture = MscBrowserCapture(manifest["task_id"], data.decode("utf-8"), container)
        status = status_from_browser_capture(capture.capture)
        if set(status.discovered_containers) != {container}:
            raise ValueError("Visible capture must identify exactly its approved container")
        if not same_port(manifest["destination_port"], status.destination_port):
            raise ValueError("Capture destination is missing, ambiguous or conflicts with approved POD")
        entries.append((capture, status))
        batch.append({"task_id": capture.task_id, "reference": container, "capture": capture.capture})
        hashes[path.name] = hashlib.sha256(data).hexdigest()
    shipment = ShipmentRef(
        task_id=manifest["task_id"], task_name=manifest["task_name"], shipping_line="msc",
        booking_no=manifest["booking_no"], container_no=", ".join(containers),
        list_id=manifest["list_id"], destination_port=manifest["destination_port"],
        expected_container_count=len(containers),
    )
    status = consolidate_browser_statuses(shipment, entries)
    report = {
        "schema_version": 1, "result": "offline capture validation passed",
        "task_id": shipment.task_id, "operator": manifest["operator"],
        "task_reviewed_at": reviewed.isoformat(), "captured_at": captured.isoformat(),
        "validated_at": now.isoformat(), "containers": containers,
        "destination_port": status.destination_port,
        "eta": status.eta_time.isoformat() if status.eta_time else None,
        "vessel_voyage": status.vessel_voyage, "capture_sha256": hashes,
        "notice": "Not a live task validation, carrier authenticity check, or write approval",
    }
    return {"captures": batch, "failures": []}, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--captures-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be an object")
    batch, report = build_package(manifest, args.captures_dir)
    batch_data = json.dumps(batch, indent=2).encode("utf-8")
    report["batch_sha256"] = hashlib.sha256(batch_data).hexdigest()
    # Never overwrite a reviewed handoff; keep shipment information private locally.
    args.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, data in (("batch.json", batch_data), ("report.json", json.dumps(report, indent=2).encode("utf-8"))):
        path = args.output_dir / name
        path.touch(mode=0o600, exist_ok=False)
        path.write_bytes(data)
    print("Offline validation passed. Created batch.json and report.json; no external calls or ClickUp writes.")


if __name__ == "__main__":
    main()
