"""Reviewed ONE repairs awaiting explicit approval; previews remain available."""
import json
from importlib.resources import files


def write_hold_reason(shipment):
    if shipment.shipping_line.strip().lower() != 'one':
        return None
    holds = json.loads(files('shipment_sync').joinpath('allocations/one_write_holds.json').read_text())
    return holds.get(shipment.task_id)
