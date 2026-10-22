from datetime import datetime, timezone

import pytest

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from test_track_trace_mapping import _settings


def movement(code, timestamp, port, mode=None):
    return MovementEvent(name=f"Movement ({code})", event_time=datetime.fromisoformat(timestamp),
        event_time_local_text=timestamp, event_state="actual", location_code=port,
        location=port, transport_mode=mode)


def plan(moves, carrier="maersk"):
    shipment = ShipmentRef("86e1qk2x1", "MTMLXGT-26408", carrier, "276508515", "TRHU8425865", "list",
        current_task_status="tránsito", destination_port="GTPRQ")
    status = ShipmentStatus("Tracking", recent_moves=moves,
        eta_time=datetime(2026, 11, 1, 22, tzinfo=timezone.utc), destination_port="GTPRQ")
    return ClickUpClient(_settings()).plan_shipment_update(shipment, status)


@pytest.mark.parametrize("carrier", ["maersk", "maersk line", "a.p. moller - maersk"])
@pytest.mark.parametrize("mode", ["TRUCK", "RAIL", "ROAD", "TRAIN"])
def test_inland_departure_after_loading_cannot_infer_barge_or_replace_ocean_etd(carrier, mode):
    moves = [
        movement("GTIN", "2026-09-24T03:26:00-06:00", "MXZLO"),
        movement("LOAD", "2026-10-07T10:44:00-06:00", "MXZLO", "VESSEL"),
        movement("DEPA", "2026-10-07T14:23:00-06:00", "MXTEQ", mode),
        movement("ARRI", "2026-10-07T19:23:00-06:00", "MXZLO", mode),
        movement("DEPA", "2026-10-08T04:23:00-06:00", "MXZLO", "VESSEL"),
        movement("ARRI", "2026-10-13T02:00:00-06:00", "MXLZC", "VESSEL"),
    ]
    for _ in range(2):
        writes = {w.field_id:w.value for w in plan(moves, carrier).custom_field_updates}
        assert writes["etd-field"].date().isoformat() == "2026-10-08"


def test_actual_origin_barge_etd_is_preserved():
    moves = [
        movement("GTIN", "2026-06-21T12:00:00+08:00", "CNHFE"),
        movement("LOAD", "2026-06-22T12:00:00+08:00", "CNHFE", "BARGE"),
        movement("DISC", "2026-06-28T12:00:00+08:00", "CNSHA", "BARGE"),
        movement("DEPA", "2026-07-01T12:00:00+08:00", "CNSHA", "VESSEL"),
    ]
    writes = {w.field_id:w.value for w in plan(moves).custom_field_updates}
    assert writes["etd-field"].date().isoformat() == "2026-06-22"


def test_inland_only_evidence_does_not_write_ocean_etd_but_preserves_gate_fields():
    moves = [
        movement("GTOT", "2026-09-09T16:53:00-06:00", "MXGDL", "TRUCK"),
        movement("DEPA", "2026-10-07T14:23:00-06:00", "MXTEQ", "TRUCK"),
    ]
    writes = {w.field_id:w.value for w in plan(moves).custom_field_updates}
    assert "etd-field" not in writes
    assert writes["gtot-empty-field"].date().isoformat() == "2026-09-09"
