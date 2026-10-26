from dataclasses import replace

import pytest

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import ShipmentStatus
from shipment_sync.msc_browser_assisted import build_queue, MscBrowserCapture, MscBrowserFailure
from shipment_sync.msc_browser_assisted_main import _import_batch
from shipment_sync.one_write_holds import MSC_LEGAL_HOLD_TAG, write_hold_reason
from test_msc_bl_validation import ship
from test_one_dcsa import conflict_client
from test_track_trace_mapping import _settings


@pytest.mark.parametrize('tag', [MSC_LEGAL_HOLD_TAG, ' LEGAL-CASE-DO-NOT-UPDATE ', 'legal case', ' Legal Case '])
@pytest.mark.parametrize('carrier', ['MSC', 'msc shipping line', 'mediterranean shipping company'])
def test_msc_legal_marker_excludes_queue_and_projection(tag, carrier):
    ref = ship(tags=(tag,), shipping_line=carrier)
    assert write_hold_reason(ref)
    assert build_queue([ref]) == []
    with pytest.raises(ValueError, match='LEGAL CASE'):
        ClickUpClient(_settings()).plan_shipment_update(ref, ShipmentStatus('Test'))


def test_legal_marker_does_not_change_other_carrier_policy():
    assert write_hold_reason(ship(shipping_line='maersk', tags=(MSC_LEGAL_HOLD_TAG,))) is None
    assert write_hold_reason(ship(tags=('ordinary',))) is None


def test_legal_hold_stops_all_writeback_before_planning(monkeypatch):
    client = ClickUpClient(_settings())
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *a: pytest.fail('Legal shipment reached planner'))
    with pytest.raises(ValueError, match='LEGAL CASE'):
        client.update_shipment_status(ship(tags=(MSC_LEGAL_HOLD_TAG,)), ShipmentStatus('Test'))


@pytest.mark.parametrize('source', ['msc-browser-assisted:capture', 'msc-api'])
@pytest.mark.parametrize('mbl', [None, 'MEDU12345678'])
def test_new_legal_marker_is_rechecked_before_stale_writeback(monkeypatch, source, mbl):
    client = ClickUpClient(_settings())
    ref = ship(master_bl_no=mbl)
    monkeypatch.setattr(client, 'get_shipments_by_task_ids', lambda *a, **k: [replace(ref, tags=(MSC_LEGAL_HOLD_TAG,))])
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *a: pytest.fail('Stale legal hold reached planner'))
    with pytest.raises(ValueError, match='LEGAL CASE'):
        client.update_shipment_status(ref, ShipmentStatus('Test', raw_source=source))


def test_missing_live_msc_task_stops_writeback(monkeypatch):
    client = ClickUpClient(_settings())
    monkeypatch.setattr(client, 'get_shipments_by_task_ids', lambda *a, **k: [])
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *a: pytest.fail('Missing live task reached planner'))
    with pytest.raises(ValueError, match='identity or workflow changed'):
        client.update_shipment_status(ship(master_bl_no=None), ShipmentStatus('Test'))


def test_task_reader_loads_legal_marker(monkeypatch):
    client, task, _, _, _ = conflict_client(monkeypatch)
    task['list'] = {'id': 'list-1', 'name': 'Shipments'}
    task['tags'] = [{'name': MSC_LEGAL_HOLD_TAG}]
    assert client.get_shipments_by_task_ids(['t'])[0].tags == (MSC_LEGAL_HOLD_TAG,)


def test_legal_marker_suppresses_failure_comment_on_live_read(monkeypatch):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['tags'] = [{'name': MSC_LEGAL_HOLD_TAG}]
    assert not client.report_tracking_failure(ship(task_id='t'), error='No results found')
    assert not posts


def test_browser_import_skips_legal_capture_and_failure_without_client_calls():
    class NoWrites:
        def __getattr__(self, name):
            pytest.fail('Legal case attempted client action: ' + name)

    class Client(NoWrites):
        settings = _settings()

    ref = ship(tags=(MSC_LEGAL_HOLD_TAG,))
    _import_batch(Client(), [ref], [MscBrowserCapture(ref.task_id, 'Untrusted capture')],
                  [MscBrowserFailure(ref.task_id, 'BOOK-1', 'No results found')], apply=True)
