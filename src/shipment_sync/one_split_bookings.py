"""Reviewed, task-scoped ONE allocations; never infer a split from a conflict."""
import json
import re
from functools import lru_cache
from importlib.resources import files

from shipment_sync.destination import same_port


@lru_cache(maxsize=1)
def _registry():
    return json.loads(files('shipment_sync').joinpath('allocations/one_split_bookings.json').read_text())


def container_bookings(shipment):
    entry = _registry().get(shipment.task_id)
    if entry is None:
        return None
    allocation = entry.get('container_bookings')
    raw_bookings = (shipment.booking_no or '').strip().upper()
    bookings = re.split(r'[\s,;]+', raw_bookings) if re.fullmatch(r'[A-Z0-9]+(?:[\s,;]+[A-Z0-9]+)*', raw_bookings) else []
    allowed_booking_sets = ({entry.get('parent_booking')}, set(allocation.values())) if isinstance(allocation, dict) else ()
    references = re.findall(r'\b[A-Z]{4}\d{7}\b', (shipment.container_no or '').upper())
    if (not isinstance(allocation, dict) or not allocation
            or not entry.get('evidence')
            or shipment.shipping_line.strip().lower() != 'one'
            or not bookings or len(bookings) != len(set(bookings))
            or set(bookings) not in allowed_booking_sets
            or not same_port(shipment.destination_port, entry.get('destination_port'))
            or len(references) != len(set(references))
            or set(references) != set(allocation)
            or shipment.expected_container_count != len(allocation)
            or any(not re.fullmatch(r'[A-Z]{4}\d{7}', key)
                   or not isinstance(value, str) or not re.fullmatch(r'[A-Z0-9]{1,35}', value)
                   for key, value in allocation.items())):
        raise ValueError('ONE split booking mapping conflicts with the current shipment; review the allocation')
    return dict(allocation)
