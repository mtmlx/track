from .terminal import terminal_safe_text
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import multiprocessing as mp
import os
from queue import Empty
import re
import signal
import socket
import threading
import unicodedata
from urllib.parse import urlparse

import requests

from shipment_sync.audit import SafeSyncAuditLogger
from shipment_sync.carriers.registry import build_carrier_registry
from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.models import ShipmentRef, ShipmentStatus


@dataclass
class UpdatedShipment:
    task_id: str
    task_name: str
    shipping_line: str
    booking_no: str | None
    container_no: str | None
    status_text: str
    location: str | None
    event_time: datetime | None
    eta_time: datetime | None
    eta_local_text: str | None
    latest_move_name: str | None
    latest_move_location: str | None
    latest_move_time_local_text: str | None
    movement_details: str | None
    vessel_voyage: str | None
    list_id: str
    list_name: str | None


@dataclass
class SyncStats:
    updated_items: list[UpdatedShipment]
    unchanged: int
    skipped: int
    total_candidates: int
    candidates_by_list: dict[str, int] = field(default_factory=dict)
    updated_by_list: dict[str, int] = field(default_factory=dict)
    unchanged_by_list: dict[str, int] = field(default_factory=dict)


def run_sync(client: ClickUpClient, shipments: list[ShipmentRef] | None = None) -> SyncStats:
    audit = SafeSyncAuditLogger.from_settings(client.settings)
    try:
        stats = _run_sync(client, shipments=shipments, audit=audit)
    except Exception as exc:
        if audit is not None:
            audit.finish_failed(exc)
        raise
    if audit is not None:
        audit.finish_success(stats)
    return stats


