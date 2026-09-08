from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shipment_sync.dcsa_event_ledger import (
    DynamoDbDcsaEventLedger,
    build_dcsa_event_ledger_from_env,
)
from shipment_sync.dcsa_tnt import parse_dcsa_tnt_event


class _ConditionalWriteFailure(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _FakeDynamoTable:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.queries: list[dict] = []

    def put_item(self, *, Item: dict, ConditionExpression: str) -> None:
        assert ConditionExpression == "attribute_not_exists(event_key)"
        key = Item["event_key"]
        if key in self.items:
            raise _ConditionalWriteFailure()
        self.items[key] = dict(Item)

    def get_item(self, *, Key: dict, ConsistentRead: bool) -> dict:
        assert ConsistentRead is True
        item = self.items.get(Key["event_key"])
        return {"Item": dict(item)} if item else {}

    def update_item(
        self,
        *,
        Key: dict,
        ConditionExpression: str,
        UpdateExpression: str,
        ExpressionAttributeValues: dict,
    ) -> None:
        assert ConditionExpression == "attribute_exists(event_key)"
        item = self.items.get(Key["event_key"])
        if item is None:
            raise _ConditionalWriteFailure()
        assert "projection_state" in UpdateExpression
        item["projection_state"] = ExpressionAttributeValues[":state"]
        item["projected_at"] = ExpressionAttributeValues[":projected_at"]
        item["projection_result"] = ExpressionAttributeValues[":result"]

    def query(self, **kwargs: object) -> dict:
        self.queries.append(dict(kwargs))
        return {"Items": list(self.items.values())}


def _event(code: str = "CONF"):
    return parse_dcsa_tnt_event(
        {
            "eventID": "9e2d710d-67c6-4a8f-b928-5d8ee08ca604",
            "eventCreatedDateTime": "2026-07-31T12:00:00Z",
            "eventDateTime": "2026-07-31T12:00:00Z",
            "eventType": "SHIPMENT",
            "eventClassifierCode": "ACT",
            "shipmentEventTypeCode": code,
            "documentID": "SHAGT3664400",
            "documentTypeCode": "BKG",
            "apiKey": "must-not-persist",
        },
        carrier="maersk",
        tnt_version="2.3",
    )


def test_dynamodb_ledger_is_idempotent_and_preserves_sanitized_evidence() -> None:
    table = _FakeDynamoTable()
    ledger = DynamoDbDcsaEventLedger(table_name="track-trace-dcsa-events", table=table)
    event = _event("CANC")

    first = ledger.record(event, task_id="task-1", received_at=datetime(2026, 7, 31, tzinfo=timezone.utc))
    second = ledger.record(event, task_id="task-1")
    ledger.mark_projection(event.idempotency_key, state="failed", result={"reason": "readback failed"})
    stored = ledger.get(event.idempotency_key)

    assert first.created is True
    assert first.projection_state == "halted"
    assert second.created is False
    assert stored is not None
    assert stored["raw_payload"]["apiKey"] == "[REDACTED]"
    assert stored["projection_state"] == "failed"
    assert stored["projection_result"] == {"reason": "readback failed"}


def test_dynamodb_ledger_requires_indexed_lookup() -> None:
    table = _FakeDynamoTable()
    ledger = DynamoDbDcsaEventLedger(table_name="track-trace-dcsa-events", table=table)
    ledger.record(_event(), task_id="task-1")

    with pytest.raises(ValueError, match="table scans are prohibited"):
        ledger.list_events()

    records = ledger.list_events(carrier="maersk")
    assert len(records) == 1
    assert table.queries[-1]["IndexName"] == "carrier-received-at-index"


def test_ledger_factory_requires_explicit_backend_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("DCSA_TNT_LEDGER_BACKEND", raising=False)
    with pytest.raises(ValueError, match="DCSA_TNT_LEDGER_BACKEND"):
        build_dcsa_event_ledger_from_env()

    monkeypatch.setenv("DCSA_TNT_LEDGER_BACKEND", "sqlite")
    monkeypatch.setenv("DCSA_TNT_LEDGER_DB_PATH", str(tmp_path / "events.sqlite3"))
    assert build_dcsa_event_ledger_from_env().__class__.__name__ == "DcsaEventLedger"
