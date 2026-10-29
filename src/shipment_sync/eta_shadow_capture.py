"""Bounded ONE/CMA observation capture. No operational projection or ClickUp writes."""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter
from shipment_sync.carriers.one_dcsa import OneDcsaClient
from shipment_sync.dcsa_tnt import sanitize_dcsa_payload
from shipment_sync.destination import same_port
from shipment_sync.models import ShipmentRef
from shipment_sync.one_split_bookings import container_bookings
from shipment_sync.one_write_holds import write_hold_reason
from shipment_sync.tracking_failures import carrier_name


PILOT_CARRIERS = {"ONE", "CMA CGM"}
MAX_ITEM_BYTES = 350_000


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _time(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return parsed if parsed and parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def _endpoint(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname not in {
        "apix.one-line.com", "apis.cma-cgm.net",
    }:
        raise ValueError("Unapproved shadow evidence endpoint")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _units(shipment: ShipmentRef) -> list[ShipmentRef]:
    numbers = re.findall(r"\b[A-Z]{4}\d{7}\b", (shipment.container_no or "").upper())
    remainder = re.sub(r"\b[A-Z]{4}\d{7}\b", "", (shipment.container_no or "").upper())
    if re.sub(r"[\s,;]+", "", remainder):
        raise ValueError("Unrecognized container text must not be treated as booking-only")
    if len(numbers) != len(set(numbers)) or len(numbers) > 20:
        raise ValueError("Ambiguous or oversized container population")
    if shipment.expected_container_count and numbers and len(numbers) != shipment.expected_container_count:
        raise ValueError("Container quantity mismatch")
    allocation = container_bookings(shipment) if carrier_name(shipment.shipping_line) == "ONE" else None
    booking = (shipment.booking_no or "").strip()
    if not allocation and not re.fullmatch(r"[A-Za-z0-9-]{1,35}", booking):
        raise ValueError("A single explicit booking or reviewed split mapping is required")
    return [replace(shipment, container_no=n, booking_no=allocation[n] if allocation else booking)
            for n in sorted(numbers)] or [replace(shipment, container_no=None, booking_no=booking)]


def _cma_allocation(events: list[dict], booking_events: list[dict], shipment: ShipmentRef, recorded: tuple[str, ...]) -> bool:
    """Prove an opaque CMA booking association by two independent complete queries."""
    if not shipment.container_no or not recorded or shipment.container_no not in recorded:
        return False
    def bookings(batch):
        return {r.get("documentReferenceValue") for e in batch for r in e.get("documentReferences", [])
                if r.get("documentReferenceType") == "BKG"}
    refs = bookings(events)
    if len(refs) != 1 or refs != bookings(booking_events) or not re.fullmatch(r"[a-f0-9]{64}", next(iter(refs)) or ""):
        return False
    equipment = {e.get("equipmentReference") for e in booking_events if e.get("equipmentReference")}
    equipment.update(r.get("referenceValue") for e in booking_events for r in e.get("references", []) if r.get("referenceType") == "EQ")
    if equipment != set(recorded):
        return False
    def signature(e):
        call=e.get("transportCall") or {}
        loc=e.get("eventLocation") or call.get("location") or {}
        return _json([e.get("eventID"),e.get("eventType"),e.get("eventDateTime"),e.get("eventClassifierCode"),
            e.get("transportEventTypeCode"),e.get("equipmentEventTypeCode"),e.get("shipmentEventTypeCode"),
            call.get("modeOfTransport"),loc.get("UNLocationCode") or call.get("UNLocationCode")])
    by_id: dict[str, set[str]] = {}
    witnesses = set()
    for e in booking_events:
        if not e.get("eventID"): continue
        by_id.setdefault(e["eventID"], set()).add(signature(e))
        eq = {e.get("equipmentReference")} | {r.get("referenceValue") for r in e.get("references", []) if r.get("referenceType") == "EQ"}
        if shipment.container_no in eq: witnesses.add(signature(e))
    # CMA's booking view can omit unit-only equipment history. Require an exact
    # allocation witness, not an invented equivalence of the two event lists.
    return (bool(events) and any(signature(e) in witnesses for e in events)
        and all(e.get("eventID") and bookings([e]) == refs
            and (e["eventID"] not in by_id or signature(e) in by_id[e["eventID"]]) for e in events))


def _normalized(events: list[dict], shipment: ShipmentRef, at: datetime, *, allocation_verified=False) -> tuple[list[dict], list[str]]:
    rows, issues = [], set()
    for event in events:
        call = event.get("transportCall") or {}
        loc = event.get("eventLocation") or call.get("location") or {}
        vessel = call.get("vessel") or {}
        port = loc.get("UNLocationCode") or call.get("UNLocationCode") or loc.get("locationName")
        bookings = {r.get("documentReferenceValue") for r in event.get("documentReferences", [])
                    if r.get("documentReferenceType") == "BKG"}
        equipment = {event.get("equipmentReference")} | {
            r.get("referenceValue") for r in event.get("references", []) if r.get("referenceType") == "EQ"}
        equipment.discard(None)
        if bookings and bookings != {shipment.booking_no} and not allocation_verified:
            issues.add("booking_conflict")
        if shipment.container_no and equipment and equipment != {shipment.container_no}:
            issues.add("container_conflict")
        code = event.get("transportEventTypeCode") or event.get("equipmentEventTypeCode") or event.get("shipmentEventTypeCode")
        classifier = event.get("eventClassifierCode")
        when = _time(event.get("eventDateTime"))
        if not event.get("eventID"): issues.add("missing_event_identity")
        if classifier not in {"ACT", "EST", "PLN"}: issues.add("unknown_classifier")
        if code in {"ARRI", "DEPA", "LOAD", "DISC"} and when is None: issues.add("missing_event_timezone")
        if classifier == "ACT" and when and when > at: issues.add("future_actual")
        booking_positive = bookings == {shipment.booking_no} or allocation_verified
        identity = booking_positive and (not shipment.container_no or equipment == {shipment.container_no})
        # A query reference alone is not proof of allocation or vessel mode.
        physical = bool(identity and shipment.container_no and when and classifier == "ACT" and when <= at
                        and call.get("modeOfTransport") == "VESSEL" and event.get("eventType") == "TRANSPORT")
        rows.append({"event_id": event.get("eventID"), "event_type": event.get("eventType"), "code": code,
            "classifier": classifier, "event_at": event.get("eventDateTime"),
            "carrier_event_created_at": event.get("eventCreatedDateTime"), "port": port,
            "mode": call.get("modeOfTransport"), "vessel": vessel.get("vesselName"),
            "import_voyage": call.get("importVoyageNumber"), "export_voyage": call.get("exportVoyageNumber"),
            "booking_identity_positive": booking_positive, "identity_positive": identity,
            "identity_basis": "independent_booking_and_container_queries" if allocation_verified else "explicit_event_references",
            "at_final_pod": same_port(shipment.destination_port, port),
            "actual_vessel_departure": physical and code == "DEPA" and not same_port(shipment.destination_port, port),
            "actual_pod_arrival": physical and code == "ARRI" and same_port(shipment.destination_port, port),
            "forecast_pod_arrival": bool(identity and when and code == "ARRI" and classifier in {"EST", "PLN"}
                and event.get("eventType") == "TRANSPORT" and call.get("modeOfTransport") == "VESSEL"
                and same_port(shipment.destination_port, port))})
    if not shipment.destination_port: issues.add("missing_authoritative_pod")
    if events and not any(r["identity_positive"] for r in rows): issues.add("allocation_identity_not_proven")
    return rows, sorted(issues)


class OneObservationClient(OneDcsaClient):
    def _request(self, method, path, **kwargs):
        result = super()._request(method, path, **kwargs)
        if method == "GET" and path == "/v2/events":
            self.observation_pages = getattr(self, "observation_pages", 0) + 1
        return result


class OfficialPilotSource:
    def __init__(self, *, one: OneDcsaClient | None = None, cma: CmaCgmAdapter | None = None):
        self.one = one or OneObservationClient()
        self.cma = cma or CmaCgmAdapter()

    def fetch(self, shipment: ShipmentRef, *, recorded_containers=()) -> tuple[list[dict], str, str, dict]:
        if carrier_name(shipment.shipping_line) == "ONE":
            self.one.observation_pages = 0
            events, _ = self.one.events(shipment, require_booking_identity=True)
            return events, self.one.base + "/v2/events", "ONE-carrier-events-v2; TNT-version-not-certified", {
                "page_count": self.one.observation_pages or None, "pagination_complete": True}
        _endpoint(self.cma._resolve_api_url())
        fetched = self.cma._fetch_dcsa_event_pages(shipment)
        events, url = list(fetched.events), fetched.source_url
        proof = {"page_count": fetched.page_count, "pagination_complete": True}
        opaque = any(re.fullmatch(r"[a-f0-9]{64}", str(r.get("documentReferenceValue", "")))
            for e in events for r in e.get("documentReferences", []) if r.get("documentReferenceType") == "BKG")
        if opaque and shipment.container_no:
            booking = self.cma._fetch_dcsa_event_pages(replace(shipment, container_no=None))
            _endpoint(booking.source_url)
            proof.update({"kind":"cma_independent_booking_query", "booking_query":shipment.booking_no,
                     "booking_events":list(booking.events), "booking_page_count":booking.page_count,
                     "recorded_containers":list(recorded_containers)})
        return events, url, "CMA-TNT-2.2", proof


def _verify_duplicate(old: dict, new: dict) -> None:
    if old.get("content_hash") != new["content_hash"]:
        raise ValueError("Capture identity reused with different evidence; start a new run")


class SqliteObservationStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS eta_observations (event_key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        os.chmod(path, 0o600)

    def get(self, key: str) -> dict | None:
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT body FROM eta_observations WHERE event_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, item: dict) -> bool:
        with sqlite3.connect(self.path) as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO eta_observations VALUES (?,?)", (item["event_key"], _json(item))).rowcount
            if not inserted:
                old = json.loads(conn.execute("SELECT body FROM eta_observations WHERE event_key=?", (item["event_key"],)).fetchone()[0])
                _verify_duplicate(old, item)
        return bool(inserted)


class DynamoObservationStore:
    """Separate key/index namespaces in the existing shadow table; never updates/deletes."""
    def __init__(self, table):
        self.table = table

    def get(self, key: str) -> dict | None:
        return self.table.get_item(Key={"event_key": key}, ConsistentRead=True).get("Item")

    def record(self, item: dict) -> bool:
        if len(_json(item).encode()) > MAX_ITEM_BYTES:
            raise ValueError("Observation exceeds bounded ledger item size")
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(event_key)")
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException": raise
            _verify_duplicate(self.get(item["event_key"]) or {}, item)
            return False
        return True


def build_observation_store():
    backend = os.getenv("ETA_SHADOW_BACKEND", "")
    if backend == "sqlite" and os.getenv("ETA_SHADOW_SQLITE_PATH"):
        return SqliteObservationStore(os.environ["ETA_SHADOW_SQLITE_PATH"])
    if backend == "dynamodb" and os.getenv("ETA_SHADOW_TABLE"):
        import boto3
        return DynamoObservationStore(boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-2")).Table(os.environ["ETA_SHADOW_TABLE"]))
    raise ValueError("Explicit ETA_SHADOW_BACKEND and durable store configuration required")


class EtaShadowCapture:
    def __init__(self, store, *, source=None, terminal_statuses=(), clock=None):
        self.store, self.source = store, source or OfficialPilotSource()
        self.terminal_statuses = {s.casefold().strip() for s in terminal_statuses}
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, shipments: list[ShipmentRef], *, run_id: str, allowed_task_ids: set[str], max_shipments: int = 2) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", run_id) or not allowed_task_ids or not 1 <= max_shipments <= 10:
            raise ValueError("An explicit run identity, task allowlist and pilot limit 1..10 are required")
        summary = {"mode": "eta-shadow-pilot", "clickup_writes": 0, "created": 0, "reused": 0,
                   "failures": 0, "needs_review": 0, "skipped": 0, "selected": 0}
        for shipment in shipments:
            line = carrier_name(shipment.shipping_line)
            if (shipment.task_id not in allowed_task_ids or line not in PILOT_CARRIERS
                or write_hold_reason(shipment) or any("legal" in t.casefold() for t in shipment.tags)
                or (shipment.current_task_status or shipment.current_status_value or "").casefold().strip() in self.terminal_statuses
                or summary["selected"] >= max_shipments):
                summary["skipped"] += 1
                continue
            summary["selected"] += 1
            try:
                units = _units(shipment)
            except ValueError:
                units = [shipment]
                invalid_identity = True
            else:
                invalid_identity = False
            for unit in units:
                identity = {"task_id": shipment.task_id, "carrier": line, "booking": unit.booking_no,
                            "container": unit.container_no, "master_bl": shipment.master_bl_no,
                            "destination": shipment.destination_port, "expected_count": shipment.expected_container_count,
                            "recorded_container_set": shipment.container_no, "native_status": shipment.current_task_status,
                            "list_id": shipment.list_id, "account_list_name": shipment.list_name}
                key = "eta-observation#v1#" + _hash([run_id, shipment.task_id, line, unit.booking_no, unit.container_no])
                existing = self.store.get(key)
                if existing:
                    saved_body = decode_observation(existing)
                    if (existing.get("identity_hash") != _hash(identity) or saved_body.get("identity") != identity
                            or saved_body.get("run_id") != run_id):
                        raise ValueError("Pilot retry identity changed; start a new run")
                    summary["reused"] += 1
                    summary["failures"] += existing.get("outcome") == "failed"
                    summary["needs_review"] += bool(existing.get("issue_count")) or existing.get("outcome") == "no_events"
                    continue
                started = self.clock()
                if not started.tzinfo: raise ValueError("Capture clock must include timezone")
                events, endpoint, contract, outcome, issues, proof = [], None, None, "failed", [], {}
                if invalid_identity:
                    issues = ["ambiguous_shipment_identity"]
                else:
                    try:
                        events, url, contract, proof = self.source.fetch(unit, recorded_containers=tuple(
                            sorted(set(re.findall(r"\b[A-Z]{4}\d{7}\b", (shipment.container_no or "").upper())))))
                        endpoint = _endpoint(url)
                        if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
                            raise ValueError("Malformed carrier event stream")
                        outcome = "complete" if events else "no_events"
                    except Exception as exc:
                        # Exception text may contain credentials, query strings or response bodies.
                        events, issues = [], ["source_" + type(exc).__name__]
                finished = self.clock()
                if not finished.tzinfo or finished < started: raise ValueError("Invalid capture observation interval")
                try:
                    allocation_verified = bool(line == "CMA CGM" and proof.get("kind") == "cma_independent_booking_query"
                        and proof.get("booking_query") == unit.booking_no
                        and _cma_allocation(events, proof.get("booking_events", []), unit, tuple(proof.get("recorded_containers", []))))
                    normalized, quality = _normalized(events, unit, finished, allocation_verified=allocation_verified)
                except (TypeError, AttributeError, ValueError):
                    normalized, quality = [], ["malformed_event_structure"]
                issues = sorted(set(issues + quality))
                body = {"schema_version": 1, "run_id": run_id, "identity": identity,
                        "collector_revision_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                        "observed_at": finished.isoformat(), "request_started_at": started.isoformat(),
                        "source_endpoint": endpoint, "source_contract": contract, "outcome": outcome,
                        "completeness": "bounded_adapter_cursor_traversal" if outcome in {"complete", "no_events"} else "unverified",
                        "issues": issues, "events": sanitize_dcsa_payload(events), "normalized_events": normalized,
                        "source_identity_evidence": sanitize_dcsa_payload(proof),
                        "original_confirmed_eta": None, "original_confirmed_etd": None, "advertised_transit": None,
                        "baseline_policy": "observations_only; no original commitment inferred; no frozen baseline overwritten"}
                compressed = base64.b64encode(gzip.compress(_json(body).encode(), mtime=0)).decode()
                item = {"event_key": key, "record_type": "eta_observation_v1", "carrier": "eta-capture#" + line,
                        "task_id": "eta-capture#" + shipment.task_id, "received_at": finished.isoformat(),
                        "identity_hash": _hash(identity), "content_hash": _hash(body), "payload_gzip_base64": compressed,
                        "outcome": outcome, "issue_count": len(issues)}
                if len(_json(item).encode()) > MAX_ITEM_BYTES: raise ValueError("Observation exceeds bounded ledger item size")
                created = self.store.record(item)
                summary["created" if created else "reused"] += 1
                summary["failures"] += outcome == "failed"
                summary["needs_review"] += bool(issues) or outcome == "no_events"
        return summary


def decode_observation(item: dict) -> dict:
    body = json.loads(gzip.decompress(base64.b64decode(item["payload_gzip_base64"])))
    if _hash(body) != item["content_hash"]: raise ValueError("Observation checksum mismatch")
    return body