def _run_sync(
    client: ClickUpClient,
    shipments: list[ShipmentRef] | None = None,
    audit: SafeSyncAuditLogger | None = None,
) -> SyncStats:
    adapters = build_carrier_registry()
    all_shipments = shipments if shipments is not None else client.list_shipments()
    if audit is not None:
        audit.log_event(
            level="info",
            message=f"Loaded {len(all_shipments)} shipment reference(s) from ClickUp.",
        )

    updated_items: list[UpdatedShipment] = []
    unchanged = 0
    skipped = 0
    candidates_by_list: dict[str, int] = {}
    updated_by_list: dict[str, int] = {}
    unchanged_by_list: dict[str, int] = {}
    prefiltered_excluded_counts: dict[str, int] = {}
    prefiltered_not_allowed_counts: dict[str, int] = {}
    prefiltered_recent_counts: dict[str, int] = {}
    prefiltered_terminal_status_counts: dict[str, int] = {}
    unsupported_line_counts: dict[str, int] = {}
    adapter_config_counts: dict[str, int] = {}
    carrier_access_failures: dict[str, str] = {}
    progress_every = _int_env("SHIPMENT_PROGRESS_EVERY", default=10, min_value=1)
    supported_lines = set(adapters.keys())
    allowed_lines = set(client.settings.shipment_allowed_lines or [])
    excluded_lines = set(client.settings.shipment_excluded_lines or [])
    default_min_sync_interval_hours = max(0, client.settings.shipment_min_sync_interval_hours)
    now_utc = datetime.now(timezone.utc)
    eligible_shipments = []

    if _any_min_sync_interval_configured(default_min_sync_interval_hours) and not client.settings.cf_status_last_checked:
        print(
            "A shipment min sync interval is set but CLICKUP_CF_STATUS_LAST_CHECKED is not configured; "
            "recent-sync prefilter is disabled.",
            file=sys.stderr,
        )

    for shipment in all_shipments:
        line_name = shipment.shipping_line
        if line_name in excluded_lines:
            prefiltered_excluded_counts[line_name] = prefiltered_excluded_counts.get(line_name, 0) + 1
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="prefiltered_excluded",
                    message=f"Carrier {line_name} is excluded by SHIPMENT_EXCLUDED_LINES.",
                )
            continue
        if allowed_lines and line_name not in allowed_lines:
            prefiltered_not_allowed_counts[line_name] = prefiltered_not_allowed_counts.get(line_name, 0) + 1
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="prefiltered_not_allowed",
                    message=f"Carrier {line_name} is not in SHIPMENT_ALLOWED_LINES.",
                )
            continue
        terminal_status = _terminal_shipment_status(
            shipment.current_task_status,
            client.settings.shipment_terminal_statuses,
        )
        if terminal_status is not None:
            prefiltered_terminal_status_counts[terminal_status] = (
                prefiltered_terminal_status_counts.get(terminal_status, 0) + 1
            )
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="prefiltered_terminal_status",
                    message=f"Task status {shipment.current_task_status!r} is terminal for Track & Trace.",
                )
            continue
        min_sync_interval_hours = _shipment_min_sync_interval_hours(
            shipment.shipping_line,
            default_min_sync_interval_hours,
        )
        if min_sync_interval_hours > 0 and shipment.last_checked_at is not None:
            age_seconds = (now_utc - shipment.last_checked_at).total_seconds()
            if age_seconds < min_sync_interval_hours * 3600:
                prefiltered_recent_counts[line_name] = prefiltered_recent_counts.get(line_name, 0) + 1
                if audit is not None:
                    audit.log_task(
                        shipment=shipment,
                        outcome="prefiltered_recent",
                        message=f"Last T&T update is newer than {min_sync_interval_hours}h.",
                    )
                continue
        if client.settings.shipment_skip_unsupported_lines and line_name not in supported_lines:
            unsupported_line_counts[line_name] = unsupported_line_counts.get(line_name, 0) + 1
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="skipped_unsupported",
                    message=f"No adapter registered for carrier {line_name}.",
                )
            continue
        eligible_shipments.append(shipment)

    total_candidates = len(eligible_shipments)
    preflight_failures: dict[str, str] = {}
    if client.settings.shipment_preflight_enabled and eligible_shipments:
        active_supported_lines = {s.shipping_line for s in eligible_shipments if s.shipping_line in supported_lines}
        preflight_failures = _preflight_supported_lines(active_supported_lines, client.settings)
        if preflight_failures:
            failed_counts: dict[str, int] = {}
            kept_shipments = []
            for shipment in eligible_shipments:
                reason = preflight_failures.get(shipment.shipping_line)
                if reason:
                    failed_counts[shipment.shipping_line] = failed_counts.get(shipment.shipping_line, 0) + 1
                    if audit is not None:
                        audit.log_task(
                            shipment=shipment,
                            outcome="skipped_preflight",
                            message=reason,
                        )
                    skipped += 1
                    continue
                kept_shipments.append(shipment)
            eligible_shipments = kept_shipments
            print("Prefiltered due to carrier preflight failure:", file=sys.stderr)
            for line_name, count in sorted(failed_counts.items(), key=lambda item: (-item[1], item[0])):
                print(terminal_safe_text(f"- {line_name}: {count} task(s) | {preflight_failures[line_name]}"), file=sys.stderr)

    total_eligible = len(eligible_shipments)
    if audit is not None:
        audit.log_event(
            level="info",
            message=f"Starting shipment sync for {total_eligible} eligible task(s).",
            data={"total_candidates": total_candidates, "total_eligible": total_eligible},
        )
    if total_eligible:
        print(terminal_safe_text(f"Starting shipment sync for {total_eligible} eligible task(s)..."), file=sys.stderr)

    for idx, shipment in enumerate(eligible_shipments, start=1):
        if idx == 1 or idx == total_eligible or (progress_every > 0 and idx % progress_every == 0):
            print(
                terminal_safe_text(f"Progress: {idx}/{total_eligible} | task={shipment.task_id} | line={shipment.shipping_line}"),
                file=sys.stderr,
            )

        list_label = _list_label(shipment.list_name, shipment.list_id)
        candidates_by_list[list_label] = candidates_by_list.get(list_label, 0) + 1

        access_failure = carrier_access_failures.get(shipment.shipping_line)
        if access_failure is not None:
            skipped += 1
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="skipped_carrier_access",
                    message=access_failure,
                )
            continue

        adapter = adapters.get(shipment.shipping_line)
        if not adapter:
            unsupported_line_counts[shipment.shipping_line] = unsupported_line_counts.get(shipment.shipping_line, 0) + 1
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="skipped_unsupported",
                    message=f"No adapter registered for carrier {shipment.shipping_line}.",
                )
            skipped += 1
            continue

        try:
            status = _fetch_carrier_status(adapter, shipment)
            write_result = client.update_shipment_status(shipment, status)
            if write_result.changed:
                updated_items.append(
                    UpdatedShipment(
                        task_id=shipment.task_id,
                        task_name=shipment.task_name,
                        shipping_line=shipment.shipping_line,
                        booking_no=shipment.booking_no,
                        container_no=shipment.container_no,
                        status_text=status.status_text,
                        location=status.location,
                        event_time=status.event_time,
                        eta_time=status.eta_time,
                        eta_local_text=status.eta_local_text,
                        latest_move_name=status.latest_move.name if status.latest_move else None,
                        latest_move_location=status.latest_move.location if status.latest_move else None,
                        latest_move_time_local_text=status.latest_move.event_time_local_text if status.latest_move else None,
                        movement_details=status.movement_details,
                        vessel_voyage=status.vessel_voyage,
                        list_id=shipment.list_id,
                        list_name=shipment.list_name,
                    )
                )
                updated_by_list[list_label] = updated_by_list.get(list_label, 0) + 1
                if audit is not None:
                    audit.log_task(
                        shipment=shipment,
                        outcome="updated",
                        status=status,
                        message=write_result.status_value,
                    )
            else:
                unchanged += 1
                unchanged_by_list[list_label] = unchanged_by_list.get(list_label, 0) + 1
                if audit is not None:
                    audit.log_task(
                        shipment=shipment,
                        outcome="unchanged",
                        status=status,
                        message=write_result.status_value,
                    )
        except Exception as exc:
            message = str(exc)
            if _carrier_access_denied(shipment.shipping_line, message):
                carrier_access_failures[shipment.shipping_line] = message
                message = (
                    f"{message}. Carrier circuit breaker opened; remaining "
                    f"{shipment.shipping_line} shipments will not be queried in this run."
                )
            if "adapter not configured" in message.lower():
                key = f"{shipment.shipping_line}: {message}"
                adapter_config_counts[key] = adapter_config_counts.get(key, 0) + 1
                if audit is not None:
                    audit.log_task(
                        shipment=shipment,
                        outcome="skipped_adapter_config",
                        error=message,
                    )
                skipped += 1
                continue
            if _wan_hai_manual_capture_needed(shipment.shipping_line, message):
                try:
                    posted = client.request_wan_hai_manual_capture(shipment, error=message)
                except Exception as comment_exc:
                    posted = False
                    print(
                        terminal_safe_text(f"Wan Hai manual capture comment failed for task {shipment.task_id}: {comment_exc}"),
                        file=sys.stderr,
                    )
                if posted:
                    print(
                        terminal_safe_text(f"Wan Hai manual capture requested for task {shipment.task_id} ({shipment.task_name})."),
                        file=sys.stderr,
                    )
            print(
                terminal_safe_text(f"Skipped task {shipment.task_id} ({shipment.task_name}): {exc}"),
                file=sys.stderr,
            )
            if audit is not None:
                audit.log_task(
                    shipment=shipment,
                    outcome="skipped_error",
                    error=message,
                )
            skipped += 1

    if carrier_access_failures:
        failures = "; ".join(
            f"{line_name}: {message}" for line_name, message in sorted(carrier_access_failures.items())
        )
        raise RuntimeError(f"Carrier access failure: {failures}")

    if prefiltered_excluded_counts:
        print("Prefiltered excluded shipping lines:", file=sys.stderr)
        for line_name, count in sorted(prefiltered_excluded_counts.items(), key=lambda item: (-item[1], item[0])):
            print(terminal_safe_text(f"- {line_name}: {count} task(s)"), file=sys.stderr)

    if prefiltered_not_allowed_counts:
        print("Prefiltered non-allowlisted shipping lines:", file=sys.stderr)
        for line_name, count in sorted(prefiltered_not_allowed_counts.items(), key=lambda item: (-item[1], item[0])):
            print(terminal_safe_text(f"- {line_name}: {count} task(s)"), file=sys.stderr)

    if prefiltered_recent_counts:
        print("Prefiltered recently synced tasks:", file=sys.stderr)
        for line_name, count in sorted(prefiltered_recent_counts.items(), key=lambda item: (-item[1], item[0])):
            print(terminal_safe_text(f"- {line_name}: {count} task(s)"), file=sys.stderr)

    if prefiltered_terminal_status_counts:
        print("Prefiltered terminal shipment statuses:", file=sys.stderr)
        for status_name, count in sorted(
            prefiltered_terminal_status_counts.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            print(terminal_safe_text(f"- {status_name}: {count} task(s)"), file=sys.stderr)

    if unsupported_line_counts:
        print("Skipped unsupported shipping lines:", file=sys.stderr)
        for line_name, count in sorted(unsupported_line_counts.items(), key=lambda item: (-item[1], item[0])):
            print(terminal_safe_text(f"- {line_name}: {count} task(s)"), file=sys.stderr)

    if adapter_config_counts:
        print("Skipped due to adapter configuration:", file=sys.stderr)
        for reason, count in sorted(adapter_config_counts.items(), key=lambda item: (-item[1], item[0])):
            print(terminal_safe_text(f"- {reason} [{count} task(s)]"), file=sys.stderr)

    return SyncStats(
        updated_items=updated_items,
        unchanged=unchanged,
        skipped=skipped,
        total_candidates=total_candidates,
        candidates_by_list=candidates_by_list,
        updated_by_list=updated_by_list,
        unchanged_by_list=unchanged_by_list,
    )


def _list_label(list_name: str | None, list_id: str) -> str:
    if list_name and list_name.strip():
        return f"{list_name.strip()} ({list_id})"
    return list_id


def _terminal_shipment_status(status: str | None, terminal_statuses: tuple[str, ...]) -> str | None:
    normalized = _normalize_status_name(status or "")
    if not normalized:
        return None
    for terminal_status in terminal_statuses:
        if _normalize_status_name(terminal_status) == normalized:
            return terminal_status
    return None


def _normalize_status_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", ascii_text.lower())


def _wan_hai_manual_capture_needed(shipping_line: str, message: str) -> bool:
    if _normalize_status_name(shipping_line) not in {"wanhai", "wanhailines", "whl"}:
        return False
    normalized_message = message.lower()
    markers = (
        "anti-bot",
        "antibot",
        "incapsula",
        "imperva",
        "query form not available",
        "blocked by",
        "wanhaiantibotblocked",
    )
    return any(marker in normalized_message for marker in markers)


def _carrier_access_denied(shipping_line: str, message: str) -> bool:
    """Identify carrier-wide anti-bot/access failures that should stop the line batch."""
    normalized_message = message.lower()
    markers = (
        "page access denied",
        "endpoint blocked by anti-bot",
        "anti-bot challenge",
        "page challenge did not clear",
        "access denied",
    )
    return any(marker in normalized_message for marker in markers)


def _fetch_carrier_status(adapter, shipment: ShipmentRef) -> ShipmentStatus:
    if _carrier_process_isolated(shipment.shipping_line):
        return _fetch_carrier_status_in_process(
            shipment,
            timeout_seconds=_carrier_process_timeout_seconds(shipment.shipping_line),
        )

    with _carrier_call_timeout(shipment):
        return adapter.fetch_status(shipment)


def _fetch_carrier_status_in_process(shipment: ShipmentRef, *, timeout_seconds: int) -> ShipmentStatus:
    if timeout_seconds <= 0:
        adapter = build_carrier_registry().get(shipment.shipping_line)
        if adapter is None:
            raise ValueError(f"No adapter registered for carrier {shipment.shipping_line}.")
        return adapter.fetch_status(shipment)

    start_method = os.getenv("SHIPMENT_FETCH_PROCESS_START_METHOD", "").strip()
    if not start_method:
        start_method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    ctx = mp.get_context(start_method)
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_fetch_carrier_status_child, args=(shipment, result_queue))
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise TimeoutError(
            f"Carrier lookup exceeded {timeout_seconds}s process timeout "
            f"for task {shipment.task_id} ({shipment.shipping_line})."
        )

    try:
        outcome, payload = result_queue.get_nowait()
    except Empty:
        raise RuntimeError(
            f"Carrier lookup process exited without a result for task {shipment.task_id} "
            f"({shipment.shipping_line}); exitcode={process.exitcode}."
        )

    if outcome == "ok":
        return payload
    raise RuntimeError(str(payload))


