from dataclasses import replace
from datetime import datetime, timezone

import pytest
import requests

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.models import ShipmentRef, ShipmentStatus, ShipmentUpdatePlan, ShipmentFieldWrite
from shipment_sync.msc_browser_assisted import MscBrowserCapture, build_queue, consolidate_browser_statuses, status_from_browser_capture
from shipment_sync.tracking_failures import classify_failure, failure_comment
from test_one_dcsa import conflict_client, shipment as one_shipment
from test_track_trace_mapping import _settings


def ship(**kwargs):
    return replace(ShipmentRef('task-1', 'Synthetic MSC B/L shipment', 'msc', 'BOOK-1',
        'MSCU1234567, MSCU7654321', 'list-1', destination_port='USCHS',
        master_bl_no='MEDUTEST001', expected_container_count=2), **kwargs)


def capture(ref='MSCU1234567', header='BILL OF LADING: MEDUTEST001', pod='Charleston, US'):
    return f'''{header}
Port of Discharge
{pod}
Shipped To
Charleston, US
Container
{ref}
26/10/2026
Charleston, US
Estimated Time of Arrival
MSC TEST 001E
01/10/2026
Ningbo, CN
Export Loaded on Vessel
MSC TEST 001E
Tracking results provided by MSC on 23.10.2026 at 18:37 Central Europe Standard Time
'''


def validate(shipment, *texts):
    pairs = [(MscBrowserCapture(shipment.task_id, t), status_from_browser_capture(t)) for t in texts]
    return consolidate_browser_statuses(shipment, pairs)


def test_recorded_master_bl_rescues_complete_booking_allocation():
    for _ in range(2):
        result = validate(ship(), capture(), capture('MSCU7654321'))
        assert result.discovered_containers == ['MSCU1234567', 'MSCU7654321']
        assert result.validated_ocean_port == 'Charleston, US'


@pytest.mark.parametrize('header', ['Bill of Lading:\nMEDUTEST001', 'BILL OF LADING NUMBER: MEDUTEST001',
    'BOOKING NUMBER: BOOK-1\nBill of Lading:\nMEDUTEST001'])
def test_header_layouts_preserve_exact_bl_binding(header):
    validate(ship(), capture(header=header), capture('MSCU7654321', header=header))


@pytest.mark.parametrize('overrides', [dict(master_bl_no=None), dict(master_bl_no='MEDUOTHER001'),
    dict(master_bl_no='MEDUTEST001, MEDUOTHER001'), dict(expected_container_count=None), dict(container_no=None)])
def test_bl_fallback_requires_recorded_identity_and_full_allocation(overrides):
    with pytest.raises(ValueError):
        validate(ship(**overrides), capture(), capture('MSCU7654321'))


@pytest.mark.parametrize('header', ['BOOKING NUMBER: OTHER\nBILL OF LADING: MEDUTEST001',
    'BOOKING NUMBER: BOOK-1\nBILL OF LADING: MEDUOTHER001',
    'BOOKING NUMBER: BOOK-1\nBILL OF LADING: MEDUTEST001\nBILL OF LADING: MEDUOTHER001',
    'CONTAINER NUMBER: MSCU1234567'])
def test_matching_container_or_booking_never_overrides_conflicting_identity(header):
    with pytest.raises(ValueError):
        validate(ship(), capture(header=header), capture('MSCU7654321'))


def test_split_bl_captures_cannot_be_combined_under_shared_booking():
    with pytest.raises(ValueError, match='B/L identity conflict'):
        validate(ship(master_bl_no=None), capture(header='BOOKING NUMBER: BOOK-1\nBILL OF LADING: MEDUTEST001'),
            capture('MSCU7654321', header='BOOKING NUMBER: BOOK-1\nBILL OF LADING: MEDUOTHER001'))


@pytest.mark.parametrize('refs,count', [('MSCU1234567, MSCU7654321', 1), ('MSCU1234567', 2)])
def test_quantity_disagreement_blocks_entire_allocation(refs, count):
    with pytest.raises(ValueError, match='container count conflict'):
        validate(ship(container_no=refs, expected_container_count=count), capture(), capture('MSCU7654321'))


@pytest.mark.parametrize('texts', [(capture(),), (capture(), capture('MSCU0000001'))])
def test_partial_or_unexpected_unit_cannot_pass_bl_fallback(texts):
    with pytest.raises(ValueError, match='review is incomplete'):
        validate(ship(), *texts)


@pytest.mark.parametrize('destination', [None, 'Puerto Quetzal', 'Charleston / Antwerp'])
def test_bl_match_does_not_override_destination_evidence(destination):
    with pytest.raises(ValueError, match='shipment destination'):
        validate(ship(destination_port=destination), capture(), capture('MSCU7654321'))


