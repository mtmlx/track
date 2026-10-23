from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.clickup_client import ClickUpClient, ClickUpWorkloadLimitError, _build_task_status_update
from shipment_sync.destination import same_delivery_place, same_port
from shipment_sync.models import ShipmentRef
from shipment_sync.msc_browser_assisted import (
    MscBrowserCapture, consolidate_browser_statuses, status_from_browser_capture,
)
from shipment_sync.msc_browser_assisted_main import _import_batch
from test_track_trace_mapping import _settings


NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


def capture(reference="MSCU1234567", pod="Long Beach, US", delivery="Kansas City, US", events=None):
    if events is None:
        events = """26/10/2026
Long Beach, US
Estimated Time of Arrival
MSC TEST 001E
29/09/2026
Ningbo, CN
Export Loaded on Vessel
MSC TEST 001E
26/09/2026
Ningbo, CN
Export received at CY
LADEN
22/09/2026
Ningbo, CN
Empty to Shipper
EMPTY
"""
    return f"""BOOKING NUMBER: BOOK-1
Port of Discharge
{pod}
Shipped To
{delivery}
Container
{reference}
{events}"""


def shipment(**overrides):
    return replace(ShipmentRef("task-1", "Synthetic MSC shipment", "msc", "BOOK-1",
        "MSCU1234567", "list-1", list_name="InUSA shipments", current_task_status="transit",
        destination_port="KANSAS, USA", expected_container_count=1), **overrides)


def parsed(ship, *texts):
    pairs = []
    for text in texts:
        status = status_from_browser_capture(text)
        pairs.append((MscBrowserCapture(ship.task_id, text, status.discovered_containers[0]), status))
    return consolidate_browser_statuses(ship, pairs)


def client():
    return ClickUpClient(_settings(clickup_use_task_status=True, cf_vessel_voyage="vessel-field"))


def transition(ship, status, eta_days=4):
    settings = client().settings
    eta = NOW + timedelta(days=eta_days)
    status = replace(status, eta_time=eta, eta_local_text=eta.date().isoformat())
    plan = client().plan_shipment_update(ship, status)
    return _build_task_status_update(shipment=ship, status=status, settings=settings,
        candidate_field_updates=plan.custom_field_updates, now_utc=NOW)


@pytest.mark.parametrize("alias", ["Tampa", "Tampa, FL", "USTPA", "Tampa, US"])
def test_tampa_port_aliases_are_explicit(alias):
    assert same_port(alias, "Tampa, US")
    assert not same_port(alias, "Tampico, MX")


def test_kansas_legacy_label_is_delivery_only():
    assert same_delivery_place("KANSAS, USA", "Kansas City, US")
    assert not same_port("KANSAS, USA", "Kansas City, US")
    assert not same_port("KANSAS, USA", "Long Beach, US")
    assert not same_delivery_place("KANSAS, USA", "Long Beach, US")
    assert not same_delivery_place("Kansas", "Kansas City, US")


def test_route_preserves_ocean_pod_and_separate_delivery():
    ship = shipment()
    status = parsed(ship, capture())
    assert status.destination_port == status.validated_ocean_port == "Long Beach, US"
    assert status.place_of_delivery == "Kansas City, US"
    plan = client().plan_shipment_update(ship, status)
    assert "Ocean port of discharge: Long Beach, US" in plan.comment_text
    assert "Place of delivery: Kansas City, US" in plan.comment_text
    assert "not inland delivery or rail-ramp arrival" in plan.comment_text
    assert all(u.label != "Discharge date" for u in plan.custom_field_updates)


@pytest.mark.parametrize("days", [1, 2, 4, 5, 10])
def test_actual_loaded_shipment_catches_up_to_near_arrival(days):
    ship = shipment()
    assert transition(ship, parsed(ship, capture()), days) == "near arrival"


@pytest.mark.parametrize("days", [-1, 0, 11])
def test_forecast_date_alone_never_implies_arrival_or_rail(days):
    ship = shipment()
    assert transition(ship, parsed(ship, capture()), days) is None


def test_estimated_departure_does_not_authorize_transit_or_near_arrival():
    ship = shipment(current_task_status="bk confirmed")
    text = capture().replace("Export Loaded on Vessel", "Estimated Time of Departure")
    assert transition(ship, parsed(ship, text)) is None


def test_future_loading_does_not_authorize_transit():
    ship = shipment(current_task_status="bk confirmed")
    text = capture().replace("29/09/2026", "29/10/2026")
    assert transition(ship, parsed(ship, text)) is None


def test_truck_loading_does_not_authorize_transit():
    ship = shipment(current_task_status="bk confirmed")
    text = capture().replace("Export Loaded on Vessel", "Truck Loaded")
    assert transition(ship, parsed(ship, text)) is None


