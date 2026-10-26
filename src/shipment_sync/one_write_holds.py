"""Reviewed repair holds and explicit MSC legal-case tracking exclusions."""
import json
from importlib.resources import files

MSC_LEGAL_HOLD_TAG = 'legal-case-do-not-update'


def msc_legal_hold(tags):
    return bool({MSC_LEGAL_HOLD_TAG, 'legal case'} & {tag.strip().lower() for tag in tags})


def write_hold_reason(shipment):
    if shipment.shipping_line.strip().lower() == 'msc' and msc_legal_hold(getattr(shipment, 'tags', ())):
        return 'LEGAL CASE - DO NOT UPDATE: explicit owner hold; no tracking fields, statuses, timestamps or automated comments until authorized release.'
    if shipment.shipping_line.strip().lower() != 'one':
        return None
    holds = json.loads(files('shipment_sync').joinpath('allocations/one_write_holds.json').read_text())
    return holds.get(shipment.task_id)
