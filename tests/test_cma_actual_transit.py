from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.cma_cgm import _consolidate_containers, _extract_moves
from shipment_sync.clickup_client import ClickUpClient, _derive_operational_status_step
from shipment_sync.models import ShipmentRef, ShipmentStatus
from test_track_trace_mapping import _settings


def evidence(code="LOAD", classifier="ACT", mode="VESSEL", days=-1, extra=None,
             carrier="CMA - CGM", current="en puerto origen", gates=True):
    now = datetime.now(timezone.utc)
    event = {"eventType": "EQUIPMENT" if code == "LOAD" else "TRANSPORT",
             "equipmentEventTypeCode" if code == "LOAD" else "transportEventTypeCode": code,
             "eventClassifierCode": classifier,
             "eventDateTime": (now + timedelta(days=days)).isoformat(),
             "transportCall": {"modeOfTransport": mode, "UNLocationCode": "CNLYG",
                 "vessel": {"vesselName": "CMA CGM POINTE DU PITON"}, "exportVoyageNumber": "KCS000S19"}}
    event.update(extra or {})
    fields = {"eta-field": now + timedelta(days=20), "etd-field": now - timedelta(days=1)}
    if gates:
        fields.update({"gtot-empty-field": now - timedelta(days=9),
                       "gtin-full-field": now - timedelta(days=9)})
    shipment = ShipmentRef("86e3a694d", "MTMLXGT-33671", carrier, "YGG0164368", "CMAU0583453",
        "list", current_task_status=current, destination_port="GTPRQ", current_field_values=fields)
    status = ShipmentStatus("Tracking", recent_moves=_extract_moves({"events": [event]}),
        eta_time=now + timedelta(days=20), destination_port="GTPRQ", require_destination_evidence=True,
        raw_source="cma-api:https://apis.cma-cgm.net/operation/trackandtrace/v1/events")
    return shipment, status


def plan(shipment, status):
    return ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment, status)


@pytest.mark.parametrize("code", ["LOAD", "DEPA"])
@pytest.mark.parametrize("mode", ["VESSEL", "BARGE"])
@pytest.mark.parametrize("carrier", ["CMA - CGM", "cma cgm", "CMA-CGM"])
@pytest.mark.parametrize("gates", [True, False])
def test_actual_marine_event_proves_transit(code, mode, carrier, gates):
    assert plan(*evidence(code=code, mode=mode, carrier=carrier, gates=gates)).task_status_update == "Tránsito"


@pytest.mark.parametrize("kwargs", [
    {"classifier": "PLN"}, {"classifier": "EST"}, {"classifier": None},
    {"mode": "TRUCK"}, {"mode": "ROAD"}, {"mode": "RAIL"}, {"mode": None},
    {"days": 1}, {"code": "GTIN"}, {"code": "GTOT"}, {"code": "ARRI"},
    {"extra": {"emptyIndicatorCode": "EMPTY"}},
    {"extra": {"eventDateTime": None, "eventCreatedDateTime": "2026-11-01T00:00:00Z"}},
    {"extra": {"eventDateTime": "not-a-date"}},
    {"extra": {"eventDateTime": "2026-11-01T10:00:00"}},
    {"extra": {"eventLocation": {"UNLocationCode": "GTPRQ"}}},
    {"extra": {"transportCall": {"modeOfTransport": "VESSEL", "UNLocationCode": "GTPRQ",
                                "vessel": {"vesselName": "SHIP"}}}},
    {"extra": {"transportCall": {"modeOfTransport": "VESSEL", "vessel": {"vesselName": "SHIP"}}}},
    {"extra": {"transportCall": {"modeOfTransport": "VESSEL", "UNLocationCode": "CNLYG"}}},
])
def test_saved_gates_and_past_etd_cannot_replace_verified_marine_event(kwargs):
    assert plan(*evidence(**kwargs)).task_status_update is None


@pytest.mark.parametrize("destination", [None, "unknown", "Puerto Quetzal / Colon"])
def test_unknown_destination_fails_closed(destination):
    shipment, status = evidence()
    shipment.destination_port = destination
    assert plan(shipment, status).task_status_update is None


def test_missing_or_naive_occurrence_time_fails_closed():
    shipment, status = evidence()
    for occurrence in [None, datetime(2026, 11, 1)]:
        status.recent_moves[0] = replace(status.recent_moves[0], event_occurrence_time=occurrence)
        assert plan(shipment, status).task_status_update is None


def test_33671_utc_date_boundary_does_not_turn_a_forecast_into_transit():
    settings = _settings(clickup_use_task_status=True)
    shipment, status = evidence(code="DEPA", classifier="PLN")
    status.recent_moves = _extract_moves({"events": [{"transportEventTypeCode": "DEPA",
        "eventClassifierCode": "PLN", "eventDateTime": "2026-11-08T01:00:00+08:00",
        "transportCall": {"modeOfTransport": "VESSEL", "UNLocationCode": "CNLYG"}}]})
    fields = {"etd-field": datetime(2026, 11, 8, 10, tzinfo=timezone.utc),
              "eta-field": datetime(2026, 12, 24, 10, tzinfo=timezone.utc),
              "gtot-empty-field": datetime(2026, 10, 30, 10, tzinfo=timezone.utc),
              "gtin-full-field": datetime(2026, 10, 30, 10, tzinfo=timezone.utc)}
    target = _derive_operational_status_step(shipment=shipment, status=status, settings=settings,
        field_values=fields, current_step="origin_port", status_by_step={},
        now_utc=datetime(2026, 11, 8, 4, 2, 19, tzinfo=timezone.utc))
    assert target == "origin_port"


def test_rolled_past_etd_keeps_actual_gates_at_origin_port():
    shipment, status = evidence(code="DEPA", classifier="PLN", current="recolectado")
    assert plan(shipment, status).task_status_update == "En puerto Origen"


def test_actual_loading_overrides_future_forecast_etd():
    shipment, status = evidence()
    shipment.current_field_values["etd-field"] = datetime.now(timezone.utc)+timedelta(days=3)
    assert plan(shipment, status).task_status_update == "Tránsito"


@pytest.mark.parametrize("current", ["tránsito", "arribado en puerto", "en almacén", "vacío devuelto", "cancelado"])
def test_no_automatic_regression_or_repeated_promotion(current):
    assert plan(*evidence(current=current)).task_status_update is None


def test_wrong_transit_is_not_repaired_without_owner_approval():
    assert plan(*evidence(code="DEPA", classifier="PLN", current="tránsito")).task_status_update is None


@pytest.mark.parametrize("all_loaded", [False, True])
def test_marine_milestone_requires_all_five_containers(all_loaded):
    shipment, status = evidence()
    references = ["CMAU0583453", "MAGU2310170", "APZU3240832", "APZU2130770", "DRYU2928022"]
    shipment.container_no = ", ".join(references)
    shipment.expected_container_count = 5
    gate = replace(status.recent_moves[0], name="Container Gated In (GTIN)", transport_mode="TRUCK",
                   vessel_voyage=None, equipment_load_state="LADEN")
    statuses = [replace(status, recent_moves=[gate]+status.recent_moves if all_loaded or i == 0 else [gate])
                for i in range(5)]
    consolidated = _consolidate_containers(shipment, references, statuses)
    assert plan(shipment, consolidated).task_status_update == ("Tránsito" if all_loaded else None)