@pytest.mark.parametrize("delivery", ["UNKNOWN", "Kansas City, US / Dallas, US", "Charleston, US"])
def test_missing_ambiguous_or_wrong_delivery_is_rejected(delivery):
    with pytest.raises(ValueError, match="journey mismatch"):
        parsed(shipment(), capture(delivery=delivery))


def test_wrong_booking_cannot_be_rescued_by_matching_route():
    with pytest.raises(ValueError, match="booking/BL"):
        parsed(shipment(), capture().replace("BOOK-1", "BOOK-WRONG"))


def test_missing_ocean_port_is_rejected_even_when_delivery_matches():
    with pytest.raises(ValueError, match="destination port"):
        parsed(shipment(), capture(pod="UNKNOWN"))


def test_delivery_disagreement_across_containers_blocks_entire_shipment():
    ship = shipment(destination_port="Long Beach",container_no="MSCU1234567, MSCU7654321",expected_container_count=2)
    with pytest.raises(ValueError, match="place of delivery"):
        parsed(ship, capture(), capture("MSCU7654321",delivery="Dallas, US"))


def test_ocean_discharge_advances_at_port_not_inland_arrival():
    ship = shipment(current_task_status="near arrival")
    status = parsed(ship, capture(events="""20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
29/09/2026
Ningbo, CN
Export Loaded on Vessel
MSC TEST 001E
"""))
    plan = client().plan_shipment_update(ship, status)
    assert plan.task_status_update == "at port"
    assert next(u for u in plan.custom_field_updates if u.label == "Discharge date").value.date().isoformat() == "2026-10-20"
    assert all(u.label != "Gate in empty" for u in plan.custom_field_updates)


def test_actual_ocean_arrival_and_discharge_dates_stay_separate():
    ship = shipment(current_task_status="near arrival")
    text = capture(events="""21/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
20/10/2026
Long Beach, US
Vessel Arrival
MSC TEST 001E
""")
    plan = client().plan_shipment_update(ship,parsed(ship,text))
    values = {u.label:u.value for u in plan.custom_field_updates}
    assert values['ETA'].date().isoformat() == "2026-10-20"
    assert values['Discharge date'].date().isoformat() == "2026-10-21"
    assert plan.task_status_update == "at port"


def test_unvalidated_capture_cannot_override_shipment_destination():
    ship = shipment()
    status = status_from_browser_capture(capture(events="""20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
"""))
    assert status.validated_ocean_port is None
    plan = client().plan_shipment_update(ship,status)
    assert not plan.task_status_update
    assert all(u.label != "Discharge date" for u in plan.custom_field_updates)


def returned_capture(reference="MSCU1234567", returned=True):
    events = ("08/07/2026\nTampa, US\nEmpty received at CY\nEMPTY\n" if returned else "")
    events += """01/07/2026
Tampa, US
Import to consignee
LADEN
30/06/2026
Tampa, US
Import Discharged from Vessel
MSC TEST 001E
20/05/2026
Ningbo, CN
Export Loaded on Vessel
MSC TEST 001E
"""
    return capture(reference,pod="Tampa, US",delivery="Tampa, US",events=events)


def test_inusa_workflow_catches_up_to_empty_returned_without_saved_gate_dates():
    ship = shipment(destination_port="Tampa")
    plan = client().plan_shipment_update(ship,parsed(ship,returned_capture()))
    assert plan.task_status_update == "empty returned"
    assert {u.label for u in plan.custom_field_updates} >= {"Discharge date", "Gate out delivery", "Gate in empty"}


def test_partial_empty_return_cannot_close_multi_container_shipment():
    ship = shipment(destination_port="Tampa",container_no="MSCU1234567, MSCU7654321",expected_container_count=2,
        current_field_values={"gtin-empty-field":str(int((NOW-timedelta(days=2)).timestamp()*1000))})
    status = parsed(ship,returned_capture(),returned_capture("MSCU7654321",returned=False))
    plan = client().plan_shipment_update(ship,status)
    assert plan.task_status_update == "released to consignee"
    assert all(u.label != "Gate in empty" for u in plan.custom_field_updates)


def test_stale_forecast_does_not_hide_last_containers_empty_return_date():
    ship = shipment(destination_port="Tampa",container_no="MSCU1234567, MSCU7654321",expected_container_count=2)
    forecast = """POD ETA
31/12/2099
31/12/2099
Tampa, US
Estimated Time of Arrival
MSC TEST 001E
"""
    first = returned_capture().replace("08/07/2026","02/07/2026").replace("02/07/2026",forecast+"02/07/2026",1)
    second = returned_capture("MSCU7654321").replace("08/07/2026",forecast+"08/07/2026",1)
    status = parsed(ship,first,second)
    plan = client().plan_shipment_update(ship,status)
    assert status.all_containers_empty_returned
    assert next(u for u in plan.custom_field_updates if u.label=='Gate in empty').value.date().isoformat() == '2026-07-08'
    assert plan.task_status_update == 'empty returned'


