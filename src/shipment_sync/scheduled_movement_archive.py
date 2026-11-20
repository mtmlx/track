"""Publish carrier evidence for separately scoped read-only shipment lists."""

import json
import os
import re
from types import SimpleNamespace

from shipment_sync.carriers.registry import build_carrier_registry
from shipment_sync.movement_archive import MovementArchivePublisher, carrier
from shipment_sync.one_write_holds import write_hold_reason
from shipment_sync.sync import _fetch_carrier_status, _terminal_shipment_status


def read_only_scope():
    lists = os.getenv('MOVEMENT_ARCHIVE_READ_ONLY_LIST_IDS', '').split()
    line = os.getenv('MOVEMENT_ARCHIVE_SCHEDULED_CARRIER', '')
    if not lists:
        return set(), ''
    if any(not re.fullmatch(r'[0-9]+', value) for value in lists):
        raise ValueError('Read-only movement lists must be explicit numeric list IDs')
    if carrier(line) not in {'one', 'maersk', 'cma cgm'}:
        raise ValueError('Unsupported scheduled movement carrier')
    return set(lists), carrier(line)


def publish_read_only(read_shipments, list_ids, line, terminal_statuses):
    """Only ClickUp GETs and native carrier reads precede archive publication."""
    publisher = MovementArchivePublisher.from_env()
    if publisher is None:
        raise ValueError('Scheduled movement archive must be enabled')
    candidates = publisher.catalog()['tasks']
    adapters = build_carrier_registry()

    for task_id, entry in candidates.items():
        binding = entry.get('binding') or {}
        list_id = str(binding.get('list_id', ''))
        if list_id not in list_ids or carrier(binding.get('carrier')) != line:
            continue
        result = {'event': 'scheduled_read_only_movement', 'task_id': task_id, 'clickup_writes': 0}
        try:
            matches = read_shipments([task_id], allowed_list_ids={list_id})
            if not matches:
                result['state'] = 'CLOSED_OR_MISSING'
            else:
                shipment, = matches
                if shipment.task_id != task_id or carrier(shipment.shipping_line) != line:
                    raise ValueError('Current shipment no longer matches the catalog')
                if _terminal_shipment_status(shipment.current_task_status, terminal_statuses):
                    result['state'] = 'CLOSED_OR_MISSING'
                elif write_hold_reason(shipment):
                    result['state'] = 'HELD'
                else:
                    allocation = SimpleNamespace(container_booking_map=binding['container_bookings'])
                    publisher._binding(shipment, allocation)
                    adapter = adapters[shipment.shipping_line]
                    status = _fetch_carrier_status(adapter, shipment)
                    if publisher.wants_complete_maersk(shipment, status):
                        adapter.capture_booking_movement_evidence(shipment, status)
                    if status.movement_evidence:
                        saved = publisher.publish(shipment, status, publisher_origin='SCHEDULED_READ_ONLY_PROVIDER_RESPONSE')
                        result.update({key: saved[key] for key in ('state', 'event_count') if key in saved})
                    else:
                        result['state'] = 'NATIVE_MOVEMENT_UNAVAILABLE'
        except Exception as exc:
            result.update(state='READ_GAP', error_type=type(exc).__name__)
        print(json.dumps(result), flush=True)
