from __future__ import annotations

import pytest

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter, CmaCgmDcsaEventFetch
from shipment_sync.carriers.maersk import MaerskAdapter
from shipment_sync.models import ShipmentRef


def _shipment(*, container_no: str | None = "MSKU1234567") -> ShipmentRef:
    return ShipmentRef(
        task_id="task-1",
        task_name="DCSA shadow fixture",
        shipping_line="maersk",
        booking_no="BOOK-1",
        container_no=container_no,
        list_id="list-1",
    )


def test_cma_shadow_source_uses_the_official_api_payload_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CMA_CGM_TRACKING_API_URL", "https://api.example.test/events")
    adapter = CmaCgmAdapter()
    requested: list[tuple[str, str]] = []

    def fake_fetch_pages(shipment: ShipmentRef) -> CmaCgmDcsaEventFetch:
        requested.append((shipment.container_no or "", "container"))
        return CmaCgmDcsaEventFetch(
            first_page_payload={"events": [{"eventID": "evt-1"}]},
            events=({"eventID": "evt-1"},),
            source_url="https://api.example.test/events",
            page_count=1,
        )

    monkeypatch.setattr(adapter, "_fetch_dcsa_event_pages", fake_fetch_pages)

    events, source_url = adapter.fetch_dcsa_events(_shipment(container_no="CMAU1234567"))

    assert requested == [("CMAU1234567", "container")]
    assert events == [{"eventID": "evt-1"}]
    assert source_url == "https://api.example.test/events"


def test_cma_shadow_source_refuses_to_use_a_website(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CMA_CGM_TRACKING_API_URL", raising=False)
    monkeypatch.delenv("CMA_CGM_API_BASE_URL", raising=False)
    monkeypatch.delenv("CMA_CGM_API_METHOD", raising=False)
    monkeypatch.delenv("CMA_CGM_API_METHOD_PATH", raising=False)
    adapter = CmaCgmAdapter()

    with pytest.raises(ValueError, match="requires CMA_CGM_TRACKING_API_URL"):
        adapter.fetch_dcsa_events(_shipment())


def test_maersk_shadow_source_requires_explicit_events_mode_and_uses_no_web_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.example.test/events")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "consumer-key")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "bearer-token")
    adapter = MaerskAdapter()
    calls: list[tuple[str, str, dict[str, str]]] = []

    def fake_fetch_all_events(reference: str, ref_type: str, headers: dict[str, str]) -> list[dict[str, str]]:
        calls.append((reference, ref_type, headers))
        return [{"eventID": "evt-1"}]

    def reject_web_fallback(*args: object, **kwargs: object) -> None:
        raise AssertionError("DCSA shadow source must not use Maersk web fallback")

    monkeypatch.setattr(adapter, "_fetch_all_events", fake_fetch_all_events)
    monkeypatch.setattr(adapter, "_try_web_fallback", reject_web_fallback)

    events, source_url = adapter.fetch_dcsa_events(_shipment())

    assert calls == [
        (
            "MSKU1234567",
            "container",
            {"API-Version": "1", "Consumer-Key": "consumer-key", "Authorization": "Bearer bearer-token"},
        )
    ]
    assert events == [{"eventID": "evt-1"}]
    assert source_url == "https://api.example.test/events"


def test_maersk_shadow_source_rejects_implicit_auto_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "auto")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.example.test/events")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "consumer-key")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "bearer-token")
    adapter = MaerskAdapter()

    with pytest.raises(ValueError, match="MAERSK_API_MODE=events"):
        adapter.fetch_dcsa_events(_shipment())
