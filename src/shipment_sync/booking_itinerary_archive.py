"""Separate booking-itinerary archive from already fetched official events.

No carrier request, ClickUp mutation, container completion or inferred ETD.
An explicit customer catalog enables the reader in the existing T&T worker.
"""
from copy import deepcopy
from datetime import datetime,timezone
import hashlib,json,os,re
from urllib.parse import urlsplit
from shipment_sync.movement_archive import MovementArchivePublisher,NATIVE_HOSTS,carrier,encoded,digest,instant,_error_code,MAX_BYTES
from shipment_sync.one_write_holds import write_hold_reason
from shipment_sync.booking_itinerary_html import verify as verify_html, FORMAT as HTML_FORMAT

READER_ID='tracktrace-booking-itinerary-v1'
SCOPE='BOOKING_ITINERARY'


class BookingItineraryArchivePublisher(MovementArchivePublisher):
    @classmethod
    def from_env(cls):
        if os.getenv('BOOKING_ITINERARY_ARCHIVE_ENABLED','').lower()!='true':return None
        return cls(region=os.getenv('BOOKING_ITINERARY_ARCHIVE_REGION',''),bucket=os.getenv('BOOKING_ITINERARY_ARCHIVE_BUCKET',''),
            prefix=os.getenv('BOOKING_ITINERARY_ARCHIVE_PREFIX',''),catalog_key=os.getenv('BOOKING_ITINERARY_ARCHIVE_CATALOG_KEY',''),
            owner=os.getenv('BOOKING_ITINERARY_ARCHIVE_BUCKET_OWNER',''),release=os.getenv('BOOKING_ITINERARY_ARCHIVE_RELEASE_SHA256',''))

    def catalog(self):
        # Re-read before each operation; an operator's newly published hold
        # cannot be bypassed by the process's older in-memory registration.
        value,meta=self._get(self.catalog_key)
        if (value.get('schema_version')!=1 or value.get('reader_id')!=READER_ID or
                value.get('reader_release_sha256')!=self.release or not isinstance(value.get('tasks'),dict) or
                not isinstance(value.get('binding'),dict)or not all(value['binding'].get(k)for k in ('tenant','workspace_id','customer_crm_task_id'))or
                not isinstance(value.get('crm_field_id'),str)or not re.fullmatch(r'[a-f0-9-]{36}',value['crm_field_id'])or
                not isinstance(value.get('catalog_revision'),str)or not isinstance(value.get('query_holds',[]),list)or
                meta.get('VersionId')in (None,'','null')):raise ValueError('Booking itinerary catalog not pinned')
        if instant(value['generated_at'])>datetime.now(timezone.utc):raise ValueError('Future booking itinerary registration')
        return value

    def _registration(self,shipment):
        if write_hold_reason(shipment):return None
        catalog=self.catalog()
        if shipment.task_id in catalog.get('query_holds',[]):return None
        entry=catalog['tasks'].get(shipment.task_id)
        if entry is None or entry.get('active')is False or entry.get('query_held')is True:return None
        binding=entry.get('binding')or{}
        # The catalog is authority to observe this customer, not proof that a
        # task still belongs to them. Use the already-read native relation;
        # names, a matching list or the previous catalog never substitute.
        current_crm=shipment.current_field_values.get(catalog['crm_field_id'])
        if (not isinstance(current_crm,list)or len(current_crm)!=1 or not isinstance(current_crm[0],dict)or
                current_crm[0].get('id')!=catalog['binding']['customer_crm_task_id']or
                current_crm[0].get('deleted')is True or current_crm[0].get('access')is False or
                (current_crm[0].get('team_id')is not None and str(current_crm[0]['team_id'])!=str(catalog['binding']['workspace_id']))):
            raise ValueError('Current native CRM customer relationship changed or missing')
        if (binding.get('coverage_kind')!=SCOPE or binding.get('task_id')!=shipment.task_id or
                str(binding.get('list_id'))!=str(shipment.list_id)or any(binding.get(k)!=v for k,v in catalog['binding'].items())or
                carrier(binding.get('carrier'))!=carrier(shipment.shipping_line)or not shipment.booking_no or
                binding.get('booking_reference')!=shipment.booking_no or entry.get('destination_port_label')!=shipment.destination_port or
                not isinstance(binding.get('destination_port_codes'),list)or not binding['destination_port_codes']):raise ValueError('Current booking itinerary binding changed')
        raw_units=(shipment.container_no or '').strip().upper()
        units=[x for x in re.split(r'[,;\s]+',raw_units)if x]
        if (any(not re.fullmatch(r'[A-Z]{4}\d{7}',x)for x in units)or len(units)!=len(set(units))or
                binding.get('assigned_containers')!=sorted(units)or
                (units and shipment.expected_container_count not in (None,len(units)))):raise ValueError('Current booking container allocation changed')
        return deepcopy(binding),catalog['catalog_revision']

    def registered(self,shipment):
        return self._registration(shipment)is not None

    def publish(self,shipment,status):
        registered=self._registration(shipment)
        if not registered:return {'state':'NOT_REGISTERED','task_id':shipment.task_id}
        if getattr(status,'booking_itinerary_evidence',[]):
            return self._publish_html(shipment,status,registered)
        binding,revision=registered;batches=getattr(status,'movement_evidence',[]);records=[];times=[];identities={}
        if not batches:return {'state':'NATIVE_BOOKING_INPUT_UNAVAILABLE','task_id':shipment.task_id}
        for batch in batches:
            url=urlsplit(batch.get('source_url',''));observed=instant(batch['observed_at'])
            if (batch.get('provider_native')is not True or batch.get('complete')is not True or
                    batch.get('request_booking')!=shipment.booking_no or batch.get('request_container')not in (None,'')or
                    url.scheme!='https'or url.hostname not in NATIVE_HOSTS.get(carrier(shipment.shipping_line),set())or
                    url.port not in (None,443)or url.username or url.password or observed>datetime.now(timezone.utc)):
                return {'state':'NATIVE_BOOKING_INPUT_UNVERIFIED','task_id':shipment.task_id}
            times.append(observed)
            for raw in batch.get('events',[]):
                if not isinstance(raw,dict):raise ValueError('Native booking raw event is not an object')
                # This lane retains transport only. Native equipment records
                # are never copied into a shipment/container-completion proof.
                if raw.get('eventType')!='TRANSPORT'or raw.get('transportEventTypeCode')not in {'DEPA','ARRI'}:continue
                bookings={r.get('documentReferenceValue')for r in raw.get('documentReferences',[])if r.get('documentReferenceType')=='BKG'}
                if bookings!={shipment.booking_no}:raise ValueError('Foreign or absent native booking identity')
                eid=raw.get('eventID')
                if not isinstance(eid,str)or not eid:raise ValueError('Native transport event identity is missing')
                if eid in identities and identities[eid]!=raw:raise ValueError('Conflicting native booking event revisions')
                if eid in identities:continue
                identities[eid]=deepcopy(raw)
                records.append({'raw':deepcopy(raw),'raw_event_sha256':digest(raw),'observed_at':observed.isoformat(),
                    'source_url':batch['source_url'],'request_booking':shipment.booking_no,'request_container':None})
        if not records:return {'state':'NATIVE_BOOKING_ITINERARY_UNAVAILABLE','task_id':shipment.task_id}
        if len(records)>5000:raise ValueError('Native booking event bound exceeded')
        manifest={'schema_version':1,'reader_id':READER_ID,'reader_release_sha256':self.release,'coverage_kind':SCOPE,
            'binding':binding,'catalog_revision':revision,'observed_at':max(times).isoformat(),'provider_native':True,'complete':True,
            'events':records,'covered_containers':[],'all_containers_observed':False,
            'publisher_origin':'EXISTING_SCHEDULED_PROVIDER_RESPONSE'}
        # A hold, scope change or booking revision between capture and publish
        # invalidates publication rather than authorizing a stale source.
        if self._registration(shipment)!=(binding,revision):raise ValueError('Booking authority changed before publication')
        return self._write_manifest(shipment,manifest)

    def _publish_html(self,shipment,status,registered):
        binding,revision=registered;batches=status.booking_itinerary_evidence
        if len(batches)!=1:raise ValueError('One exact current native booking capture required')
        batch=batches[0];observed=instant(batch['observed_at'])
        if (carrier(shipment.shipping_line)!='wan hai'or batch.get('native_format')!=HTML_FORMAT or
                batch.get('request_booking')!=shipment.booking_no or batch.get('request_container')not in (None,'')or
                batch.get('provider_native')is not True or batch.get('complete')is not True or
                observed>datetime.now(timezone.utc)):raise ValueError('Native booking capture identity changed')
        manifest={'schema_version':1,'reader_id':READER_ID,'reader_release_sha256':self.release,'coverage_kind':SCOPE,
            'binding':binding,'catalog_revision':revision,'observed_at':observed.isoformat(),'provider_native':True,'complete':True,
            'events':[],'covered_containers':[],'all_containers_observed':False,'native_format':HTML_FORMAT,
            'native_documents':deepcopy(batch['native_documents']),'publisher_origin':'EXISTING_SCHEDULED_PROVIDER_RESPONSE'}
        verify_html(manifest,binding,{'native_hosts':{binding['carrier']:['www.wanhai.com','vip.wanhai.com']}})
        if self._registration(shipment)!=(binding,revision):raise ValueError('Booking authority changed before publication')
        return self._write_manifest(shipment,manifest)

    def _write_manifest(self,shipment,manifest):
        body=encoded(manifest)
        if len(body)>MAX_BYTES:raise ValueError('Booking itinerary archive bound exceeded')
        sha=hashlib.sha256(body).hexdigest();root=self.prefix+'/'+shipment.task_id+'/booking-itinerary/';latest=root+'latest.json'
        current,meta=None,None
        try:current,meta=self._get(latest)
        except Exception as error:
            if _error_code(error)!='NoSuchKey':raise
        if current:
            before=instant(current['observed_at']);observed=instant(manifest['observed_at'])
            if before>observed:return {'state':'OLDER_SOURCE_PRESERVED','task_id':shipment.task_id}
            if before==observed:return {'state':'ALREADY_ARCHIVED'if current.get('manifest_ref',{}).get('sha256')==sha else'SOURCE_CONFLICT','task_id':shipment.task_id}
        key=root+'manifests/'+sha+'.json'
        try:written=self.s3.put_object(Bucket=self.bucket,Key=key,Body=body,ContentType='application/json',ServerSideEncryption='AES256',ExpectedBucketOwner=self.owner,IfNoneMatch='*')
        except Exception as error:
            if _error_code(error)!='PreconditionFailed':raise
            existing,written=self._get(key)
            if encoded(existing)!=body:raise ValueError('Immutable booking archive collision')
        version=written.get('VersionId')
        if version in (None,'','null'):raise ValueError('Versioned booking archive required')
        response=self.s3.get_object(Bucket=self.bucket,Key=key,VersionId=version,ExpectedBucketOwner=self.owner)
        if response.get('VersionId')!=version or response['Body'].read(MAX_BYTES+1)!=body:raise ValueError('Booking manifest native readback changed')
        ref={'kind':'S3','region':self.region,'bucket':self.bucket,'key':key,'version_id':version,'sha256':sha}
        pointer={'binding':manifest['binding'],'observed_at':manifest['observed_at'],'manifest_ref':ref}
        args={'Bucket':self.bucket,'Key':latest,'Body':encoded(pointer),'ContentType':'application/json',
            'ServerSideEncryption':'AES256','ExpectedBucketOwner':self.owner}
        if meta:
            if not meta.get('ETag'):raise ValueError('Booking pointer CAS unavailable')
            args['IfMatch']=meta['ETag']
        else:args['IfNoneMatch']='*'
        self.s3.put_object(**args)
        saved,_=self._get(latest)
        if saved!=pointer:raise ValueError('Booking pointer readback changed')
        return {'state':'ARCHIVED','task_id':shipment.task_id,'coverage_kind':SCOPE,'manifest_ref':ref,'event_count':len(manifest['events']),
            'all_containers_observed':False,'source_field_writes':0}