def test_laden_rail_gate_is_not_empty_return():
    ship = shipment(current_task_status="at port")
    text = capture(events="""21/10/2026
Kansas City, US
Gate In at Rail Ramp
LADEN
20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
""")
    plan = client().plan_shipment_update(ship,parsed(ship,text))
    assert plan.task_status_update != "empty returned"
    assert all(u.label != "Gate in empty" for u in plan.custom_field_updates)


@pytest.mark.parametrize("event,target", [
    ("Rail Departure", "at rail"),
    ("Container Arrived at Rail Ramp", "container arrived at ramp"),
])
def test_actual_rail_milestones_require_separate_events(event, target):
    ship = shipment(current_task_status="at port")
    text = capture(events=f"""21/10/2026
Kansas City, US
{event}
LADEN
20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
""")
    plan = client().plan_shipment_update(ship,parsed(ship,text))
    assert plan.task_status_update == target
    assert all(u.label != "Gate in empty" for u in plan.custom_field_updates)


@pytest.mark.parametrize("event", ["Estimated Rail Departure", "Estimated Arrival at Rail Ramp"])
def test_estimated_rail_movement_does_not_advance_status(event):
    ship = shipment(current_task_status="at port")
    text = capture(events=f"""21/10/2026
Kansas City, US
{event}
LADEN
20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
""")
    assert client().plan_shipment_update(ship,parsed(ship,text)).task_status_update is None


def test_unknown_workflow_is_not_guessed():
    ship = shipment(current_task_status="awaiting inspection")
    assert transition(ship,parsed(ship,capture())) is None


def test_only_statuses_available_on_the_list_are_selected():
    ship = shipment(current_task_status="at port", workflow_status_names=(
        "transit", "near arrival", "at port", "at rail", "released to consignee", "empty returned"))
    text = capture(events="""21/10/2026
Kansas City, US
Container Arrived at Rail Ramp
LADEN
20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
""")
    plan = client().plan_shipment_update(ship,parsed(ship,text))
    assert plan.task_status_update is None
    assert all(u.label != "ETA" for u in plan.custom_field_updates)


def test_list_workflow_schema_is_read_once_and_cached(monkeypatch):
    c = client()
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"statuses": [{"status": "transit"}, {"status": "empty returned"}]}

    def read(method, url, **kwargs):
        calls.append((method,url))
        return Response()

    monkeypatch.setattr(c,"_request",read)
    expected = ("transit", "empty returned")
    assert c._workflow_status_names("list-1","transit","InUSA shipments") == expected
    assert c._workflow_status_names("list-1","near arrival","InUSA shipments") == expected
    assert calls == [("get", c.base_url+"/list/list-1")]


def test_missing_workflow_schema_fails_closed(monkeypatch):
    c = client()

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {}

    monkeypatch.setattr(c,"_request",lambda *args,**kwargs:Response())
    with pytest.raises(ClickUpWorkloadLimitError,match="schema is unavailable"):
        c._workflow_status_names("list-1","transit","InUSA shipments")


def test_wrong_rail_ramp_cannot_mark_final_delivery_arrival():
    ship = shipment(current_task_status="at port")
    text = capture(events="""21/10/2026
Los Angeles, US
Container Arrived at Rail Ramp
LADEN
20/10/2026
Long Beach, US
Import Discharged from Vessel
MSC TEST 001E
""")
    assert client().plan_shipment_update(ship,parsed(ship,text)).task_status_update is None


@pytest.mark.parametrize("terminal", ["empty returned", "vacío devuelto", "booking canceled", "blocked", "closed"])
def test_browser_import_never_touches_terminal_shipments(terminal):
    class NoWrites:
        settings = client().settings

        def __getattr__(self, name):
            raise AssertionError(f"Terminal shipment attempted {name}")

    ship = shipment(destination_port="Tampa", current_task_status=terminal)
    _import_batch(NoWrites(),[ship],[MscBrowserCapture(ship.task_id,returned_capture())],[],apply=True)


def test_repeat_projection_does_not_restore_inconsistent_fields():
    ship = shipment(destination_port="Tampa")
    c = client()
    status = parsed(ship,returned_capture())
    plan = c.plan_shipment_update(ship,status)
    values = dict(ship.current_field_values)
    values.update({u.field_id:u.value for u in plan.custom_field_updates})
    repeat = c.plan_shipment_update(replace(ship,current_task_status=plan.task_status_update,current_field_values=values),status)
    assert not repeat.task_status_update
    assert all(u.label == "Last T&T Update" for u in repeat.custom_field_updates)
