from __future__ import annotations

from datetime import datetime, timezone

import pytest

from shipment_sync.dcsa_event_ledger import DcsaEventLedger
from shipment_sync.dcsa_tnt import DcsaTntValidationError, parse_dcsa_tnt_event


_EVENT_ID = "9e2d710d-67c6-4a8f-b928-5d8ee08ca604"
_TRANSPORT_CALL_ID = "b75b0b1c-4f11-4d14-8628-aaf41d5fd70f"


def _shipment_event(code: str = "CONF") -> dict[str, object]:
    return {
        "eventID": _EVENT_ID,
        "eventCreatedDateTime": "2026-07-31T12:00:00Z",
        "eventDateTime": "2026-07-31T12:00:00Z",
        "eventType": "SHIPMENT",
        "eventClassifierCode": "ACT",
        "shipmentEventTypeCode": code,
        "documentID": "SHAGT3664400",
        "documentTypeCode": "BKG",
    }


def _transport_event() -> dict[str, object]:
    return {
        "eventID": _EVENT_ID,
        "eventCreatedDateTime": "2026-07-31T12:00:00+08:00",
        "eventType": "TRANSPORT",
        "eventClassifierCode": "ACT",
        "transportEventTypeCode": "DEPA",
        "facilityTypeCode": "POTE",
        "transportCall": {
            "transportCallID": _TRANSPORT_CALL_ID,
            "transportCallSequenceNumber": 2,
            "modeOfTransport": "VESSEL",
            "location": {
                "UNLocationCode": "CNSHA",
                "facilityCode": "CNSHA-01",
                "facilityCodeListProvider": "SMDG",
            },
            "vessel": {"vesselName": "MTM TEST VESSEL"},
            "exportVoyageNumber": "001E",
        },
    }


def test_tnt_23_preserves_pending_confirmation_references_and_redacts_sensitive_values() -> None:
    payload = _shipment_event("PENC")
    payload["documentReferences"] = [
        {"documentReferenceType": "CBR", "documentReferenceValue": "REQUEST-1"},
        {"documentReferenceType": "SHI", "documentReferenceValue": "SI-1"},
    ]
    payload["references"] = [{"referenceType": "BID", "referenceValue": "BID-1"}]
    payload["apiKey"] = "must-not-persist"

    event = parse_dcsa_tnt_event(payload, carrier="CMA CGM", tnt_version="2.3.0")

    assert event.tnt_version == "2.3"
    assert event.is_pending_confirmation
    assert event.initial_projection_state == "requires_review"
    assert [item.reference_type for item in event.document_references] == ["CBR", "SHI"]
    assert event.references[0].reference_type == "BID"
    assert event.raw_payload["apiKey"] == "[REDACTED]"
    assert event.event_created_at == datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def test_tnt_23_cancellation_is_halted_and_idempotently_persisted(tmp_path) -> None:
    event = parse_dcsa_tnt_event(_shipment_event("CANC"), carrier="maersk", tnt_version="2.3")
    ledger = DcsaEventLedger(str(tmp_path / "dcsa-events.sqlite3"))

    first = ledger.record(event, task_id="task-1")
    second = ledger.record(event, task_id="task-1")
    stored = ledger.get(event.idempotency_key)

    assert event.is_cancelled
    assert first.created is True
    assert first.projection_state == "halted"
    assert second.created is False
    assert stored is not None
    assert stored["projection_state"] == "halted"
    assert stored["task_id"] == "task-1"
    assert stored["normalized"]["event_code"] == "CANC"


def test_tnt_23_preserves_root_facility_and_transport_call_location() -> None:
    event = parse_dcsa_tnt_event(_transport_event(), carrier="hapag lloyd", tnt_version="2.3")

    assert event.location.un_location_code == "CNSHA"
    assert event.location.facility_code == "CNSHA-01"
    assert event.location.facility_type_code == "POTE"
    assert event.transport_call_sequence_number == 2
    assert event.vessel_name == "MTM TEST VESSEL"
    assert event.export_voyage_number == "001E"


