from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib,json
import pytest
from test_movement_archive import S3
from shipment_sync.models import ShipmentRef,ShipmentStatus
from shipment_sync.movement_archive import encoded,native_batch
from shipment_sync.booking_itinerary_archive import BookingItineraryArchivePublisher
from shipment_sync.carriers.wan_hai import _booking_html_capture,WanHaiAdapter

NOW=datetime.now(timezone.utc).isoformat()
CRM_FIELD='5e1637c2-6bb2-4428-a592-06477fd13ef1'
RESULT='<table class="tbl-list"><tr><th>No</th><th>Booking</th></tr><tr><td>1</td><td>BOOK1</td><td>2026/11/16</td><td>101E</td><td>TEST SHIP</td><td>Booking Data</td></tr></table>'
DETAIL='''<h1>Tracking Information Content [By Book No.]</h1><table>
<tr><td>訂艙號碼</td><td>BOOK1</td><td>船名</td><td>TEST SHIP</td><td>航次</td><td>101E</td></tr>
<tr><td>Place of Receipt</td><td>NINGBO (CN)</td><td>Port of Loading</td><td>NINGBO (CN)</td></tr>
<tr><td>Port of Discharging</td><td>PUERTO QUETZAL (GT)</td><td>Estimated Departure Date</td><td>2026/11/25</td></tr>
</table>'''
URL='https://vip.wanhai.com/views/cargo_track_v2/tracking_data_list.xhtml'
DETAIL_URL='https://vip.wanhai.com/views/cargo_track_v2/booking_data.xhtml'
def capture():return _booking_html_capture('BOOK1',RESULT,URL,DETAIL,DETAIL_URL,{URL:200,DETAIL_URL:200})

@pytest.fixture
def fixture():
 binding={'tenant':'synthetic','workspace_id':'1','customer_crm_task_id':'customer','list_id':'list','task_id':'shipment',
          'carrier':'WAN HAI','booking_reference':'BOOK1','assigned_containers':[], 'destination_port_codes':['GTPRQ'],
          'destination_port_label':'Puerto Quetzal','coverage_kind':'BOOKING_ITINERARY'}
 catalog={'schema_version':1,'reader_id':'tracktrace-booking-itinerary-v1','reader_release_sha256':'a'*64,
          'crm_field_id':CRM_FIELD,
          'binding':{k:binding[k]for k in ('tenant','workspace_id','customer_crm_task_id')},'generated_at':NOW,
          'catalog_revision':'synthetic-1','query_holds':[],
          'tasks':{'shipment':{'binding':binding,'destination_port_label':'Puerto Quetzal'}}}
 store=S3(catalog);pub=BookingItineraryArchivePublisher(region='region',bucket='bucket',prefix='feed',catalog_key='feed/catalog.json',owner='123456789012',release='a'*64,s3=store)
 shipment=ShipmentRef('shipment','Synthetic','wan hai','BOOK1',None,'list',expected_container_count=3,destination_port='Puerto Quetzal')
 shipment.current_field_values={CRM_FIELD:[{'id':'customer','team_id':'1','deleted':False,'access':True}]}
 status=ShipmentStatus('Original status',booking_itinerary_evidence=[capture()])
 return pub,store,shipment,status

def test_disabled_no_client(monkeypatch):
 monkeypatch.delenv('BOOKING_ITINERARY_ARCHIVE_ENABLED',raising=False)
 assert BookingItineraryArchivePublisher.from_env()is None

def test_html_archive_keeps_exact_bytes_and_no_fabricated_events(fixture):
 pub,s3,shipment,status=fixture;before=deepcopy(status)
 result=pub.publish(shipment,status);assert result['state']=='ARCHIVED'
 manifest=json.loads(s3.objects[result['manifest_ref']['key']]);assert manifest['events']==[]
 assert not manifest['all_containers_observed']and manifest['covered_containers']==[]
 assert manifest['native_documents'][1]['html']==DETAIL
 assert manifest['native_documents'][1]['sha256']==hashlib.sha256(DETAIL.encode()).hexdigest()
 assert status==before and result['source_field_writes']==0
 assert pub.publish(shipment,status)['state']=='ALREADY_ARCHIVED'