def test_msc_queue_exposes_master_bl_without_changing_booking():
    row = build_queue([ship()])[0]
    assert row.master_bl_no == 'MEDUTEST001' and row.booking_no == 'BOOK-1'


def test_live_task_reader_loads_configured_master_bl(monkeypatch):
    c, task, _, _, _ = conflict_client(monkeypatch)
    c.settings.cf_master_bl = 'master-field'
    task['list'] = {'id':'list-1', 'name':'Shipments'}
    task['custom_fields'].append({'id':'master-field', 'value':'MEDUTEST001'})
    assert c.get_shipments_by_task_ids(['t'])[0].master_bl_no == 'MEDUTEST001'


def test_changed_master_bl_blocks_stale_failure_comment(monkeypatch):
    c, task, _, posts, _ = conflict_client(monkeypatch)
    ref = replace(one_shipment(), shipping_line='msc', master_bl_no='MEDUTEST001')
    task['custom_fields'][2]['value'] = 'msc'
    task['custom_fields'].append({'id':c.settings.cf_master_bl, 'value':'MEDUREPLACED001'})
    assert not c.report_tracking_failure(ref, error='MSC booking search returned No results found')
    assert not posts


@pytest.mark.parametrize('error,category,detail', [
    ('MSC booking search returned No results found. HTTP 500 token=TOPSECRET', 'msc_reference_not_found', 'No results found'),
    ('Shipment destination/POD is missing in ClickUp.', 'msc_destination_missing', 'POD is blank'),
    ('MSC container count conflict: recorded 2 unique containers; expected 1.', 'msc_container_count_conflict', 'Recorded unique containers: 2'),
    ('MSC B/L identity conflict: conflicting header token=TOPSECRET', 'msc_bl_identity_conflict', 'Master B/L'),
    ('MSC browser review is incomplete: missing container capture(s): MSCU7654321; unexpected container capture(s): MSCU0000001', 'msc_container_coverage', 'Missing container evidence: MSCU7654321'),
    ('MSC browser review is incomplete: missing container capture(s): MSCU7654321. B/L capture binding limitation', 'msc_bl_capture_incomplete', 'integration evidence gap'),
])
def test_failure_comments_include_specific_safe_facts(error, category, detail):
    ref = ship(expected_container_count=1 if 'count conflict' in error else 2)
    notice = classify_failure(ref, error)
    text = failure_comment(ref, notice, method='browser capture/import', phase='lookup')
    assert notice.category == category
    assert detail in text and 'Recorded Master B/L: MEDUTEST001' in text
    assert 'TOPSECRET' not in text and 'token=' not in text and 'HTTP 500' not in text
    assert 'WHY WE CANNOT PROCEED:' in text and 'CORRECTIVE ACTION' in text


def test_msc_diagnostic_deduplication_and_owner_mentions(monkeypatch):
    c, task, history, posts, _ = conflict_client(monkeypatch)
    ref = replace(one_shipment(), shipping_line='msc', master_bl_no='MEDUTEST001')
    task['custom_fields'][2]['value'] = 'msc'
    task['custom_fields'].append({'id':c.settings.cf_master_bl, 'value':ref.master_bl_no})
    error = 'MSC booking search returned No results found'
    assert c.report_tracking_failure(ref, error=error)
    history.append({'id':'1', 'date':str(int(datetime.now(timezone.utc).timestamp()*1000)), 'comment':posts[0]['comment']})
    assert not c.report_tracking_failure(ref, error=error)
    assert [p['user']['id'] for p in posts[0]['comment'] if p.get('type')=='tag'] == [12,34]


def test_msc_status_write_failure_never_stamps_success(monkeypatch):
    c = ClickUpClient(_settings())
    ref = ship(master_bl_no=None)
    status = ShipmentStatus('Test', raw_source='msc-browser-assisted:capture')
    plan = ShipmentUpdatePlan(True, 'Test', 'hash',
        [ShipmentFieldWrite(c.settings.cf_status_last_checked, datetime.now(timezone.utc), 'datetime')],
        task_status_update='Tránsito')
    monkeypatch.setattr(c, 'plan_shipment_update', lambda *a:plan)
    monkeypatch.setattr(c, '_set_task_status', lambda *a: (_ for _ in ()).throw(requests.Timeout('Timeout')))
    monkeypatch.setattr(c, 'report_tracking_failure', lambda *a,**k:False)
    monkeypatch.setattr(c, '_set_date_custom_field', lambda *a,**k:pytest.fail('Success timestamp written after failed status'))
    with pytest.raises(requests.Timeout):
        c.update_shipment_status(ref, status)


