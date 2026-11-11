"""Regression for MTMLXGT-26065: POD arrival sorts after its discharge."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.one import OneAdapter
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from test_one_adapter import _StubSession
from test_track_trace_mapping import _settings


def evidence():
    now = datetime.now(timezone.utc)
    discharge = now - timedelta(days=9)
    moves = [
        MovementEvent("Container Discharged (DISC)", "MANZANILLO", discharge-timedelta(days=40), event_state="actual", location_code="MXZLO"),
        MovementEvent("Container Loaded (LOAD)", "MANZANILLO", discharge-timedelta(days=10), event_state="actual"),
        MovementEvent("Container Discharged (DISC)", "PUERTO QUETZAL", discharge, event_state="actual", location_code="GTPRQ"),
        MovementEvent("Transport Arrived (ARRI)", "PUERTO QUETZAL", discharge+timedelta(hours=2, minutes=24), event_state="actual", location_code="GTPRQ"),
        MovementEvent("Container Gated Out (GTOT)", "PUERTO QUETZAL", event_state="actual"),
        MovementEvent("Container Gated In (GTIN)", "PUERTO QUETZAL", now-timedelta(days=2), event_state="actual",
                      source_event_name="Empty Container Returned from Customer", location_code="GTPRQ"),
    ]
    shipment = ShipmentRef("task", "26065", "one", "BOOK", "ONEU6507241", "list",
                           current_task_status="arribado en puerto", destination_port="PUERTO QUETZAL")
    status = ShipmentStatus("Tracking", recent_moves=moves, destination_port=shipment.destination_port, require_destination_evidence=True)
    return shipment, status


def plan(shipment, status):
    return ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)

@pytest.mark.parametrize("verified, population, closes", [
    (None, ["TIIU7491493", "TIIU7491683"], False),
    (False, ["TIIU7491493", "TIIU7491683"], False),
    (True, ["TIIU7491493"], False),
    (True, ["TIIU7491493", "ONEU6507241"], False),
    (True, ["TIIU7491493", "TIIU7491683"], True),
])
def test_31953_requires_verified_return_for_exact_container_population(verified, population, closes):
    shipment, status = evidence()
    shipment.container_no = "TIIU7491493, TIIU7491683"
    shipment.expected_container_count = 2
    shipment.current_task_status = "en almacén"
    shipment.current_field_values["gtin-empty-field"] = datetime.now(timezone.utc)-timedelta(days=4)
    status.discovered_containers = population
    status.all_containers_empty_returned = verified
    result = plan(shipment, status)
    assert result.task_status_update == ("Vacío devuelto" if closes else None)
    assert any(w.field_id == "gtin-empty-field" for w in result.custom_field_updates) == closes


def test_destination_discharge_and_return_without_gate_out():
    shipment, status = evidence()
    result = plan(shipment, status)
    fields = {w.field_id: w.value for w in result.custom_field_updates}
    assert fields["disc-field"].date() == status.recent_moves[2].event_time.date()
    assert fields["gtin-empty-field"].date() == status.recent_moves[-1].event_time.date()
    assert "gtot-delivery-field" not in fields
    assert result.task_status_update == "Vacío devuelto"
    shipment.current_field_values.update(fields)
    shipment.current_task_status = result.task_status_update
    repeated = plan(shipment, status)
    assert repeated.task_status_update is None
    assert not any(w.field_id in {"disc-field", "gtin-empty-field"} for w in repeated.custom_field_updates)


@pytest.mark.parametrize("change", [
    {"event_state": "estimated"}, {"event_time": None},
    {"event_time": datetime.now(timezone.utc)+timedelta(days=3)},
    {"source_event_name": None}, {"source_event_name": "Gate In to Outbound Terminal"},
    {"location_code": "MXZLO"}, {"location_code": None, "location": None},
    {"location_code": None, "location": "Puerto Quetzal / Manzanillo"},
    {"event_time": datetime.now(timezone.utc)-timedelta(days=12)},
])
def test_unverified_return_cannot_skip_warehouse(change):
    shipment, status = evidence()
    status.recent_moves[-1] = replace(status.recent_moves[-1], **change)
    assert plan(shipment, status).task_status_update is None


@pytest.mark.parametrize("destination", [None, "unknown", "HNPCR", "Puerto Quetzal / Colon"])
def test_missing_or_conflicting_destination_fails_closed(destination):
    shipment, status = evidence()
    shipment.destination_port = destination
    status.destination_port = destination
    result = plan(shipment, status)
    assert result.task_status_update is None
    assert not any(w.field_id in {"disc-field", "gtin-empty-field"} for w in result.custom_field_updates)


def test_persisted_discharge_does_not_replace_live_destination_evidence():
    shipment, status = evidence()
    shipment.current_field_values["disc-field"] = datetime.now(timezone.utc)-timedelta(days=9)
    status.recent_moves = [m for m in status.recent_moves if m.location_code != "GTPRQ" or "DISC" not in m.name]
    assert plan(shipment, status).task_status_update is None


def test_other_carriers_do_not_get_one_status_exception():
    shipment, status = evidence()
    shipment.shipping_line = "msc"
    assert plan(shipment, status).task_status_update is None


def test_adapter_preserves_explicit_return_and_location_code(monkeypatch):
    adapter = OneAdapter()
    adapter.session = _StubSession({"data": [{
        "eventName": "Empty Container Returned from Customer", "triggerType": "ACTUAL",
        "eventLocalPortDate": "2026-09-07T16:57:00.000Z",
        "location": {"code": "GTPRQ", "locationName": "PUERTO QUETZAL"},
    }]})
    moves = adapter._fetch_recent_moves("BOOK", "ONEU6507241")
    assert moves[0].source_event_name == "Empty Container Returned from Customer"
    assert moves[0].location_code == "GTPRQ"
    assert moves[0].event_state == "actual"
    shipment, status = evidence()
    monkeypatch.setattr(adapter, "_fetch_status_from_edh", lambda *a, **kw: replace(status, destination_port=None, require_destination_evidence=False))
    fetched = adapter.fetch_status(shipment)
    assert fetched.destination_port == shipment.destination_port
    assert fetched.require_destination_evidence is True


@pytest.mark.parametrize("offset", [-3, 0, 3])
@pytest.mark.parametrize("current", ["arribado en puerto", "en almacén"])
def test_saved_gate_dates_never_replace_actual_events(offset, current):
    shipment, status = evidence()
    shipment.current_task_status = current
    saved = datetime.now(timezone.utc) + timedelta(days=offset)
    shipment.current_field_values.update({"gtot-delivery-field": saved, "gtin-empty-field": saved})
    status.recent_moves = status.recent_moves[:4]
    for _ in range(2):
        result = plan(shipment, status)
        assert result.task_status_update is None
        assert not any(w.field_id in {"gtot-delivery-field", "gtin-empty-field"}
                       for w in result.custom_field_updates)


@pytest.mark.parametrize("change", [
    {"event_state": "estimated"}, {"event_state": None}, {"event_time": None},
    {"event_time": datetime.now(timezone.utc)+timedelta(days=1)},
    {"location_code": "MXZLO"}, {"location_code": "AMBIGUOUS"},
    {"location_code": None, "location": None},
    {"equipment_load_state": "EMPTY"}, {"equipment_load_state": None},
    {"event_time": datetime.now(timezone.utc)-timedelta(days=12)},
    {},
])
def test_delivery_requires_actual_laden_gate_at_destination_after_discharge(change):
    shipment, status = evidence()
    gate = MovementEvent("Container Gated Out (GTOT)", "PUERTO QUETZAL",
        datetime.now(timezone.utc)-timedelta(days=1), event_state="actual",
        location_code="GTPRQ", equipment_load_state="LADEN")
    status.recent_moves = status.recent_moves[:4] + [replace(gate, **change)]
    result = plan(shipment, status)
    valid = not change
    assert result.task_status_update == ("en ruta a almacén" if valid else None)
    assert any(w.field_id == "gtot-delivery-field" for w in result.custom_field_updates) == valid


def test_return_rejects_contradictory_laden_state_even_with_saved_date():
    shipment, status = evidence()
    shipment.current_task_status = "en almacén"
    shipment.current_field_values["gtin-empty-field"] = datetime.now(timezone.utc)-timedelta(days=1)
    status.recent_moves[-1] = replace(status.recent_moves[-1], equipment_load_state="LADEN")
    result = plan(shipment, status)
    assert result.task_status_update is None
    assert not any(w.field_id == "gtin-empty-field" for w in result.custom_field_updates)


def test_gate_validator_rejects_naive_timestamp():
    from shipment_sync.clickup_client import _verified_one_destination_gate
    _, status = evidence()
    gate = MovementEvent("Container Gated Out (GTOT)", "PUERTO QUETZAL",
        datetime.now(), event_state="actual", location_code="GTPRQ", equipment_load_state="LADEN")
    assert not _verified_one_destination_gate(status, gate, datetime.now(timezone.utc))