def test_hold_rechecked_before_publication(fixture):
 pub,s3,shipment,status=fixture;assert pub.registered(shipment)
 catalog=json.loads(s3.objects['feed/catalog.json']);catalog['query_holds']=['shipment'];s3.objects['feed/catalog.json']=encoded(catalog)
 assert pub.publish(shipment,status)['state']=='NOT_REGISTERED'
 assert not any(k=='PUT'for k,_ in s3.calls)

@pytest.mark.parametrize('crm',[None,[],[{'id':'foreign'}],[{'id':'customer'},{'id':'foreign'}],
 [{'id':'customer','deleted':True}],[{'id':'customer','team_id':'different-workspace'}]])
def test_current_crm_is_required_before_capture_and_publish(fixture,crm):
 pub,s3,shipment,status=fixture;shipment.current_field_values={CRM_FIELD:crm,'other-field':[{'id':'customer'}]}
 with pytest.raises(ValueError,match='CRM'):pub.registered(shipment)
 with pytest.raises(ValueError,match='CRM'):pub.publish(shipment,status)
 assert not any(k=='PUT'for k,_ in s3.calls)

def test_catalog_must_explicitly_bind_native_crm_field(fixture):
 pub,s3,shipment,status=fixture;catalog=json.loads(s3.objects['feed/catalog.json']);catalog.pop('crm_field_id');s3.objects['feed/catalog.json']=encoded(catalog)
 with pytest.raises(ValueError,match='catalog'):pub.registered(shipment)
 assert not any(k=='PUT'for k,_ in s3.calls)

def test_current_customer_change_after_registration_prevents_archive(fixture):
 pub,s3,shipment,status=fixture;assert pub.registered(shipment)
 shipment.current_field_values[CRM_FIELD]=[{'id':'foreign'}]
 with pytest.raises(ValueError,match='CRM'):pub.publish(shipment,status)
 assert not any(k=='PUT'for k,_ in s3.calls)

@pytest.mark.parametrize('crm',[None,[],[{'id':'foreign'}]])
def test_sync_does_not_capture_or_publish_booking_evidence_for_wrong_customer(fixture,monkeypatch,crm):
 from shipment_sync.sync import _run_sync
 from shipment_sync.clickup_client import ClickUpClient
 from test_sync_runtime_guards import _settings
 pub,s3,shipment,status=fixture;shipment.current_field_values={CRM_FIELD:crm}
 client=ClickUpClient(_settings(shipment_preflight_enabled=False));normal_calls=[];capture_flags=[]
 monkeypatch.setattr('shipment_sync.sync.build_carrier_registry',lambda:{'wan hai':object()})
 monkeypatch.setattr('shipment_sync.sync.BookingItineraryArchivePublisher.from_env',lambda:pub)
 monkeypatch.setattr('shipment_sync.sync.MovementArchivePublisher.from_env',lambda:None)
 def ordinary_fetch(adapter,current):
  normal_calls.append(current.task_id);capture_flags.append(current.capture_booking_itinerary);return status
 monkeypatch.setattr('shipment_sync.sync._fetch_carrier_status',ordinary_fetch)
 monkeypatch.setattr(client,'update_shipment_status',lambda *_:SimpleNamespace(changed=False,status_value='unchanged'))
 monkeypatch.setattr(pub,'publish',lambda *_:pytest.fail('Foreign/missing customer must never publish'))
 stats=_run_sync(client,shipments=[shipment])
 # Existing normal tracking remains unchanged; this opt-in lane causes no
 # capture, extra provider request or archive write for a mismatched customer.
 assert normal_calls==['shipment'] and capture_flags==[False] and stats.unchanged==1
 assert not any(k=='PUT'for k,_ in s3.calls)

@pytest.mark.parametrize('change',[lambda s:setattr(s,'booking_no','FOREIGN'),lambda s:setattr(s,'shipping_line','cma cgm'),lambda s:setattr(s,'destination_port','Other'),lambda s:setattr(s,'container_no','ABCD1234567')])
def test_current_scope_change_rejects(fixture,change):
 pub,s3,shipment,status=fixture;change(shipment)
 with pytest.raises(ValueError):pub.publish(shipment,status)
 assert not any(k=='PUT'for k,_ in s3.calls)