def _fetch_carrier_status_child(shipment: ShipmentRef, result_queue) -> None:
    try:
        adapter = build_carrier_registry().get(shipment.shipping_line)
        if adapter is None:
            raise ValueError(f"No adapter registered for carrier {shipment.shipping_line}.")
        result_queue.put(("ok", adapter.fetch_status(shipment)))
    except BaseException as exc:
        result_queue.put(("error", f"{type(exc).__name__}: {exc}"))


@contextmanager
def _carrier_call_timeout(shipment: ShipmentRef):
    timeout_seconds = _carrier_call_timeout_seconds(shipment.shipping_line)
    if (
        timeout_seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "SIGALRM")
        or not hasattr(signal, "setitimer")
    ):
        yield
        return

    def _raise_timeout(signum, frame):
        raise TimeoutError(
            f"Carrier lookup exceeded {timeout_seconds}s per-shipment timeout "
            f"for task {shipment.task_id} ({shipment.shipping_line})."
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])
        signal.signal(signal.SIGALRM, previous_handler)


def _carrier_call_timeout_seconds(shipping_line: str) -> int:
    prefix = _env_prefix_for_line(shipping_line)
    if prefix:
        line_timeout_key = f"{prefix}_PER_TASK_TIMEOUT_SECONDS"
        if line_timeout_key in os.environ:
            line_timeout = _int_env(line_timeout_key, default=0, min_value=0)
            return line_timeout
    return _int_env("SHIPMENT_PER_TASK_TIMEOUT_SECONDS", default=0, min_value=0)


