from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.maersk import _event_to_movement
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import ShipmentRef, ShipmentStatus
from test_track_trace_mapping import _settings


@pytest.mark.parametrize("pod,location,extra,arrives", [
    ("PUERTO QUETZAL", "MTLHK", {}, False),
    ("PUERTO QUETZAL", "TCQ", {"eventLocation": {"UNLocationCode": "GTPRQ"}}, True),
    ("PUERTO QUETZAL", "Puerto Quetzal, Guatemala", {}, True),
    ("PUERTO QUETZAL", None, {}, False),
    (None, "Puerto Quetzal, Guatemala", {}, False),
    ("PUERTO QUETZAL", "TCQ", {}, False),
    ("PUERTO QUETZAL", "Puerto Quetzal, Guatemala", {"UNLocationCode": "HKHKG"}, False),
    ("PUERTO QUETZAL", "TCQ", {"UNLocationCode": "HKHKG", "transportCall": {"UNLocationCode": "GTPRQ"}}, False),
])
@pytest.mark.parametrize("stored_discharge", [False, True])
def test_partial_maersk_response_requires_pod(pod, location, extra, arrives, stored_discharge):
    now = datetime.now(timezone.utc)
    event = {"equipmentEventTypeCode": "DISC", "eventClassifierCode": "ACT",
             "eventDateTime": (now - timedelta(days=2)).isoformat(),
             "locationName": location, **extra}
    shipment = ShipmentRef("task", "33497", "maersk", "276933530", "MRKU7771908", "list",
        current_task_status="por arribar", destination_port=pod,
        current_field_values={"disc-field": "1788220800000"} if stored_discharge else {})
    # No future itinerary and no adapter opt-in: the write boundary must protect it.
    status = ShipmentStatus("Tracking", recent_moves=[_event_to_movement(event)])
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    for iteration in range(3):
        plan = client.plan_shipment_update(shipment, status)
        assert (plan.task_status_update == "arribado en puerto") is arrives
        assert any(w.field_id == "disc-field" for w in plan.custom_field_updates) is (arrives and iteration == 0)
        shipment.current_field_values.update({w.field_id: w.value for w in plan.custom_field_updates})
    assert status.require_destination_evidence is False  # no mutation of caller


@pytest.mark.parametrize("state,days", [("EST", -1), ("ACT", 2)])
def test_destination_discharge_must_be_actual_and_not_future(state, days):
    shipment = ShipmentRef("task", "33497", "maersk", "BOOK", "MRKU7771908", "list",
        current_task_status="por arribar", destination_port="GTPRQ")
    move = _event_to_movement({"equipmentEventTypeCode": "DISC", "eventClassifierCode": state,
        "eventDateTime": (datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),
        "transportCall": {"location": {"UNLocationCode": "GTPRQ"}}})
    plan = ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(
        shipment, ShipmentStatus("Tracking", recent_moves=[move]))
    assert plan.task_status_update is None
    assert not any(w.field_id == "disc-field" for w in plan.custom_field_updates)


def test_historical_wrong_arrival_needs_separate_repair():
    shipment = ShipmentRef("task", "33497", "maersk", "BOOK", "MRKU7771908", "list",
        current_task_status="arribado en puerto", destination_port="GTPRQ",
        current_field_values={"disc-field": "1788220800000"})
    status = ShipmentStatus("Tracking")
    plan = ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)
    assert plan.task_status_update is None
    assert not any(w.field_id == "disc-field" for w in plan.custom_field_updates)
