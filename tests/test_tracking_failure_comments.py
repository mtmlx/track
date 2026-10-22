from datetime import datetime, timedelta, timezone
import hashlib
import json
from urllib.parse import urlsplit

import pytest
import requests

from test_one_dcsa import conflict_client, shipment
from shipment_sync.models import ShipmentRef, ShipmentStatus, ShipmentUpdatePlan, ShipmentWriteResult
from shipment_sync.sync import _run_sync
from shipment_sync.tracking_failures import carrier_name, classify_failure, failure_comment, source_url


@pytest.mark.parametrize('line', ['one', 'msc', 'maersk', 'cma-cgm', 'wan hai',
    'cosco', 'hapag-lloyd', 'evergreen', 'pil', 'oocl'])
def test_all_carriers_notify_current_assignees_without_field_writes(monkeypatch, line):
    client, task, history, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = line
    ref = shipment(); ref.shipping_line = line
    assert source_url(ref).startswith('https://')
    assert client.report_tracking_failure(ref, error='returned no events')
    text = posts[0]['comment'][0]['text']
    assert all(s in text for s in ('ISSUE:', 'IMPACT:', 'WHY WE CANNOT PROCEED:', 'CORRECTIVE ACTION'))
    assert [part['user']['id'] for part in posts[0]['comment'] if part.get('type') == 'tag'] == [12, 34]
    assert 'did not write shipment fields' in text
    history.append({'id': '1', 'date': str(int(datetime.now(timezone.utc).timestamp()*1000)),
                    'comment': posts[0]['comment']})
    assert not client.report_tracking_failure(ref, error='returned no events', method='browser capture/import')
    assert len(posts) == 1


@pytest.mark.parametrize('error,category', [
    ('ONE booking lookup is unavailable or incomplete', 'carrier_data_incomplete'),
    ('ONE DCSA needs between one and twenty validated containers', 'missing_references'),
    ('MSC journey mismatch: booking does not match', 'identity_conflict'),
    ('MSC browser review is incomplete: missing container capture(s)', 'missing_references'),
    ('MSC destination port is missing or disagrees', 'destination_evidence'),
    ('ONE booking lookup has conflicting destination schedules', 'destination_evidence'),
    ('WanHaiAntiBotBlocked: page blocked by anti-bot protection', 'carrier_blocked'),
    ('MSC returned no result', 'carrier_data_incomplete'),
    ('API HTTP 403', 'integration_access'),
    ('adapter not configured: API credentials missing', 'integration_access'),
    ('Read timed out', 'service_unavailable'),
    ('HTTP 429 rate limit', 'service_unavailable'),
    ('unexpected parser exception', 'technical_review'),
])
def test_failure_categories_are_actionable_not_invalid_booking_claims(error, category):
    assert classify_failure(shipment(), error).category == category


def test_raw_errors_and_credentials_are_never_copied_into_task_comments(monkeypatch):
    client, _, _, posts, _ = conflict_client(monkeypatch)
    assert client.report_tracking_failure(shipment(), error='HTTP 403 https://api.test?token=TOPSECRET Authorization: bearer SECRET')
    text = posts[0]['comment'][0]['text']
    assert 'TOPSECRET' not in text and 'SECRET' not in text and 'api.test' not in text
    assert 'Never post credentials' in text
    assert 'not proof that the booking is invalid' in text


@pytest.mark.parametrize('change', ['carrier', 'count', 'checked', 'terminal', 'booking', 'destination'])
def test_stale_failures_do_not_notify_after_task_changes(monkeypatch, change):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    if change == 'carrier': task['custom_fields'][2]['value'] = 'msc'
    if change == 'count': task['custom_fields'].append({'id':'count','name':'Number of Containers','value':'2'})
    if change == 'checked': task['custom_fields'].append({'id':client.settings.cf_status_last_checked,
        'value': str(int(datetime.now(timezone.utc).timestamp()*1000))})
    if change == 'terminal': task['status']['status'] = 'cancelado'
    if change == 'booking': task['custom_fields'][0]['value'] = 'REPLACED'
    if change == 'destination': task['custom_fields'][3]['value'] = 'PUERTO CORTES'
    assert not client.report_tracking_failure(shipment(), error='no events')
    assert not posts


