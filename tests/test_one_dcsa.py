from datetime import datetime, timedelta, timezone
import pytest
from shipment_sync.carriers.one_dcsa import OneDcsaClient
from shipment_sync.models import ShipmentRef
from shipment_sync.clickup_client import ClickUpClient
from test_track_trace_mapping import _settings


def shipment(**kwargs):
    return ShipmentRef('t', 'test', 'one', 'BOOK', 'ONEU2154315', 'l', destination_port='PUERTO QUETZAL', **kwargs)


def event(identity, code, port, classifier='ACT', days=-2, empty='LADEN'):
    return {'eventID':identity, 'eventCreatedDateTime':'2026-10-01T00:00:00Z',
        'eventType':'EQUIPMENT', 'equipmentEventTypeCode':code,
        'equipmentReference':'ONEU2154315','documentReferences':[{'documentReferenceType':'BKG','documentReferenceValue':'BOOK'}],
        'eventClassifierCode':classifier,'emptyIndicatorCode':empty,
        'eventDateTime':(datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),
        'eventLocation':{'UNLocationCode':port,'locationName':port},
        'transportCall':{'vessel':{'vesselName':'FINAL SHIP'},'exportVoyageNumber':'001E','importVoyageNumber':'001E'}}


def client(monkeypatch, pages):
    c=OneDcsaClient();monkeypatch.setattr(c,'_token',lambda:'token')
    iterator=iter(pages);monkeypatch.setattr(c,'_request',lambda *a,**kw:next(iterator))
    return c


def sailing_event(identity, code, port, voyage, *, classifier='ACT', days=-2, vessel='ONE CLARA', inbound=None):
    row = event(identity, code, port, classifier, days)
    row['transportCall'] = {'vessel': {'vesselName': vessel},
                            'exportVoyageNumber': voyage, 'importVoyageNumber': inbound}
    if code in ('DEPA', 'ARRI'):
        row['eventType'] = 'TRANSPORT'
        row['transportEventTypeCode'] = row.pop('equipmentEventTypeCode')
    return row


@pytest.mark.parametrize('reverse', [False, True])
def test_destination_import_reference_cannot_replace_actual_sailing_voyage(monkeypatch, reverse):
    rows = [sailing_event('load', 'LOAD', 'MXLZC', '0018E', days=-3),
            sailing_event('departure', 'DEPA', 'MXLZC', '0018E', days=-2),
            sailing_event('arrival', 'ARRI', 'GTPRQ', '', classifier='EST', days=5, inbound='0018W')]
    if reverse:
        rows.reverse()
    c = client(monkeypatch, [(rows, {})])
    status = c.fetch_status(shipment())
    assert status.final_vessel_voyage == 'ONE CLARA 0018E'
    assert status.eta_time > datetime.now(timezone.utc)
    settings = _settings(cf_vessel_voyage='vessel-field')
    s = shipment()
    s.current_field_values = {'vessel-field': 'ONE CLARA 0018W'}
    plan = ClickUpClient(settings).plan_shipment_update(s, status)
    assert next(f.value for f in plan.custom_field_updates if f.field_id == 'vessel-field') == 'ONE CLARA 0018E'
    s.current_field_values['vessel-field'] = 'ONE CLARA 0018E'
    repeated = ClickUpClient(settings).plan_shipment_update(s, c._map_status(s, rows, ['ONEU2154315']))
    assert 'vessel-field' not in {f.field_id for f in repeated.custom_field_updates}


