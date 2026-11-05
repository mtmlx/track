from dataclasses import replace
from datetime import datetime, timezone

import pytest

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter, CmaCgmDcsaEventFetch, _extract_moves, _pick_reference
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import ShipmentRef
from test_track_trace_mapping import _settings


def shipment(**changes):
    return replace(ShipmentRef('task-cma','Synthetic CMA shipment','cma cgm',None,
                   'CMAU-123456-7, TGHU-765432-1','list-1',expected_container_count=2,
                   destination_port='Alexandria, Egipto',current_task_status='tránsito'),**changes)


def event(reference, code, classifier='ACT', port='MXATM', time='2026-09-30T07:39:00-06:00'):
    transport = code in {'ARRI','DEPA'}
    return {'eventType':'TRANSPORT' if transport else 'EQUIPMENT',
            'equipmentReference':reference, 'eventClassifierCode':classifier,
            'eventDateTime':time, 'eventCreatedDateTime':'2099-01-01T00:00:00Z',
            ('transportEventTypeCode' if transport else 'equipmentEventTypeCode'):code,
            'emptyIndicatorCode':'EMPTY' if code=='GTOT' else 'LADEN',
            'eventLocation':{'UNLocationCode':port},
            'transportCall':{'UNLocationCode':port,'modeOfTransport':'VESSEL',
                             'vessel':{'vesselName':'CMA FINAL'},'exportVoyageNumber':'001E'}}


def events(reference):
    return [event(reference,'GTOT',time='2026-09-18T14:12:00-06:00'),
            event(reference,'GTIN',time='2026-09-22T11:37:00-06:00'),
            event(reference,'LOAD'),event(reference,'DEPA',time='2026-09-30T15:57:23-06:00'),
            event(reference,'ARRI','PLN','NLRTM','2099-10-29T06:00:00+01:00'),
            event(reference,'ARRI','PLN','EGALY','2099-12-05T07:00:00+02:00')]


def adapter(monkeypatch, transform=lambda ref, values:values):
    a=CmaCgmAdapter()
    monkeypatch.setattr(a,'_api_mode_requested',lambda:True)
    called=[]
    def fetch(ship):
        called.append(ship.container_no)
        values=transform(ship.container_no,events(ship.container_no))
        return CmaCgmDcsaEventFetch({'events':values},tuple(values),'https://api.example.test/events',1)
    monkeypatch.setattr(a,'_fetch_dcsa_event_pages',fetch)
    return a,called


def test_normalizes_and_fetches_every_container_and_maps_fields(monkeypatch):
    a,called=adapter(monkeypatch)
    ship=shipment()
    status=a.fetch_status(ship)
    assert called==['CMAU1234567','TGHU7654321']
    assert status.discovered_containers==called
    assert status.eta_local_text=='2099-12-05T07:00:00+02:00'
    assert status.final_vessel_voyage=='CMA FINAL 001E'
    assert status.require_destination_evidence
    assert status.latest_move.event_state=='actual'
    assert status.latest_move.location_code=='MXATM'
    assert all(m.event_state=='estimated' for m in status.recent_moves if m.name.endswith('(ARRI)'))
    c=ClickUpClient(_settings(clickup_use_task_status=True))
    plan=c.plan_shipment_update(ship,status)
    updates={u.field_id:u.value for u in plan.custom_field_updates}
    assert updates['eta-field'].date().isoformat()=='2099-12-05'
    assert updates['etd-field'].date().isoformat()=='2026-09-30'
    assert 'disc-field' not in updates and 'gtin-empty-field' not in updates
    assert plan.task_status_update is None
    values=dict(ship.current_field_values)
    values.update(updates)
    again=c.plan_shipment_update(replace(ship,current_field_values=values),status)
    assert all(u.field_id=='last-checked-field' for u in again.custom_field_updates)


@pytest.mark.parametrize('classifier,expected',[('ACT','actual'),('EST','estimated'),('PLN','estimated'),('XXX','xxx')])
def test_classifier_and_port_are_independent_of_json_key_order(classifier,expected):
    raw=event('CMAU1234567','DEPA',classifier)
    move=_extract_moves({'events':[raw]})[0]
    assert move.event_state==expected
    assert move.location_code=='MXATM' and move.transport_mode=='VESSEL'
    assert move.event_time==datetime(2026,9,30,13,39,tzinfo=timezone.utc)
    raw['eventLocation']['UNLocationCode']='NLRTM'
    assert _extract_moves({'events':[raw]})[0].location_code=='AMBIGUOUS'


