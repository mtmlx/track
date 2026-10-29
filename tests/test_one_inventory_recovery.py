from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.one_dcsa import _origin_departure
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import MovementEvent
from shipment_sync.one_split_bookings import container_bookings
from shipment_sync.sync import _run_sync
from test_one_dcsa import client, conflict_client, event, shipment, split_events, split_shipment, reviewed_combined_split_fixture
from test_one_rebooking import forecast_rows, writer
from test_track_trace_mapping import _settings


@pytest.mark.parametrize('depot,port', [('CNSXG','CNNGB'), ('CNTSN','CNTXG'), ('INJAI','INMUN'), ('TWKHH','TWKEL')])
@pytest.mark.parametrize('classifier', ['ACT', 'EST'])
def test_depot_does_not_bind_sailing_origin(monkeypatch, depot, port, classifier):
    rows = forecast_rows()
    rows[0]['eventLocation']['UNLocationCode'] = depot
    for row in rows[1:3]:
        row['eventLocation']['UNLocationCode'] = port
        row['eventClassifierCode'] = classifier
        if classifier == 'ACT':
            row['eventDateTime'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    s = shipment(current_task_status='Recolectado')
    status = client(monkeypatch, [(rows,{})]).fetch_status(s)
    assert status.origin_departure.location_code == port
    plan = writer().plan_shipment_update(s, status)
    assert any(u.label == 'ETD' for u in plan.custom_field_updates)
    assert plan.task_status_update == ('Tránsito' if classifier == 'ACT' else None)


def test_missing_feeder_departure_never_borrows_mother_vessel_departure():
    now = datetime.now(timezone.utc)
    moves = [MovementEvent('Container Gated Out (GTOT)', event_time=now-timedelta(days=1),
                           location_code='DEPOT', equipment_load_state='EMPTY'),
             MovementEvent('Container Loaded (LOAD)', event_time=now, location_code='FEEDER', transport_mode='BARGE'),
             MovementEvent('Transport Departed (DEPA)', event_time=now+timedelta(days=3), location_code='CNSHA')]
    assert _origin_departure(moves,'GTPRQ') is None


@pytest.mark.parametrize('depot,port', [('TWKHH','TWKEL'), ('TWKEL','TWKHH')])
def test_depot_gate_in_then_gate_out_does_not_bind_sailing_origin(depot, port):
    now = datetime.now(timezone.utc)
    moves = [MovementEvent('Container Gated In (GTIN)', event_time=now-timedelta(days=4),
                           location_code=depot, equipment_load_state='LADEN', event_state='actual'),
             MovementEvent('Container Gated Out (GTOT)', event_time=now-timedelta(days=3),
                           location_code=depot, equipment_load_state='EMPTY', event_state='actual'),
             MovementEvent('Container Gated In (GTIN)', event_time=now-timedelta(days=2),
                           location_code=port, equipment_load_state='LADEN', event_state='actual'),
             MovementEvent('Container Loaded (LOAD)', event_time=now-timedelta(days=1),
                           location_code=port, event_state='actual'),
             MovementEvent('Transport Departed (DEPA)', event_time=now,
                           location_code=port, event_state='actual')]
    assert _origin_departure(moves, 'GTPRQ') is moves[-1]


def test_out_of_order_forecast_gate_in_never_replaces_first_loaded_port():
    now = datetime.now(timezone.utc)
    moves = [MovementEvent('Container Gated In (GTIN)', event_time=now,
                           location_code='KRPUS', equipment_load_state='LADEN', event_state='estimated'),
             MovementEvent('Container Loaded (LOAD)', event_time=now+timedelta(days=3),
                           location_code='CNTXG', event_state='estimated'),
             MovementEvent('Transport Departed (DEPA)', event_time=now+timedelta(days=3,hours=1),
                           location_code='CNTXG', event_state='estimated'),
             MovementEvent('Transport Departed (DEPA)', event_time=now+timedelta(days=8),
                           location_code='KRPUS', event_state='estimated')]
    assert _origin_departure(moves, 'GTPRQ') is moves[2]


def test_actual_feeder_gate_in_without_departure_still_blocks_mother_departure():
    now = datetime.now(timezone.utc)
    moves = [MovementEvent('Container Gated In (GTIN)', event_time=now,
                           location_code='FEEDER', equipment_load_state='LADEN', event_state='actual'),
             MovementEvent('Container Loaded (LOAD)', event_time=now+timedelta(days=3),
                           location_code='CNSHA', event_state='estimated'),
             MovementEvent('Transport Departed (DEPA)', event_time=now+timedelta(days=4),
                           location_code='CNSHA', event_state='estimated')]
    assert _origin_departure(moves, 'GTPRQ') is None


@pytest.mark.parametrize('bookings', ['NB6BI3181300', 'NB6BI3181300, NB6BI3181301',
                                    'NB6BI3181301; NB6BI3181300', 'nb6bi3181301\nNB6BI3181300'])
def test_exact_reviewed_split_reference_set_is_accepted_without_rewriting(monkeypatch, bookings):
    s = split_shipment(); s.booking_no = bookings
    rows = [split_events('ONEU4413040','NB6BI3181301',15,'ONE SERENITY'),
            split_events('ONEU4454604','NB6BI3181300',10,'ONE SHINE')]
    status = client(monkeypatch, [(r,{}) for r in rows]).fetch_status(s)
    assert status.container_booking_map == {'ONEU4454604':'NB6BI3181300','ONEU4413040':'NB6BI3181301'}
    assert s.booking_no == bookings
    assert status.final_vessel_voyage is None
    plan = writer().plan_shipment_update(s,status)
    assert 'booking-field' not in {u.field_id for u in plan.custom_field_updates}
    assert plan.task_status_update is None


@pytest.mark.parametrize('bookings', ['NB6BI3181301', 'NB6BI3181300,OTHER',
    'NB6BI3181300,NB6BI3181301,OTHER', 'NB6BI3181300,NB6BI3181300',
    'NB6BI3181300/ NB6BI3181301', 'NB6BI3181300,', ''])
def test_unreviewed_or_malformed_split_sets_fail_closed(bookings):
    s = split_shipment(); s.booking_no = bookings
    with pytest.raises(ValueError, match='mapping conflicts'):
        container_bookings(s)


def test_reviewed_set_cannot_be_borrowed_by_other_task(monkeypatch):
    s = split_shipment(); s.task_id = 'other'; s.booking_no = 'NB6BI3181300, NB6BI3181301'
    assert container_bookings(s) is None
    rows = split_events('ONEU4413040','NB6BI3181301')
    with pytest.raises(ValueError, match='conflicting booking'):
        client(monkeypatch, [(rows,{})]).fetch_status(s)


def test_one_inventory_retains_missing_reference_tasks_for_notice_only():
    from test_clickup_carrier_prefilter import _FakeResponse, _FakeSession, _task, _settings as inventory_settings
    task = _task(); task['custom_fields'] = task['custom_fields'][:1]
    task['custom_fields'][0]['type'] = 'drop_down'
    task['custom_fields'][0]['value'] = 0
    c = ClickUpClient(inventory_settings(shipment_allowed_lines=['one']))
    c.session = _FakeSession(task_responses=[_FakeResponse({'tasks':[task], 'last_page':True})])
    refs = c.list_shipments()
    assert len(refs) == 1 and refs[0].booking_no is None and refs[0].container_no is None


def test_missing_reference_notice_precedes_recent_skip_and_preflight(monkeypatch):
    c,task,history,posts,_ = conflict_client(monkeypatch)
    s = shipment(current_task_status='Pendiente de booking'); s.booking_no = None; s.container_no = None
    s.last_checked_at = datetime.now(timezone.utc)
    task['custom_fields'][0]['value'] = None; task['custom_fields'][1]['value'] = None
    c.settings = replace(c.settings, shipment_min_sync_interval_hours=24, shipment_preflight_enabled=True)
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry',lambda:{})
    monkeypatch.setattr('shipment_sync.sync._preflight_supported_lines',lambda *a:pytest.fail('missing references never call a carrier'))
    monkeypatch.setattr(c,'update_shipment_status',lambda *a:pytest.fail('must not stamp or update fields'))
    stats = _run_sync(c,[s])
    assert stats.skipped == 1 and len(posts) == 1
    text = posts[0]['comment'][0]['text']
    assert 'Both Booking number' in text and 'Last T&T Update is not refreshed' in text
    assert 'keep the task pending' in text
    assert [p['user']['id'] for p in posts[0]['comment'] if p.get('type')=='tag'] == [12,34]
    history.append({'id':'comment-1','date':str(int(datetime.now(timezone.utc).timestamp()*1000)), 'comment':posts[0]['comment']})
    _run_sync(c,[s])
    assert len(posts) == 1


@pytest.mark.parametrize('status,task_id', [('cancelado','t'), ('VACIO DEVUELTO','t'), ('Pendiente de booking','86e3etkkm')])
def test_terminal_and_held_missing_tasks_are_never_notified(monkeypatch,status,task_id):
    c = ClickUpClient(_settings())
    s = shipment(current_task_status=status); s.task_id=task_id; s.booking_no=None; s.container_no=None
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry',lambda:{})
    monkeypatch.setattr(c,'report_tracking_failure',lambda *a,**k:pytest.fail('excluded task must not be touched'))
    _run_sync(c,[s])


def test_concurrently_entered_reference_suppresses_obsolete_notice(monkeypatch):
    c,task,_,posts,_ = conflict_client(monkeypatch)
    s = shipment(); s.booking_no=None; s.container_no=None
    assert not c.report_tracking_failure(s,error='missing booking and container',phase='references')
    assert not posts


def test_missing_reference_notice_links_to_search_form_not_empty_query():
    from shipment_sync.tracking_failures import source_url
    s = shipment(); s.booking_no=None; s.container_no=None
    assert source_url(s) == 'https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking'
