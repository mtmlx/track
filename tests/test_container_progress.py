from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

def test_maersk_register_requires_occurrence_timezone_and_explicit_classifier():
    from shipment_sync.carriers.maersk import _equipment_movement_records

    event = dict(eventType="EQUIPMENT", equipmentReference="DRYU9944977",
                 equipmentEventTypeCode="GTOT", emptyIndicatorCode="LADEN",
                 eventClassifierCode="ACT", eventID="gate-1",
                 eventDateTime="2026-11-17T10:01:00-06:00",
                 transportCall={"UNLocationCode": "GTPRQ"})
    record, = _equipment_movement_records([event], "BOOK", None)
    move, = record.movements
    assert (move.container_reference, move.source_event_id, move.equipment_load_state) == (
        "DRYU9944977", "gate-1", "LADEN")
    assert move.event_time.isoformat() == event["eventDateTime"]
    record, = _equipment_movement_records(
        [{**event, "eventDateTime": "2026-11-17T10:01:00"}], "BOOK", None)
    assert not record.movements
    assert not _equipment_movement_records([{**event, "eventClassifierCode": None}], "BOOK", None)

from shipment_sync.container_progress import container_progress, progress_comment
from shipment_sync.models import ContainerMovementRecord, MovementEvent, ShipmentRef, ShipmentStatus
from shipment_sync.clickup_client import ClickUpClient
from test_track_trace_mapping import _settings


def sample():
    now = datetime.now(timezone.utc)
    shipment = ShipmentRef('task', '31923', 'ONE', 'BOOK',
        'DRYU9944977, KKFU7910952', 'list', expected_container_count=2,
        destination_port='PUERTO QUETZAL', current_task_status='en almacén')
    records = []
    for i, ref in enumerate(('DRYU9944977', 'KKFU7910952')):
        def move(code, days, load, state='actual'):
            when = now - timedelta(days=days, hours=i)
            return MovementEvent(f'Container ({code})', 'PUERTO QUETZAL', when,
                event_time_local_text=when.astimezone(timezone(timedelta(hours=-6))).isoformat(),
                event_state=state, location_code='GTPRQ', equipment_load_state=load,
                container_reference=ref, source_event_id=f'{ref}-{code}')
        records.append(ContainerMovementRecord(ref, 'BOOK', f'https://carrier/{ref}',
            [move('DISC', 5, 'LADEN'), move('GTOT', 3, 'LADEN'),
             move('GTIN', 2, 'EMPTY', 'estimated')]))
    return shipment, ShipmentStatus('Tracking', discovered_containers=[r.container for r in records],
        raw_source='https://apix.one-line.com/v2/events', container_movements=records)


def test_staggered_gate_out_and_estimated_returns():
    sh, status = sample()
    p = container_progress(sh, status)
    assert p['gate_out']['completed'] == p['gate_out']['expected'] == 2
    assert p['gate_out']['complete']
    assert p['gate_out']['completed_at'] == status.container_movements[0].movements[1].event_time.isoformat()
    assert p['empty_return']['completed'] == 0
    assert p['empty_return']['completed_at'] is None
    assert all(r['gate_out']['event_id'] for r in p['containers'])
    assert '-06:00' in progress_comment(p)
    assert '2/2 ACTUAL' in progress_comment(p)
    assert '0/2 ACTUAL' in progress_comment(p)


@pytest.mark.parametrize("carrier", ["maersk", "cma-cgm"])
def test_carrier_publication_only_posts_once_and_never_updates_fields(carrier):
    from types import SimpleNamespace
    from shipment_sync.carrier_register import publish_carrier_register

    sh, status = sample()
    sh.shipping_line = carrier
    comments = []
    client = SimpleNamespace(
        get_shipments_by_task_ids=lambda *a, **kw: [sh],
        _fetch_recent_task_comment_texts=lambda *a, **kw: comments,
        _post_comment=lambda task, text: comments.insert(0, text),
    )
    adapter = SimpleNamespace(fetch_container_progress_status=lambda shipment: status)
    assert publish_carrier_register(client, sh, adapter) == "published"
    assert publish_carrier_register(client, sh, adapter) == "unchanged"
    assert len(comments) == 1
    status.container_movements[0].movements[-1].event_state = "actual"
    assert publish_carrier_register(client, sh, adapter) == "published"
    assert "Empty returned: 1/2 ACTUAL" in comments[0]


def test_one_return_then_all_returns():
    sh, status = sample()
    status.container_movements[0].movements[-1].event_state = 'actual'
    p = container_progress(sh, status)
    assert p['empty_return']['completed'] == 1
    assert not p['empty_return']['complete']
    status.container_movements[1].movements[-1].event_state = 'actual'
    assert container_progress(sh, status)['empty_return']['complete']


