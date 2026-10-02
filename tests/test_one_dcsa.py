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
        'transportCall':{'vessel':{'vesselName':'FINAL SHIP'},'importVoyageNumber':'001E'}}


def client(monkeypatch, pages):
    c=OneDcsaClient();monkeypatch.setattr(c,'_token',lambda:'token')
    iterator=iter(pages);monkeypatch.setattr(c,'_request',lambda *a,**kw:next(iterator))
    return c


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
    c=client(monkeypatch,[([event('1','ARRI','GTPRQ','EST',5)],{})])
    s=shipment();s.destination_port=None
    status=c.fetch_status(s)
    assert status.eta_time is None and status.final_vessel_voyage is None


def test_token_cached_and_entitlement_checked(monkeypatch):
    c=OneDcsaClient();c.key='dummy';c.secret='dummy';calls=[]
    def request(*a,**kw):
        calls.append(a);return {'access_token':'token','expires_in':'3600','api_product_list_json':['DCSA TNT_PROD']},{}
    monkeypatch.setattr(c,'_request',request)
    assert c._token()==c._token()=='token' and len(calls)==1
    c.token=None;monkeypatch.setattr(c,'_request',lambda *a,**kw:({'access_token':'token','api_product_list_json':['OTHER']},{}))
    with pytest.raises(RuntimeError):c._token()


def test_booking_multiple_containers_not_projected(monkeypatch):
    c=client(monkeypatch,[([event('1','LOAD','TWKEL'),{**event('2','LOAD','TWKEL'),'equipmentReference':'CAAU2475597'}],{})])
    s=shipment();s.container_no=None
    with pytest.raises(ValueError):c.fetch_status(s)


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
