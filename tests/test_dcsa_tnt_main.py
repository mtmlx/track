from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipment_sync.dcsa_event_ledger import DcsaEventLedger
from shipment_sync.dcsa_tnt_main import main


def _payload(code: str = "CONF") -> dict[str, object]:
    return {
        "eventID": "9e2d710d-67c6-4a8f-b928-5d8ee08ca604",
        "eventCreatedDateTime": "2026-07-31T12:00:00Z",
        "eventDateTime": "2026-07-31T12:00:00Z",
        "eventType": "SHIPMENT",
        "eventClassifierCode": "ACT",
        "shipmentEventTypeCode": code,
        "documentID": "SHAGT3664400",
        "documentTypeCode": "BKG",
    }


def test_shadow_ingest_dry_run_validates_without_creating_ledger(tmp_path, monkeypatch, capsys) -> None:
    payload_file = tmp_path / "event.json"
    ledger_path = tmp_path / "ledger.sqlite3"
    payload_file.write_text(json.dumps(_payload("PENC")), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "dcsa-tnt-ingest",
            "--payload-file",
            str(payload_file),
            "--carrier",
            "cma cgm",
            "--tnt-version",
            "2.3",
            "--dry-run",
        ],
    )

    main()

    summary = json.loads(capsys.readouterr().out)
    assert summary["mode"] == "dry-run"
    assert summary["requires_review"] == 1
    assert not ledger_path.exists()


def test_shadow_ingest_validates_the_full_batch_before_creating_ledger(tmp_path, monkeypatch) -> None:
    payload_file = tmp_path / "events.json"
    ledger_path = tmp_path / "ledger.sqlite3"
    invalid = _payload("PENC")
    payload_file.write_text(json.dumps([_payload(), invalid]), encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "dcsa-tnt-ingest",
            "--payload-file",
            str(payload_file),
            "--carrier",
            "maersk",
            "--tnt-version",
            "2.2",
            "--ledger-db",
            str(ledger_path),
        ],
    )

    with pytest.raises(ValueError, match="shipmentEventTypeCode"):
        main()

    assert not ledger_path.exists()


def test_shadow_ingest_records_and_deduplicates_payloads(tmp_path, monkeypatch, capsys) -> None:
    payload_file = tmp_path / "event.json"
    ledger_path = tmp_path / "ledger.sqlite3"
    payload_file.write_text(json.dumps({"events": [_payload("CANC")]}), encoding="utf-8")
    args = [
        "dcsa-tnt-ingest",
        "--payload-file",
        str(payload_file),
        "--carrier",
        "maersk",
        "--tnt-version",
        "2.3",
        "--ledger-db",
        str(ledger_path),
        "--task-id",
        "task-1",
    ]
    monkeypatch.setattr("sys.argv", args)
    main()
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setattr("sys.argv", args)
    main()
    second = json.loads(capsys.readouterr().out)

    assert first["created"] == 1
    assert first["halted"] == 1
    assert second["duplicates"] == 1
    assert DcsaEventLedger(str(ledger_path)).list_events(task_id="task-1")[0]["projection_state"] == "halted"