def _carrier_process_isolated(shipping_line: str) -> bool:
    isolated_lines = _csv_env("SHIPMENT_PROCESS_ISOLATED_LINES")
    return (shipping_line or "").strip().lower() in isolated_lines


def _carrier_process_timeout_seconds(shipping_line: str) -> int:
    prefix = _env_prefix_for_line(shipping_line)
    if prefix:
        line_timeout_key = f"{prefix}_PROCESS_TIMEOUT_SECONDS"
        if line_timeout_key in os.environ:
            return _int_env(line_timeout_key, default=0, min_value=0)
    return _int_env("SHIPMENT_PROCESS_TIMEOUT_SECONDS", default=0, min_value=0)


def _shipment_min_sync_interval_hours(shipping_line: str, default_hours: int) -> int:
    prefix = _env_prefix_for_line(shipping_line)
    if prefix:
        line_interval = _int_env(f"{prefix}_MIN_SYNC_INTERVAL_HOURS", default=0, min_value=0)
        if line_interval > 0:
            return line_interval
    return default_hours


def _any_min_sync_interval_configured(default_hours: int) -> bool:
    if default_hours > 0:
        return True
    for key in os.environ:
        if key.endswith("_MIN_SYNC_INTERVAL_HOURS") and key != "SHIPMENT_MIN_SYNC_INTERVAL_HOURS":
            if _int_env(key, default=0, min_value=0) > 0:
                return True
    return False