@pytest.mark.parametrize('kind', ['inbound-only', 'wrong-vessel', 'old-rotation', 'conflicting'])
def test_unresolved_destination_voyage_preserves_verified_field(monkeypatch, kind):
    rows = [sailing_event('arrival', 'ARRI', 'GTPRQ', '', classifier='EST', days=5, inbound='0018W')]
    if kind == 'wrong-vessel':
        rows.append(sailing_event('load', 'LOAD', 'MXLZC', '0018E', vessel='OTHER SHIP'))
    elif kind == 'old-rotation':
        rows.append(sailing_event('load', 'LOAD', 'MXLZC', '0017E'))
    elif kind == 'conflicting':
        first = sailing_event('load', 'LOAD', 'MXLZC', '0018E')
        second = sailing_event('departure', 'DEPA', 'MXLZC', '0018W')
        second['eventDateTime'] = first['eventDateTime']
        rows.extend([first, second])
    status = client(monkeypatch, [(rows, {})]).fetch_status(shipment())
    assert status.final_vessel_voyage is None
    assert status.eta_time is not None
    s = shipment()
    s.current_field_values = {'vessel-field': 'ONE CLARA 0018E'}
    plan = ClickUpClient(_settings(cf_vessel_voyage='vessel-field')).plan_shipment_update(s, status)
    assert 'vessel-field' not in {f.field_id for f in plan.custom_field_updates}


def test_new_final_vessel_remains_visible_without_actual_loading(monkeypatch):
    rows = [sailing_event('load', 'LOAD', 'TWKEL', '0018E', vessel='OLD SHIP'),
            sailing_event('arrival', 'ARRI', 'GTPRQ', '0020W', classifier='EST', days=5, vessel='NEW SHIP')]
    assert client(monkeypatch, [(rows, {})]).fetch_status(shipment()).final_vessel_voyage == 'NEW SHIP 0020W'


def test_actual_vs_estimated_destination_and_final_vessel(monkeypatch):
    actual=event('1','DISC','MXZLO');forecast=event('2','ARRI','GTPRQ','EST',5)
    forecast['eventType']='TRANSPORT';forecast['transportEventTypeCode']=forecast.pop('equipmentEventTypeCode')
    c=client(monkeypatch,[([actual,forecast],{})]);status=c.fetch_status(shipment())
    assert status.latest_move.event_state=='actual' and status.latest_move.location_code=='MXZLO'
    assert status.eta_time>datetime.now(timezone.utc)
    assert status.final_vessel_voyage=='FINAL SHIP 001E'
    assert status.raw_source=='https://apix.one-line.com/v2/events'
    assert status.source_url=='https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking?trakNoParam=ONEU2154315&trakNoTpCdParam=C'
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment(),status)
    assert 'disc-field' not in {f.field_id for f in plan.custom_field_updates}
    assert plan.task_status_update not in ('Arribado en puerto','Vacío devuelto')


def test_pagination_deduplicates(monkeypatch):
    e=event('1','LOAD','TWKEL')
    c=client(monkeypatch,[([e],{'Next-Page-Cursor':'next'}),([e,event('2','DISC','GTPRQ')],{})])
    assert len(c.events(shipment())[0])==2


@pytest.mark.parametrize('change', [
    {'equipmentReference':'OTHER1234567'},
    {'documentReferences':[{'documentReferenceType':'BKG','documentReferenceValue':'OTHER'}]},
    {'eventID':None}, {'eventDateTime':'2026-09-01T12:00:00'},
    {'eventClassifierCode':'UNKNOWN'},
])
def test_invalid_identity_or_evidence_fails_closed(monkeypatch,change):
    e=event('1','DISC','GTPRQ');e.update(change)
    c=client(monkeypatch,[([e],{})])
    with pytest.raises(ValueError):c.fetch_status(shipment())


def test_future_actual_rejected(monkeypatch):
    c=client(monkeypatch,[([event('1','DISC','GTPRQ',days=3)],{})])
    with pytest.raises(ValueError):c.fetch_status(shipment())


def test_repeated_cursor_rejected(monkeypatch):
    c=client(monkeypatch,[([],{'Next-Page-Cursor':'same'}),([] ,{'Next-Page-Cursor':'same'})])
    with pytest.raises(ValueError):c.events(shipment())


def test_full_page_without_cursor_rejected(monkeypatch):
    c=client(monkeypatch,[([event(str(i),'LOAD','TWKEL') for i in range(100)],{})])
    with pytest.raises(ValueError):c.events(shipment())


