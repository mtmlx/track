from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.destination import same_port
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from test_track_trace_mapping import _settings


@pytest.mark.parametrize("destination,location,match", [
    ("Puerto Cortes, HN", "Colón, PA", False),
    ("PUERTO CORTÉS", "Puerto Cortes, HN", True),
    ("HNPCR", "Puerto Cortes, Honduras", True),
    (None, "Puerto Cortes, HN", False),
    ("HNPCR", None, False),
    ("unknown", "unknown", False),
    ("Puerto Cortes / Colon", "Puerto Cortes, HN", False),
    ("Puerto Cortes, US", "Puerto Cortes, HN", False),
    ("Colon", "Colon", False),
    ("47", "47", False),
])
def test_port_identity(destination, location, match):
    assert same_port(destination, location) is match


@pytest.mark.parametrize("destination,location,state,offset,arrives", [
    ("HNPCR", "Colon, PA", "actual", -7, False),
    ("HNPCR", "Puerto Cortes, HN", "actual", -1, True),
    (None, "Puerto Cortes, HN", "actual", -1, False),
    ("HNPCR", None, "actual", -1, False),
    ("HNPCR", "Puerto Cortes, HN", "estimate", -1, False),
    ("HNPCR", "Puerto Cortes, HN", "actual", 3, False),
])
def test_persisted_discharge_cannot_bypass_destination_evidence(destination, location, state, offset, arrives):
    now = datetime.now(timezone.utc)
    shipment = ShipmentRef("task", "27395", "msc", "BOOK", "MSMU8716298", "list",
        current_task_status="por arribar",
        current_field_values={"disc-field": str(int((now-timedelta(days=7)).timestamp()*1000))})
    status = ShipmentStatus("Tracking", destination_port=destination,
        require_destination_evidence=True,
        recent_moves=[MovementEvent("Container Discharged (DISC)", location,
            now+timedelta(days=offset), event_state=state)])
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    for iteration in range(2):
        plan = client.plan_shipment_update(shipment, status)
        assert (plan.task_status_update == "arribado en puerto") is arrives
        assert any(w.field_id == "disc-field" for w in plan.custom_field_updates) is (arrives and iteration == 0)
        shipment.current_field_values.update({w.field_id: w.value for w in plan.custom_field_updates})


def test_historical_arrival_is_left_for_explicit_repair():
    shipment = ShipmentRef("task", "27395", "msc", "BOOK", "MSMU8716298", "list",
        current_task_status="arribado en puerto", current_field_values={"disc-field": "1788220800000"})
    status = ShipmentStatus("Tracking", destination_port="HNPCR", require_destination_evidence=True)
    plan = ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)
    assert plan.task_status_update is None
    assert not any(w.field_id == "disc-field" for w in plan.custom_field_updates)


def test_carrier_destination_cannot_override_shipment_destination():
    shipment = ShipmentRef("task", "27395", "msc", "BOOK", None, "list",
        current_task_status="por arribar", destination_port="HNPCR")
    status = ShipmentStatus("Tracking", destination_port="Colon, PA", require_destination_evidence=True,
        recent_moves=[MovementEvent("Container Discharged (DISC)", "Colon, PA",
            datetime.now(timezone.utc)-timedelta(days=1), event_state="actual")])
    plan = ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)
    assert plan.task_status_update is None
    assert not any(w.field_id == "disc-field" for w in plan.custom_field_updates)
    assert status.destination_port == "Colon, PA"


def test_corrected_discharge_is_not_reintroduced_from_browser_capture():
    from shipment_sync.msc_browser_assisted import status_from_browser_capture

    capture = """CONTAINER NUMBER: MSDU7230448
Port of Discharge
Puerto Cortes, HN
POD ETA
11/09/2026
Date
Location
Description
11/09/2026
Puerto Cortes, HN
Estimated Time of Arrival
MSC WESER LF637R
04/09/2026
Colon, PA
Full Transshipment Loaded
MSC WESER LF636A
01/09/2026
Colon, PA
Full Transshipment Discharged
MSC AURIGA UX629A
"""
    status = status_from_browser_capture(capture)
    assert status.destination_port == "Puerto Cortes, HN"
    shipment = ShipmentRef("task", "27395", "msc", "BOOK", "MSDU7230448", "list",
        current_task_status="por arribar", destination_port="PUERTO CORTES",
        current_field_values={"disc-field": None})
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    for _ in range(3):
        plan = client.plan_shipment_update(shipment, status)
        assert plan.task_status_update is None
        assert not any(w.field_id == "disc-field" for w in plan.custom_field_updates)
        shipment.current_field_values.update({w.field_id: w.value for w in plan.custom_field_updates})
