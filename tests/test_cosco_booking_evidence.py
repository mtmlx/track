import copy,json
from datetime import datetime,timezone,timedelta
from unittest.mock import patch
import pytest
import requests
from shipment_sync.cosco_booking_evidence import capture,native_booking,PRODUCTION_ORIGIN,TENANT
from shipment_sync.carriers.cosco import CoscoAdapter
NOW=datetime(2026,11,17,6,0,tzinfo=timezone.utc)
def scope():
 return {'active':True,'query_held':False,'binding':{'tenant':TENANT,'task_id':'86e3bvrv7','customer_crm_task_id':'86dwkvu5b','workspace_id':'8451352','list_id':'901704769513','carrier':'COSCO','booking_reference':'6474704250','assigned_containers':[],'destination_port_label':'PUERTO QUETZAL','destination_port_codes':['GTPRQ'],'coverage_kind':'BOOKING_ITINERARY'},'native_scope_verified_at':(NOW-timedelta(minutes=1)).isoformat(),'expires_at':(NOW+timedelta(minutes=30)).isoformat(),'native_source_sha256':'a'*64,'authority_ref':'reviewed-read-only-proof'}
def payload():
 return {'code':0,'data':{'type':'bkg','numberType':'both','content':{'trackingPath':{'billOfladingRefCode':'6474704250','pod':'PUERTO QUETZAL'},'actualShipment':[{'sequenceNumber':1,'vesselName':'NATIVE SHIP','voyageNo':'091E','portOfLoading':'CHONGQING','portOfDischarge':'SHANGHAI','expectedDateOfDeparture':'2026-11-19 09:00:00'}],'containerStatus':[]}}}
def adapter(data=None,error=None):
 with patch.dict('os.environ',{'COSCO_MODE':'cop','COSCO_COP_BASE_URL':PRODUCTION_ORIGIN,'COSCO_COP_API_KEY':'synthetic','COSCO_COP_SECRET_KEY':'synthetic'},clear=True):a=CoscoAdapter()
 calls=[]
 def get(url,**kwargs):
  calls.append((url,kwargs))
  if error:raise error
  r=requests.Response();r.status_code=200;r.url=url;r._content=json.dumps(payload()if data is None else data).encode();r._content_consumed=True;r.headers['Content-Type']='application/json';return r
 a.session.get=get
 return a,calls
def test_exact_read_once_no_status_engine_or_write():
 a,calls=adapter();a.fetch_status=lambda *_:pytest.fail('normal adapter status not used')
 r=capture(scope(),adapter_factory=lambda:a,now=NOW)
 assert r['state']=='NATIVE_BOOKING_ITINERARY_OBSERVED' and len(calls)==1
 assert calls[0][0]==PRODUCTION_ORIGIN+'/service/info/tracking/6474704250?numberType=bkg'
 assert calls[0][1]['allow_redirects']is False and a.max_retries==0
 assert r['source_field_writes']==r['archive_writes']==0
 assert r['observation']['native_itinerary_rows'][0]['fields']['vesselName']['json_pointer']=='/data/content/actualShipment/0/vesselName'
 assert r['observation']['not_a_dcsa_event']is True and r['observation']['atlas_document_approval']is False
 assert 'synthetic'not in json.dumps(r)
def test_unknown_network_result_does_not_replay():
 a,calls=adapter(error=requests.Timeout('synthetic sensitive provider body'))
 r=capture(scope(),adapter_factory=lambda:a,now=NOW)
 assert len(calls)==1 and r['state']=='NATIVE_READ_GAP' and r['retry_allowed']is False
 assert 'sensitive'not in json.dumps(r)
def test_missing_credentials_zero_native_requests():
 a,calls=adapter();a.cop_api_key='';r=capture(scope(),adapter_factory=lambda:a,now=NOW)
 assert r['state']=='ADAPTER_CREDENTIALS_MISSING' and calls==[]
@pytest.mark.parametrize('change',[{'query_held':True},{'active':False},{'expires_at':NOW.isoformat()},{'native_scope_verified_at':(NOW-timedelta(hours=2)).isoformat()}])
def test_held_or_stale_never_constructs_adapter(change):
 s=scope();s.update(change)
 with pytest.raises(ValueError):capture(s,adapter_factory=lambda:pytest.fail('must not construct adapter'),now=NOW)
def test_foreign_task_never_constructs_adapter():
 s=scope();s['binding']['task_id']='86-held-foreign'
 with pytest.raises(ValueError):capture(s,adapter_factory=lambda:pytest.fail('must not construct adapter'),now=NOW)
def test_uat_or_auto_forbidden_before_provider_call():
 for field,value in [('cop_base_url','https://api-pp.lines.coscoshipping.com'),('mode','auto'),('cop_booking_number_type','bl')]:
  a,calls=adapter();setattr(a,field,value)
  with pytest.raises(ValueError):capture(scope(),adapter_factory=lambda:a,now=NOW)
  assert calls==[]
def test_wrong_native_booking_cannot_clear():
 p=payload();p['data']['content']['trackingPath']['billOfladingRefCode']='different';a,calls=adapter(p)
 r=capture(scope(),adapter_factory=lambda:a,now=NOW)
 assert len(calls)==1 and r['state']=='NATIVE_READ_GAP' and 'observation'not in r
 assert r['native_response']['http_status']==200 and r['retry_allowed']is False
def test_missing_native_itinerary_retains_gap():
 p=payload();p['data']['content']['actualShipment']=[];r=native_booking(json.dumps(p).encode(),scope()['binding'])
 assert r['state']=='BOOKING_IDENTIFIED_INCOMPLETE_EVIDENCE' and not r['container_movements_completed']
def test_container_allocation_or_destination_conflict_retains_gap():
 s=scope();s['binding']['assigned_containers']=['ABCD1234567'];p=payload();p['data']['content']['trackingPath']['pod']='OTHER PORT'
 r=native_booking(json.dumps(p).encode(),s['binding'])
 assert r['state']=='BOOKING_IDENTIFIED_INCOMPLETE_EVIDENCE' and not r['container_binding_verified'] and not r['destination_verified']
def test_native_dates_never_gain_timezone_or_actual_event():
 r=native_booking(json.dumps(payload()).encode(),scope()['binding']);value=r['native_itinerary_rows'][0]['fields']['expectedDateOfDeparture']
 assert value['value']=='2026-11-19 09:00:00' and not r['date_timezone_inferred'] and not r['container_movements_completed']
 assert 'events'not in r and 'departure_at'not in r