def _env_prefix_for_line(shipping_line: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (shipping_line or "").upper()).strip("_")


def _preflight_supported_lines(lines: set[str], settings: Settings) -> dict[str, str]:
    failures: dict[str, str] = {}
    session = requests.Session()
    timeout = max(1, settings.shipment_preflight_timeout_seconds)
    for line_name in sorted(lines):
        if line_name in {"cosco", "cosco shipping", "cosco shipping line", "cosco shipping lines"}:
            ok, reason = _probe_cosco_tracking_access(timeout, session)
            if not ok:
                failures[line_name] = reason
            continue
        if line_name in {"cma cgm", "cma-cgm", "cma - cgm"}:
            ok, reason = _probe_cma_cgm_tracking_access(timeout, session)
            if not ok:
                failures[line_name] = reason
            continue
        hosts = _preflight_hosts_for_line(line_name)
        if not hosts:
            continue
        for host in hosts:
            ok, reason = _probe_host(host, timeout, session)
            if not ok:
                failures[line_name] = f"{host}: {reason}"
                break
    return failures


def _preflight_hosts_for_line(line_name: str) -> list[str]:
    normalized = (line_name or "").strip().lower()
    hosts: list[str] = []

    if normalized in {"one", "ocean network express"}:
        one_use_edh = _env_bool("ONE_USE_EDH_API", True)
        one_template = os.getenv("ONE_TRACKING_URL_TEMPLATE", "").strip()
        one_api_url = os.getenv("ONE_TRACKING_API_URL", "").strip()
        if one_use_edh:
            _append_host(hosts, os.getenv("ONE_EDH_BASE_URL", "https://ecomm.one-line.com/api/v1/edh"))
        if one_api_url:
            _append_host(hosts, one_api_url)
        if one_template:
            _append_host(hosts, one_template)
        if not hosts:
            _append_host(hosts, "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking")
        return hosts

    if normalized in {"maersk", "maersk line", "a.p. moller - maersk"}:
        api_mode = os.getenv("MAERSK_API_MODE", "auto").strip().lower()
        consumer_key = os.getenv("MAERSK_CONSUMER_KEY", "").strip()
        bearer_token = os.getenv("MAERSK_BEARER_TOKEN", "").strip()
        oauth_token_url = os.getenv("MAERSK_OAUTH_TOKEN_URL", "").strip()
        web_fallback_enabled = _env_bool("MAERSK_WEB_FALLBACK_ON_API_ERROR", True)
        maersk_template = os.getenv("MAERSK_TRACKING_URL_TEMPLATE", "").strip()
        maersk_api_url = os.getenv("MAERSK_TRACKING_API_URL", "").strip()
        use_events_api = api_mode == "events" or (api_mode != "legacy" and bool(consumer_key or bearer_token or oauth_token_url))

        # If API URL is configured, preflight API/OAuth first and only include web host when fallback is enabled.
        if maersk_api_url:
            _append_host(hosts, maersk_api_url)
            if use_events_api and not bearer_token:
                _append_host(hosts, oauth_token_url or "https://api.maersk.com/oauth2/access_token")
            if web_fallback_enabled:
                _append_host(hosts, maersk_template or "https://www.maersk.com/tracking/{reference}")
            return hosts

        # No API URL configured: web mode only.
        _append_host(hosts, maersk_template or "https://www.maersk.com/tracking/{reference}")
        return hosts

    if normalized in {"hapag lloyd", "hapag-lloyd", "hapag lloyd ag"}:
        hapag_use_playwright = _env_bool("HAPAG_USE_PLAYWRIGHT", True)
        hapag_api_url = os.getenv("HAPAG_TRACKING_API_URL", "").strip()
        hapag_template = os.getenv("HAPAG_TRACKING_URL_TEMPLATE", "").strip()
        hapag_page = os.getenv(
            "HAPAG_TRACKING_PAGE_URL_TEMPLATE",
            "https://www.hapag-lloyd.com/en/online-business/track/track-by-container-solution.html",
        ).strip()
        hapag_api_key = os.getenv("HAPAG_API_KEY", "").strip()
        hapag_client_id = os.getenv("HAPAG_CLIENT_ID", "").strip()
        hapag_client_secret = os.getenv("HAPAG_CLIENT_SECRET", "").strip()
        bearer_token = os.getenv("HAPAG_BEARER_TOKEN", "").strip()
        oauth_token_url = os.getenv("HAPAG_OAUTH_TOKEN_URL", "").strip()
        oauth_client_id = os.getenv("HAPAG_OAUTH_CLIENT_ID", "").strip()
        oauth_client_secret = os.getenv("HAPAG_OAUTH_CLIENT_SECRET", "").strip()
        hapag_has_auth = bool(
            bearer_token
            or hapag_api_key
            or (hapag_client_id and hapag_client_secret)
            or (oauth_token_url and oauth_client_id and oauth_client_secret)
        )

        if hapag_use_playwright:
            _append_host(hosts, hapag_page)
            return hosts

        if hapag_api_url:
            if not hapag_template and not hapag_has_auth:
                failures[normalized] = (
                    "Hapag API requires auth; set HAPAG_BEARER_TOKEN, "
                    "HAPAG_OAUTH_TOKEN_URL + HAPAG_OAUTH_CLIENT_ID + HAPAG_OAUTH_CLIENT_SECRET, "
                    "HAPAG_API_KEY, or HAPAG_CLIENT_ID + HAPAG_CLIENT_SECRET"
                )
                return failures
            _append_host(hosts, hapag_api_url)
            if not bearer_token and oauth_token_url:
                _append_host(hosts, oauth_token_url)
        elif hapag_template:
            _append_host(hosts, hapag_template)
        else:
            _append_host(hosts, hapag_page)
        return hosts

    if normalized in {"cosco", "cosco shipping", "cosco shipping line", "cosco shipping lines"}:
        cosco_mode = os.getenv("COSCO_MODE", "cop").strip().lower() or "cop"
        cosco_cop_base = os.getenv("COSCO_COP_BASE_URL", "https://api-pp.lines.coscoshipping.com").strip()
        cosco_api_key = os.getenv("COSCO_COP_API_KEY", "").strip()
        cosco_secret = os.getenv("COSCO_COP_SECRET_KEY", "").strip()
        cosco_has_cop_creds = bool(cosco_api_key and cosco_secret)
        cosco_use_legacy_api = _env_bool("COSCO_USE_API", True)
        cosco_legacy_api_base = os.getenv("COSCO_TRACKING_API_BASE_URL", "").strip()
        cosco_template = os.getenv("COSCO_TRACKING_URL_TEMPLATE", "").strip()

        if cosco_mode == "cop":
            _append_host(hosts, cosco_cop_base)
            return hosts

        if cosco_mode == "auto" and cosco_has_cop_creds:
            _append_host(hosts, cosco_cop_base)
            return hosts

        if cosco_use_legacy_api and cosco_legacy_api_base:
            _append_host(hosts, cosco_legacy_api_base)
        elif cosco_template:
            _append_host(hosts, cosco_template)
        else:
            _append_host(hosts, "https://elines.coscoshipping.com/ebusiness/cargoTracking")
        return hosts

    if normalized in {"cma cgm", "cma-cgm", "cma - cgm"}:
        cma_api_url = os.getenv("CMA_CGM_TRACKING_API_URL", "").strip()
        cma_template = os.getenv("CMA_CGM_TRACKING_URL_TEMPLATE", "").strip()
        if cma_api_url:
            _append_host(hosts, cma_api_url)
        elif cma_template:
            _append_host(hosts, cma_template)
        else:
            _append_host(hosts, "https://www.cma-cgm.com/ebusiness/tracking/detail/MSCU1234567")
        return hosts

    if normalized in {"msc", "msc shipping line", "mediterranean shipping company"}:
        use_playwright = _env_bool("MSC_USE_PLAYWRIGHT", True)
        playwright_required = _env_bool("MSC_PLAYWRIGHT_REQUIRED", False)
        if use_playwright:
            _append_host(
                hosts,
                os.getenv("MSC_PLAYWRIGHT_TRACKING_URL", "https://www.msc.com/en/track-a-shipment"),
            )
            _append_host(
                hosts,
                os.getenv("MSC_PLAYWRIGHT_API_ENDPOINT", "https://www.msc.com/api/feature/tools/TrackingInfo"),
            )
            if playwright_required:
                return hosts

        fallback_hosts = _hosts_from_generic_prefix(
            env_prefix="MSC",
            default_template="https://www.msc.com/en/track-a-shipment",
        )
        for host in fallback_hosts:
            if host not in hosts:
                hosts.append(host)
        return hosts

    if normalized in {"pil", "pacific international lines"}:
        return _hosts_from_generic_prefix(
            env_prefix="PIL",
            default_template="https://www.pilship.com/en-our-track-and-trace-p.html",
        )

    if normalized in {"evergreen", "evergreen line", "evergreen marine"}:
        return _hosts_from_generic_prefix(
            env_prefix="EVERGREEN",
            default_template="https://www.evergreen-line.com/",
        )

    if normalized in {"wan hai", "wan hai lines"}:
        return _hosts_from_generic_prefix(
            env_prefix="WAN_HAI",
            default_template="https://www.wanhai.com/views/cargoTrack/CargoTrack.xhtml",
        )

    if normalized in {"oocl", "orient overseas container line"}:
        return _hosts_from_generic_prefix(
            env_prefix="OOCL",
            default_template="https://www.oocl.com/eng/ourservices/eservices/cargotracking/Pages/cargotracking.aspx",
        )

    return hosts