@pytest.mark.parametrize('value',['CMAU123','../../secret','CMAU1234567, INVALID'])
def test_invalid_container_does_not_fall_back_to_booking(value,monkeypatch):
    a,called=adapter(monkeypatch)
    with pytest.raises(ValueError):a.fetch_status(shipment(container_no=value))
    assert not called


def test_booking_reference_keeps_its_hyphens():
    assert _pick_reference(shipment(container_no=None,booking_no='BOOK-1'),'booking','container')==('BOOK-1','booking')


@pytest.mark.parametrize('problem',['missing','wrong_container','count'])
def test_partial_or_wrong_response_never_returns_a_shipment_status(monkeypatch,problem):
    def transform(ref,values):
        if ref=='TGHU7654321':
            if problem=='missing':return []
            if problem=='wrong_container':return events('OTHER1234567')
        return values
    a,called=adapter(monkeypatch,transform)
    with pytest.raises(ValueError):a.fetch_status(shipment(expected_container_count=3) if problem=='count' else shipment())
    assert called==([] if problem=='count' else ['CMAU1234567','TGHU7654321'])


def test_one_container_actual_does_not_complete_shipment_milestone(monkeypatch):
    def transform(ref,values):
        if ref=='TGHU7654321':
            for value in values:
                if value.get('transportEventTypeCode')=='DEPA':value['eventClassifierCode']='PLN'
        return values
    a,_=adapter(monkeypatch,transform)
    status=a.fetch_status(shipment())
    assert not any(m.name.endswith('(DEPA)') for m in status.recent_moves)
    assert any(m.name.endswith('(LOAD)') and m.event_state=='actual' for m in status.recent_moves)


@pytest.mark.parametrize('destination',[None,'unknown','HNPCR'])
def test_unmatched_destination_never_uses_transshipment_eta(monkeypatch,destination):
    a,_=adapter(monkeypatch)
    status=a.fetch_status(shipment(destination_port=destination))
    assert status.eta_time is None and status.final_vessel_voyage is None


def test_transshipment_discharge_is_not_destination_discharge(monkeypatch):
    a,_=adapter(monkeypatch,lambda ref,values:values+[event(ref,'DISC',port='NLRTM')])
    status=a.fetch_status(shipment())
    plan=ClickUpClient(_settings(clickup_use_task_status=True)).plan_shipment_update(shipment(),status)
    assert all(u.field_id!='disc-field' for u in plan.custom_field_updates)
    assert plan.task_status_update is None


def test_actual_pod_arrival_is_separate_from_transshipment_discharge(monkeypatch):
    a,_=adapter(monkeypatch,lambda ref,values:values+[
        event(ref,'ARRI',port='EGALY',time='2026-01-05T07:00:00+02:00'),
        event(ref,'DISC',port='NLRTM',time='2026-01-04T07:00:00+01:00')])
    status=a.fetch_status(shipment())
    assert status.eta_local_text=='2026-01-05T07:00:00+02:00'
    assert all(m.location_code for m in status.recent_moves)
    plan=ClickUpClient(_settings()).plan_shipment_update(shipment(),status)
    assert all(u.field_id!='disc-field' for u in plan.custom_field_updates)


def test_unverified_unit_cannot_complete_destination_discharge(monkeypatch):
    def transform(ref,values):
        return values+[event(ref,'DISC','ACT' if ref=='CMAU1234567' else 'PLN','EGALY')]
    a,_=adapter(monkeypatch,transform)
    status=a.fetch_status(shipment())
    assert not any(m.name.endswith('(DISC)') for m in status.recent_moves)
    plan=ClickUpClient(_settings()).plan_shipment_update(shipment(),status)
    assert all(u.field_id!='disc-field' for u in plan.custom_field_updates)


def booking_adapter(monkeypatch, transform=lambda values: values):
    a=CmaCgmAdapter()
    monkeypatch.setattr(a,'_api_mode_requested',lambda:True)
    called=[]
    def fetch(ship):
        called.append((ship.booking_no,ship.container_no))
        assert ship.container_no is None, 'must not query a reused container cycle'
        values=events('CMAU1234567')+events('TGHU7654321')
        for value in values:
            value['documentReferences']=[{'documentReferenceType':'BKG','documentReferenceValue':'BOOK-1'}]
        values=transform(values)
        return CmaCgmDcsaEventFetch({'events':values},tuple(values),'https://api.example.test/events',2)
    monkeypatch.setattr(a,'_fetch_dcsa_event_pages',fetch)
    return a,called


