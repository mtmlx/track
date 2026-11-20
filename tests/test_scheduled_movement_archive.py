import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from shipment_sync import main, scheduled_movement_archive as scheduled
from shipment_sync.models import ShipmentRef, ShipmentStatus


def test_scheduled_archive_reads_only_approved_list_and_carrier(monkeypatch, capsys):
    ship = ShipmentRef('task1', 'Test', 'one', 'BOOK', 'ABCD1234567', '1000')
    publisher = Mock()
    monkeypatch.setattr(scheduled.MovementArchivePublisher, 'from_env', lambda: publisher)
    publisher.catalog.return_value = {'tasks': {
        'task1': {'binding': {'list_id': '1000', 'carrier': 'ONE',
                             'container_bookings': {'ABCD1234567': 'BOOK'}}},
        'task2': {'binding': {'list_id': '1001', 'carrier': 'ONE'}},
        'task3': {'binding': {'list_id': '1000', 'carrier': 'MAERSK'}},
    }}
    publisher.wants_complete_maersk.return_value = False
    publisher.publish.return_value = {'state': 'ARCHIVED', 'event_count': 1}
    monkeypatch.setattr(scheduled, 'build_carrier_registry', lambda: {'one': 'adapter'})
    fetch = Mock(return_value=ShipmentStatus('at sea', movement_evidence=[{}]))
    monkeypatch.setattr(scheduled, '_fetch_carrier_status', fetch)
    source_read = Mock(return_value=[ship])

    scheduled.publish_read_only(source_read, {'1000'}, 'one', ('closed',))

    source_read.assert_called_once_with(['task1'], allowed_list_ids={'1000'})
    fetch.assert_called_once_with('adapter', ship)
    publisher.publish.assert_called_once_with(ship, fetch.return_value,
                                              publisher_origin='SCHEDULED_READ_ONLY_PROVIDER_RESPONSE')
    assert json.loads(capsys.readouterr().out) == {
        'event': 'scheduled_read_only_movement', 'task_id': 'task1', 'clickup_writes': 0,
        'state': 'ARCHIVED', 'event_count': 1,
    }

    publisher._binding.side_effect = ValueError('drifted shipment')
    fetch.reset_mock()
    publisher.publish.reset_mock()
    scheduled.publish_read_only(source_read, {'1000'}, 'one', ('closed',))
    fetch.assert_not_called()
    publisher.publish.assert_not_called()
    assert json.loads(capsys.readouterr().out)['state'] == 'READ_GAP'
    ship.current_task_status = 'closed'
    scheduled.publish_read_only(source_read, {'1000'}, 'one', ('closed',))
    fetch.assert_not_called()
    assert json.loads(capsys.readouterr().out)['state'] == 'CLOSED_OR_MISSING'


def test_read_only_list_stays_out_of_sync_when_archive_unavailable(monkeypatch):
    monkeypatch.setenv('MOVEMENT_ARCHIVE_READ_ONLY_LIST_IDS', '1000')
    monkeypatch.setenv('MOVEMENT_ARCHIVE_SCHEDULED_CARRIER', 'one')
    monkeypatch.setattr('sys.argv', ['shipment-sync'])
    monkeypatch.setattr(main, 'load_dotenv', lambda: None)
    settings = SimpleNamespace(eta_only_mode=False, shipment_terminal_statuses=('closed',))
    monkeypatch.setattr(main.Settings, 'from_env', lambda: settings)
    protected, other = [ShipmentRef(t, 'Test', 'one', 'BOOK', 'ABCD1234567', l)
                        for t, l in (('a', '1000'), ('b', '1001'))]
    client = Mock()
    client.list_shipments.return_value = [protected, other]
    monkeypatch.setattr(main, 'ClickUpClient', lambda _: client)
    archive = Mock(side_effect=ValueError)
    monkeypatch.setattr(main, 'publish_read_only', archive)
    sync = Mock(return_value=SimpleNamespace(
        updated_items=[], skipped=0, unchanged=1, total_candidates=1,
        candidates_by_list={}, updated_by_list={}, unchanged_by_list={}))
    monkeypatch.setattr(main, 'run_sync', sync)

    main.main()

    archive.assert_called_once_with(client.get_shipments_by_task_ids, {'1000'}, 'one', ('closed',))
    sync.assert_called_once_with(client, shipments=[other])
