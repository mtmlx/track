from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from shipment_sync.models import ShipmentRef, ShipmentStatus
from shipment_sync.movement_archive import MovementArchivePublisher, native_batch, encoded, digest


class S3Error(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class S3:
    def __init__(self, catalog):
        self.objects = {'feed/catalog.json': encoded(catalog)}
        self.calls = []
        self.versions = {'feed/catalog.json': 'catalog-v1'}

    def get_object(self, **kw):
        self.calls.append(('GET', kw))
        if kw['Key'] not in self.objects:
            raise S3Error('NoSuchKey')
        body = self.objects[kw['Key']]
        return {'Body': io.BytesIO(body), 'VersionId': self.versions[kw['Key']], 'ETag': hashlib.md5(body).hexdigest()}

    def put_object(self, **kw):
        self.calls.append(('PUT', kw))
        key = kw['Key']
        if kw.get('IfNoneMatch') == '*' and key in self.objects:
            raise S3Error('PreconditionFailed')
        if kw.get('IfMatch') and hashlib.md5(self.objects[key]).hexdigest() != kw['IfMatch']:
            raise S3Error('PreconditionFailed')
        self.objects[key] = kw['Body']
        self.versions[key] = 'version-' + str(len(self.calls))
        return {'VersionId': self.versions[key]}


@pytest.fixture
def fixture():
    binding = {'tenant': 'synthetic-tenant', 'workspace_id': '1', 'customer_crm_task_id': 'customer',
               'task_id': 'shipment', 'list_id': 'list', 'carrier': 'ONE',
               'booking_reference': 'BOOK1',
               'container_bookings': {'ABCD1234567': 'BOOK1'},
               'destination_port_codes': ['GTPRQ'], 'terminal_facility_codes': ['TERM']}
    catalog = {'schema_version': 1, 'reader_id': 'tracktrace-s3-v1', 'reader_release_sha256': 'a' * 64,
               'binding': {k: binding[k] for k in ('tenant', 'workspace_id', 'customer_crm_task_id')},
               'generated_at': '2026-11-16T18:00:00Z', 'catalog_revision': 'synthetic-v1',
               'tasks': {'shipment': {'binding': binding, 'destination_port_label': 'Puerto Quetzal'}}}
    store = S3(catalog)
    publisher = MovementArchivePublisher(region='region', bucket='bucket', prefix='feed', catalog_key='feed/catalog.json',
                                         owner='123456789012', release='a' * 64, s3=store)
    shipment = ShipmentRef('shipment', 'Synthetic Shipment', 'one', 'BOOK1', 'ABCD1234567', 'list',
                           expected_container_count=1, destination_port='Puerto Quetzal')
    raw = {'eventID': 'native-1', 'eventType': 'EQUIPMENT', 'eventClassifierCode': 'ACT',
           'equipmentReference': 'ABCD1234567', 'equipmentEventTypeCode': 'GTOT',
           'emptyIndicatorCode': 'LADEN', 'eventDateTime': '2026-11-16T10:00:00-06:00',
           'eventLocation': {'UNLocationCode': 'GTPRQ', 'facilityCode': 'TERM'}}
    status = ShipmentStatus('Current status', movement_evidence=[native_batch([raw], 'https://apix.one-line.com/v2/events',
                            request_booking='BOOK1', request_container='ABCD1234567', observed_at='2026-11-16T18:30:00Z')])
    return publisher, store, shipment, status, raw


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv('MOVEMENT_ARCHIVE_ENABLED', raising=False)
    assert MovementArchivePublisher.from_env() is None


def test_immutable_manifest_hash_version_and_conditional_pointer(fixture):
    publisher, store, shipment, status, raw = fixture
    before = deepcopy(status)
    result = publisher.publish(shipment, status)
    assert result['state'] == 'ARCHIVED'
    ref = result['manifest_ref']
    body = store.objects[ref['key']]
    assert hashlib.sha256(body).hexdigest() == ref['sha256']
    assert ref['version_id'] == store.versions[ref['key']]
    manifest = json.loads(body)
    assert manifest['events'][0]['raw'] == raw
    assert manifest['events'][0]['raw_event_sha256'] == digest(raw)
    assert manifest['provider_native'] and manifest['complete']
    assert manifest['covered_containers'] == ['ABCD1234567']
    assert status == before
    assert all(call['ExpectedBucketOwner'] == '123456789012' for _, call in store.calls)
    assert store.calls[-1][1]['IfNoneMatch'] == '*'


def test_identical_capture_does_not_duplicate_manifest_or_pointer(fixture):
    publisher, store, shipment, status, _ = fixture
    publisher.publish(shipment, status)
    writes = sum(k == 'PUT' for k, _ in store.calls)
    assert publisher.publish(shipment, status)['state'] == 'ALREADY_ARCHIVED'
    assert sum(k == 'PUT' for k, _ in store.calls) == writes
    assert sum(k == 'GET' and v['Key'] == 'feed/catalog.json' for k, v in store.calls) == 1


def test_later_capture_uses_pointer_cas_and_older_capture_cannot_regress(fixture):
    publisher, store, shipment, status, _ = fixture
    publisher.publish(shipment, status)
    old = deepcopy(status)
    status.movement_evidence[0]['observed_at'] = '2026-11-16T19:00:00Z'
    assert publisher.publish(shipment, status)['state'] == 'ARCHIVED'
    assert 'IfMatch' in store.calls[-1][1]
    pointer = store.objects['feed/shipment/latest.json']
    assert publisher.publish(shipment, old)['state'] == 'OLDER_SOURCE_PRESERVED'
    assert store.objects['feed/shipment/latest.json'] == pointer


@pytest.mark.parametrize('change', [lambda s: setattr(s, 'list_id', 'foreign'),
                                    lambda s: setattr(s, 'booking_no', 'foreign'),
                                    lambda s: setattr(s, 'container_no', 'EFGH7654321'),
                                    lambda s: setattr(s, 'destination_port', 'Elsewhere')])
def test_current_binding_mismatch_never_publishes(fixture, change):
    publisher, store, shipment, status, _ = fixture
    change(shipment)
    with pytest.raises(ValueError):
        publisher.publish(shipment, status)
    assert not any(k == 'PUT' for k, _ in store.calls)


def test_outside_catalog_is_read_only_no_publication(fixture):
    publisher, store, shipment, status, _ = fixture
    shipment.task_id = 'not-registered'
    assert publisher.publish(shipment, status)['state'] == 'NOT_REGISTERED'
    assert not any(k == 'PUT' for k, _ in store.calls)


def test_legacy_or_browser_status_archives_explicit_gap_without_fake_events(fixture):
    publisher, store, shipment, status, _ = fixture
    status.movement_evidence = []
    result = publisher.publish(shipment, status)
    assert result['state'] == 'ARCHIVED_READ_GAP'
    manifest = json.loads(store.objects[result['manifest_ref']['key']])
    assert not manifest['provider_native'] and not manifest['complete']
    assert manifest['events'] == []


def test_partial_unit_scope_is_explicit_not_inferred_missing_movement(fixture):
    publisher, store, shipment, status, _ = fixture
    catalog = json.loads(store.objects['feed/catalog.json'])
    catalog['tasks']['shipment']['binding']['container_bookings']['EFGH7654321'] = 'BOOK1'
    store.objects['feed/catalog.json'] = encoded(catalog)
    shipment.container_no += ', EFGH7654321'
    shipment.expected_container_count = 2
    result = publisher.publish(shipment, status)
    manifest = json.loads(store.objects[result['manifest_ref']['key']])
    assert manifest['provider_native'] and manifest['complete']
    assert not manifest['all_containers_observed']
    assert manifest['covered_containers'] == ['ABCD1234567']


def test_maersk_extra_booking_capture_requires_explicit_flag_and_registered_scope(fixture, monkeypatch):
    publisher, store, shipment, status, _ = fixture
    shipment.shipping_line = 'maersk'
    catalog = json.loads(store.objects['feed/catalog.json'])
    catalog['tasks']['shipment']['binding']['carrier'] = 'Maersk'
    catalog['tasks']['shipment']['binding']['container_bookings']['EFGH7654321'] = 'BOOK1'
    store.objects['feed/catalog.json'] = encoded(catalog)
    shipment.container_no += ', EFGH7654321'
    shipment.expected_container_count = 2
    monkeypatch.delenv('MOVEMENT_ARCHIVE_COMPLETE_MAERSK_ENABLED', raising=False)
    assert not publisher.wants_complete_maersk(shipment, status)
    monkeypatch.setenv('MOVEMENT_ARCHIVE_COMPLETE_MAERSK_ENABLED', 'true')
    assert publisher.wants_complete_maersk(shipment, status)
    shipment.task_id = 'not-registered'
    assert not publisher.wants_complete_maersk(shipment, status)


def test_maersk_booking_evidence_only_changes_archive_field_and_calls_booking_once(monkeypatch):
    from shipment_sync.carriers.maersk import MaerskAdapter
    adapter = object.__new__(MaerskAdapter)
    adapter.api_mode = 'events'
    adapter.api_url = 'https://api.maersk.com/trackAndTrace/v2/events'
    adapter._credentials_for = lambda s: SimpleNamespace(is_configured=True)
    adapter._build_events_api_headers = lambda credentials: {}
    calls = []
    rows = [{'eventType': 'EQUIPMENT', 'equipmentReference': c} for c in ['ABCD1234567', 'EFGH7654321']]
    def fetch(reference, kind, headers):
        calls.append((reference, kind))
        return deepcopy(rows)
    adapter._fetch_all_events = fetch
    shipment = ShipmentRef('shipment', 'Synthetic', 'maersk', 'BOOK1', 'ABCD1234567, EFGH7654321', 'list')
    status = ShipmentStatus('Original', eta_time=datetime(2026, 12, 1, tzinfo=timezone.utc), vessel_voyage='Original Vessel')
    before = deepcopy(status)
    assert adapter.capture_booking_movement_evidence(shipment, status)
    assert calls == [('BOOK1', 'booking')]
    assert status.movement_evidence[0]['events'] == rows
    status.movement_evidence = []
    assert status == before


def test_archive_failure_does_not_change_existing_tracking_write_behavior(monkeypatch):
    from shipment_sync import sync
    from shipment_sync.clickup_client import ClickUpClient
    from shipment_sync.models import ShipmentWriteResult
    from test_sync_runtime_guards import _settings
    calls = []
    status = ShipmentStatus('Original status')
    class Publisher:
        def wants_complete_maersk(self, shipment, value):
            return False
        def publish(self, shipment, value):
            calls.append('archive')
            raise RuntimeError('Synthetic archive unavailable')
    class Client(ClickUpClient):
        def update_shipment_status(self, shipment, value):
            assert value is status
            calls.append('source-update')
            return ShipmentWriteResult(False, 'Original status', 'unchanged')
    monkeypatch.setattr(sync.MovementArchivePublisher, 'from_env', lambda: Publisher())
    monkeypatch.setattr(sync, 'build_carrier_registry', lambda: {'one': object()})
    monkeypatch.setattr(sync, '_fetch_carrier_status', lambda adapter, shipment: calls.append('fetch') or status)
    shipment = ShipmentRef('synthetic-task', 'Synthetic', 'one', 'BOOK', 'ABCD1234567', 'list-1')
    stats = sync._run_sync(Client(_settings()), [shipment])
    assert calls == ['fetch', 'archive', 'source-update']
    assert stats.unchanged == 1


def test_existing_query_hold_prevents_fetch_archive_and_source_write(monkeypatch):
    from shipment_sync import sync
    from shipment_sync.clickup_client import ClickUpClient
    from test_sync_runtime_guards import _settings
    class Publisher:
        def wants_complete_maersk(self, *args):
            pytest.fail('Held task must not enter archive path')
        def publish(self, *args):
            pytest.fail('Held task must not publish')
    monkeypatch.setattr(sync.MovementArchivePublisher, 'from_env', lambda: Publisher())
    monkeypatch.setattr(sync, 'build_carrier_registry', lambda: {'one': object()})
    monkeypatch.setattr(sync, 'write_hold_reason', lambda shipment: 'Explicit no-query hold')
    monkeypatch.setattr(sync, '_fetch_carrier_status', lambda *args: pytest.fail('Held task must not fetch'))
    shipment = ShipmentRef('synthetic-held-task', 'Synthetic', 'one', 'BOOK', 'ABCD1234567', 'list-1')
    sync._run_sync(ClickUpClient(_settings()), [shipment])
