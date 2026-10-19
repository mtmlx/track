"""Forecast reconciliation must never become proof of sailing."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from shipment_sync.carriers.one_dcsa import _origin_departure
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import MovementEvent
from shipment_sync.one_write_holds import write_hold_reason
from test_one_dcsa import booking_event, client, event, sailing_event, shipment
from test_track_trace_mapping import _settings


@pytest.fixture
def booking_source(monkeypatch):
    import shipment_sync.carriers.one as one
    payload = {'status':200, 'total':1, 'data':[{
        'bookingNo':'BOOK', 'latestEvent':{'eventName':'Booking Confirmed'},
        'por':{'code':'TWKEL','locationName':'KEELUNG'}, 'pod':{'code':'GTPRQ'},
        'vesselVoyage':{'vesselName':'MONTEVIDEO EXPRESS','outboundConsortiumVoyage':'2645E'},
        'cargoEvents':[{'matrixId':'E061','trigger':'ESTIMATED','locationName':'KEELUNG',
                        'date':'2026-11-10T17:00:00Z','localPortDate':'2026-11-11T01:00:00Z'},
                       {'matrixId':'E089','trigger':'ESTIMATED','locationName':'PUERTO QUETZAL',
                        'date':'2026-12-26T10:00:00Z','localPortDate':'2026-12-26T04:00:00Z'}]}]}
    class Response:
        def raise_for_status(self): pass
        def close(self): pass
    adapter=one.OneAdapter()
    monkeypatch.setattr(adapter.session,'post',lambda *a,**k:Response())
    monkeypatch.setattr(one,'bounded_response_json',lambda r:payload)
    s=shipment(current_task_status='BK confirmado'); s.container_no=None
    return adapter,s,payload['data'][0]


def test_34725_booking_only_origin_uses_port_local_day_and_preserves_vessel(booking_source):
    adapter,s,row=booking_source
    s.current_field_values={'vessel-field':'VERIFIED DESTINATION VESSEL 123E'}
    status=adapter._fetch_booking_only_status(s)
    assert status.origin_departure.location_code=='TWKEL'
    assert status.origin_departure.event_state=='estimated'
    assert status.origin_departure.event_time.day==10
    plan=writer().plan_shipment_update(s,status)
    fields={u.field_id:u.value for u in plan.custom_field_updates}
    assert fields['etd-field'].date().isoformat()=='2026-11-11'
    assert status.final_vessel_voyage is None and status.vessel_voyage is None
    assert 'vessel-field' not in fields
    assert plan.task_status_update is None
    assert not status.recent_moves and status.latest_move is None
    assert not {'gtout-empty-field','gtin-full-field','disc-field'} & fields.keys()


@pytest.mark.parametrize('booking_status',['Processing','Booking Cancelled','Booking Canceled','Unknown'])
def test_edh_unconfirmed_booking_never_publishes_forecast(booking_source,booking_status):
    adapter,s,row=booking_source
    row['latestEvent']['eventName']=booking_status
    status=adapter._fetch_booking_only_status(s)
    assert status.origin_departure is None and status.eta_time is None
    assert status.final_vessel_voyage is None


@pytest.mark.parametrize('change', ['missing_por','missing_code','wrong_location','destination_as_origin',
                                  'wrong_pod','missing_date','missing_local_date','actual','unknown_trigger','inland'])
def test_booking_origin_requires_matching_explicit_forecast_evidence(booking_source,change):
    adapter,s,row=booking_source
    event=row['cargoEvents'][0]
    if change=='missing_por': row.pop('por')
    elif change=='missing_code': row['por'].pop('code')
    elif change=='wrong_location': event['locationName']='KAOHSIUNG'
    elif change=='destination_as_origin': row['por']['code']='GTPRQ'
    elif change=='wrong_pod': row['pod']['code']='PABLB'
    elif change=='missing_date': event.pop('date')
    elif change=='missing_local_date': event.pop('localPortDate')
    elif change=='actual': event['trigger']='ACTUAL'
    elif change=='unknown_trigger': event['trigger']='UNKNOWN'
    elif change=='inland': event['matrixId']='E051'
    assert adapter._fetch_booking_only_status(s).origin_departure is None


@pytest.mark.parametrize('key',['date','localPortDate'])
def test_booking_origin_naive_timestamp_fails_closed(booking_source,key):
    adapter,s,row=booking_source
    row['cargoEvents'][0][key]='2026-11-11T01:00:00'
    with pytest.raises(ValueError,match='explicit timezone'):
        adapter._fetch_booking_only_status(s)


def test_booking_origin_conflicting_schedules_fail_closed(booking_source):
    adapter,s,row=booking_source
    row['cargoEvents'].append(dict(row['cargoEvents'][0],date='2026-11-13T17:00:00Z'))
    with pytest.raises(ValueError,match='conflicting origin'):
        adapter._fetch_booking_only_status(s)


def test_past_booking_forecast_does_not_prove_transit(booking_source):
    adapter,s,row=booking_source
    row['cargoEvents'][0].update(date='2026-01-01T10:00:00Z',localPortDate='2026-01-01T18:00:00Z')
    status=adapter._fetch_booking_only_status(s)
    plan=writer().plan_shipment_update(s,status)
    assert plan.task_status_update is None
    assert not status.recent_moves


def test_held_33793_remains_blocked_with_booking_origin_forecast(booking_source):
    adapter,s,row=booking_source
    s.task_id='86e3etkkm'
    assert write_hold_reason(s)
    with pytest.raises(ValueError,match='33793'):
        writer().update_shipment_status(s,adapter._fetch_booking_only_status(s))


def test_booking_forecast_projection_requires_supported_one_source(booking_source):
    adapter,s,_=booking_source
    status=adapter._fetch_booking_only_status(s)
    status.raw_source='https://untrusted.example/events'
    assert 'etd-field' not in {u.field_id for u in writer().plan_shipment_update(s,status).custom_field_updates}


def test_identical_booking_departure_rows_are_idempotent(booking_source):
    adapter,s,row=booking_source
    row['cargoEvents'].append(dict(row['cargoEvents'][0]))
    status=adapter._fetch_booking_only_status(s)
    c=writer()
    plan=c.plan_shipment_update(s,status)
    s.current_field_values={u.field_id:int(u.value.timestamp()*1000) if isinstance(u.value,datetime) else u.value
                            for u in plan.custom_field_updates}
    s.track_trace_snapshot_hash=plan.snapshot_hash
    assert not c.plan_shipment_update(s,status).changed


def forecast_rows():
    return [
        event('empty', 'GTOT', 'CNSHA', days=-2, empty='EMPTY'),
        sailing_event('load', 'LOAD', 'CNSHA', '2642E', classifier='EST', days=5, vessel='RDO ACE'),
        sailing_event('departure', 'DEPA', 'CNSHA', '2642E', classifier='EST', days=6, vessel='RDO ACE'),
        sailing_event('arrival', 'ARRI', 'GTPRQ', '', classifier='EST', days=35,
                      vessel='RDO ACE', inbound='2642W'),
    ]


def writer():
    return ClickUpClient(_settings(clickup_use_task_status=True, cf_vessel_voyage='vessel-field'))


def test_33572_updates_forecast_etd_but_keeps_collection_not_transit(monkeypatch):
    rows = forecast_rows()
    s = shipment(current_task_status='Recolectado')
    s.current_field_values = {'etd-field': int(datetime.now(timezone.utc).timestamp() * 1000),
                              'vessel-field': 'ONE SERENITY 2639E'}
    status = client(monkeypatch, [(rows, {})]).fetch_status(s)
    plan = writer().plan_shipment_update(s, status)
    fields = {u.field_id: u.value for u in plan.custom_field_updates}
    assert fields['etd-field'].date().isoformat() == rows[2]['eventDateTime'][:10]
    assert fields['vessel-field'] == 'RDO ACE 2642E'
    assert 'gtin-full-field' not in fields and 'disc-field' not in fields
    assert plan.task_status_update is None
    assert 'Departure NOT validated' in plan.comment_text


def test_33793_preview_corrects_schedule_without_automatic_status_rollback(monkeypatch):
    rows = forecast_rows()
    s = shipment(current_task_status='Tránsito')
    s.current_field_values = {'vessel-field': 'ONE SERENITY 2639E'}
    status = client(monkeypatch, [(rows, {})]).fetch_status(s)
    plan = writer().plan_shipment_update(s, status)
    assert plan.task_status_update is None
    assert any(u.label == 'ETD' for u in plan.custom_field_updates)
    assert 'assignee must review' in plan.comment_text
    assert 'ACTUAL' not in plan.comment_text.split('Origin departure: ')[1].split('\n')[0]


@pytest.mark.parametrize('code', ['PENC', 'CANC', 'REJE', 'UNKNOWN'])
def test_unconfirmed_booking_cannot_publish_forecast_schedule(monkeypatch, code):
    s = shipment(current_task_status='Pendiente de booking'); s.container_no = None
    rows = forecast_rows()[1:]
    for row in rows:
        row.pop('equipmentReference')
    status = client(monkeypatch, [([booking_event(code)] + rows, {})]).fetch_status(s)
    plan = writer().plan_shipment_update(s, status)
    assert status.booking_only and status.origin_departure is None
    assert status.final_vessel_voyage is None
    assert not {'etd-field', 'vessel-field', 'eta-field'} & {u.field_id for u in plan.custom_field_updates}
    assert plan.task_status_update is None


def test_confirmed_booking_schedule_without_containers_is_not_physical_evidence(monkeypatch):
    s = shipment(current_task_status='Pendiente de booking'); s.container_no = None
    rows = forecast_rows()[1:]
    for row in rows:
        row.pop('equipmentReference')
    status = client(monkeypatch, [([booking_event()] + rows, {})]).fetch_status(s)
    plan = writer().plan_shipment_update(s, status)
    assert status.booking_only and not status.recent_moves and status.latest_move is None
    assert status.origin_departure.event_state == 'estimated'
    assert status.final_vessel_voyage == 'RDO ACE 2642E'
    assert plan.task_status_update == 'BK confirmado'
    assert {'etd-field', 'vessel-field'} <= {u.field_id for u in plan.custom_field_updates}
    assert not any(u.label and 'Gate' in u.label for u in plan.custom_field_updates)


def test_containerless_departure_requires_positive_booking_identity(monkeypatch):
    s = shipment(); s.container_no = None
    row = forecast_rows()[2]; row.pop('equipmentReference'); row['documentReferences'] = []
    status = client(monkeypatch, [([booking_event(), row], {})]).fetch_status(s)
    assert status.origin_departure is None


@pytest.mark.parametrize('mode', ['TRUCK', 'ROAD', 'RAIL', 'TRAIN'])
def test_actual_inland_departure_never_authorizes_one_transit(monkeypatch, mode):
    rows = forecast_rows()
    rows[2] = sailing_event('depart', 'DEPA', 'CNSHA', '2642E', days=-1, vessel='RDO ACE')
    rows[2]['transportCall']['modeOfTransport'] = mode
    s = shipment(current_task_status='Recolectado')
    status = client(monkeypatch, [(rows, {})]).fetch_status(s)
    assert status.origin_departure is None
    plan = writer().plan_shipment_update(s, status)
    assert plan.task_status_update is None
    assert 'etd-field' not in {u.field_id for u in plan.custom_field_updates}
    assert 'Departure NOT validated' in plan.comment_text


def test_actual_ocean_departure_still_authorizes_transit(monkeypatch):
    rows = forecast_rows()
    rows[2] = sailing_event('depart', 'DEPA', 'CNSHA', '2642E', days=-1, vessel='RDO ACE')
    rows[2]['transportCall']['modeOfTransport'] = 'VESSEL'
    s = shipment(current_task_status='Recolectado')
    status = client(monkeypatch, [(rows, {})]).fetch_status(s)
    assert status.origin_departure.event_state == 'actual'
    plan = writer().plan_shipment_update(s, status)
    assert plan.task_status_update == 'Tránsito'
    assert 'Departure NOT validated' not in plan.comment_text


def test_forecast_schedule_repeated_plan_is_idempotent(monkeypatch):
    s = shipment(current_task_status='Recolectado')
    status = client(monkeypatch, [(forecast_rows(), {})]).fetch_status(s)
    plan = writer().plan_shipment_update(s, status)
    saved = {u.field_id: int(u.value.timestamp() * 1000) if hasattr(u.value, 'timestamp') else u.value
             for u in plan.custom_field_updates}
    again = writer().plan_shipment_update(replace(s, current_field_values=saved), status)
    assert not again.changed and again.comment_text is None
    assert {u.label for u in again.custom_field_updates} == {'Last T&T Update'}


def test_first_origin_and_local_date_preserved_over_transshipment(monkeypatch):
    rows = forecast_rows()
    rows[2]['eventDateTime'] = (datetime.now(timezone.utc) + timedelta(days=6)).strftime('%Y-%m-%dT01:00:00+08:00')
    rows.append(sailing_event('second-leg', 'DEPA', 'KRPUS', '999E', classifier='EST', days=12))
    status = client(monkeypatch, [(rows, {})]).fetch_status(shipment())
    plan = writer().plan_shipment_update(shipment(), status)
    assert status.origin_departure.location_code == 'CNSHA'
    assert next(u.value for u in plan.custom_field_updates if u.label == 'ETD').date().isoformat() == rows[2]['eventDateTime'][:10]


def test_ambiguous_origin_schedules_hold_before_writes(monkeypatch):
    rows = forecast_rows()
    rows.append(sailing_event('conflicting', 'DEPA', 'CNSHA', '2643E', classifier='EST', days=7, vessel='OTHER'))
    with pytest.raises(ValueError, match='conflicting origin departure'):
        client(monkeypatch, [(rows, {})]).fetch_status(shipment())


def test_schedule_conflict_notice_explains_issue_impact_and_correction():
    from shipment_sync.tracking_failures import classify_failure
    notice = classify_failure(shipment(), 'ONE booking has conflicting origin departure schedules; confirm the rebooking before updating')
    assert notice.category == 'booking_schedule_conflict'
    assert 'incorrect status' in notice.reason
    assert 'cutoff' in notice.actions[0]
    assert 'Do not change container identities' in notice.actions[1]


def test_missing_origin_does_not_borrow_transshipment_departure():
    now = datetime.now(timezone.utc)
    moves = [MovementEvent('Container Loaded (LOAD)', event_time=now, location_code='CNHFE'),
             MovementEvent('Transport Departed (DEPA)', event_time=now + timedelta(days=2), location_code='CNSHA')]
    assert _origin_departure(moves, 'GTPRQ') is None


def test_actual_origin_departure_wins_over_revised_forecast(monkeypatch):
    rows = forecast_rows()
    rows.append(sailing_event('actual', 'DEPA', 'CNSHA', '2642E', days=-1, vessel='RDO ACE'))
    status = client(monkeypatch, [(rows, {})]).fetch_status(shipment())
    assert status.origin_departure.event_state == 'actual'


def test_33793_hold_blocks_all_mutations_but_allows_preview(monkeypatch):
    s = shipment(); s.task_id = '86e3etkkm'
    status = client(monkeypatch, [(forecast_rows(), {})]).fetch_status(s)
    c = writer()
    monkeypatch.setattr(c, '_request', lambda *a, **k: pytest.fail('held task must not be touched'))
    assert c.plan_shipment_update(s, status).changed
    with pytest.raises(ValueError, match='approved repair preview'):
        c.update_shipment_status(s, status)
    assert write_hold_reason(replace(s, shipping_line='maersk')) is None


def test_scheduled_hold_precedes_preflight_lookup_and_failure_comments(monkeypatch):
    from shipment_sync.sync import _run_sync
    s = shipment(); s.task_id = '86e3etkkm'
    c = writer()
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry', lambda: {})
    monkeypatch.setattr(c, 'report_tracking_failure', lambda *a, **k: pytest.fail('no comment'))
    stats = _run_sync(c, [s])
    assert stats.skipped == 1 and stats.total_candidates == 0


@pytest.mark.parametrize('fail', [False, True])
def test_one_completion_stamp_follows_successful_fields_and_comment(monkeypatch, fail):
    s = shipment()
    status = client(monkeypatch, [(forecast_rows(), {})]).fetch_status(s)
    c = writer()
    calls = []
    monkeypatch.setattr(c, '_set_date_custom_field', lambda task, field, value, **kw: calls.append(field))
    monkeypatch.setattr(c, '_set_custom_field', lambda task, field, value: calls.append(field))
    def comment(*args):
        calls.append('comment')
        if fail:
            raise RuntimeError('comment failed')
    monkeypatch.setattr(c, '_post_comment', comment)
    if fail:
        with pytest.raises(RuntimeError, match='comment failed'):
            c.update_shipment_status(s, status)
        assert 'last-checked-field' not in calls
    else:
        c.update_shipment_status(s, status)
        assert calls[-1] == 'last-checked-field' and calls[-2] == 'comment'