def test_missing_destination_never_sets_eta_or_final_vessel(monkeypatch):
    c=client(monkeypatch,[([event('0','LOAD','TWKEL'),event('1','ARRI','GTPRQ','EST',5)],{})])
    s=shipment();s.destination_port=None
    status=c.fetch_status(s)
    assert status.eta_time is None and status.final_vessel_voyage is None
    plan=ClickUpClient(_settings(cf_vessel_voyage='vessel-field')).plan_shipment_update(s,status)
    assert 'vessel-field' not in {f.field_id for f in plan.custom_field_updates}


def test_token_cached_and_entitlement_checked(monkeypatch):
    c=OneDcsaClient();c.key='dummy';c.secret='dummy';calls=[]
    def request(*a,**kw):
        calls.append(a);return {'access_token':'token','expires_in':'3600','api_product_list_json':['DCSA TNT_PROD']},{}
    monkeypatch.setattr(c,'_request',request)
    assert c._token()==c._token()=='token' and len(calls)==1
    c.token=None;monkeypatch.setattr(c,'_request',lambda *a,**kw:({'access_token':'token','api_product_list_json':['OTHER']},{}))
    with pytest.raises(RuntimeError):c._token()


def test_booking_multiple_containers_are_fetched_individually(monkeypatch):
    first=event('1','LOAD','TWKEL')
    second={**event('2','LOAD','TWKEL'),'equipmentReference':'CAAU2475597'}
    c=multi_client(monkeypatch,{'ONEU2154315':[first],'CAAU2475597':[second]},[first,second])
    s=shipment();s.container_no=None
    status=c.fetch_status(s)
    assert status.discovered_containers==['CAAU2475597','ONEU2154315']
    assert status.latest_move.name=='Container Loaded (LOAD)'
    assert status.container_discovery_authoritative is False


def test_verified_empty_return_remains_destination_bound(monkeypatch):
    c=client(monkeypatch,[([event('1','DISC','GTPRQ',days=-5),event('2','GTIN','GTPRQ',empty='EMPTY')],{})])
    status=c.fetch_status(shipment())
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment(current_task_status='arribado en puerto'),status)
    assert plan.task_status_update=='Vacío devuelto'


def test_preflight_uses_only_new_domain_when_enabled(monkeypatch):
    from shipment_sync.sync import _preflight_hosts_for_line
    monkeypatch.setenv('ONE_DCSA_ENABLED', 'true')
    monkeypatch.setenv('ONE_TRACKING_URL_TEMPLATE', 'https://ecomm.one-line.com/legacy')
    assert _preflight_hosts_for_line('one') == ['apix.one-line.com']
    monkeypatch.setenv('ONE_DCSA_ENABLED', 'false')
    assert _preflight_hosts_for_line('one') == ['ecomm.one-line.com']


def test_estimated_origin_events_do_not_write_actual_fields_or_advance_status(monkeypatch):
    from test_track_trace_mapping import _settings
    forecasts=[event('gate-out','GTOT','TWKEL','EST',-4),event('gate-in','GTIN','TWKEL','EST',-3),event('depart','DEPA','TWKEL','EST',-2),event('arrive','ARRI','GTPRQ','EST',12)]
    c=client(monkeypatch,[(forecasts,{})]);s=shipment(current_task_status='pendiente de booking')
    s.current_field_values={'gtot-empty-field':datetime.now(timezone.utc)-timedelta(days=4),'gtin-full-field':datetime.now(timezone.utc)-timedelta(days=3),'etd-field':datetime.now(timezone.utc)-timedelta(days=2)}
    status=c.fetch_status(s)
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(s,status)
    fields={f.field_id for f in plan.custom_field_updates}
    assert not fields.intersection({'gtot-empty-field','gtin-full-field','etd-field'})
    assert plan.task_status_update is None


