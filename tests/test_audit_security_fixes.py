from __future__ import annotations

import ast
import csv
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from urllib.parse import urlencode, parse_qs, urlparse

import pytest
from fastapi import HTTPException
from shipment_sync import clickup_oauth_main as oauth
from shipment_sync import document_routes
from shipment_sync import linkedin_copilot_main as linkedin
from shipment_sync.carriers.common import CarrierResponseLimitError
from shipment_sync.carriers.one import OneAdapter
from shipment_sync.carriers.maersk import MaerskAdapter, MaerskCredentialProfile
from shipment_sync.carriers.wan_hai import WanHaiAdapter, _build_reference_attempts
from shipment_sync.models import ShipmentRef, ShipmentStatus


@pytest.mark.parametrize('token,header,code', [('', None, 503), ('secret', None, 401), ('secret', 'wrong', 403), ('secret', 'secret', None)])
def test_document_auth_boundary(monkeypatch, token, header, code):
    monkeypatch.setenv('SHIPMENT_API_TRIGGER_TOKEN', token)
    if code:
        with pytest.raises(HTTPException) as error:
            document_routes._require_operator_auth(x_trigger_token=header)
        assert error.value.status_code == code
    else:
        document_routes._require_operator_auth(x_trigger_token=header)


def callback(holder, query):
    handler = oauth._build_handler('/callback', holder, expected_state='expected')
    request = handler.__new__(handler)
    request.path = '/callback?' + query
    request.wfile = io.BytesIO()
    request.send_response = lambda status: setattr(request, 'status', status)
    request.send_header = lambda *args: None
    request.end_headers = lambda: None
    request.do_GET()
    return request


@pytest.mark.parametrize('query', ['code=attacker', 'state=wrong&code=attacker', 'state=expected&state=wrong&code=attacker', 'state=expected&state=&code=attacker', 'state=%E2%98%83&error=denied'])
def test_oauth_rejects_invalid_state_without_consuming_flow(query):
    holder = {'code': None, 'error': None}
    assert callback(holder, query).status == 400
    assert holder == {'code': None, 'error': None}
    assert callback(holder, 'state=expected&code=legitimate').status == 200
    assert holder['code'] == 'legitimate'


def test_oauth_url_and_error_escaping(tmp_path):
    config = oauth.OAuthConfig('id', 'secret', 'http://localhost:8080/callback', tmp_path / '.env')
    assert parse_qs(urlparse(oauth._build_auth_url(config, state='expected')).query)['state'] == ['expected']
    request = callback({'code': None, 'error': None}, urlencode({'state': 'expected', 'error_description': '<img src=x onerror=alert(1)>'}))
    assert b'<img' not in request.wfile.getvalue()
    assert b'&lt;img' in request.wfile.getvalue()


@pytest.mark.parametrize('payload', ['=1+1', '+SUM(1)', '-1+2', '@SUM(1)', '\t =1', '\r\n+1'])
def test_all_csv_exports_literalize_untrusted_cells(tmp_path, payload):
    values = dict(id='1', full_name=payload, linkedin_url='https://example.test', score=5,
                  company='Ordinary Company', position=payload, connected_on='2026-01-01')
    review = tmp_path / 'review.csv'
    linkedin._write_review_csv(selected=[SimpleNamespace(**values, match_summary=payload, job_matches=[payload], message_draft=payload, comment_draft=payload)], destination=review)
    filtered = tmp_path / 'filtered.csv'
    linkedin._write_filtered_csv(records=[SimpleNamespace(**values, reason=payload)], destination=filtered)
    for path in (review, filtered):
        row = next(csv.DictReader(path.open(newline="")))
        assert row['full_name'] == "'" + payload
        assert row['company'] == 'Ordinary Company'
        assert row['score'] == '5'
    with review.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0].update(approved='yes', edited_message=payload, edited_comment=payload, notes=payload)
    with review.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    approved = linkedin._build_approved_queue(review, output_dir=tmp_path, timestamp='test')
    row = next(csv.DictReader(approved.open(newline="")))
    assert row['full_name'] == "'" + payload  # no double escaping on round trip
    assert row['message_to_send'].startswith("'")
    assert row['comment_to_post'].startswith("'")
    assert row['notes'].startswith("'")


