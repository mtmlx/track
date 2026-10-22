from __future__ import annotations

import json

import pytest
import requests

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter
from shipment_sync.models import ShipmentRef


def _response(payload: object, *, status_code: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://example.test"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    return response


class _OAuthSession:
    def __init__(self) -> None:
        self.post_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> requests.Response:
        self.post_calls.append({"url": url, **kwargs})
        return _response({"access_token": "cma-access-token", "expires_in": 300})

    def get(self, url: str, **kwargs: object) -> requests.Response:
        self.get_calls.append({"url": url, **kwargs})
        return _response([{
            "equipmentReference": "TGHU1371739", "eventType": "EQUIPMENT",
            "eventClassifierCode": "ACT", "equipmentEventTypeCode": "LOAD",
            "eventDateTime": "2026-01-01T09:00:00Z",
            "eventLocation": {"UNLocationCode": "CNSHA"},
        }])


def _configure_cma_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMA_CGM_API_BASE_URL", "https://apis.cma-cgm.net/operation/trackandtrace/v1")
    monkeypatch.setenv("CMA_CGM_API_METHOD", "searchMoveOnCommercialCycle")
    monkeypatch.setenv("CMA_CGM_API_METHOD_PATH", "/events")
    monkeypatch.setenv("CMA_CGM_OAUTH_TOKEN_URL", "https://auth.cma-cgm.com/as/token.oauth2")
    monkeypatch.setenv("CMA_CGM_OAUTH_CLIENT_ID", "test-client")
    monkeypatch.setenv("CMA_CGM_OAUTH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("CMA_CGM_OAUTH_SCOPE", "tandtcommercial:read:be")


def test_cma_oauth_api_uses_cached_token_without_legacy_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cma_api(monkeypatch)
    monkeypatch.setenv("CMA_CGM_API_KEY", "legacy-key")
    adapter = CmaCgmAdapter()
    session = _OAuthSession()
    adapter.session = session  # type: ignore[assignment]

    adapter._fetch_payload(reference="TGHU1371739", ref_type="container")
    adapter._fetch_payload(reference="TGHU1371739", ref_type="container")

    assert len(session.post_calls) == 1
    assert session.post_calls[0]["url"] == "https://auth.cma-cgm.com/as/token.oauth2"
    assert session.post_calls[0]["auth"] == ("test-client", "test-secret")
    assert session.post_calls[0]["data"] == {
        "grant_type": "client_credentials",
        "scope": "tandtcommercial:read:be",
    }

    assert len(session.get_calls) == 2
    request = session.get_calls[0]
    assert request["url"] == "https://apis.cma-cgm.net/operation/trackandtrace/v1/events"
    assert request["params"] == {"equipmentReference": "TGHU1371739"}
    assert request["headers"] == {"Authorization": "Bearer cma-access-token"}


def test_cma_api_configuration_never_falls_back_to_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_cma_api(monkeypatch)
    monkeypatch.setenv("CMA_CGM_USE_PLAYWRIGHT", "true")
    adapter = CmaCgmAdapter()
    session = _OAuthSession()
    adapter.session = session  # type: ignore[assignment]

    def fail_playwright(**_: object) -> None:
        raise AssertionError("configured CMA API must not select Playwright")

    monkeypatch.setattr(adapter, "_fetch_status_playwright", fail_playwright)
    status = adapter.fetch_status(
        ShipmentRef(
            task_id="task-1",
            task_name="CMA shipment",
            shipping_line="cma - cgm",
            booking_no=None,
            container_no="TGHU1371739",
            list_id="list-1",
        )
    )

    assert status.raw_source == "cma-api:https://apis.cma-cgm.net/operation/trackandtrace/v1/events"
    assert len(session.get_calls) == 1