@pytest.mark.parametrize('mutation',[
 lambda b:b['native_documents'][0].update(navigation_status=302),
 lambda b:b['native_documents'][1].update(html=DETAIL+'tampered'),
 lambda b:b['native_documents'][1].update(url='https://foreign.test/detail'),
 lambda b:b.update(request_booking='FOREIGN'),
 lambda b:b.update(complete=False),
])
def test_unverified_html_never_publishes(fixture,mutation):
 pub,s3,shipment,status=fixture;mutation(status.booking_itinerary_evidence[0])
 with pytest.raises(ValueError):pub.publish(shipment,status)
 assert not any(k=='PUT'for k,_ in s3.calls)

def test_exact_assigned_population_supports_document_qa_only(fixture):
 pub,s3,shipment,status=fixture;shipment.container_no='ABCD1234567';shipment.expected_container_count=1
 catalog=json.loads(s3.objects['feed/catalog.json']);catalog['tasks']['shipment']['binding']['assigned_containers']=['ABCD1234567'];s3.objects['feed/catalog.json']=encoded(catalog)
 doc=status.booking_itinerary_evidence[0]['native_documents'][1]
 doc['html']+='<table><tr><th>Ctnr No.</th><th>Seal No.</th></tr><tr><td>ABCD1234567</td><td>S1</td></tr></table>';doc['sha256']=hashlib.sha256(doc['html'].encode()).hexdigest()
 assert pub.publish(shipment,status)['state']=='ARCHIVED'
 doc['html']=doc['html'].replace('ABCD1234567','EFGH7654321');doc['sha256']=hashlib.sha256(doc['html'].encode()).hexdigest()
 with pytest.raises(ValueError):pub.publish(shipment,status)

@pytest.mark.parametrize('receipts',[{}, {URL:200,DETAIL_URL:401}])
def test_navigation_status_without_proven_pages_never_captures(receipts):
 with pytest.raises(ValueError):_booking_html_capture('BOOK1',RESULT,URL,DETAIL,DETAIL_URL,receipts)

def test_response_body_must_match_exact_requested_booking():
 with pytest.raises(ValueError):_booking_html_capture('BOOK2',RESULT,URL,DETAIL,DETAIL_URL,{URL:200,DETAIL_URL:200})

def test_flag_is_only_given_to_current_structured_booking(monkeypatch):
 adapter=object.__new__(WanHaiAdapter);adapter.max_retries=0;adapter.retry_delay_seconds=0
 calls=[]
 def request(**kw):calls.append(kw);return ShipmentStatus('Unchanged')
 adapter._playwright_request=request
 shipment=ShipmentRef('task','Synthetic','wan hai','BOOK1',None,'list')
 adapter._fetch_status_playwright(shipment);assert calls[-1]=={'reference':'BOOK1','cargo_type':'2'}
 shipment.capture_booking_itinerary=True;adapter._fetch_status_playwright(shipment)
 assert calls[-1]['capture_booking_itinerary']is True

def test_access_denied_is_not_absence_or_permission_to_publish(fixture):
 from test_movement_archive import S3Error
 pub,s3,shipment,status=fixture;original=s3.get_object
 def denied(**kw):
  if kw['Key'].endswith('/latest.json'):raise S3Error('AccessDenied')
  return original(**kw)
 s3.get_object=denied
 with pytest.raises(S3Error):pub.publish(shipment,status)
 assert not any(k=='PUT'for k,_ in s3.calls)

def test_cma_registered_booking_uses_same_native_response_for_status_and_archive():
 from shipment_sync.carriers.cma_cgm import CmaCgmAdapter
 adapter=object.__new__(CmaCgmAdapter);adapter._api_mode_requested=lambda:True
 adapter.url_template='https://www.cma-cgm.com/{reference}';adapter.booking_type_code='booking'
 status=ShipmentStatus('Original');calls=[]
 raw={'eventType':'TRANSPORT','documentReferences':[{'documentReferenceType':'BKG','documentReferenceValue':'BOOK1'}]}
 first={'events':[raw]}
 adapter._fetch_dcsa_event_pages=lambda shipment:calls.append(shipment.booking_no)or SimpleNamespace(events=[raw],first_page_payload=first,source_url='https://apis.cma-cgm.net/events')
 def parsed(**kw):assert kw['payload']is first;return status
 adapter._status_from_payload=parsed
 shipment=ShipmentRef('task','Synthetic','cma cgm','BOOK1',None,'list',capture_booking_itinerary=True)
 result=adapter.fetch_status(shipment)
 assert result is status and calls==['BOOK1'];assert result.movement_evidence[0]['events']==[raw]
 assert result.status_text=='Original'