def _append_host(hosts: list[str], url_value: str) -> None:
    host = _host_from_url(url_value)
    if host and host not in hosts:
        hosts.append(host)


def _hosts_from_generic_prefix(*, env_prefix: str, default_template: str) -> list[str]:
    hosts: list[str] = []
    api_url = os.getenv(f"{env_prefix}_TRACKING_API_URL", "").strip()
    url_template = os.getenv(f"{env_prefix}_TRACKING_URL_TEMPLATE", "").strip()
    if api_url:
        _append_host(hosts, api_url)
    elif url_template:
        _append_host(hosts, url_template)
    else:
        _append_host(hosts, default_template)
    return hosts


def _host_from_url(url_value: str | None) -> str | None:
    if not url_value:
        return None
    candidate = url_value.strip()
    if not candidate:
        return None
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    return parsed.hostname


def _probe_host(host: str, timeout_seconds: int, session: requests.Session) -> tuple[bool, str]:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return False, f"DNS resolution failed ({exc})"

    try:
        response = session.get(f"https://{host}", timeout=timeout_seconds, allow_redirects=True)
    except requests.RequestException as exc:
        return False, f"HTTPS probe failed ({exc})"
    return True, f"reachable (HTTP {response.status_code})"


def _probe_cosco_tracking_access(timeout_seconds: int, session: requests.Session) -> tuple[bool, str]:
    cosco_mode = os.getenv("COSCO_MODE", "cop").strip().lower() or "cop"
    cosco_api_key = os.getenv("COSCO_COP_API_KEY", "").strip()
    cosco_secret = os.getenv("COSCO_COP_SECRET_KEY", "").strip()
    has_cop_creds = bool(cosco_api_key and cosco_secret)

    if cosco_mode == "cop":
        if not has_cop_creds:
            return False, "COSCO COP not configured (set COSCO_COP_API_KEY and COSCO_COP_SECRET_KEY)"
        host = _host_from_url(os.getenv("COSCO_COP_BASE_URL", "https://api-pp.lines.coscoshipping.com"))
        if not host:
            return False, "COSCO COP base URL is invalid"
        ok, reason = _probe_host(host, timeout_seconds, session)
        if not ok:
            return False, f"COSCO COP host check failed ({reason})"
        return True, f"COSCO COP host reachable ({reason})"

    if cosco_mode == "auto" and has_cop_creds:
        host = _host_from_url(os.getenv("COSCO_COP_BASE_URL", "https://api-pp.lines.coscoshipping.com"))
        if host:
            ok, reason = _probe_host(host, timeout_seconds, session)
            if ok:
                return True, f"COSCO COP host reachable ({reason})"

    url = "https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType=CONTAINER&number=MSCU1234567"
    try:
        response = session.get(url, timeout=timeout_seconds, allow_redirects=True)
    except requests.RequestException as exc:
        return False, f"COSCO probe failed ({exc})"

    body = (response.text or "").lower()
    blocked = (
        "this page can't be displayed" in body
        or "页面无法显示" in response.text
        or "support@coscon.com" in body
        or "<title>error</title>" in body
    )
    if blocked:
        return False, "COSCO legacy tracking blocked by anti-bot (recommend COSCO COP API mode)"
    return True, f"COSCO tracking reachable (HTTP {response.status_code})"


