"""Archive existing native carrier responses for a registered read-only consumer.

No carrier calls or ClickUp writes exist here. The normal T&T worker remains the
only polling owner; unsupported browser/legacy summaries are explicit gaps.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from urllib.parse import urlsplit, urlunsplit

READER_ID = 'tracktrace-s3-v1'
MAX_BYTES = 16 * 1024 * 1024
NATIVE_HOSTS = {'one': {'apix.one-line.com'}, 'maersk': {'api.maersk.com'},
                'cma cgm': {'apis.cma-cgm.net', 'api.cma-cgm.com', 'api.cma-cgm.net'}}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError('Timezone-aware source observation required')
    return result.astimezone(timezone.utc)


def carrier(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value).lower()).strip()


def native_batch(events, source_url, *, request_booking=None, request_container=None, complete=True, observed_at=None):
    """Copy already fetched native events; never reconstruct DCSA from prose."""
    parsed = urlsplit(source_url)
    clean_url = urlunsplit((parsed.scheme, parsed.hostname or '', parsed.path, '', ''))
    return {'events': deepcopy(events), 'source_url': clean_url, 'provider_native': True,
            'request_booking': request_booking, 'request_container': request_container,
            'complete': bool(complete),
            'observed_at': instant(observed_at or datetime.now(timezone.utc)).isoformat()}


def _error_code(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code')


class MovementArchivePublisher:
    def __init__(self, *, region, bucket, prefix, catalog_key, owner, release, s3=None):
        if (not re.fullmatch(r'[0-9]{12}', owner or '')
                or not re.fullmatch(r'[a-f0-9]{64}', release or '')
                or not all(isinstance(x, str) and x.strip() for x in (region, bucket, prefix, catalog_key))
                or prefix.startswith('/') or prefix.endswith('/') or '..' in prefix.split('/')
                or not catalog_key.startswith(prefix + '/')):
            raise ValueError('Explicit archive registration required')
        self.region, self.bucket, self.prefix = region, bucket, prefix
        self.catalog_key, self.owner, self.release = catalog_key, owner, release
        self._s3, self._catalog = s3, None

    @classmethod
    def from_env(cls):
        if os.getenv('MOVEMENT_ARCHIVE_ENABLED', '').lower() != 'true':
            return None
        return cls(region=os.getenv('MOVEMENT_ARCHIVE_REGION', ''), bucket=os.getenv('MOVEMENT_ARCHIVE_BUCKET', ''),
                   prefix=os.getenv('MOVEMENT_ARCHIVE_PREFIX', ''), catalog_key=os.getenv('MOVEMENT_ARCHIVE_CATALOG_KEY', ''),
                   owner=os.getenv('MOVEMENT_ARCHIVE_BUCKET_OWNER', ''), release=os.getenv('MOVEMENT_ARCHIVE_RELEASE_SHA256', ''))

    @property
    def s3(self):
        if self._s3 is None:
            import boto3
            from botocore.config import Config
            self._s3 = boto3.client('s3', region_name=self.region,
                                    config=Config(connect_timeout=3, read_timeout=10, retries={'total_max_attempts': 1}))
        return self._s3

    def _get(self, key):
        response = self.s3.get_object(Bucket=self.bucket, Key=key, ExpectedBucketOwner=self.owner)
        stream = response['Body']
        try:
            raw = stream.read(MAX_BYTES + 1)
        finally:
            if hasattr(stream, 'close'):
                stream.close()
        if len(raw) > MAX_BYTES:
            raise ValueError('Archive read size exceeded')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Archive object required')
        return value, response

    def catalog(self):
        if self._catalog is None:
            value, meta = self._get(self.catalog_key)
            if (value.get('schema_version') != 1 or value.get('reader_id') != READER_ID
                    or value.get('reader_release_sha256') != self.release
                    or not isinstance(value.get('tasks'), dict)
                    or not isinstance(value.get('binding'), dict)
                    or not all(value['binding'].get(k) for k in ('tenant', 'workspace_id', 'customer_crm_task_id'))
                    or not isinstance(value.get('catalog_revision'), str)
                    or not meta.get('VersionId') or meta['VersionId'] == 'null'):
                raise ValueError('Customer catalog verification failed')
            instant(value['generated_at'])
            self._catalog = value
        return self._catalog

    def _binding(self, shipment, status):
        catalog = self.catalog()
        entry = catalog['tasks'].get(shipment.task_id)
        if entry is None:
            return None
        binding = entry.get('binding') if isinstance(entry, dict) else None
        if (not isinstance(binding, dict) or binding.get('task_id') != shipment.task_id
                or str(binding.get('list_id')) != str(shipment.list_id)
                or any(binding.get(k) != v for k, v in catalog['binding'].items())
                or carrier(binding.get('carrier')) != carrier(shipment.shipping_line)
                or binding.get('booking_reference') != shipment.booking_no
                or entry.get('destination_port_label') != shipment.destination_port):
            raise ValueError('Current shipment catalog binding mismatch')
        mapping = binding.get('container_bookings')
        units = set(re.findall(r'\b[A-Z]{4}[0-9]{7}\b', (shipment.container_no or '').upper()))
        if (not isinstance(mapping, dict) or not mapping or units != set(mapping)
                or shipment.expected_container_count not in (None, len(units))):
            raise ValueError('Current container population changed')
        if len(set(mapping.values())) == 1:
            if {shipment.booking_no} != set(mapping.values()):
                raise ValueError('Current booking changed')
        elif status.container_booking_map != mapping:
            raise ValueError('Current split allocation changed')
        return deepcopy(binding)

    def publish(self, shipment, status, *, publisher_origin='EXISTING_SCHEDULED_PROVIDER_RESPONSE'):
        binding = self._binding(shipment, status)
        if binding is None:
            return {'state': 'NOT_REGISTERED'}
        batches = getattr(status, 'movement_evidence', [])
        records = []
        native = bool(batches)
        complete = bool(batches)
        observations = []
        covered_units = set()
        for batch in batches:
            observed = instant(batch['observed_at'])
            observations.append(observed)
            source_url = batch.get('source_url', '')
            parsed = urlsplit(source_url)
            host_ok = (parsed.scheme == 'https' and parsed.port in (None, 443) and not parsed.username and not parsed.password
                       and parsed.hostname in NATIVE_HOSTS.get(carrier(binding['carrier']), set()))
            native = native and batch.get('provider_native') is True and host_ok
            complete = complete and batch.get('complete') is True
            request_unit = batch.get('request_container')
            request_booking = batch.get('request_booking')
            if request_unit:
                if (request_unit not in binding['container_bookings'] or
                        (request_booking is not None and binding['container_bookings'][request_unit] != request_booking)):
                    raise ValueError('Provider request allocation changed')
                covered_units.add(request_unit)
            for raw in batch.get('events', []):
                if not isinstance(raw, dict):
                    raise ValueError('Native event must remain an object')
                # Carrier status summarizers may manufacture event-like rows.
                # Only the existing official DCSA path may label this native.
                if raw.get('eventType') not in {'EQUIPMENT', 'TRANSPORT', 'SHIPMENT'}:
                    native = False
                unit = raw.get('equipmentReference')
                if unit:
                    if unit not in binding['container_bookings']:
                        raise ValueError('Provider returned a foreign container')
                    covered_units.add(unit)
                records.append({'raw': deepcopy(raw), 'raw_event_sha256': digest(raw),
                                'observed_at': observed.isoformat(), 'source_url': source_url,
                                'request_booking': request_booking, 'request_container': request_unit})
        if len(records) > 5000:
            raise ValueError('Native event inventory bound exceeded')
        observed = max(observations).isoformat() if observations else datetime.now(timezone.utc).isoformat()
        manifest = {'schema_version': 1, 'reader_id': READER_ID, 'reader_release_sha256': self.release,
                    'binding': binding, 'observed_at': observed, 'provider_native': bool(native),
                    'complete': bool(complete), 'events': records,
                    'publisher_origin': publisher_origin, 'catalog_revision': self.catalog()['catalog_revision'],
                    'covered_containers': sorted(covered_units),
                    'all_containers_observed': covered_units == set(binding['container_bookings']),
                    'coverage_reason': 'EXISTING_NATIVE_DCSA_CAPTURE' if native and complete else 'NATIVE_MOVEMENT_EVIDENCE_UNAVAILABLE'}
        body = encoded(manifest)
        if len(body) > MAX_BYTES:
            raise ValueError('Archive write size exceeded')
        sha = hashlib.sha256(body).hexdigest()
        root = self.prefix + '/' + shipment.task_id + '/'
        latest_key = root + 'latest.json'
        current, current_meta = None, None
        try:
            current, current_meta = self._get(latest_key)
        except Exception as error:
            # Without ListBucket, S3 returns AccessDenied for a missing key.
            # A failed read permits only the same conditional first-create;
            # IfNoneMatch still refuses an existing pointer, so a denied read
            # cannot authorize overwriting or bypass the source-time guard.
            if _error_code(error) not in {'NoSuchKey', 'AccessDenied'}:
                raise
        if current:
            prior = instant(current['observed_at'])
            if prior > instant(observed):
                return {'state': 'OLDER_SOURCE_PRESERVED'}
            if prior == instant(observed):
                return {'state': 'ALREADY_ARCHIVED' if current.get('manifest_ref', {}).get('sha256') == sha else 'SOURCE_CONFLICT'}
        key = root + 'manifests/' + sha + '.json'
        args = {'Bucket': self.bucket, 'Key': key, 'Body': body, 'ContentType': 'application/json',
                'ServerSideEncryption': 'AES256', 'ExpectedBucketOwner': self.owner, 'IfNoneMatch': '*'}
        try:
            written = self.s3.put_object(**args)
        except Exception as error:
            if _error_code(error) != 'PreconditionFailed':
                raise
            existing, meta = self._get(key)
            if encoded(existing) != body:
                raise ValueError('Immutable archive collision')
            written = meta
        version = written.get('VersionId')
        if not version or version == 'null':
            raise ValueError('Archive bucket must retain object versions')
        ref = {'kind': 'S3', 'region': self.region, 'bucket': self.bucket, 'key': key,
               'version_id': version, 'sha256': sha}
        pointer = {'manifest_ref': ref, 'observed_at': observed, 'binding': binding}
        put = {'Bucket': self.bucket, 'Key': latest_key, 'Body': encoded(pointer), 'ContentType': 'application/json',
               'ServerSideEncryption': 'AES256', 'ExpectedBucketOwner': self.owner}
        if current_meta:
            if not current_meta.get('ETag'):
                raise ValueError('Pointer concurrency guard unavailable')
            put['IfMatch'] = current_meta['ETag']
        else:
            put['IfNoneMatch'] = '*'
        self.s3.put_object(**put)
        return {'state': 'ARCHIVED' if native and complete else 'ARCHIVED_READ_GAP',
                'task_id': shipment.task_id, 'manifest_ref': ref, 'event_count': len(records)}

    def wants_complete_maersk(self, shipment, status):
        """An optional same-worker read; no authority for source field changes."""
        if (os.getenv('MOVEMENT_ARCHIVE_COMPLETE_MAERSK_ENABLED', '').lower() != 'true'
                or carrier(shipment.shipping_line) != 'maersk'):
            return False
        binding = self._binding(shipment, status)
        if binding is None:
            return False
        # A split needs its own reviewed carrier allocation contract.
        if len(set(binding['container_bookings'].values())) != 1:
            return False
        covered = {event.get('equipmentReference')
                   for batch in status.movement_evidence if batch.get('provider_native') is True
                   for event in batch.get('events', []) if isinstance(event, dict)}
        return not set(binding['container_bookings']).issubset(covered)