def test_new_owner_gets_comment_and_unchanged_owner_does_not(monkeypatch):
    client, task, history, posts, _ = conflict_client(monkeypatch)
    assert client.report_tracking_failure(shipment(), error='no events')
    history.append({'id':'1','date':str(int(datetime.now(timezone.utc).timestamp()*1000)), 'comment':posts[0]['comment']})
    task['assignees'] = [{'id': 99}]
    assert client.report_tracking_failure(shipment(), error='no events')
    assert [p['user']['id'] for p in posts[1]['comment'] if p.get('type')=='tag'] == [99]


def test_partial_write_notice_never_claims_no_fields_changed(monkeypatch):
    client, _, _, posts, _ = conflict_client(monkeypatch)
    assert client.report_tracking_failure(shipment(), error='Read timed out', phase='writeback')
    text = posts[0]['comment'][0]['text']
    assert 'Some fields or comments may already have been saved' in text
    assert 'did not write shipment fields' not in text
    assert 'before retrying' in text


def test_wan_hai_recovery_keeps_full_screen_tracy_instructions(monkeypatch):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = 'Wan Hai Lines'
    ref = shipment(); ref.shipping_line = 'wan hai'
    assert client.request_wan_hai_manual_capture(ref, error='captcha challenge')
    text = posts[0]['comment'][0]['text']
    assert 'Wan Hai Manual Capture needed' in text
    assert 'entire result screen/page' in text and 'TracyAI' in text
    carrier_url = next(line.removeprefix('Carrier source: ') for line in text.splitlines()
                       if line.startswith('Carrier source: '))
    assert urlsplit(carrier_url).scheme == 'https'
    assert urlsplit(carrier_url).hostname == 'www.wanhai.com'


def test_wan_hai_manual_capture_opt_out_is_preserved(monkeypatch):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = 'wan hai'
    client.settings.wan_hai_manual_capture_comment = False
    ref = shipment(); ref.shipping_line = 'wan hai'
    assert not client.report_tracking_failure(ref, error='captcha challenge', phase='not_queried')
    assert not posts


def test_browser_failure_uses_same_guarded_assignee_notification(monkeypatch):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = 'msc'
    ref = shipment(); ref.shipping_line = 'msc'
    assert client.report_msc_tracking_failure(ref, reference='ONEU2154315', error='access denied')
    text = posts[0]['comment'][0]['text']
    assert 'browser capture/import' in text
    assert 'Reference checked: ONEU2154315' in text
    assert 'not mean the booking is invalid' in text


def test_sync_covers_every_carrier_failure_and_keeps_batch_running(monkeypatch):
    client, _, _, _, _ = conflict_client(monkeypatch)
    client.settings.shipment_preflight_enabled = False
    class Adapter:
        def fetch_status(self, ref): raise ValueError('returned no events')
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry', lambda: {'maersk':Adapter(), 'cma-cgm':Adapter()})
    calls=[]
    def report(ref, **kwargs):
        calls.append((ref.task_id, kwargs['phase']))
        raise requests.Timeout('comment delivery uncertain')
    monkeypatch.setattr(client, 'report_tracking_failure', report)
    refs=[shipment(), shipment()]
    refs[0].shipping_line='maersk'; refs[1].shipping_line='cma-cgm'; refs[1].task_id='next'
    stats = _run_sync(client, refs)
    assert stats.skipped == 2 and calls == [('t','lookup'), ('next','lookup')]


def test_preflight_and_circuit_breaker_skips_get_honest_comments(monkeypatch):
    client, _, _, _, _ = conflict_client(monkeypatch)
    client.settings.shipment_preflight_enabled = True
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry', lambda: {'msc':object()})
    monkeypatch.setattr('shipment_sync.sync._preflight_supported_lines', lambda *args: {'msc':'DNS unavailable'})
    calls=[]
    monkeypatch.setattr(client,'report_tracking_failure',lambda ref, **kwargs: calls.append(kwargs) or True)
    ref=shipment(); ref.shipping_line='msc'
    assert _run_sync(client,[ref]).skipped == 1
    assert calls[0]['phase'] == 'preflight'
    notice = classify_failure(ref, 'DNS unavailable', phase='preflight')
    assert 'not queried' in failure_comment(ref, notice, method='automated', phase='preflight')
    client.settings.shipment_preflight_enabled = False
    class Blocked:
        def fetch_status(self, ref): raise ValueError('MSC page access denied in browser session')
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry', lambda: {'msc':Blocked()})
    second=shipment(); second.shipping_line='msc'; second.task_id='next'
    with pytest.raises(RuntimeError, match='Carrier access failure'):
        _run_sync(client,[ref,second])
    assert calls[-1]['phase'] == 'not_queried'
    blocked=classify_failure(ref,'access denied')
    assert 'not a shipment-specific rejection' in failure_comment(ref, blocked, method='automated', phase='not_queried')