def shipment(containers='ABCD1234567'):
    return ShipmentRef('task', 'shipment', 'wan hai', 'BOOKING', containers, 'list', reference_hints=['OTHER'])


@pytest.mark.parametrize('returned,accepted', [([], False), (['WXYZ1234567'], False), (['ABCD1234567'], True)])
def test_wan_hai_hint_requires_known_container_match(monkeypatch, returned, accepted):
    adapter = WanHaiAdapter()
    adapter.max_retries = 0
    calls = []
    def lookup(*, reference, cargo_type):
        calls.append(reference)
        if reference != 'OTHER':
            raise ValueError('structured lookup unavailable')
        return ShipmentStatus('ETA', discovered_containers=returned)
    monkeypatch.setattr(adapter, '_playwright_request', lookup)
    if accepted:
        assert adapter._fetch_status_playwright(shipment()).status_text == 'ETA'
    else:
        with pytest.raises(ValueError, match='does not match'):
            adapter._fetch_status_playwright(shipment())
    assert calls == ['BOOKING', 'ABCD1234567', 'OTHER']


def test_wan_hai_no_identity_cannot_use_comments(monkeypatch):
    assert _build_reference_attempts(shipment(None)) == [('BOOKING', '2')]
    unknown = shipment(None)
    unknown.booking_no = None
    assert _build_reference_attempts(unknown) == []
    adapter = WanHaiAdapter()
    monkeypatch.setattr(adapter, '_playwright_request', lambda **kwargs: ShipmentStatus('legitimate'))
    assert adapter._fetch_status_playwright(shipment()).status_text == 'legitimate'


class StreamResponse:
    def __init__(self, body, declared=False):
        self.body = body
        self.headers = {'Content-Length': str(len(body))} if declared else {}
        self.closed = False
        self.status_code = 200
    def iter_content(self, chunk_size):
        yield self.body
    def raise_for_status(self):
        pass
    def close(self):
        self.closed = True


@pytest.mark.parametrize('route', ['search', 'voyage', 'events', 'oauth'])
@pytest.mark.parametrize('declared', [True, False])
def test_carrier_limits_at_actual_request_boundaries(monkeypatch, route, declared):
    monkeypatch.setenv('CARRIER_RESPONSE_MAX_BYTES', '16')
    response = StreamResponse(b'x' * 17, declared)
    def request(*args, **kwargs):
        assert kwargs['stream'] is True
        assert kwargs['allow_redirects'] is False
        return response
    adapter = MaerskAdapter() if route == 'oauth' else OneAdapter()
    adapter.session = SimpleNamespace(get=request, post=request)
    with pytest.raises(CarrierResponseLimitError):
        invoke_carrier(adapter, route)
    assert response.closed


def invoke_carrier(adapter, route):
    if route == 'search':
        return adapter._fetch_status_from_edh('BOOKING', adapter.booking_type_code)
    if route == 'voyage':
        return adapter._fetch_voyage_list('BOOKING')
    if route == 'events':
        return adapter._fetch_recent_moves('BOOKING', 'ABCD1234567')
    return adapter._get_oauth_access_token(MaerskCredentialProfile('test', '', '', '', 'https://example.test/token', 'id', 'secret', ''))


def test_maersk_bounded_token_success_and_cache(monkeypatch):
    response = StreamResponse(json.dumps({'access_token': 'token', 'expires_in': 300}).encode())
    adapter = MaerskAdapter()
    calls = []
    def request(*args, **kwargs):
        calls.append(kwargs)
        return response
    adapter.session = SimpleNamespace(post=request)
    assert invoke_carrier(adapter, 'oauth') == 'token'
    assert invoke_carrier(adapter, 'oauth') == 'token'
    assert len(calls) == 1
    assert response.closed


