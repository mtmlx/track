from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any, Protocol
import unicodedata

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter
from shipment_sync.carriers.maersk import MaerskAdapter
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.dcsa_event_ledger import DcsaEventStore
from shipment_sync.dcsa_tnt import DcsaTntValidationError, normalize_tnt_version, parse_dcsa_tnt_event
from shipment_sync.models import ShipmentRef


_CARRIER_ALIASES = {
    "cma cgm": "cma cgm",
    "cma-cgm": "cma cgm",
    "cma - cgm": "cma cgm",
    "maersk": "maersk",
    "maersk line": "maersk",
    "a.p. moller - maersk": "maersk",
}


class DcsaShadowSource(Protocol):
    carrier: str

    def fetch_events(self, shipment: ShipmentRef) -> tuple[list[dict[str, Any]], str]: ...


@dataclass(frozen=True)
class DcsaShadowSettings:
    enabled: bool
    max_shipments: int
    carrier_versions: dict[str, str]

    @classmethod
    def from_env(cls, *, require_enabled: bool) -> "DcsaShadowSettings":
        enabled = _env_bool("DCSA_TNT_SHADOW_ENABLED", default=False)
        requested_carriers = [_canonical_carrier(value) for value in _csv("DCSA_TNT_SHADOW_CARRIERS")]
        requested_carriers = list(dict.fromkeys(value for value in requested_carriers if value))
        if require_enabled and not enabled:
            raise ValueError("DCSA shadow run is disabled. Set DCSA_TNT_SHADOW_ENABLED=true before using --run.")
        if require_enabled and not requested_carriers:
            raise ValueError("DCSA shadow run requires DCSA_TNT_SHADOW_CARRIERS.")

        versions: dict[str, str] = {}
        for carrier in requested_carriers:
            if carrier == "cma cgm":
                version = normalize_tnt_version(os.getenv("DCSA_TNT_SHADOW_CMA_CGM_VERSION", "2.2"))
                if version != "2.2":
                    raise ValueError(
                        "CMA's currently approved Track & Trace endpoint is TNT 2.2. "
                        "Do not declare it TNT 2.3 until CMA provides an updated contract."
                    )
            elif carrier == "maersk":
                configured_version = os.getenv("DCSA_TNT_SHADOW_MAERSK_VERSION", "").strip()
                if not configured_version:
                    raise ValueError(
                        "Set DCSA_TNT_SHADOW_MAERSK_VERSION after validating the Maersk payload contract; "
                        "the API-Version header is not a TNT version."
                    )
                version = normalize_tnt_version(configured_version)
            else:
                raise ValueError(f"Unsupported DCSA shadow carrier {carrier!r}.")
            versions[carrier] = version

        return cls(
            enabled=enabled,
            max_shipments=_bounded_positive_int("DCSA_TNT_SHADOW_MAX_SHIPMENTS", default=25, maximum=250),
            carrier_versions=versions,
        )


class CmaCgmDcsaShadowSource:
    carrier = "cma cgm"

    def __init__(self, adapter: CmaCgmAdapter | None = None) -> None:
        self.adapter = adapter or CmaCgmAdapter()

    def fetch_events(self, shipment: ShipmentRef) -> tuple[list[dict[str, Any]], str]:
        return self.adapter.fetch_dcsa_events(shipment)


class MaerskDcsaShadowSource:
    carrier = "maersk"

    def __init__(self, adapter: MaerskAdapter | None = None) -> None:
        self.adapter = adapter or MaerskAdapter()

    def fetch_events(self, shipment: ShipmentRef) -> tuple[list[dict[str, Any]], str]:
        return self.adapter.fetch_dcsa_events(shipment)


@dataclass
class DcsaShadowRunSummary:
    candidates: int = 0
    selected_shipments: int = 0
    recorded_events: int = 0
    duplicate_events: int = 0
    halted_events: int = 0
    review_events: int = 0
    no_event_shipments: int = 0
    skipped_terminal: int = 0
    skipped_unsupported: int = 0
    skipped_limit: int = 0
    source_failures: int = 0
    validation_failures: int = 0
    validation_failure_reasons: dict[str, int] = field(default_factory=dict)
    conformance_warning_events: int = 0
    conformance_warning_codes: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int | str | dict[str, int]]:
        return {
            "mode": "shadow",
            "candidates": self.candidates,
            "selected_shipments": self.selected_shipments,
            "recorded_events": self.recorded_events,
            "duplicate_events": self.duplicate_events,
            "halted_events": self.halted_events,
            "review_events": self.review_events,
            "no_event_shipments": self.no_event_shipments,
            "skipped_terminal": self.skipped_terminal,
            "skipped_unsupported": self.skipped_unsupported,
            "skipped_limit": self.skipped_limit,
            "source_failures": self.source_failures,
            "validation_failures": self.validation_failures,
            "validation_failure_reasons": dict(sorted(self.validation_failure_reasons.items())),
            "conformance_warning_events": self.conformance_warning_events,
            "conformance_warning_codes": dict(sorted(self.conformance_warning_codes.items())),
        }


