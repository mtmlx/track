from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Protocol

from .dcsa_tnt import DcsaTntEvent


_PROJECTION_STATES = frozenset({"pending", "requires_review", "halted", "projected", "failed"})


@dataclass(frozen=True)
class DcsaLedgerWrite:
    event_key: str
    created: bool
    projection_state: str


class DcsaEventStore(Protocol):
    """Storage contract shared by local validation and production shadow runs."""

    def record(
        self,
        event: DcsaTntEvent,
        *,
        task_id: str | None = None,
        received_at: datetime | None = None,
    ) -> DcsaLedgerWrite: ...

    def mark_projection(
        self,
        event_key: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
    ) -> None: ...

    def get(self, event_key: str) -> dict[str, Any] | None: ...

    def list_events(
        self,
        *,
        carrier: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]: ...


class DcsaEventLedger:
    """SQLite-backed canonical event ledger for local and shadow-mode use.

    This is the developer and fixture store. Production shadow workers use
    :class:`DynamoDbDcsaEventLedger`, which preserves the same contract without
    relying on Fargate ephemeral storage.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def record(self, event: DcsaTntEvent, *, task_id: str | None = None, received_at: datetime | None = None) -> DcsaLedgerWrite:
        received = (received_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
        normalized_json = _json(event.normalized_record())
        raw_payload_json = _json(event.raw_payload)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO dcsa_events (
                    event_key, carrier, tnt_version, event_id, event_type, event_code,
                    event_classifier_code, event_created_at, event_at, task_id,
                    projection_state, received_at, normalized_json, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (
                    event.idempotency_key,
                    event.carrier,
                    event.tnt_version,
                    event.event_id,
                    event.event_type,
                    event.event_code,
                    event.event_classifier_code,
                    event.event_created_at.isoformat(),
                    event.event_at.isoformat() if event.event_at else None,
                    task_id,
                    event.initial_projection_state,
                    received,
                    normalized_json,
                    raw_payload_json,
                ),
            )
            if cursor.rowcount:
                return DcsaLedgerWrite(event_key=event.idempotency_key, created=True, projection_state=event.initial_projection_state)
            row = conn.execute(
                "SELECT projection_state FROM dcsa_events WHERE event_key = ?",
                (event.idempotency_key,),
            ).fetchone()
        if row is None:  # pragma: no cover - defensive against an unexpected database failure
            raise RuntimeError(f"DCSA event {event.idempotency_key} was not persisted.")
        return DcsaLedgerWrite(event_key=event.idempotency_key, created=False, projection_state=str(row["projection_state"]))

    def mark_projection(self, event_key: str, *, state: str, result: dict[str, Any] | None = None) -> None:
        if state not in _PROJECTION_STATES:
            raise ValueError(f"Unsupported projection state {state!r}.")
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE dcsa_events
                SET projection_state = ?, projected_at = ?, projection_result_json = ?
                WHERE event_key = ?
                """,
                (
                    state,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    _json(result or {}),
                    event_key,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown DCSA event key {event_key!r}.")

    def get(self, event_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM dcsa_events WHERE event_key = ?", (event_key,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_events(self, *, carrier: str | None = None, task_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if carrier:
            clauses.append("carrier = ?")
            params.append(carrier.strip().lower())
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(limit, 1000)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM dcsa_events {where} ORDER BY received_at ASC, event_key ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dcsa_events (
                    event_key TEXT PRIMARY KEY,
                    carrier TEXT NOT NULL,
                    tnt_version TEXT NOT NULL,
                    event_id TEXT,
                    event_type TEXT NOT NULL,
                    event_code TEXT NOT NULL,
                    event_classifier_code TEXT,
                    event_created_at TEXT NOT NULL,
                    event_at TEXT,
                    task_id TEXT,
                    projection_state TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    projected_at TEXT,
                    normalized_json TEXT NOT NULL,
                    raw_payload_json TEXT NOT NULL,
                    projection_result_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_dcsa_events_carrier_received
                    ON dcsa_events(carrier, received_at);
                CREATE INDEX IF NOT EXISTS idx_dcsa_events_task_received
                    ON dcsa_events(task_id, received_at);
                CREATE INDEX IF NOT EXISTS idx_dcsa_events_projection_state
                    ON dcsa_events(projection_state, received_at);
                """
            )


class DynamoDbDcsaEventLedger:
    """DynamoDB implementation for the DCSA shadow-event ledger.

    The table must have ``event_key`` as its partition key plus the following
    global secondary indexes:

    - ``carrier-received-at-index``: ``carrier`` / ``received_at``
    - ``task-received-at-index``: ``task_id`` / ``received_at``

    The implementation deliberately uses only conditional puts, reads, queries,
    and updates. It never deletes carrier evidence.
    """

    carrier_index_name = "carrier-received-at-index"
    task_index_name = "task-received-at-index"

    def __init__(
        self,
        *,
        table_name: str,
        region_name: str | None = None,
        table: Any | None = None,
    ) -> None:
        cleaned_table_name = table_name.strip()
        if not cleaned_table_name:
            raise ValueError("DCSA TNT DynamoDB ledger requires a table name.")
        self.table_name = cleaned_table_name
        if table is not None:
            self._table = table
            return

        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - exercised by container dependency check
            raise RuntimeError(
                "DynamoDB ledger support requires boto3. Rebuild the Track & Trace image with locked dependencies."
            ) from exc
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(cleaned_table_name)

    def record(
        self,
        event: DcsaTntEvent,
        *,
        task_id: str | None = None,
        received_at: datetime | None = None,
    ) -> DcsaLedgerWrite:
        received = _utc_iso(received_at)
        item = _dynamodb_event_item(event=event, task_id=task_id, received_at=received)
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(event_key)",
            )
        except Exception as exc:
            if _dynamodb_error_code(exc) != "ConditionalCheckFailedException":
                raise
            stored = self.get(event.idempotency_key)
            if stored is None:  # pragma: no cover - defensive against a transient eventual-consistency failure
                raise RuntimeError(f"DCSA event {event.idempotency_key} duplicate could not be read back.") from exc
            return DcsaLedgerWrite(
                event_key=event.idempotency_key,
                created=False,
                projection_state=str(stored["projection_state"]),
            )
        return DcsaLedgerWrite(
            event_key=event.idempotency_key,
            created=True,
            projection_state=event.initial_projection_state,
        )

    def mark_projection(
        self,
        event_key: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if state not in _PROJECTION_STATES:
            raise ValueError(f"Unsupported projection state {state!r}.")
        try:
            self._table.update_item(
                Key={"event_key": event_key},
                ConditionExpression="attribute_exists(event_key)",
                UpdateExpression=(
                    "SET projection_state = :state, projected_at = :projected_at, projection_result = :result"
                ),
                ExpressionAttributeValues={
                    ":state": state,
                    ":projected_at": _utc_iso(),
                    ":result": result or {},
                },
            )
        except Exception as exc:
            if _dynamodb_error_code(exc) == "ConditionalCheckFailedException":
                raise KeyError(f"Unknown DCSA event key {event_key!r}.") from exc
            raise

    def get(self, event_key: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"event_key": event_key}, ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        return _dynamodb_item_to_record(item) if isinstance(item, Mapping) else None

    def list_events(
        self,
        *,
        carrier: str | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not carrier and not task_id:
            raise ValueError("DynamoDB event listing requires carrier or task_id; table scans are prohibited.")
        try:
            from boto3.dynamodb.conditions import Attr, Key
        except ImportError as exc:  # pragma: no cover - exercised by container dependency check
            raise RuntimeError("DynamoDB ledger support requires boto3.") from exc

        capped_limit = max(1, min(limit, 1000))
        if task_id:
            query: dict[str, Any] = {
                "IndexName": self.task_index_name,
                "KeyConditionExpression": Key("task_id").eq(task_id),
                "ScanIndexForward": True,
                "Limit": capped_limit,
            }
            if carrier:
                query["FilterExpression"] = Attr("carrier").eq(carrier.strip().lower())
        else:
            query = {
                "IndexName": self.carrier_index_name,
                "KeyConditionExpression": Key("carrier").eq(carrier.strip().lower()),
                "ScanIndexForward": True,
                "Limit": capped_limit,
            }
        response = self._table.query(**query)
        items = response.get("Items", []) if isinstance(response, Mapping) else []
        return [_dynamodb_item_to_record(item) for item in items if isinstance(item, Mapping)]


def build_dcsa_event_ledger_from_env() -> DcsaEventStore:
    """Build the explicitly configured shadow-ledger backend.

    There is no default local file because an ECS task must never silently put
    production evidence on its ephemeral filesystem.
    """

    backend = os.getenv("DCSA_TNT_LEDGER_BACKEND", "").strip().lower()
    if backend == "sqlite":
        db_path = os.getenv("DCSA_TNT_LEDGER_DB_PATH", "").strip()
        if not db_path:
            raise ValueError("DCSA_TNT_LEDGER_DB_PATH is required when DCSA_TNT_LEDGER_BACKEND=sqlite.")
        return DcsaEventLedger(db_path)
    if backend == "dynamodb":
        table_name = os.getenv("DCSA_TNT_LEDGER_TABLE", "").strip()
        if not table_name:
            raise ValueError("DCSA_TNT_LEDGER_TABLE is required when DCSA_TNT_LEDGER_BACKEND=dynamodb.")
        region_name = os.getenv("AWS_REGION", "").strip() or None
        return DynamoDbDcsaEventLedger(table_name=table_name, region_name=region_name)
    raise ValueError(
        "Set DCSA_TNT_LEDGER_BACKEND to 'sqlite' for local validation or 'dynamodb' for the ECS shadow worker."
    )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    for key in ("normalized_json", "raw_payload_json", "projection_result_json"):
        record[key.removesuffix("_json")] = json.loads(record.pop(key))
    return record


def _utc_iso(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def _dynamodb_event_item(*, event: DcsaTntEvent, task_id: str | None, received_at: str) -> dict[str, Any]:
    return {
        "event_key": event.idempotency_key,
        "carrier": event.carrier,
        "tnt_version": event.tnt_version,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_code": event.event_code,
        "event_classifier_code": event.event_classifier_code,
        "event_created_at": event.event_created_at.isoformat(),
        "event_at": event.event_at.isoformat() if event.event_at else None,
        "task_id": task_id,
        "projection_state": event.initial_projection_state,
        "received_at": received_at,
        "projected_at": None,
        "normalized": event.normalized_record(),
        "raw_payload": event.raw_payload,
        "projection_result": {},
    }


def _dynamodb_item_to_record(item: Mapping[str, Any]) -> dict[str, Any]:
    return dict(item)


def _dynamodb_error_code(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    if not isinstance(response, Mapping):
        return None
    error = response.get("Error")
    if not isinstance(error, Mapping):
        return None
    code = error.get("Code")
    return str(code) if code is not None else None
