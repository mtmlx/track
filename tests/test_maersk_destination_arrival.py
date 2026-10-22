from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.maersk import MaerskAdapter, _event_to_movement, _status_from_events
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


def _arrival(code, when, state="ACT", mode="VESSEL", **extra):
    return {"eventType": "TRANSPORT", "transportEventTypeCode": "ARRI",
            "eventDateTime": when, "eventClassifierCode": state,
            "transportCall": {"UNLocationCode": code, "modeOfTransport": mode,
                              "vessel": {"vesselName": "MSC SARYA III"},
                              "exportVoyageNumber": "641A"}, **extra}


def _33773_events():
    return [
        _arrival("USHOU", "2026-10-07T23:00:00-05:00", "EST", "TRUCK"),
        _arrival("GTPBR", "2026-10-17T19:05:00-06:00"),
        {"eventType": "EQUIPMENT", "equipmentEventTypeCode": "DISC",
         "eventClassifierCode": "ACT", "eventDateTime": "2026-10-18T00:05:00-06:00",
         "transportCall": {"UNLocationCode": "GTPBR", "modeOfTransport": "VESSEL"}},
    ]


def test_33773_actual_pod_arrival_replaces_inland_estimate_and_keeps_discharge_separate():
    status = _status_from_events(_33773_events(), "maersk-events-api:test", source_url=None,
                                 destination_port="PUERTO BARRIOS")
    assert status.eta_local_text == "2026-10-17T19:05:00-06:00"
    assert status.final_vessel_voyage == "MSC SARYA III 641A"
    shipment = ShipmentRef("task", "33773", "maersk", "277740394", "MRSU8843754", "list",
        destination_port="GTPBR", current_task_status="arribado en puerto",
        current_field_values={"eta-field": "1791367200000", "disc-field": "1792231200000"})
    # Exercise the write boundary with an independently corrupted/stale adapter ETA.
    status.eta_local_text = "2026-10-07T23:00:00-05:00"
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    first = client.plan_shipment_update(shipment, status)
    writes = {w.field_id: w.value for w in first.custom_field_updates}
    assert writes["eta-field"].date().isoformat() == "2026-10-17"
    assert writes["disc-field"].date().isoformat() == "2026-10-18"
    shipment.current_field_values.update(writes)
    for _ in range(3):
        plan = client.plan_shipment_update(shipment, status)
        assert not {"eta-field", "disc-field"}.intersection(w.field_id for w in plan.custom_field_updates)
        assert plan.task_status_update is None


@pytest.mark.parametrize("pod", ["GTPBR", "PUERTO BARRIOS", "Puerto Barrios, Guatemala"])
def test_maersk_adapter_uses_authoritative_shipment_destination(monkeypatch, pod):
    adapter = MaerskAdapter()
    monkeypatch.setattr(adapter, "_fetch_payload", lambda *a: ({"events": _33773_events()}, "maersk-events-api:test"))
    shipment = ShipmentRef("task", "33773", "maersk", "277740394", "MRSU8843754", "list", destination_port=pod)
    status = adapter.fetch_status(shipment)
    assert status.eta_local_text == "2026-10-17T19:05:00-06:00"


@pytest.mark.parametrize("pod,event", [
    (None, _arrival("GTPBR", "2026-10-17T19:05:00-06:00")),
    ("GTPBR", _arrival("AMBIGUOUS", "2026-10-17T19:05:00-06:00", locationName="Puerto Barrios")),
    ("GTPBR", _arrival("USHOU", "2026-10-17T19:05:00-06:00")),
    ("GTPBR", _arrival("GTPBR", "2026-10-17T19:05:00-06:00", mode="TRUCK")),
    ("GTPBR", _arrival("GTPBR", "2099-10-17T19:05:00-06:00")),
    ("GTPBR", _arrival("GTPBR", "2026-10-17T19:05:00-06:00", state="UNKNOWN")),
    ("GTPBR", _arrival("GTPBR", "2026-10-17T19:05:00-06:00", UNLocationCode="USHOU")),
    ("GTPBR", _arrival("GTPBR", "invalid")),
    ("GTPBR", _arrival("GTPBR", None, eventCreatedDateTime="2026-10-17T19:05:00-06:00")),
    ("GTPBR", _arrival("GTPBR", "2026-10-17T19:05:00-06:00", modeOfTransport="TRUCK")),
])
def test_unverified_arrival_does_not_overwrite_saved_eta(pod, event):
    status = _status_from_events([event], "maersk-events-api:test", source_url=None, destination_port=pod)
    assert status.eta_time is None and status.eta_local_text is None
    shipment = ShipmentRef("task", "33773", "maersk", "BOOK", "CONT", "list", destination_port=pod,
                            current_field_values={"eta-field": "1792231200000"})
    plan = ClickUpClient(_settings()).plan_shipment_update(shipment, status)
    assert not any(w.field_id == "eta-field" for w in plan.custom_field_updates)


def test_actual_destination_arrival_wins_over_later_estimate():
    events = _33773_events() + [_arrival("GTPBR", "2026-11-17T19:05:00-06:00", "EST")]
    status = _status_from_events(events, "maersk-events-api:test", source_url=None, destination_port="GTPBR")
    assert status.eta_local_text == "2026-10-17T19:05:00-06:00"


def test_no_actual_arrival_uses_only_destination_vessel_estimate():
    events = [_arrival("GTPBR", "2026-11-17T19:05:00-06:00", "EST"),
              _arrival("USHOU", "2026-12-17T19:05:00-06:00", "EST", "TRUCK")]
    status = _status_from_events(events, "maersk-events-api:test", source_url=None, destination_port="GTPBR")
    assert status.eta_local_text == "2026-11-17T19:05:00-06:00"