@pytest.mark.parametrize('change', [
    dict(event_state='estimated'), dict(event_state=None),
    dict(event_time=None), dict(event_time=datetime.now()),
    dict(event_time=datetime.now(timezone.utc)+timedelta(days=1)),
    dict(location_code='MXZLO'), dict(location_code=None, location='unknown'),
    dict(container_reference=None), dict(container_reference='OTHER'),
    dict(equipment_load_state='EMPTY'),
    dict(event_time=datetime.now(timezone.utc)-timedelta(days=10)),
])
def test_bad_gate_evidence_never_counts(change):
    sh, status = sample()
    record = status.container_movements[0]
    record.movements[1] = replace(record.movements[1], **change)
    p = container_progress(sh, status)
    assert p['gate_out']['completed'] == 1
    assert not p['gate_out']['complete']


def test_missing_population_cannot_complete():
    sh, status = sample()
    status.container_movements.pop()
    p = container_progress(sh, status)
    assert not p['population_verified']
    assert p['gate_out']['outstanding'] == ['KKFU7910952']
    assert not p['gate_out']['complete']


def test_new_timestamp_changes_snapshot_and_comment_without_status_change():
    sh, status = sample()
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    first = client.plan_shipment_update(sh, status)
    assert 'Container movement register' in first.comment_text
    assert first.task_status_update is None
    sh.track_trace_snapshot_hash = first.snapshot_hash
    sh.current_field_values.update({w.field_id:w.value for w in first.custom_field_updates})
    repeated = client.plan_shipment_update(sh, status)
    assert 'Container movement register' not in (repeated.comment_text or '')
    status.container_movements[0].movements[1].event_time -= timedelta(minutes=5)
    status.container_movements[0].movements[1].event_time_local_text = status.container_movements[0].movements[1].event_time.isoformat()
    changed = client.plan_shipment_update(sh, status)
    assert changed.snapshot_hash != first.snapshot_hash
    assert 'Container movement register' in changed.comment_text


def test_other_carriers_unchanged():
    sh, status = sample()
    sh.shipping_line = 'MSC'
    assert container_progress(sh, status) is None


def test_single_container_workflow_is_unchanged():
    sh, status = sample()
    sh.expected_container_count = 1
    sh.container_no = 'DRYU9944977'
    status.container_movements = status.container_movements[:1]
    assert container_progress(sh, status) is None


def test_comment_fingerprint_deduplicates_without_snapshot_field(monkeypatch):
    sh, status = sample()
    client = ClickUpClient(_settings(clickup_use_task_status=True, cf_track_trace_snapshot=None))
    first = client.plan_shipment_update(sh, status)
    sh.current_field_values.update({w.field_id:w.value for w in first.custom_field_updates})
    saved = progress_comment(container_progress(sh, status))
    monkeypatch.setattr(client, '_fetch_recent_task_comment_texts', lambda *a, **kw: [saved])
    monkeypatch.setattr(client, '_set_custom_field', lambda *a, **kw: None)
    monkeypatch.setattr(client, '_set_date_custom_field', lambda *a, **kw: None)
    monkeypatch.setattr(client, '_post_comment', lambda *a, **kw: pytest.fail('duplicate report'))
    result = client.update_shipment_status(sh, status)
    assert not result.changed


def test_dcsa_fetch_retains_per_container_identity_and_event_id(monkeypatch):
    from shipment_sync.carriers.one_dcsa import OneDcsaClient
    from test_one_dcsa import split_events
    sh, _ = sample()
    client = OneDcsaClient()
    monkeypatch.setattr(client, 'events', lambda scoped: (
        split_events(scoped.container_no, 'BOOK'), [scoped.container_no]))
    status = client.fetch_status(sh)
    assert len(status.container_movements) == 2
    for record in status.container_movements:
        assert record.booking == 'BOOK'
        assert all(m.container_reference == record.container for m in record.movements)
        assert all(m.source_event_id for m in record.movements)


def test_reinstated_evidence_is_not_suppressed_by_older_matching_register(monkeypatch):
    sh, status = sample()
    client = ClickUpClient(_settings(clickup_use_task_status=True, cf_track_trace_snapshot=None))
    first = client.plan_shipment_update(sh, status)
    sh.current_field_values.update({w.field_id:w.value for w in first.custom_field_updates})
    report_a = progress_comment(container_progress(sh, status))
    newer_b = 'Container movement register\nContainer register fingerprint: different-evidence'
    monkeypatch.setattr(client, '_fetch_recent_task_comment_texts', lambda *a, **kw: [newer_b,report_a])
    monkeypatch.setattr(client, '_set_custom_field', lambda *a, **kw: None)
    monkeypatch.setattr(client, '_set_date_custom_field', lambda *a, **kw: None)
    posted = []
    monkeypatch.setattr(client, '_post_comment', lambda task, text: posted.append(text))
    client.update_shipment_status(sh, status)
    assert len(posted) == 1 and report_a in posted[0]
