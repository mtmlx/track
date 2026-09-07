from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
import requests

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter
from shipment_sync.cma_cgm_comparison import (
    CmaCgmComparisonRunner,
    CmaCgmComparisonSettings,
    run_cma_cgm_comparison_from_clickup,
)
from shipment_sync.models import ShipmentRef


def _response(payload: object, *, next_page: str | None = None) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.url = "https://api.example.test/events"
    response.headers["Content-Type"] = "application/json"
    if next_page is not None:
        response.headers["Next-Page"] = next_page
    response._content = json.dumps(payload).encode("utf-8")
    response._content_consumed = True
    return response


class _SequenceSession:
    def __init__(self, responses: list[requests.Response]) -> None:
        self.responses = list(responses)
        self.get_calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> requests.Response:
        self.get_calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


class _InventoryClient:
    def __init__(self, shipments: list[ShipmentRef]) -> None:
        self.shipments = shipments
        self.list_calls = 0
        self.require_carrier_prefilter = False

    def list_shipments(self, *, require_carrier_prefilter: bool = False) -> list[ShipmentRef]:
        self.list_calls += 1
        self.require_carrier_prefilter = require_carrier_prefilter
        return self.shipments


def _shipment(*, status: str | None = None) -> ShipmentRef:
    return ShipmentRef(
        task_id="task-cma-1",
        task_name="CMA comparison fixture",
        shipping_line="CMA CGM",
        booking_no="BOOK-1",
        container_no="CMAU1234567",
        list_id="list-1",
        current_task_status=status,
    )


def _equipment_load() -> dict[str, object]:
    return {
        "eventID": "CMA-LOAD-1",
        "eventCreatedDateTime": "2026-01-02T09:00:00Z",
        "eventDateTime": "2026-01-02T08:00:00Z",
        "eventType": "EQUIPMENT",
        "eventClassifierCode": "ACT",
        "equipmentEventTypeCode": "LOAD",
        "equipmentReference": "CMAU1234567",
        "emptyIndicatorCode": "LADEN",
        "eventLocation": {"UNLocationCode": "CNSHA"},
    }


def _arrival() -> dict[str, object]:
    return {
        "eventID": "CMA-ARRI-2",
        "eventCreatedDateTime": "2099-01-04T09:00:00Z",
        "eventDateTime": "2099-01-04T08:00:00Z",
        "eventType": "TRANSPORT",
        "eventClassifierCode": "EST",
        "transportEventTypeCode": "ARRI",
        "transportCall": {
            "transportCallID": "call-2",
            "modeOfTransport": "VESSEL",
            "location": {"UNLocationCode": "GTPRQ"},
            "vessel": {"vesselName": "CMA FINAL", "vesselIMONumber": "1234567"},
            "exportVoyageNumber": "002E",
        },
    }


def _configure_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMA_CGM_TRACKING_API_URL", "https://api.example.test/events")
    monkeypatch.setenv("CMA_CGM_API_KEY", "test-key")
    monkeypatch.setenv("CMA_CGM_DCSA_MAX_PAGES", "3")


def _settings(
    *,
    terminal_statuses: tuple[str, ...] = (),
    shipment_allowed_lines: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        shipment_terminal_statuses=terminal_statuses,
        shipment_allowed_lines=shipment_allowed_lines if shipment_allowed_lines is not None else ["cma cgm"],
    )


def test_cma_dcsa_fetch_follows_documented_cursor_without_changing_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_api(monkeypatch)
    adapter = CmaCgmAdapter()
    session = _SequenceSession(
        [
            _response([_equipment_load()], next_page="opaque-cursor"),
            _response([_arrival()]),
        ]
    )
    adapter.session = session  # type: ignore[assignment]

    events, source_url = adapter.fetch_dcsa_events(_shipment())

    assert source_url == "https://api.example.test/events"
    assert [event["eventID"] for event in events] == ["CMA-LOAD-1", "CMA-ARRI-2"]
    assert [call["params"] for call in session.get_calls] == [
        {"equipmentReference": "CMAU1234567"},
        {"equipmentReference": "CMAU1234567", "cursor": "opaque-cursor"},
    ]


