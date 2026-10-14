from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.maersk import _event_to_movement
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import ShipmentRef, ShipmentStatus
from test_track_trace_mapping import _settings


def _plan(code="LOAD", classifier="ACT", mode="VESSEL", days=-4, extra=None,
          carrier="maersk", current="en puerto origen", booking_only=False):
    now = datetime.now(timezone.utc)
    event = {"eventClassifierCode": classifier,
             "eventDateTime": (now + timedelta(days=days)).isoformat(),
             "transportCall": {"modeOfTransport": mode, "UNLocationCode": "USHOU"},
             "equipmentEventTypeCode" if code == "LOAD" else "transportEventTypeCode": code}
    event.update(extra or {})
    shipment = ShipmentRef("86e3dgd2j", "MTMLXGT-33773", carrier, "277740394",
        "MRSU8843754", "list", current_task_status=current, destination_port="GTPBR")
    status = ShipmentStatus("Tracking", eta_time=now + timedelta(days=4),
        destination_port="GTPBR", recent_moves=[_event_to_movement(event)],
        booking_only=booking_only)
    return ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)


@pytest.mark.parametrize("code", ["LOAD", "DEPA"])
@pytest.mark.parametrize("carrier", ["maersk", "maersk line", "a.p. moller - maersk"])
def test_actual_vessel_proves_transit_without_empty_gate(code, carrier):
    plan = _plan(code=code, carrier=carrier)
    assert plan.task_status_update == "Tránsito"
    assert not any(w.field_id == "gtot-empty-field" for w in plan.custom_field_updates)


@pytest.mark.parametrize("kwargs", [
    {"mode": "TRUCK"}, {"mode": "RAIL"}, {"mode": None},
    {"classifier": "EST"}, {"classifier": "PLN"}, {"classifier": None},
    {"days": 1}, {"code": "ARRI"}, {"carrier": "one"},
    {"booking_only": True},
    {"extra": {"eventDateTime": None, "eventCreatedDateTime": "2026-10-01T00:00:00Z"}},
    {"extra": {"eventDateTime": "not-a-date"}},
    {"extra": {"modeOfTransport": "TRUCK"}},
    {"extra": {"UNLocationCode": "GTPBR"}},
    {"extra": {"transportCall": {"modeOfTransport": "VESSEL", "UNLocationCode": "GTPBR"}}},
    {"extra": {"transportCall": {"modeOfTransport": "VESSEL"}}},
])
def test_unproven_or_non_ocean_movement_cannot_promote(kwargs):
    assert _plan(**kwargs).task_status_update is None


@pytest.mark.parametrize("current", ["arribado en puerto", "en almacén", "vacío devuelto", "cancelado"])
def test_later_or_terminal_status_is_not_regressed(current):
    assert _plan(current=current).task_status_update is None


def test_repeated_transit_preview_is_idempotent():
    assert _plan(current="tránsito").task_status_update is None


def test_occurrence_time_and_transport_mode_are_preserved():
    move = _event_to_movement({"equipmentEventTypeCode": "LOAD",
        "eventClassifierCode": "ACT", "eventDateTime": "2026-10-10T15:38:00-05:00",
        "transportCall": {"modeOfTransport": "VESSEL", "UNLocationCode": "USHOU"}})
    assert move.transport_mode == "VESSEL"
    assert move.event_occurrence_time == datetime(2026, 10, 10, 20, 38, tzinfo=timezone.utc)