def multi_client(monkeypatch, rows, booking_rows=None):
    c=OneDcsaClient();monkeypatch.setattr(c,'_token',lambda:'token')
    def request(method,path,**kwargs):
        reference=kwargs['params'].get('equipmentReference')
        return (rows[reference] if reference else booking_rows),{}
    monkeypatch.setattr(c,'_request',request)
    return c


def second_container(*events):
    return [{**e,'equipmentReference':'CAAU2475597'} for e in events]


def multi_shipment():
    s=shipment(current_task_status='en tránsito')
    s.container_no='ONEU2154315, CAAU2475597';s.expected_container_count=2
    return s


def test_partial_container_discharge_cannot_complete_shipment(monkeypatch):
    first=[event('a-load','LOAD','TWKEL',days=-5),event('a-disc','DISC','GTPRQ')]
    second=second_container(event('b-load','LOAD','TWKEL',days=-4),event('b-disc','DISC','GTPRQ','EST',2))
    status=multi_client(monkeypatch,{'ONEU2154315':first,'CAAU2475597':second}).fetch_status(multi_shipment())
    assert status.latest_move.name=='Container Loaded (LOAD)'
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(multi_shipment(),status)
    assert 'disc-field' not in {f.field_id for f in plan.custom_field_updates}
    assert plan.task_status_update not in ('Arribado en puerto','Vacío devuelto')


def test_complete_container_discharge_uses_last_completion(monkeypatch):
    first=event('a','DISC','GTPRQ',days=-4);second=second_container(event('b','DISC','GTPRQ',days=-2))[0]
    status=multi_client(monkeypatch,{'ONEU2154315':[first],'CAAU2475597':[second]}).fetch_status(multi_shipment())
    assert status.latest_move.event_time==datetime.fromisoformat(second['eventDateTime'])
    assert status.latest_move.event_state=='actual'
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(multi_shipment(),status)
    assert 'disc-field' in {f.field_id for f in plan.custom_field_updates}


def test_multi_eta_uses_last_arrival_and_vessel_requires_agreement(monkeypatch):
    first=event('a','ARRI','GTPRQ','EST',3)
    second=second_container(event('b','ARRI','GTPRQ','EST',5))[0]
    second['transportCall']={'vessel':{'vesselName':'OTHER SHIP'},'exportVoyageNumber':'002E','importVoyageNumber':'002E'}
    rows={'ONEU2154315':[event('a-load','LOAD','TWKEL'),first],'CAAU2475597':second_container(event('b-load','LOAD','TWKEL'))+[second]}
    status=multi_client(monkeypatch,rows).fetch_status(multi_shipment())
    assert status.eta_time==datetime.fromisoformat(second['eventDateTime'])
    assert status.final_vessel_voyage is None and status.latest_move.event_state=='actual'
    plan=ClickUpClient(_settings(cf_vessel_voyage='vessel-field')).plan_shipment_update(multi_shipment(),status)
    assert 'vessel-field' not in {f.field_id for f in plan.custom_field_updates}


def test_multi_missing_destination_eta_preserves_eta(monkeypatch):
    c=multi_client(monkeypatch,{'ONEU2154315':[event('a','ARRI','GTPRQ','EST',3)],'CAAU2475597':second_container(event('b','LOAD','TWKEL'))})
    status=c.fetch_status(multi_shipment())
    assert status.eta_time is None and status.final_vessel_voyage is None


@pytest.mark.parametrize('rows', [[],second_container(event('bad','LOAD','TWKEL'))])
def test_multi_missing_or_wrong_booking_holds_entire_record(monkeypatch,rows):
    if rows:rows[0]['documentReferences'][0]['documentReferenceValue']='OTHER'
    c=multi_client(monkeypatch,{'ONEU2154315':[event('a','LOAD','TWKEL')],'CAAU2475597':rows})
    with pytest.raises(ValueError):c.fetch_status(multi_shipment())