def test_tnt_22_accepts_legacy_document_reference_values_and_rejects_tnt_23_codes() -> None:
    legacy = _shipment_event("CONF")
    legacy["eventClassifierCode"] = "EST"
    legacy["documentReferences"] = [
        {"documentReferenceType": "BKG (Booking)", "documentReferenceValue": "BOOK-1"},
        {"documentReferenceType": "TRD (Transport Document)", "documentReferenceValue": "TD-1"},
    ]

    event = parse_dcsa_tnt_event(legacy, carrier="cma cgm", tnt_version="2.2")
    assert [item.reference_type for item in event.document_references] == ["BKG", "TRD"]

    with pytest.raises(DcsaTntValidationError, match="shipmentEventTypeCode"):
        parse_dcsa_tnt_event(_shipment_event("PENC"), carrier="cma cgm", tnt_version="2.2")


def test_cma_tnt_22_accepts_only_its_documented_reference_extensions() -> None:
    payload = _shipment_event("CONF")
    payload["references"] = [
        {"referenceType": "LOAD", "referenceValue": "LOAD-1"},
        {"referenceType": "ERT", "referenceValue": "ERT-1"},
    ]

    event = parse_dcsa_tnt_event(payload, carrier="CMA CGM", tnt_version="2.2")

    assert [item.reference_type for item in event.references] == ["LOAD", "ERT"]
    with pytest.raises(DcsaTntValidationError, match="Unsupported referenceType"):
        parse_dcsa_tnt_event(payload, carrier="maersk", tnt_version="2.2")


def test_cma_tnt_22_preserves_non_uuid_event_ids_as_a_conformance_warning() -> None:
    payload = _shipment_event("CONF")
    payload["eventID"] = "CMA-OPAQUE-EVENT-1"

    event = parse_dcsa_tnt_event(payload, carrier="CMA CGM", tnt_version="2.2")

    assert event.event_id == "CMA-OPAQUE-EVENT-1"
    assert event.conformance_warnings == ("carrier-event-id-not-uuid",)
    assert event.normalized_record()["conformance_warnings"] == ["carrier-event-id-not-uuid"]
    with pytest.raises(DcsaTntValidationError, match="eventID must be a UUID"):
        parse_dcsa_tnt_event(payload, carrier="maersk", tnt_version="2.2")


def test_tnt_event_requires_exactly_one_matching_typed_code() -> None:
    payload = _shipment_event("CONF")
    payload["equipmentEventTypeCode"] = "LOAD"

    with pytest.raises(DcsaTntValidationError, match="exactly one matching event code"):
        parse_dcsa_tnt_event(payload, carrier="maersk", tnt_version="2.3")


def test_tnt_23_rejects_non_actual_shipment_event_and_excess_reason_length() -> None:
    invalid_classifier = _shipment_event("CONF")
    invalid_classifier["eventClassifierCode"] = "EST"
    with pytest.raises(DcsaTntValidationError, match="must use eventClassifierCode ACT"):
        parse_dcsa_tnt_event(invalid_classifier, carrier="maersk", tnt_version="2.3")

    invalid_reason = _shipment_event("CONF")
    invalid_reason["reason"] = "x" * 251
    with pytest.raises(DcsaTntValidationError, match="at most 250"):
        parse_dcsa_tnt_event(invalid_reason, carrier="maersk", tnt_version="2.3")


def test_ledger_records_projection_result_and_lists_by_carrier(tmp_path) -> None:
    event = parse_dcsa_tnt_event(_shipment_event("CONF"), carrier="CMA CGM", tnt_version="2.3")
    ledger = DcsaEventLedger(str(tmp_path / "dcsa-events.sqlite3"))
    ledger.record(event, task_id="task-2")
    ledger.mark_projection(event.idempotency_key, state="projected", result={"task_status": "BK confirmado"})

    records = ledger.list_events(carrier="cma cgm")

    assert len(records) == 1
    assert records[0]["projection_state"] == "projected"
    assert records[0]["projection_result"] == {"task_status": "BK confirmado"}