@pytest.mark.parametrize('mode', ['headers', 'stream', 'oversize', 'success'])
def test_msc_javascript_actual_deadline_and_success(mode):
    import shipment_sync.carriers.msc as msc
    tree = ast.parse(Path(msc.__file__).read_text())
    script = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str) and 'async ({ url, token, trackingMode' in n.value)
    harness = '''
const assert = require('node:assert/strict');
const mode = process.argv[1];
const run = SCRIPT;
global.fetch = async () => {
  if (mode === 'headers') return new Promise(() => {});
  let read = false;
  return {status:200, url:'local', headers:{get:()=>null}, body:{getReader:()=>({read:()=> {
    if (mode === 'stream') return new Promise(() => {});
    if (read) return Promise.resolve({done:true});
    read = true;
    return Promise.resolve({done:false,value:new TextEncoder().encode(mode === 'oversize' ? 'x'.repeat(101) : '{}')});
  }})}};
};
(async()=>{
 const args={url:'local',token:'test',trackingMode:'container',reference:'test',maxBytes:100,timeoutMs:20};
 if (mode==='headers'||mode==='stream') await assert.rejects(run(args), /deadline exceeded/);
 else {
  const result=await run(args);
  if(mode==='oversize') assert.equal(result.responseTooLarge,true);
  else assert.equal(result.text,'{}');
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''.replace('SCRIPT', script)
    result = subprocess.run(['node', '-e', harness, mode], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('route', ['search', 'voyage', 'events', 'oauth'])
@pytest.mark.parametrize('status', [302, 307])
def test_redirect_body_is_never_buffered_by_requests(route, status):
    import requests
    class ForbiddenBody(io.BytesIO):
        def read(self, *args, **kwargs):
            pytest.fail('requests buffered a redirect before the byte limit')
    class RedirectAdapter(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = status
            response.headers['Location'] = '/redirect-target'
            response.raw = ForbiddenBody(b'oversized redirect body')
            response.request = request
            response.url = request.url
            return response
        def close(self):
            pass
    adapter = MaerskAdapter() if route == 'oauth' else OneAdapter()
    adapter.session = requests.Session()
    adapter.session.trust_env = False
    adapter.session.mount('https://', RedirectAdapter())
    if route == 'oauth':
        with pytest.raises(ValueError, match='unexpected redirect'):
            invoke_carrier(adapter, route)
    else:
        assert invoke_carrier(adapter, route) in (None, [])


def test_oauth_manual_code_workflow_remains_available(monkeypatch, tmp_path):
    import sys
    config = oauth.OAuthConfig('id', 'secret', 'http://localhost:8080/callback', tmp_path / '.env')
    monkeypatch.setattr(sys, 'argv', ['clickup-oauth', '--code', 'manual-code'])
    monkeypatch.setattr(oauth, '_load_config', lambda: config)
    monkeypatch.setattr(oauth, '_capture_code', lambda *a, **kw: pytest.fail('manual code opened callback server'))
    def exchange(config, code):
        assert code == 'manual-code'
        return 'test-token'
    monkeypatch.setattr(oauth, '_exchange_code', exchange)
    oauth.main()
    assert 'CLICKUP_OAUTH_ACCESS_TOKEN=test-token' in config.env_path.read_text()


def test_oauth_capture_survives_invalid_callback(monkeypatch, tmp_path):
    config = oauth.OAuthConfig('id', 'secret', 'http://localhost:8080/callback', tmp_path / '.env')
    class Server:
        timeout = None
        closed = False
        count = 0
        def __init__(self, address, handler):
            self.handler = handler
        def handle_request(self):
            self.count += 1
            request = self.handler.__new__(self.handler)
            request.path = '/callback?code=valid&state=' + ('wrong' if self.count == 1 else 'expected')
            request.wfile = io.BytesIO()
            request.send_response = lambda *args: None
            request.send_header = lambda *args: None
            request.end_headers = lambda: None
            request.do_GET()
        def server_close(self):
            self.closed = True
    instances = []
    def build(*args):
        instance = Server(*args)
        instances.append(instance)
        return instance
    monkeypatch.setattr(oauth, 'HTTPServer', build)
    assert oauth._capture_code(config, 'https://example.test', state='expected', timeout_seconds=1, open_browser=False) == 'valid'
    assert instances[0].count == 2
    assert instances[0].closed
