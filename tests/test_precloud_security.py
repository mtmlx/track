from __future__ import annotations

import csv
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
import requests
from fastapi.testclient import TestClient

from shipment_sync import clickup_oauth_main as oauth
from shipment_sync.api import create_app
from shipment_sync.carriers.one import OneAdapter
from shipment_sync.contacts_sync import run_contacts_sync
from shipment_sync.docuseal_client import DocuSealClient
from shipment_sync.linkedin_copilot_main import (
    DraftRecord, FilteredOutRecord, _write_review_csv, _write_filtered_csv, _build_approved_queue,
)


@pytest.mark.parametrize("route,body", [
    ("/api/documents/nda", {"counterparty": {"company_name": "Acme"},
      "counterparty_signer": {"name": "A", "email": "a@example.test"},
      "mtm_signer": {"name": "B", "email": "b@example.test"}}),
    ("/api/webhooks/clickup/credit-contract", {"task_id": "task", "customer_company_name": "Acme",
      "signer_name": "A", "signer_email": "a@example.test"}),
])
def test_document_auth_blocks_mutations_and_preserves_authorized_dry_run(monkeypatch, route, body):
    calls = []
    monkeypatch.setattr(DocuSealClient, "create_submission", lambda self, data: calls.append(data) or [])
    monkeypatch.setenv("DOCUSEAL_API_KEY", "test-key")
    monkeypatch.setenv("DOCUSEAL_NDA_TEMPLATE_ID", "1")
    monkeypatch.setenv("DOCUSEAL_CREDIT_CONTRACT_TEMPLATE_ID", "2")
    monkeypatch.setenv("SHIPMENT_API_ALLOW_QUERY_TOKEN", "false")
    client = TestClient(create_app())
    for token in ("", "   "):
        monkeypatch.setenv("SHIPMENT_API_TRIGGER_TOKEN", token)
        assert client.post(route, json={**body, "dry_run": False}).status_code == 503
    monkeypatch.setenv("SHIPMENT_API_TRIGGER_TOKEN", "operator")
    assert client.post(route, json=body).status_code == 401
    assert client.post(route, json=body, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert client.post(route + "?token=operator", json=body).status_code == 401
    for headers in ({"Authorization": "Bearer operator"}, {"X-Trigger-Token": "operator"}):
        response = client.post(route, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["dry_run"] is True
    assert calls == []
    monkeypatch.setenv("SHIPMENT_API_ALLOW_QUERY_TOKEN", "true")
    assert client.post(route + "?token=operator", json=body).status_code == 200
    response = client.post(route, json={**body, "dry_run": False}, headers={"X-Trigger-Token": "operator"})
    assert response.status_code == 200
    assert len(calls) == 1


def callback(query, holder=None):
    holder = holder if holder is not None else {"code": None, "error": None}
    handler = object.__new__(oauth._build_handler("/callback", holder, expected_state="nonce"))
    handler.path = "/callback?" + query
    handler.wfile = BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.do_GET()
    return statuses[0], holder, handler.wfile.getvalue()


@pytest.mark.parametrize("query", ["code=evil", "state=wrong&code=evil", "state=&code=evil",
                                        "state=nonce&state=wrong&code=evil", "state=%C3%A9&code=evil"])
def test_oauth_rejects_unbound_callbacks(query):
    status, holder, _ = callback(query)
    assert status == 400
    assert holder == {"code": None, "error": None}


def test_oauth_valid_callback_and_escaped_error():
    assert callback("state=nonce&code=valid")[1]["code"] == "valid"
    status, _, body = callback(urlencode({"state": "nonce", "error_description": '<script>alert("x")</script>'}))
    assert status == 200
    assert b"<script>" not in body
    assert b"&lt;script&gt;" in body
    config = oauth.OAuthConfig("client", "secret", "http://localhost:8080/callback", Path("unused"))
    assert "state=nonce" in oauth._build_auth_url(config, state="nonce")


def test_oauth_invalid_request_does_not_end_wait(monkeypatch):
    instances = []
    class Server:
        def __init__(self, address, handler):
            self.handler = handler
            self.calls = 0
            self.closed = False
            instances.append(self)
        def handle_request(self):
            self.calls += 1
            obj = object.__new__(self.handler)
            obj.path = "/callback?state=" + ("wrong" if self.calls == 1 else "nonce") + "&code=valid"
            obj.wfile = BytesIO()
            obj.send_response = lambda *args: None
            obj.send_header = lambda *args: None
            obj.end_headers = lambda: None
            obj.do_GET()
        def server_close(self):
            self.closed = True
    monkeypatch.setattr(oauth, "HTTPServer", Server)
    config = oauth.OAuthConfig("client", "secret", "http://localhost:8080/callback", Path("unused"))
    assert oauth._capture_code(config, "unused", state="nonce", timeout_seconds=2, open_browser=False) == "valid"
    assert instances[0].calls == 2 and instances[0].closed


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("formula", ["=1+1", " +1+1", "\t@SUM(1)", "\r-1+2"])
def test_all_csv_writers_keep_cells_literal_and_approval_roundtrip(tmp_path, formula):
    plain = 'María, "Logix"'
    row = DraftRecord("1", plain, "https://linkedin.com/in/test", 42, "match", [], formula,
                      "Director", None, "Hello", "Thanks")
    review = tmp_path / "review.csv"
    _write_review_csv(selected=[row], destination=review)
    values = read_csv(review)
    assert values[0]["company"] == "'" + formula
    assert values[0]["full_name"] == plain
    assert row.company == formula
    filtered = tmp_path / "filtered.csv"
    _write_filtered_csv(records=[FilteredOutRecord("1", plain, row.linkedin_url, 42, "reason", formula, None, None)], destination=filtered)
    assert read_csv(filtered)[0]["company"] == "'" + formula
    values[0].update(approved="yes", edited_message=formula, notes=formula)
    with review.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)
    approved = _build_approved_queue(review, output_dir=tmp_path, timestamp="test")
    result = read_csv(approved)[0]
    assert result["message_to_send"] == "'" + formula.strip()
    assert result["notes"] == "'" + formula.strip()
    assert result["full_name"] == plain


def test_contact_terminal_output_is_literal_without_changing_input(capsys):
    name = "María\x1b]52;c;secret\x07\r\x9b"
    contact = SimpleNamespace(full_name=name, email=None, phone=None, task_id="task")
    client = SimpleNamespace(list_contacts=lambda: [contact])
    run_contacts_sync(client, None, dry_run=True)
    output = capsys.readouterr().out
    assert "María" in output and "\\x1b" in output
    assert all(c not in output for c in ("\x1b", "\x07", "\r", "\x9b"))
    assert contact.full_name == name


@pytest.mark.parametrize("method", ["search", "voyage", "events"])
@pytest.mark.parametrize("mode", ["declared", "streamed", "invalid-json", "http-error", "valid"])
def test_one_edh_bounds_and_closes_every_response(monkeypatch, method, mode):
    monkeypatch.setenv("CARRIER_RESPONSE_MAX_BYTES", "32")
    class Response:
        headers = {"Content-Length": "999"} if mode == "declared" else {}
        encoding = "utf-8"
        _content_consumed = False
        closed = False
        consumed = False
        def raise_for_status(self):
            if mode == "http-error":
                raise ValueError("http error")
        def iter_content(self, chunk_size):
            self.consumed = True
            yield b"x" * 33 if mode == "streamed" else b"oops" if mode == "invalid-json" else b'{"data":[]}'
        def close(self):
            self.closed = True
        def json(self):
            pytest.fail("Unbounded JSON parser called")
    response = Response()
    def request(*args, **kwargs):
        assert kwargs["stream"] is True
        return response
    adapter = OneAdapter()
    adapter.session = SimpleNamespace(get=request, post=request)
    if method == "search":
        adapter._fetch_status_from_edh("container", adapter.container_type_code)
    elif method == "voyage":
        adapter._fetch_voyage_list("booking")
    else:
        adapter._fetch_recent_moves("booking", "container")
    assert response.closed
    if mode in {"declared", "http-error"}:
        assert not response.consumed


@pytest.mark.parametrize("method", ["search", "voyage", "events"])
@pytest.mark.parametrize("declared", [True, False])
def test_one_redirect_body_is_bounded_before_requests_consumes_it(monkeypatch, method, declared):
    monkeypatch.setenv("CARRIER_RESPONSE_MAX_BYTES", "32")
    class Transport(requests.adapters.BaseAdapter):
        def __init__(self):
            self.raw = BytesIO(b"x" * 100)
            self.calls = 0
        def send(self, request, **kwargs):
            self.calls += 1
            response = requests.Response()
            response.status_code = 302
            response.url = request.url
            response.request = request
            response.headers["Location"] = "https://carrier.test/redirected"
            if declared:
                response.headers["Content-Length"] = "100"
            response.raw = self.raw
            return response
        def close(self):
            pass
    transport = Transport()
    session = requests.Session()
    session.trust_env = False
    session.mount("https://", transport)
    adapter = OneAdapter()
    adapter.session = session
    if method == "search":
        adapter._fetch_status_from_edh("container", adapter.container_type_code)
    elif method == "voyage":
        adapter._fetch_voyage_list("booking")
    else:
        adapter._fetch_recent_moves("booking", "container")
    assert transport.calls == 1
    assert transport.raw.closed