def test_browser_preview_never_comments_and_apply_failure_does(monkeypatch):
    from shipment_sync.msc_browser_assisted import MscBrowserFailure
    from shipment_sync.msc_browser_assisted_main import _import_batch
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = 'msc'
    ref=shipment(); ref.shipping_line='msc'
    failure=MscBrowserFailure(ref.task_id, 'ONEU2154315', 'no result')
    _import_batch(client,[ref],[],[failure],apply=False)
    assert not posts
    _import_batch(client,[ref],[],[failure],apply=True)
    assert len(posts) == 1


def test_legacy_one_conflict_comment_remains_suppressed_during_upgrade(monkeypatch):
    client, _, history, posts, _ = conflict_client(monkeypatch)
    ref = shipment()
    fingerprint = hashlib.sha256(json.dumps([ref.task_id, ref.booking_no, ref.container_no,
        [12,34]], separators=(',', ':')).encode()).hexdigest()[:24]
    history.append({'id':'legacy','date':str(int(datetime.now(timezone.utc).timestamp()*1000)),
                    'comment_text': f'ONE T&T booking conflict [{fingerprint}]'})
    assert not client.report_one_booking_conflict(ref)
    assert not posts


def test_comment_outage_pauses_after_three_failures_without_blind_posts(monkeypatch):
    client, _, _, posts, _ = conflict_client(monkeypatch)
    calls=[]
    def unavailable(*args, **kwargs):
        calls.append(1)
        raise requests.Timeout('ClickUp unavailable')
    monkeypatch.setattr(client, '_request', unavailable)
    for _ in range(3):
        with pytest.raises(requests.Timeout):
            client.report_tracking_failure(shipment(), error='no result')
    assert not client.report_tracking_failure(shipment(), error='no result')
    assert len(calls) == 3 and not posts


def test_correction_during_history_read_prevents_stale_comment(monkeypatch):
    client, task, _, posts, _ = conflict_client(monkeypatch)
    original = client._request
    def request(method, url, **kwargs):
        result = original(method, url, **kwargs)
        if url.endswith('/comment'):
            task['assignees'] = [{'id':99}]
        return result
    monkeypatch.setattr(client, '_request', request)
    assert not client.report_tracking_failure(shipment(), error='no result')
    assert not posts


def test_browser_uncertain_write_posts_warning_and_does_not_replay(monkeypatch):
    from shipment_sync.msc_browser_assisted import MscBrowserCapture
    from shipment_sync.msc_browser_assisted_main import _import_batch
    client, task, _, posts, _ = conflict_client(monkeypatch)
    task['custom_fields'][2]['value'] = 'msc'
    ref = shipment(); ref.shipping_line = 'msc'
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.status_from_browser_capture',
                        lambda text: ShipmentStatus('transito'))
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.consolidate_browser_statuses',
                        lambda ref, entries: entries[0][1])
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *args:
                        ShipmentUpdatePlan(True,'transito','hash'))
    calls=[]
    def write(*args):
        calls.append(1)
        raise requests.Timeout('ambiguous write')
    monkeypatch.setattr(client, 'update_shipment_status', write)
    _import_batch(client,[ref],[MscBrowserCapture(ref.task_id,'visible capture')],[],apply=True)
    assert calls == [1] and len(posts) == 1
    assert 'Some fields or comments may already have been saved' in posts[0]['comment'][0]['text']


def test_task_status_write_failure_notifies_without_claiming_complete_success(monkeypatch):
    client, _, _, posts, _ = conflict_client(monkeypatch)
    plan = ShipmentUpdatePlan(True,'transito','hash',task_status_update='transito')
    monkeypatch.setattr(client, 'plan_shipment_update', lambda *args: plan)
    def fail(*args): raise requests.Timeout('status update uncertain')
    monkeypatch.setattr(client, '_set_task_status', fail)
    with pytest.raises(requests.Timeout, match='status update uncertain'):
        client.update_shipment_status(shipment(), ShipmentStatus('transito'))
    assert len(posts) == 1
    assert 'Write outcome is uncertain' in posts[0]['comment'][0]['text']