def test_booking_anchor_prevents_reused_container_journeys_and_is_idempotent(monkeypatch):
    a,called=booking_adapter(monkeypatch)
    ship=shipment(booking_no='BOOK-1')
    status=a.fetch_status(ship)
    assert called==[('BOOK-1',None)]
    assert status.discovered_containers==['CMAU1234567','TGHU7654321']
    assert status.final_vessel_voyage=='CMA FINAL 001E'
    assert status.source_url.endswith('/BOOK-1')
    assert status.require_destination_evidence
    client=ClickUpClient(_settings(clickup_use_task_status=True))
    plan=client.plan_shipment_update(ship,status)
    assert plan.task_status_update is None
    current=dict(ship.current_field_values)
    current.update({u.field_id:u.value for u in plan.custom_field_updates})
    repeat=client.plan_shipment_update(replace(ship,current_field_values=current),status)
    assert not repeat.task_status_update
    assert all(u.field_id=='last-checked-field' for u in repeat.custom_field_updates)


@pytest.mark.parametrize('problem',['empty','missing','extra','different_booking','mixed_opaque'])
def test_booking_scope_failures_never_fall_back_to_container_queries(monkeypatch,problem):
    def transform(values):
        if problem=='empty':return []
        if problem=='missing':return [v for v in values if v['equipmentReference']!='TGHU7654321']
        if problem=='extra':return values+[event('OTHER1234567','LOAD')]
        if problem=='different_booking':values[0]['documentReferences'][0]['documentReferenceValue']='OTHER-BOOKING'
        if problem=='mixed_opaque':
            for i,v in enumerate(values):v['documentReferences'][0]['documentReferenceValue']=('a' if i%2 else 'b')*64
        return values
    a,called=booking_adapter(monkeypatch,transform)
    with pytest.raises(ValueError,match='CMA booking'):
        a.fetch_status(shipment(booking_no='BOOK-1'))
    assert called==[('BOOK-1',None)]


def test_shared_transport_calls_require_consistent_booking_scope(monkeypatch):
    def transform(values):
        for v in values:
            v['documentReferences'][0]['documentReferenceValue']='a'*64
            if v['eventType']=='TRANSPORT':
                v.pop('equipmentReference')
                v['references']=[{'referenceType':'EQ','referenceValue':'CMAU1234567'},
                                 {'referenceType':'EQ','referenceValue':'TGHU7654321'}]
        return values
    a,_=booking_adapter(monkeypatch,transform)
    status=a.fetch_status(shipment(booking_no='BOOK-1'))
    assert status.final_vessel_voyage=='CMA FINAL 001E'
    assert status.eta_local_text=='2099-12-05T07:00:00+02:00'


def test_unscoped_transport_calls_cannot_invent_eta(monkeypatch):
    def transform(values):
        for v in values:
            if v['eventType']=='TRANSPORT':
                v.pop('equipmentReference');v.pop('documentReferences')
        return values
    a,_=booking_adapter(monkeypatch,transform)
    status=a.fetch_status(shipment(booking_no='BOOK-1'))
    assert status.eta_time is None
    assert not any(m.name.endswith('(ARRI)') for m in status.recent_moves)


def test_booking_anchor_preserves_actual_only_multi_unit_discharge(monkeypatch):
    def transform(values):
        for ref,cl in [('CMAU1234567','ACT'),('TGHU7654321','PLN')]:
            v=event(ref,'DISC',cl,'EGALY')
            v['documentReferences']=[{'documentReferenceType':'BKG','documentReferenceValue':'BOOK-1'}]
            values.append(v)
        return values
    a,_=booking_adapter(monkeypatch,transform)
    status=a.fetch_status(shipment(booking_no='BOOK-1'))
    assert not any(m.name.endswith('(DISC)') for m in status.recent_moves)


def test_booking_anchor_rejects_invalid_allocation_before_carrier_lookup(monkeypatch):
    a,called=booking_adapter(monkeypatch)
    with pytest.raises(ValueError):a.fetch_status(shipment(booking_no='BOOK-1',expected_container_count=3))
    assert called==[]