class DcsaShadowRunner:
    """Read carrier events and record them without ever projecting to ClickUp."""

    def __init__(
        self,
        *,
        settings: Settings,
        shadow_settings: DcsaShadowSettings,
        ledger: DcsaEventStore,
        sources: dict[str, DcsaShadowSource] | None = None,
    ) -> None:
        self.settings = settings
        self.shadow_settings = shadow_settings
        self.ledger = ledger
        self.sources = sources or {
            "cma cgm": CmaCgmDcsaShadowSource(),
            "maersk": MaerskDcsaShadowSource(),
        }

    def run(self, shipments: list[ShipmentRef]) -> DcsaShadowRunSummary:
        summary = DcsaShadowRunSummary(candidates=len(shipments))
        for shipment in shipments:
            carrier = _canonical_carrier(shipment.shipping_line)
            if carrier not in self.shadow_settings.carrier_versions or carrier not in self.sources:
                summary.skipped_unsupported += 1
                continue
            if _is_terminal(shipment, self.settings.shipment_terminal_statuses):
                summary.skipped_terminal += 1
                continue
            if summary.selected_shipments >= self.shadow_settings.max_shipments:
                summary.skipped_limit += 1
                continue

            summary.selected_shipments += 1
            try:
                raw_events, source_url = self.sources[carrier].fetch_events(shipment)
            except Exception:
                summary.source_failures += 1
                continue
            if not raw_events:
                summary.no_event_shipments += 1
                continue

            try:
                # Validate the complete shipment batch before creating any event
                # record. A malformed carrier response is quarantined as a unit.
                events = [
                    parse_dcsa_tnt_event(
                        event,
                        carrier=carrier,
                        tnt_version=self.shadow_settings.carrier_versions[carrier],
                        source_url=source_url,
                    )
                    for event in raw_events
                ]
            except DcsaTntValidationError as exc:
                summary.validation_failures += 1
                reason = _validation_failure_reason(exc)
                summary.validation_failure_reasons[reason] = summary.validation_failure_reasons.get(reason, 0) + 1
                continue

            for event in events:
                if event.conformance_warnings:
                    summary.conformance_warning_events += 1
                    for warning in event.conformance_warnings:
                        summary.conformance_warning_codes[warning] = (
                            summary.conformance_warning_codes.get(warning, 0) + 1
                        )
                write = self.ledger.record(event, task_id=shipment.task_id)
                if write.created:
                    summary.recorded_events += 1
                else:
                    summary.duplicate_events += 1
                if write.projection_state == "halted":
                    summary.halted_events += 1
                if write.projection_state == "requires_review":
                    summary.review_events += 1
        return summary


def run_dcsa_shadow_from_clickup(
    *,
    settings: Settings,
    shadow_settings: DcsaShadowSettings,
    ledger: DcsaEventStore,
    client: ClickUpClient | None = None,
    sources: dict[str, DcsaShadowSource] | None = None,
) -> DcsaShadowRunSummary:
    """Load the existing shipment inventory and run a carrier-only shadow pass.

    ``list_shipments`` is the sole ClickUp interaction in this function. The
    runner has no update plan, comment, field-write, or status-write path.
    """

    inventory_client = client or ClickUpClient(settings)
    shipments = inventory_client.list_shipments()
    return DcsaShadowRunner(
        settings=settings,
        shadow_settings=shadow_settings,
        ledger=ledger,
        sources=sources,
    ).run(shipments)


def _canonical_carrier(value: str | None) -> str:
    normalized = " ".join((value or "").strip().lower().split())
    return _CARRIER_ALIASES.get(normalized, normalized)


def _is_terminal(shipment: ShipmentRef, terminal_statuses: tuple[str, ...]) -> bool:
    configured = {_normalize_status(value) for value in terminal_statuses if _normalize_status(value)}
    # Native task status owns workflow state. The custom carrier-status field
    # is used only if a native status is absent on the inventory response.
    current = shipment.current_task_status or shipment.current_status_value
    return _normalize_status(current) in configured


def _normalize_status(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def _csv(key: str) -> list[str]:
    return [item.strip() for item in os.getenv(key, "").split(",") if item.strip()]


def _env_bool(key: str, *, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _bounded_positive_int(key: str, *, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(key, str(default)).strip())
    except ValueError:
        return default
    return max(1, min(value, maximum))


def _validation_failure_reason(error: DcsaTntValidationError) -> str:
    """Return a stable diagnostic category without echoing carrier payload values."""

    message = str(error)
    categories = (
        ("Event must contain exactly one matching event code", "event-code-shape"),
        ("Unsupported shipmentEventTypeCode", "unsupported-shipment-event-code"),
        ("Unsupported transportEventTypeCode", "unsupported-transport-event-code"),
        ("Unsupported equipmentEventTypeCode", "unsupported-equipment-event-code"),
        ("Unsupported eventType", "unsupported-event-type"),
        ("Unsupported eventClassifierCode", "unsupported-event-classifier"),
        ("eventID must be a UUID", "invalid-event-id"),
        ("eventCreatedDateTime", "invalid-event-created-time"),
        ("eventDateTime", "invalid-event-time"),
        ("Transport events require transportCall", "missing-transport-call"),
        ("Equipment events require emptyIndicatorCode", "missing-empty-indicator"),
        ("Equipment events require equipmentReference", "missing-equipment-reference"),
        ("Shipment events require documentID", "missing-shipment-document"),
        ("Unsupported referenceType", "unsupported-reference-type"),
        ("Unsupported documentReferenceType", "unsupported-document-reference-type"),
        ("references must be an array", "invalid-references-shape"),
        ("documentReferences must be an array", "invalid-document-references-shape"),
        ("facilityTypeCode", "invalid-facility-type"),
    )
    for prefix, category in categories:
        if message.startswith(prefix):
            return category
    return "other-dcsa-validation-error"
