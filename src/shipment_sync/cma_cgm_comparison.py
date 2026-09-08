from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
import unicodedata

from shipment_sync.carriers.cma_cgm import CmaCgmAdapter, CmaCgmComparisonInput
from shipment_sync.carriers.common import render_vessel_voyage
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.dcsa_tnt import DcsaTntEvent, DcsaTntValidationError, parse_dcsa_tnt_event
from shipment_sync.models import ShipmentRef, ShipmentStatus


_CMA_CGM_ALIASES = frozenset({"cma cgm", "cma-cgm", "cma - cgm"})
_EVENT_CODE_PATTERN = re.compile(r"\(([A-Z]{4})\)$")


@dataclass(frozen=True)
class CmaCgmComparisonSettings:
    enabled: bool
    max_shipments: int

    @classmethod
    def from_env(cls, *, require_enabled: bool) -> "CmaCgmComparisonSettings":
        enabled = _env_bool("CMA_CGM_COMPARISON_ENABLED", default=False)
        if require_enabled and not enabled:
            raise ValueError(
                "CMA comparison is disabled. Set CMA_CGM_COMPARISON_ENABLED=true before using --run."
            )
        return cls(
            enabled=enabled,
            max_shipments=_bounded_positive_int("CMA_CGM_COMPARISON_MAX_SHIPMENTS", default=25, maximum=250),
        )


@dataclass(frozen=True)
class CmaCgmComparableSnapshot:
    container_numbers: tuple[str, ...]
    latest_event_code: str | None
    latest_event_at: datetime | None
    eta_time: datetime | None
    vessel_voyage: str | None
    event_count: int

    def as_dict(self) -> dict[str, int | str | None]:
        """Return diagnostics without leaking individual container identifiers."""

        return {
            "container_count": len(self.container_numbers),
            "latest_event_code": self.latest_event_code,
            "latest_event_at": _iso(self.latest_event_at),
            "eta_time": _iso(self.eta_time),
            "vessel_voyage": self.vessel_voyage,
            "event_count": self.event_count,
        }


@dataclass(frozen=True)
class CmaCgmComparisonResult:
    task_id: str
    page_count: int
    legacy: CmaCgmComparableSnapshot
    dcsa: CmaCgmComparableSnapshot
    differing_fields: tuple[str, ...]
    conformance_warnings: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.differing_fields

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "matches": self.matches,
            "differing_fields": list(self.differing_fields),
            "page_count": self.page_count,
            "legacy": self.legacy.as_dict(),
            "dcsa": self.dcsa.as_dict(),
            "conformance_warnings": list(self.conformance_warnings),
        }


@dataclass
class CmaCgmComparisonSummary:
    candidates: int = 0
    selected_shipments: int = 0
    compared_shipments: int = 0
    matching_shipments: int = 0
    differing_shipments: int = 0
    no_event_shipments: int = 0
    skipped_terminal: int = 0
    skipped_non_cma: int = 0
    skipped_limit: int = 0
    source_failures: int = 0
    validation_failures: int = 0
    pages_fetched: int = 0
    conformance_warning_events: int = 0
    conformance_warning_codes: dict[str, int] = field(default_factory=dict)
    differing_field_counts: dict[str, int] = field(default_factory=dict)
    results: list[CmaCgmComparisonResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": "cma-cgm-legacy-vs-dcsa-comparison",
            "clickup_writes": False,
            "candidates": self.candidates,
            "selected_shipments": self.selected_shipments,
            "compared_shipments": self.compared_shipments,
            "matching_shipments": self.matching_shipments,
            "differing_shipments": self.differing_shipments,
            "no_event_shipments": self.no_event_shipments,
            "skipped_terminal": self.skipped_terminal,
            "skipped_non_cma": self.skipped_non_cma,
            "skipped_limit": self.skipped_limit,
            "source_failures": self.source_failures,
            "validation_failures": self.validation_failures,
            "pages_fetched": self.pages_fetched,
            "conformance_warning_events": self.conformance_warning_events,
            "conformance_warning_codes": dict(sorted(self.conformance_warning_codes.items())),
            "differing_field_counts": dict(sorted(self.differing_field_counts.items())),
            "results": [result.as_dict() for result in self.results],
        }