def _probe_cma_cgm_tracking_access(timeout_seconds: int, session: requests.Session) -> tuple[bool, str]:
    cma_api_url = os.getenv("CMA_CGM_TRACKING_API_URL", "").strip()
    cma_api_base = os.getenv("CMA_CGM_API_BASE_URL", "").strip()
    cma_api_method = os.getenv("CMA_CGM_API_METHOD", "").strip()
    cma_api_method_path = os.getenv("CMA_CGM_API_METHOD_PATH", "").strip()
    api_mode_requested = bool(cma_api_method or cma_api_method_path or cma_api_base)

    if api_mode_requested and not cma_api_url and not cma_api_base:
        return (
            False,
            "CMA-CGM API mode configured but endpoint missing "
            "(set CMA_CGM_TRACKING_API_URL or CMA_CGM_API_BASE_URL)",
        )

    if cma_api_url or cma_api_base:
        if cma_api_url:
            endpoint = cma_api_url
        else:
            method_path = cma_api_method_path or _cma_method_name_to_path(cma_api_method)
            if method_path:
                normalized_method = method_path if method_path.startswith("/") else f"/{method_path}"
                endpoint = f"{cma_api_base.rstrip('/')}{normalized_method}"
            else:
                endpoint = cma_api_base

        host = _host_from_url(endpoint)
        if not host:
            return False, "CMA-CGM API URL is invalid"

        ok, reason = _probe_host(host, timeout_seconds, session)
        if not ok:
            return False, f"CMA-CGM API host check failed ({reason})"
        return True, f"CMA-CGM API host reachable ({reason})"

    url = "https://www.cma-cgm.com/ebusiness/tracking/detail/MSCU1234567"
    try:
        response = session.get(url, timeout=timeout_seconds, allow_redirects=True)
    except requests.RequestException as exc:
        return False, f"CMA-CGM probe failed ({exc})"

    body = (response.text or "").lower()
    blocked = (
        "please enable js and disable any ad blocker" in body
        or "captcha" in body
        or "access denied" in body
        or "<title>cma-cgm.com</title>" in body
    )
    if blocked:
        return False, "CMA-CGM anti-bot challenge page returned"
    return True, f"CMA-CGM tracking reachable (HTTP {response.status_code})"


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(key: str, default: int, *, min_value: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        parsed = int(raw.strip())
    except Exception:
        return default
    return parsed if parsed >= min_value else default


def _csv_env(key: str) -> set[str]:
    raw = os.getenv(key, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _cma_method_name_to_path(method_name: str) -> str:
    normalized = (method_name or "").strip().lower()
    if not normalized:
        return ""
    if normalized == "searchmoveoncommercialcycle":
        return "/events"
    if normalized == "getmoveoncommercialcycle":
        return "/events/{trackingReference}"
    return method_name