def test_each_failed_browser_reference_is_visible_with_its_own_cooldown(monkeypatch):
    client, task, history, posts, _ = conflict_client(monkeypatch)
    ref=shipment(); ref.shipping_line='msc'
    task['custom_fields'][2]['value']='msc'
    assert client.report_msc_tracking_failure(ref, reference='ONEU2154315', error='no result')
    history.append({'id':'1','date':str(int(datetime.now(timezone.utc).timestamp()*1000)),
                    'comment':posts[0]['comment']})
    assert client.report_msc_tracking_failure(ref, reference='MSCU1234567', error='no result')
    history.append({'id':'2','date':str(int(datetime.now(timezone.utc).timestamp()*1000)),
                    'comment':posts[1]['comment']})
    assert not client.report_msc_tracking_failure(ref, reference=' mscu1234567 ', error='no result')
    assert len(posts)==2
    assert 'Reference checked: MSCU1234567' in posts[1]['comment'][0]['text']


def test_read_only_planning_failure_keeps_its_type_and_notifies_as_projection(monkeypatch):
    client, _, _, posts, _ = conflict_client(monkeypatch)
    client.settings.shipment_preflight_enabled=False
    class Adapter:
        def fetch_status(self, ref): return ShipmentStatus('transito')
    monkeypatch.setattr('shipment_sync.sync.build_carrier_registry',lambda:{'one':Adapter()})
    def invalid_plan(*args): raise ValueError('destination evidence missing')
    monkeypatch.setattr(client,'plan_shipment_update',invalid_plan)
    with pytest.raises(ValueError) as caught:
        client.update_shipment_status(shipment(),ShipmentStatus('transito'))
    assert caught.value.shipment_sync_phase=='projection'
    assert _run_sync(client,[shipment()]).skipped==1
    text=posts[0]['comment'][0]['text']
    assert 'failure stage: projection' in text
    assert 'Some fields or comments may already have been saved' not in text
    assert 'did not write shipment fields' in text


def test_browser_projection_error_is_retained_in_private_report(monkeypatch,tmp_path,capsys):
    from shipment_sync.msc_browser_assisted import MscBrowserCapture
    from shipment_sync.msc_browser_assisted_main import _import_batch
    client, task, _, posts, _ = conflict_client(monkeypatch)
    ref=shipment(); ref.shipping_line='msc'; task['custom_fields'][2]['value']='msc'
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.status_from_browser_capture',lambda text:ShipmentStatus('transito'))
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.consolidate_browser_statuses',lambda ref,entries:entries[0][1])
    def invalid_plan(*args): raise ValueError('private projection diagnostic')
    monkeypatch.setattr(client,'plan_shipment_update',invalid_plan)
    path=tmp_path/'private.jsonl'
    _import_batch(client,[ref],[MscBrowserCapture(ref.task_id,'capture')],[],apply=True,report_path=path)
    row=json.loads(path.read_text().splitlines()[0])
    assert row['outcome']=='projection_rejected'
    assert row['reason']=='private projection diagnostic' and row['exception_type']=='ValueError'
    assert 'private projection diagnostic' in capsys.readouterr().out
    assert 'private projection diagnostic' not in posts[0]['comment'][0]['text']


def test_accepted_capture_plus_failed_reference_is_not_an_uncertain_write(monkeypatch):
    from shipment_sync.msc_browser_assisted import MscBrowserCapture,MscBrowserFailure
    from shipment_sync.msc_browser_assisted_main import _import_batch
    client,task,_,posts,_=conflict_client(monkeypatch)
    ref=shipment(); ref.shipping_line='msc'; task['custom_fields'][2]['value']='msc'
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.status_from_browser_capture',lambda text:ShipmentStatus('transito'))
    monkeypatch.setattr('shipment_sync.msc_browser_assisted_main.consolidate_browser_statuses',lambda ref,entries:entries[0][1])
    monkeypatch.setattr(client,'plan_shipment_update',lambda *args:ShipmentUpdatePlan(True,'transito','hash'))
    def accepted(*args):
        task['custom_fields'].append({'id':client.settings.cf_status_last_checked,
            'value':str(int(datetime.now(timezone.utc).timestamp()*1000))})
        return ShipmentWriteResult(True,'transito','hash')
    monkeypatch.setattr(client,'update_shipment_status',accepted)
    _import_batch(client,[ref],[MscBrowserCapture(ref.task_id,'capture')],
        [MscBrowserFailure(ref.task_id,'MSCU1234567','no result')],apply=True)
    assert len(posts)==1
    text=posts[0]['comment'][0]['text']
    assert 'partial_browser_batch' in text and 'Reference checked: MSCU1234567' in text
    assert 'not evidence that an accepted ClickUp write failed' in text
    assert 'Write outcome is uncertain' not in text
    assert 'did not write shipment fields' not in text