class CmaCgmComparisonRunner:
    """Compare CMA's existing API projection with all validated DCSA pages.

    This runner deliberately has no ClickUp update, comment, field-write,
    status-write, scheduler, or ledger-write path.  It is a bounded read-only
    test that emits a sanitized comparison report to its caller.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        comparison_settings: CmaCgmComparisonSettings,
        adapter: CmaCgmAdapter | None = None,
        now: datetime | None = None,
    ) -> None:
        self.settings = settings
        self.comparison_settings = comparison_settings
        self.adapter = adapter or CmaCgmAdapter()
        self.now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def run(self, shipments: list[ShipmentRef]) -> CmaCgmComparisonSummary:
        summary = CmaCgmComparisonSummary(candidates=len(shipments))
        for shipment in shipments:
            if not _is_cma_cgm(shipment.shipping_line):
                summary.skipped_non_cma += 1
                continue
            if _is_terminal(shipment, self.settings.shipment_terminal_statuses):
                summary.skipped_terminal += 1
                continue
            if summary.selected_shipments >= self.comparison_settings.max_shipments:
                summary.skipped_limit += 1
                continue

            summary.selected_shipments += 1
            try:
                comparison_input = self.adapter.fetch_comparison_input(shipment)
            except Exception:
                summary.source_failures += 1
                continue

            raw_events = comparison_input.dcsa_fetch.events
            if not raw_events:
                summary.no_event_shipments += 1
                continue
            try:
                events = tuple(
                    parse_dcsa_tnt_event(
                        raw_event,
                        carrier="cma cgm",
                        tnt_version="2.2",
                        source_url=comparison_input.dcsa_fetch.source_url,
                    )
                    for raw_event in raw_events
                )
            except DcsaTntValidationError:
                summary.validation_failures += 1
                continue

            result = _compare(
                task_id=shipment.task_id,
                comparison_input=comparison_input,
                events=events,
                now=self.now,
            )
            summary.compared_shipments += 1
            summary.pages_fetched += result.page_count
            summary.results.append(result)
            if result.matches:
                summary.matching_shipments += 1
            else:
                summary.differing_shipments += 1
                for field_name in result.differing_fields:
                    summary.differing_field_counts[field_name] = summary.differing_field_counts.get(field_name, 0) + 1
            for event in events:
                for warning in event.conformance_warnings:
                    summary.conformance_warning_events += 1
                    summary.conformance_warning_codes[warning] = summary.conformance_warning_codes.get(warning, 0) + 1
        return summary


def run_cma_cgm_comparison_from_clickup(
    *,
    settings: Settings,
    comparison_settings: CmaCgmComparisonSettings,
    client: ClickUpClient | None = None,
    adapter: CmaCgmAdapter | None = None,
    now: datetime | None = None,
) -> CmaCgmComparisonSummary:
    """Read shipment inventory only, then run the bounded CMA comparison."""

    _require_cma_prefilter_scope(settings)
    inventory_client = client or ClickUpClient(settings)
    shipments = inventory_client.list_shipments(require_carrier_prefilter=True)
    return CmaCgmComparisonRunner(
        settings=settings,
        comparison_settings=comparison_settings,
        adapter=adapter,
        now=now,
    ).run(shipments)


def _compare(
    *,
    task_id: str,
    comparison_input: CmaCgmComparisonInput,
    events: tuple[DcsaTntEvent, ...],
    now: datetime,
) -> CmaCgmComparisonResult:
    legacy = _legacy_snapshot(comparison_input.legacy_status)
    dcsa = _dcsa_snapshot(events, now=now)
    differing_fields = tuple(
        field_name
        for field_name, matched in (
            ("containers", legacy.container_numbers == dcsa.container_numbers),
            ("latest_event_code", legacy.latest_event_code == dcsa.latest_event_code),
            ("latest_event_at", _same_instant(legacy.latest_event_at, dcsa.latest_event_at)),
            ("eta_time", _same_instant(legacy.eta_time, dcsa.eta_time)),
            ("vessel_voyage", _same_text(legacy.vessel_voyage, dcsa.vessel_voyage)),
        )
        if not matched
    )
    warnings = tuple(
        sorted({warning for event in events for warning in event.conformance_warnings})
    )
    return CmaCgmComparisonResult(
        task_id=task_id,
        page_count=comparison_input.dcsa_fetch.page_count,
        legacy=legacy,
        dcsa=dcsa,
        differing_fields=differing_fields,
        conformance_warnings=warnings,
    )


def _legacy_snapshot(status: ShipmentStatus) -> CmaCgmComparableSnapshot:
    latest_move = status.latest_move
    return CmaCgmComparableSnapshot(
        container_numbers=_normalized_containers(status.discovered_containers),
        latest_event_code=_movement_event_code(latest_move.name) if latest_move else None,
        latest_event_at=latest_move.event_time if latest_move else None,
        eta_time=status.eta_time,
        vessel_voyage=status.vessel_voyage,
        event_count=len(status.recent_moves),
    )


def _dcsa_snapshot(events: tuple[DcsaTntEvent, ...], *, now: datetime) -> CmaCgmComparableSnapshot:
    latest = _latest_event(events)
    return CmaCgmComparableSnapshot(
        container_numbers=_normalized_containers(event.equipment_reference for event in events),
        latest_event_code=latest.event_code if latest else None,
        latest_event_at=_event_time(latest) if latest else None,
        eta_time=_dcsa_eta(events, now=now),
        vessel_voyage=_final_destination_vessel(events),
        event_count=len(events),
    )


def _latest_event(events: tuple[DcsaTntEvent, ...]) -> DcsaTntEvent | None:
    candidates = [event for event in events if event.event_at is not None]
    if not candidates:
        return None
    return max(enumerate(candidates), key=lambda item: (_event_time(item[1]), item[0]))[1]


def _dcsa_eta(events: tuple[DcsaTntEvent, ...], *, now: datetime) -> datetime | None:
    candidates = [
        (event.event_at, event.event_classifier_code in {"PLN", "EST"})
        for event in events
        if event.event_type == "TRANSPORT" and event.event_code == "ARRI" and event.event_at is not None
    ]
    if not candidates:
        return None

    future_estimated = [candidate for candidate in candidates if candidate[1] and candidate[0] >= now]
    if future_estimated:
        return max(candidate[0] for candidate in future_estimated)
    future_any = [candidate for candidate in candidates if candidate[0] >= now]
    if future_any:
        return max(candidate[0] for candidate in future_any)
    estimated_any = [candidate for candidate in candidates if candidate[1]]
    if estimated_any:
        return max(candidate[0] for candidate in estimated_any)
    return max(candidate[0] for candidate in candidates)


def _final_destination_vessel(events: tuple[DcsaTntEvent, ...]) -> str | None:
    candidates: list[tuple[int, datetime | None, int, str]] = []
    for index, event in enumerate(events):
        vessel_voyage = render_vessel_voyage(
            event.vessel_name,
            event.export_voyage_number or event.import_voyage_number,
        )
        if not vessel_voyage:
            continue
        score = 3 if event.event_type == "EQUIPMENT" and event.event_code == "DISC" else 0
        if event.event_type == "TRANSPORT" and event.event_code == "ARRI":
            score = 2
        if not score:
            continue
        candidates.append((score, _event_time(event), index, vessel_voyage))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            item[0],
            item[1] is not None,
            item[1].isoformat() if item[1] is not None else "",
            item[2],
        ),
    )[3]


def _event_time(event: DcsaTntEvent) -> datetime:
    return event.event_at or event.event_created_at


def _movement_event_code(value: str) -> str | None:
    match = _EVENT_CODE_PATTERN.search(value.strip())
    return match.group(1) if match else None


def _normalized_containers(values: Iterable[str | None]) -> tuple[str, ...]:
    normalized = {
        str(value).strip().upper()
        for value in values
        if value is not None and str(value).strip()
    }
    return tuple(sorted(normalized))


def _same_instant(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return left.astimezone(timezone.utc).replace(microsecond=0) == right.astimezone(timezone.utc).replace(microsecond=0)


def _same_text(left: str | None, right: str | None) -> bool:
    return _normalize_text(left) == _normalize_text(right)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _is_cma_cgm(value: str | None) -> bool:
    normalized = " ".join((value or "").strip().lower().split())
    return normalized in _CMA_CGM_ALIASES


def _require_cma_prefilter_scope(settings: Settings) -> None:
    allowed_lines = settings.shipment_allowed_lines or []
    if len(allowed_lines) != 1 or not _is_cma_cgm(allowed_lines[0]):
        raise ValueError(
            "CMA comparison requires SHIPMENT_ALLOWED_LINES=cma cgm so ClickUp can apply a strict API-side filter."
        )


def _is_terminal(shipment: ShipmentRef, terminal_statuses: tuple[str, ...]) -> bool:
    terminal = {_normalize_text(value) for value in terminal_statuses if _normalize_text(value)}
    current = shipment.current_task_status or shipment.current_status_value
    return _normalize_text(current) in terminal


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
