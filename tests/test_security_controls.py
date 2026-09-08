from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from shipment_sync.api import _require_operator_auth
from shipment_sync.carriers.common import (
    CarrierPayloadLimitError,
    CarrierResponseLimitError,
    bounded_response_bytes,
    extract_first,
)
from shipment_sync.clickup_client import ClickUpClient, ClickUpWorkloadLimitError
from shipment_sync.clickup_fields_main import _spreadsheet_safe_cell
from shipment_sync.playwright_runner import configured_browser_channel
from shipment_sync.pricing_sync import run_bulk_pricing_sync
from shipment_sync.pricing_sync_config import PricingSyncSettings
from shipment_sync.terminal import terminal_safe_text


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _StreamingResponse:
    def __init__(self, chunks: list[bytes], *, content_length: int | None = None) -> None:
        self.headers: dict[str, str] = {}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._chunks = chunks
        self._content_consumed = False
        self.closed = False
        self.encoding = "utf-8"

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        assert chunk_size > 0
        return list(self._chunks)

    def close(self) -> None:
        self.closed = True


def test_carrier_response_declared_size_limit_closes_response() -> None:
    response = _StreamingResponse([b"unused"], content_length=11)

    with pytest.raises(CarrierResponseLimitError):
        bounded_response_bytes(response, max_bytes=10)

    assert response.closed is True


def test_carrier_response_streaming_limit_closes_response() -> None:
    response = _StreamingResponse([b"12345", b"67890"])

    with pytest.raises(CarrierResponseLimitError):
        bounded_response_bytes(response, max_bytes=9)

    assert response.closed is True


def test_payload_traversal_limit_rejects_wide_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARRIER_PAYLOAD_MAX_NODES", "3")

    with pytest.raises(CarrierPayloadLimitError):
        extract_first(["one", "two", "three"], ["missing"])


def test_system_browser_channel_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PLAYWRIGHT_ALLOW_SYSTEM_BROWSER_CHANNEL", raising=False)
    assert configured_browser_channel("chrome", browser_name="chromium") is None

    monkeypatch.setenv("PLAYWRIGHT_ALLOW_SYSTEM_BROWSER_CHANNEL", "true")
    assert configured_browser_channel("chrome", browser_name="chromium") == "chrome"
    assert configured_browser_channel("chrome", browser_name="firefox") is None


def test_spreadsheet_cells_are_literalized_before_export() -> None:
    assert _spreadsheet_safe_cell("=HYPERLINK(\"https://example.test\")") == "'=HYPERLINK(\"https://example.test\")"
    assert _spreadsheet_safe_cell(" +1+1") == "' +1+1"
    assert _spreadsheet_safe_cell("plain text") == "plain text"


def test_terminal_controls_are_rendered_as_text() -> None:
    assert terminal_safe_text("task\x1b]8;;https://example.test\x07name") == (
        "task\\x1b]8;;https://example.test\\x07name"
    )


def test_api_trigger_fails_closed_without_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHIPMENT_API_TRIGGER_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        _require_operator_auth()

    assert exc_info.value.status_code == 503


def test_clickup_task_page_budget_stops_unbounded_pagination() -> None:
    class _Settings:
        clickup_auth_header_value = "token"
        cf_shipping_line = "shipping-line"
        clickup_max_pages_per_list = 1
        clickup_max_tasks_per_list = 100

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"tasks": [{"id": "task-1"}], "last_page": False}

    client = ClickUpClient(_Settings())  # type: ignore[arg-type]
    client._request = lambda *args, **kwargs: _Response()  # type: ignore[method-assign]

    with pytest.raises(ClickUpWorkloadLimitError, match="CLICKUP_MAX_PAGES_PER_LIST"):
        client._fetch_tasks_page_loop(
            list_id="list-1",
            archived=False,
            carrier_filter_value=None,
        )


def test_ambiguous_quote_match_is_skipped_without_writes() -> None:
    shipment_task = {
        "id": "shipment-1",
        "custom_id": "MTMLXGT-1",
        "name": "Shipment",
        "custom_fields": [{"id": "booking", "name": "MTM Booking", "value": "BOOK-1"}],
    }
    quote_tasks = [
        {
            "id": "quote-1",
            "custom_id": "MTMQUOTE-1",
            "name": "Quote one",
            "custom_fields": [{"id": "shipment-associated", "name": "Shipment associated", "value": "BOOK-1"}],
        },
        {
            "id": "quote-2",
            "custom_id": "MTMQUOTE-2",
            "name": "Quote two",
            "custom_fields": [{"id": "shipment-associated", "name": "Shipment associated", "value": "BOOK-1"}],
        },
    ]

    class _Client:
        def list_tasks(self, list_ids: list[str]) -> list[dict[str, object]]:
            return [shipment_task] if list_ids == ["shipment-list"] else quote_tasks

        def update_custom_field(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("An ambiguous match must not write ClickUp fields")

    settings = PricingSyncSettings(
        clickup_api_token="token",
        clickup_oauth_access_token=None,
        clickup_team_id="8451352",
        clickup_shipment_list_id="shipment-list",
        clickup_shipment_list_ids=["shipment-list"],
        clickup_pricing_list_id="quote-list",
        clickup_pricing_list_ids=["quote-list"],
        clickup_pricing_match_field="MTM Quote #",
        clickup_pricing_shipment_match_fields=["MTM Booking"],
        clickup_pricing_quote_match_fields=["Shipment associated"],
        clickup_pricing_copy_fields=None,
        clickup_pricing_only_empty_targets=True,
        clickup_pricing_set_quote_number=True,
    )

    result = run_bulk_pricing_sync(_Client(), settings, dry_run=False, overwrite_existing=False)

    assert result["shipments_matched"] == 0
    assert result["shipments_updated"] == 0
    assert result["shipments_skipped"] == 1
    assert result["results"][0]["match_selector"] == "MTM Booking"
    assert result["results"][0]["skip_reason"] == "ambiguous quote match for MTM Booking=BOOK-1"


def test_deployment_build_uses_pinned_and_hashed_inputs() -> None:
    dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    lockfile = (REPOSITORY_ROOT / "requirements.lock").read_text(encoding="utf-8")
    build_lockfile = (REPOSITORY_ROOT / "build-requirements.lock").read_text(encoding="utf-8")

    assert "@sha256:" in dockerfile
    assert "--require-hashes -r /app/requirements.lock" in dockerfile
    assert "apt-get upgrade" not in dockerfile
    assert "requirements-os.lock" in dockerfile
    os_pins = (REPOSITORY_ROOT / "requirements-os.lock").read_text().splitlines()
    assert all("=" in line and "*" not in line for line in os_pins if line and not line.startswith("#"))
    assert "playwright install" not in dockerfile
    assert "--hash=sha256:" in lockfile
    assert "--hash=sha256:" in build_lockfile


def test_lightsail_api_is_loopback_only() -> None:
    compose = (REPOSITORY_ROOT / "deploy/aws-lightsail/docker-compose.yml").read_text(encoding="utf-8")
    port_lines = [line.strip() for line in compose.splitlines() if line.strip().startswith('- "')]

    assert '"127.0.0.1:10000:10000"' in compose
    assert '- "80:10000"' not in port_lines
    assert '- "10000:10000"' not in port_lines