def test_cma_dcsa_fetch_rejects_cross_origin_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_api(monkeypatch)
    adapter = CmaCgmAdapter()
    adapter.session = _SequenceSession([_response([_equipment_load()], next_page="https://other.example.test/events?cursor=x")])  # type: ignore[assignment]

    with pytest.raises(ValueError, match="different origin"):
        adapter.fetch_dcsa_events(_shipment())


def test_cma_comparison_reports_first_page_legacy_delta_against_all_dcsa_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_api(monkeypatch)
    adapter = CmaCgmAdapter()
    adapter.session = _SequenceSession(
        [
            _response([_equipment_load()], next_page="?cursor=page-2"),
            _response([_arrival()]),
        ]
    )  # type: ignore[assignment]
    runner = CmaCgmComparisonRunner(
        settings=_settings(),  # type: ignore[arg-type]
        comparison_settings=CmaCgmComparisonSettings(enabled=True, max_shipments=5),
        adapter=adapter,
        now=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )

    summary = runner.run([_shipment()])

    assert summary.compared_shipments == 1
    assert summary.matching_shipments == 0
    assert summary.differing_shipments == 1
    assert summary.pages_fetched == 2
    result = summary.results[0]
    assert result.differing_fields == ("latest_event_code", "latest_event_at", "eta_time", "vessel_voyage")
    assert result.legacy.as_dict() == {
        "container_count": 1,
        "latest_event_code": "LOAD",
        "latest_event_at": "2026-01-02T08:00:00+00:00",
        "eta_time": None,
        "vessel_voyage": None,
        "event_count": 1,
    }
    assert result.dcsa.as_dict() == {
        "container_count": 1,
        "latest_event_code": "ARRI",
        "latest_event_at": "2099-01-04T08:00:00+00:00",
        "eta_time": "2099-01-04T08:00:00+00:00",
        "vessel_voyage": "CMA FINAL 002E",
        "event_count": 2,
    }
    assert result.conformance_warnings == ("carrier-event-id-not-uuid",)


def test_cma_comparison_reads_inventory_only_and_skips_terminal_shipments(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_api(monkeypatch)
    adapter = CmaCgmAdapter()
    inventory = _InventoryClient([_shipment(status="Cancelado")])

    summary = run_cma_cgm_comparison_from_clickup(
        settings=_settings(terminal_statuses=("cancelado",)),  # type: ignore[arg-type]
        comparison_settings=CmaCgmComparisonSettings(enabled=True, max_shipments=5),
        client=inventory,  # type: ignore[arg-type]
        adapter=adapter,
    )

    assert inventory.list_calls == 1
    assert inventory.require_carrier_prefilter is True
    assert summary.selected_shipments == 0
    assert summary.skipped_terminal == 1
    assert summary.source_failures == 0


def test_cma_comparison_settings_require_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CMA_CGM_COMPARISON_ENABLED", raising=False)

    with pytest.raises(ValueError, match="CMA comparison is disabled"):
        CmaCgmComparisonSettings.from_env(require_enabled=True)


def test_cma_comparison_requires_an_exact_single_carrier_inventory_scope() -> None:
    inventory = _InventoryClient([])

    with pytest.raises(ValueError, match="SHIPMENT_ALLOWED_LINES=cma cgm"):
        run_cma_cgm_comparison_from_clickup(
            settings=_settings(shipment_allowed_lines=["cma cgm", "maersk"]),  # type: ignore[arg-type]
            comparison_settings=CmaCgmComparisonSettings(enabled=True, max_shipments=5),
            client=inventory,  # type: ignore[arg-type]
        )

    assert inventory.list_calls == 0