def test_container_population_must_match_declared_count(monkeypatch):
    c=client(monkeypatch,[]);s=multi_shipment();s.expected_container_count=1
    with pytest.raises(ValueError,match='population'):c.fetch_status(s)


def test_booking_discovery_completes_declared_population(monkeypatch):
    first=event('a','LOAD','TWKEL');second=second_container(event('b','LOAD','TWKEL'))[0]
    c=multi_client(monkeypatch,{'ONEU2154315':[first],'CAAU2475597':[second]},[first,second])
    s=shipment(expected_container_count=2)
    status=c.fetch_status(s)
    assert status.discovered_containers==['CAAU2475597','ONEU2154315']
    assert status.latest_move.name=='Container Loaded (LOAD)'


def test_booking_discovery_must_include_original_container(monkeypatch):
    second=second_container(event('b','LOAD','TWKEL'))[0]
    c=multi_client(monkeypatch,{},[second]);s=shipment(expected_container_count=2)
    with pytest.raises(ValueError,match='does not include'):c.fetch_status(s)


def test_container_workload_limit(monkeypatch):
    s=shipment();s.container_no=','.join(f'ONEU{i:07d}' for i in range(21))
    with pytest.raises(ValueError,match='twenty'):client(monkeypatch,[]).fetch_status(s)


def test_multi_empty_return_requires_every_container_after_discharge(monkeypatch):
    first=[event('a-disc','DISC','GTPRQ',days=-5),event('a-return','GTIN','GTPRQ',empty='EMPTY')]
    second=second_container(event('b-disc','DISC','GTPRQ',days=-3),event('b-return','GTIN','GTPRQ',days=-4,empty='EMPTY'))
    c=multi_client(monkeypatch,{'ONEU2154315':first,'CAAU2475597':second})
    status=c.fetch_status(multi_shipment())
    assert all(m.source_event_name!='Empty Container Returned from Customer' for m in status.recent_moves)
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(multi_shipment(),status)
    assert plan.task_status_update!='Vacío devuelto'
    second[-1]['eventDateTime']=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
    returned=multi_shipment();returned.current_task_status='arribado en puerto'
    status=c.fetch_status(returned)
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(returned,status)
    assert plan.task_status_update=='Vacío devuelto'


@pytest.mark.parametrize('code,port,empty,field', [
    ('GTIN','TWKEL','EMPTY','gtin-full-field'),
    ('GTOT','TWKEL','LADEN','gtot-empty-field'),
    ('GTIN','GTPRQ','LADEN','gtin-empty-field'),
    ('GTOT','GTPRQ','EMPTY','gtot-delivery-field'),
    ('DISC','GTPRQ','EMPTY','disc-field'),
])
def test_gate_and_discharge_fields_require_correct_load_state(monkeypatch,code,port,empty,field):
    rows=([event('disc','DISC','GTPRQ',days=-5)] if port=='GTPRQ' and code!='DISC' else [])+[event('gate',code,port,empty=empty)]
    status=client(monkeypatch,[(rows,{})]).fetch_status(shipment())
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment(),status)
    assert field not in {f.field_id for f in plan.custom_field_updates}


def test_multi_different_load_states_do_not_complete_same_milestone(monkeypatch):
    first=event('a','GTOT','TWKEL',empty='EMPTY')
    second=second_container(event('b','GTOT','TWKEL',empty='LADEN'))[0]
    status=multi_client(monkeypatch,{'ONEU2154315':[first],'CAAU2475597':[second]}).fetch_status(multi_shipment())
    assert status.latest_move is None and status.recent_moves==[]


def test_laden_origin_gate_out_does_not_validate_persisted_empty_pickup(monkeypatch):
    s=shipment(current_task_status='pendiente de booking')
    s.current_field_values={'gtot-empty-field':datetime.now(timezone.utc)-timedelta(days=3)}
    status=client(monkeypatch,[([event('gate','GTOT','TWKEL',empty='LADEN')],{})]).fetch_status(s)
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(s,status)
    assert plan.task_status_update is None
