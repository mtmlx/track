from __future__ import annotations

from dataclasses import dataclass

import pytest

from shipment_sync.config import Settings
from shipment_sync.dcsa_event_ledger import DcsaEventLedger
from shipment_sync.dcsa_shadow import (
    DcsaShadowRunner,
    DcsaShadowSettings,
    run_dcsa_shadow_from_clickup,
)
from shipment_sync.models import ShipmentRef


def _settings(**overrides: object) -> Settings:
    base = dict(
        clickup_api_token="token",
        clickup_oauth_access_token=None,
        clickup_oauth_client_id=None,
        clickup_oauth_client_secret=None,
        clickup_oauth_redirect_uri=None,
        clickup_list_id="list-1",
        clickup_list_ids=["list-1"],
        clickup_team_id=None,
        clickup_space_ids=[],
        clickup_folder_ids=[],
        clickup_discover_from_spaces=False,
        clickup_discover_from_team=False,
        cf_container_no="container-field",
        cf_booking_no="booking-field",
        cf_shipping_line="carrier-field",
        cf_shipment_status=None,
        cf_status_last_checked="last-checked-field",
        cf_track_trace_snapshot=None,
        cf_eta="eta-field",
        cf_etd="etd-field",
        cf_discharge_date="disc-field",
        cf_gate_in_full="gtin-full-field",
        cf_gate_out_empty="gtot-empty-field",
        cf_gate_out_delivery="gtot-delivery-field",
        cf_gate_in_empty="gtin-empty-field",
    )
    base.update(overrides)
    return Settings(**base)


def _shipment(
    *,
    task_id: str = "task-1",
    status: str | None = None,
    shipping_line: str = "Maersk",
) -> ShipmentRef:
    return ShipmentRef(
        task_id=task_id,
        task_name="Shadow fixture",
        shipping_line=shipping_line,
        booking_no="BOOK-1",
        container_no=None,
        list_id="list-1",
        current_task_status=status,
    )


def _event(code: str = "PENC") -> dict[str, object]:
    return {
        "eventID": "9e2d710d-67c6-4a8f-b928-5d8ee08ca604",
        "eventCreatedDateTime": "2026-07-31T12:00:00Z",
        "eventDateTime": "2026-07-31T12:00:00Z",
        "eventType": "SHIPMENT",
        "eventClassifierCode": "ACT",
        "shipmentEventTypeCode": code,
        "documentID": "SHAGT3664400",
        "documentTypeCode": "BKG",
    }


@dataclass
class _Source:
    events: list[dict[str, object]]
    carrier: str = "maersk"
    calls: int = 0

    def fetch_events(self, shipment: ShipmentRef) -> tuple[list[dict[str, object]], str]:
        self.calls += 1
        return self.events, "https://api.example.test/events"


class _ReadOnlyInventory:
    def __init__(self, shipments: list[ShipmentRef]) -> None:
        self.shipments = shipments
        self.list_calls = 0

    def list_shipments(self) -> list[ShipmentRef]:
        self.list_calls += 1
        return self.shipments

    def update_task(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - must never be called
        raise AssertionError("DCSA shadow runner must not update ClickUp")


def _shadow_settings(*, version: str = "2.3") -> DcsaShadowSettings:
    return DcsaShadowSettings(enabled=True, max_shipments=10, carrier_versions={"maersk": version})


def test_shadow_runner_records_events_without_clickup_projection(tmp_path) -> None:
    source = _Source(events=[_event()])
    summary = run_dcsa_shadow_from_clickup(
        settings=_settings(),
        shadow_settings=_shadow_settings(),
        ledger=DcsaEventLedger(str(tmp_path / "events.sqlite3")),
        client=_ReadOnlyInventory([_shipment()]),  # type: ignore[arg-type]
        sources={"maersk": source},
    )

    assert source.calls == 1
    assert summary.recorded_events == 1
    assert summary.review_events == 1
    assert summary.halted_events == 0


def test_shadow_runner_skips_native_terminal_status_before_carrier_call(tmp_path) -> None:
    source = _Source(events=[_event()])
    summary = DcsaShadowRunner(
        settings=_settings(),
        shadow_settings=_shadow_settings(),
        ledger=DcsaEventLedger(str(tmp_path / "events.sqlite3")),
        sources={"maersk": source},
    ).run([_shipment(status="Cancelado")])

    assert source.calls == 0
    assert summary.skipped_terminal == 1


def test_shadow_runner_quarantines_an_invalid_batch_without_partial_evidence(tmp_path) -> None:
    source = _Source(events=[_event("CONF"), _event("PENC")])
    ledger = DcsaEventLedger(str(tmp_path / "events.sqlite3"))
    summary = DcsaShadowRunner(
        settings=_settings(),
        shadow_settings=_shadow_settings(version="2.2"),
        ledger=ledger,
        sources={"maersk": source},
    ).run([_shipment()])

    assert summary.validation_failures == 1
    assert summary.validation_failure_reasons == {"unsupported-shipment-event-code": 1}
    assert ledger.list_events() == []


def test_shadow_runner_records_cma_conformance_warnings_without_projection(tmp_path) -> None:
    event = _event("CONF")
    event["eventID"] = "CMA-OPAQUE-EVENT-1"
    source = _Source(events=[event], carrier="cma cgm")
    summary = DcsaShadowRunner(
        settings=_settings(),
        shadow_settings=DcsaShadowSettings(enabled=True, max_shipments=10, carrier_versions={"cma cgm": "2.2"}),
        ledger=DcsaEventLedger(str(tmp_path / "events.sqlite3")),
        sources={"cma cgm": source},
    ).run([_shipment(shipping_line="CMA CGM")])

    assert summary.recorded_events == 1
    assert summary.conformance_warning_events == 1
    assert summary.conformance_warning_codes == {"carrier-event-id-not-uuid": 1}


def test_shadow_configuration_requires_explicit_maersk_contract_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCSA_TNT_SHADOW_ENABLED", "true")
    monkeypatch.setenv("DCSA_TNT_SHADOW_CARRIERS", "maersk")
    monkeypatch.delenv("DCSA_TNT_SHADOW_MAERSK_VERSION", raising=False)

    with pytest.raises(ValueError, match="DCSA_TNT_SHADOW_MAERSK_VERSION"):
        DcsaShadowSettings.from_env(require_enabled=True)


def test_shadow_configuration_defaults_cma_to_its_published_tnt_22_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DCSA_TNT_SHADOW_ENABLED", "true")
    monkeypatch.setenv("DCSA_TNT_SHADOW_CARRIERS", "cma cgm")
    monkeypatch.delenv("DCSA_TNT_SHADOW_CMA_CGM_VERSION", raising=False)

    config = DcsaShadowSettings.from_env(require_enabled=True)

    assert config.carrier_versions == {"cma cgm": "2.2"}