def test_changed_master_bl_blocks_write_before_any_mutation(monkeypatch):
    c = ClickUpClient(_settings())
    ref = ship()
    monkeypatch.setattr(c, 'get_shipments_by_task_ids', lambda *a,**k:[replace(ref,master_bl_no='MEDUREPLACED001')])
    monkeypatch.setattr(c, 'plan_shipment_update', lambda *a:pytest.fail('Stale evidence reached planner'))
    with pytest.raises(ValueError, match='changed after capture review') as error:
        c.update_shipment_status(ref, ShipmentStatus('Test', raw_source='msc-browser-assisted:capture'))
    assert error.value.shipment_sync_phase == 'projection'


BOOKING_ONLY = 'BOOKING NUMBER: BOOK-1 1 Bill of Lading found\nBill of Lading:\n(2 Containers )'


def test_booking_fallback_validates_complete_set_without_replacing_master_bl():
    ref = ship()
    result = validate(ref, capture(header=BOOKING_ONLY), capture('MSCU7654321', header=BOOKING_ONLY))
    assert result.msc_booking_anchored_fallback
    assert result.discovered_containers == ['MSCU1234567', 'MSCU7654321']
    assert not result.container_discovery_authoritative
    assert ref.master_bl_no == 'MEDUTEST001'


@pytest.mark.parametrize('header', [
    BOOKING_ONLY.replace('BOOK-1', 'OTHER'),
    BOOKING_ONLY.replace('1 Bill', '2 Bills'),
    BOOKING_ONLY.replace('(2 Containers )', '(1 Container )'),
    BOOKING_ONLY.replace(' 1 Bill of Lading found', ''),
    BOOKING_ONLY.replace('(2 Containers )', ''),
    BOOKING_ONLY + '\nBILL OF LADING: MEDUOTHER001',
    BOOKING_ONLY + '\nBOOKING NUMBER: OTHER',
])
def test_booking_fallback_rejects_ambiguous_or_contradictory_identity(header):
    with pytest.raises(ValueError):
        validate(ship(), capture(header=header), capture('MSCU7654321', header=header))


@pytest.mark.parametrize('overrides', [dict(expected_container_count=None), dict(container_no=None),
    dict(destination_port=None), dict(destination_port='Charleston / Antwerp')])
def test_booking_fallback_requires_known_allocation_and_destination(overrides):
    with pytest.raises(ValueError):
        validate(ship(**overrides), capture(header=BOOKING_ONLY), capture('MSCU7654321', header=BOOKING_ONLY))


def test_booking_fallback_cannot_match_only_inland_delivery():
    with pytest.raises(ValueError):
        validate(ship(), capture(header=BOOKING_ONLY, pod='Long Beach, US'),
                 capture('MSCU7654321', header=BOOKING_ONLY, pod='Long Beach, US'))


@pytest.mark.parametrize('texts', [
    (capture(header=BOOKING_ONLY),),
    (capture(header=BOOKING_ONLY), capture('MSCU0000001', header=BOOKING_ONLY)),
    (capture(header=BOOKING_ONLY), capture('MSCU7654321', header=BOOKING_ONLY), capture(header=BOOKING_ONLY)),
    (capture(header=BOOKING_ONLY), capture('MSCU7654321')),
])
def test_booking_fallback_rejects_partial_extra_duplicate_or_mixed_evidence(texts):
    with pytest.raises(ValueError):
        validate(ship(), *texts)


def test_confirmed_bl_does_not_claim_booking_fallback():
    assert not validate(ship(), capture(), capture('MSCU7654321')).msc_booking_anchored_fallback


def test_booking_fallback_projection_is_flagged_and_preserves_master_bl():
    ref = ship()
    result = validate(ref, capture(header=BOOKING_ONLY), capture('MSCU7654321', header=BOOKING_ONLY))
    client = ClickUpClient(_settings())
    plan = client.plan_shipment_update(ref, result)
    assert 'Master B/L not independently validated' in plan.comment_text
    assert 'Recorded Master B/L retained: MEDUTEST001' in plan.comment_text
    assert all(u.field_id != client.settings.cf_master_bl for u in plan.custom_field_updates)


def test_booking_fallback_audit_only_comment_is_deduplicated(monkeypatch):
    from shipment_sync.clickup_client import _msc_booking_fallback_note

    client = ClickUpClient(_settings())
    ref = ship()
    status = ShipmentStatus('Test', raw_source='msc-browser-assisted:capture', msc_booking_anchored_fallback=True)
    note = _msc_booking_fallback_note(ref, status)
    plan = ShipmentUpdatePlan(False, 'Test', 'hash', comment_text=note)
    monkeypatch.setattr(client, 'get_shipments_by_task_ids', lambda *a, **k: [ref])
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *a: plan)
    monkeypatch.setattr(client, '_fetch_recent_task_comment_texts', lambda *a, **k: ['Earlier update\n' + note])
    monkeypatch.setattr(client, '_post_comment', lambda *a: pytest.fail('Repeated fallback-only comment'))
    client.update_shipment_status(ref, status)
