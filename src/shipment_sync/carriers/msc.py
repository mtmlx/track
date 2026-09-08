from __future__ import annotations

from datetime import datetime
import os
import re
import time
from typing import Any

from shipment_sync.carriers.base import CarrierAdapter
from shipment_sync.carriers.common import (
    carrier_response_max_bytes,
    extract_container_numbers,
    extract_event_vessel_voyage,
    extract_event_state_hint,
    extract_first,
    render_vessel_voyage,
    parse_event_time,
    to_dcsa_movement_name,
)
from shipment_sync.carriers.generic_line import GenericLineAdapter
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from shipment_sync.playwright_runner import configured_browser_channel, run_sync_playwright


class MscAdapter(CarrierAdapter):
    def __init__(self) -> None:
        self.eta_only_mode = _env_bool("SHIPMENT_ETA_ONLY", default=True)
        self.use_playwright = _env_bool("MSC_USE_PLAYWRIGHT", default=True)
        self.playwright_required = _env_bool("MSC_PLAYWRIGHT_REQUIRED", default=False)
        self.playwright_headless = _env_bool("MSC_PLAYWRIGHT_HEADLESS", default=True)
        self.playwright_timeout_seconds = int(os.getenv("MSC_PLAYWRIGHT_TIMEOUT_SECONDS", "90"))
        self.playwright_request_delay_seconds = float(os.getenv("MSC_PLAYWRIGHT_REQUEST_DELAY_SECONDS", "0.5"))
        self.playwright_browser = os.getenv("MSC_PLAYWRIGHT_BROWSER", "chromium").strip() or "chromium"
        self.playwright_channel = os.getenv("MSC_PLAYWRIGHT_CHANNEL", "").strip()
        self.playwright_locale = os.getenv("MSC_PLAYWRIGHT_LOCALE", "en-US").strip() or "en-US"
        self.playwright_challenge_timeout_seconds = int(os.getenv("MSC_PLAYWRIGHT_CHALLENGE_TIMEOUT_SECONDS", "20"))
        self.playwright_challenge_reload_attempts = int(os.getenv("MSC_PLAYWRIGHT_CHALLENGE_RELOAD_ATTEMPTS", "1"))
        self.playwright_user_agent = (
            os.getenv(
                "MSC_PLAYWRIGHT_USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            ).strip()
            or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        self.tracking_page_url = (
            os.getenv("MSC_PLAYWRIGHT_TRACKING_URL", "https://www.msc.com/en/track-a-shipment").strip()
            or "https://www.msc.com/en/track-a-shipment"
        )
        self.tracking_api_url = (
            os.getenv("MSC_PLAYWRIGHT_API_ENDPOINT", "https://www.msc.com/api/feature/tools/TrackingInfo").strip()
            or "https://www.msc.com/api/feature/tools/TrackingInfo"
        )
        self.max_retries = int(os.getenv("MSC_MAX_RETRIES", "2"))
        self.retry_delay_seconds = float(os.getenv("MSC_RETRY_DELAY_SECONDS", "2"))
        self.max_reference_attempts = _int_env("MSC_MAX_REFERENCE_ATTEMPTS", default=4, minimum=0)
        self.response_max_bytes = carrier_response_max_bytes()

        self.generic_fallback = GenericLineAdapter(
            env_prefix="MSC",
            line_label="MSC",
            default_page_url_template="https://www.msc.com/en/track-a-shipment",
            challenge_markers=("captcha", "access denied", "forbidden", "please enable javascript"),
        )

    def fetch_status(self, shipment: ShipmentRef) -> ShipmentStatus:
        playwright_error: Exception | None = None
        if self.use_playwright:
            try:
                return self._fetch_status_playwright(shipment)
            except Exception as exc:
                playwright_error = exc
                if self.playwright_required:
                    raise

        try:
            status = self.generic_fallback.fetch_status(shipment)
        except Exception as fallback_error:
            if playwright_error is not None:
                raise ValueError(f"MSC Playwright failed ({playwright_error}); fallback failed ({fallback_error})")
            raise

        if playwright_error is not None:
            status.raw_source = _append_raw_source(
                status.raw_source,
                f"msc-playwright-error:{playwright_error}",
            )
        return status

    def _fetch_status_playwright(self, shipment: ShipmentRef) -> ShipmentStatus:
        attempts = _limit_reference_attempts(
            _build_reference_attempts(shipment),
            limit=self.max_reference_attempts,
        )
        if not attempts:
            raise ValueError("Missing booking/container number")

        last_error: Exception | None = None
        for reference, tracking_mode in attempts:
            for attempt in range(self.max_retries + 1):
                try:
                    payload, source = self._playwright_request(
                        reference=reference,
                        tracking_mode=tracking_mode,
                    )
                    return _status_from_payload(
                        payload=payload,
                        source=source,
                        source_url=self.tracking_page_url,
                        eta_only_mode=self.eta_only_mode,
                    )
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.max_retries:
                        break
                    time.sleep(self.retry_delay_seconds * (attempt + 1))

        if last_error:
            raise last_error
        raise ValueError("MSC Playwright request failed without specific error")

    def _playwright_request(self, *, reference: str, tracking_mode: str) -> tuple[dict[str, Any], str]:
        def _run() -> tuple[dict[str, Any], str]:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                raise ValueError("Playwright is not installed. Run: pip install -e .[browser] && playwright install") from exc

            timeout_ms = max(1, self.playwright_timeout_seconds) * 1000
            with sync_playwright() as p:
                browser_type = getattr(p, self.playwright_browser, None)
                if browser_type is None:
                    raise ValueError(f"Unsupported Playwright browser type: {self.playwright_browser}")

                launch_kwargs: dict[str, Any] = {
                    "headless": self.playwright_headless,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                channel = configured_browser_channel(
                    self.playwright_channel,
                    browser_name=self.playwright_browser,
                )
                if channel:
                    launch_kwargs["channel"] = channel

                browser = browser_type.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=self.playwright_user_agent,
                        locale=self.playwright_locale,
                        viewport={"width": 1440, "height": 900},
                    )
                    context.set_extra_http_headers(
                        {
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                        }
                    )
                    context.add_init_script(_STEALTH_INIT_SCRIPT)
                    page = context.new_page()
                    page.goto(self.tracking_page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    _wait_for_msc_page_ready(
                        page,
                        timeout_ms=timeout_ms,
                        challenge_timeout_seconds=self.playwright_challenge_timeout_seconds,
                        reload_attempts=self.playwright_challenge_reload_attempts,
                    )
                    page.wait_for_selector(
                        "input[name='__RequestVerificationToken']",
                        timeout=timeout_ms,
                        state="attached",
                    )

                    html = page.content().lower()
                    if "access denied" in html:
                        raise ValueError("MSC page access denied in browser session")

                    token = page.locator("input[name='__RequestVerificationToken']").first.get_attribute("value")
                    if not token:
                        raise ValueError("MSC request verification token not found in page")

                    if self.playwright_request_delay_seconds > 0:
                        page.wait_for_timeout(int(self.playwright_request_delay_seconds * 1000))

                    response_payload = page.evaluate(
                        """
                        async ({ url, token, trackingMode, reference, maxBytes, timeoutMs }) => {
                            const controller = new AbortController();
                            let timer;
                            const deadline = new Promise((_, reject) => {
                                timer = setTimeout(() => {
                                    controller.abort();
                                    reject(new Error("MSC TrackingInfo response deadline exceeded"));
                                }, timeoutMs);
                            });
                            try {
                                const body = new URLSearchParams({
                                    "__RequestVerificationToken": token,
                                    "trackingMode": trackingMode,
                                    "trackingNumber": reference,
                                });
                                const response = await Promise.race([fetch(url, {
                                    signal: controller.signal,
                                    method: "POST",
                                    credentials: "same-origin",
                                    headers: {
                                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                        "X-Requested-With": "XMLHttpRequest",
                                        "Accept": "application/json, text/plain, */*",
                                    },
                                    body,
                                }), deadline]);
                                const declaredLength = Number(response.headers.get("content-length") || "0");
                                if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
                                    controller.abort();
                                    return {
                                        status: response.status,
                                        responseTooLarge: true,
                                        responseBytes: declaredLength,
                                        url: response.url,
                                    };
                                }
                                const reader = response.body?.getReader();
                                if (!reader) {
                                    throw new Error("MSC TrackingInfo response has no readable body");
                                }
                                let total = 0;
                                const chunks = [];
                                while (true) {
                                    const { done, value } = await Promise.race([reader.read(), deadline]);
                                    if (done) break;
                                    total += value.byteLength;
                                    if (total > maxBytes) {
                                        controller.abort();
                                        return {
                                            status: response.status,
                                            responseTooLarge: true,
                                            responseBytes: total,
                                            url: response.url,
                                        };
                                    }
                                    chunks.push(value);
                                }
                                const bytes = new Uint8Array(total);
                                let offset = 0;
                                for (const chunk of chunks) {
                                    bytes.set(chunk, offset);
                                    offset += chunk.byteLength;
                                }
                                const text = new TextDecoder().decode(bytes);
                                return {
                                    status: response.status,
                                    text,
                                    responseBytes: total,
                                    url: response.url,
                                };
                            } finally {
                                clearTimeout(timer);
                                controller.abort();
                            }
                        }
                        """,
                        {
                            "url": self.tracking_api_url,
                            "token": token,
                            "trackingMode": tracking_mode,
                            "reference": reference,
                            "maxBytes": self.response_max_bytes,
                            "timeoutMs": timeout_ms,
                        },
                    )
                    response_status = int(response_payload.get("status", 0))
                    if response_payload.get("responseTooLarge"):
                        response_bytes = response_payload.get("responseBytes", "unknown")
                        raise ValueError(
                            f"MSC TrackingInfo response exceeds the {self.response_max_bytes}-byte limit "
                            f"({response_bytes} bytes)"
                        )
                    response_text = str(response_payload.get("text", ""))
                    response_url = str(response_payload.get("url", self.tracking_api_url))
                    if response_status >= 400:
                        snippet = response_text.strip().replace("\n", " ")[:240]
                        raise ValueError(f"MSC TrackingInfo failed HTTP {response_status}: {snippet}")
                    try:
                        payload = page.evaluate("payload => JSON.parse(payload)", response_text)
                    except Exception as exc:
                        snippet = response_text.strip().replace("\n", " ")[:240]
                        raise ValueError(f"MSC TrackingInfo returned non-JSON payload: {snippet}") from exc
                    if not isinstance(payload, dict):
                        raise ValueError("MSC TrackingInfo returned non-object JSON payload")
                    return payload, f"msc-playwright:{response_url}"
                finally:
                    browser.close()

        return run_sync_playwright(_run)


def _build_reference_attempts(shipment: ShipmentRef) -> list[tuple[str, str]]:
    attempts: list[tuple[str, str]] = []
    if shipment.booking_no:
        ref = _normalize_reference(shipment.booking_no)
        if ref:
            # MSC UI mode "1" = Booking Number. Prefer it because it returns sibling containers.
            attempts.append((ref, "1"))
    if shipment.container_no:
        refs = _split_container_references(shipment.container_no)
        if not refs:
            refs = [_normalize_reference(shipment.container_no)]
        for ref in refs:
            # MSC UI mode "0" = Container/Bill of Lading Number.
            attempts.append((ref, "0"))
    return _dedupe_reference_attempts(attempts)


def _limit_reference_attempts(attempts: list[tuple[str, str]], *, limit: int) -> list[tuple[str, str]]:
    if limit <= 0 or len(attempts) <= limit:
        return attempts

    limited = attempts[:limit]
    has_booking_attempt = any(tracking_mode == "1" for _, tracking_mode in limited)
    booking_attempt = next(((reference, mode) for reference, mode in attempts if mode == "1"), None)
    if booking_attempt is None or has_booking_attempt:
        return limited

    # Preserve the booking fallback when a task has many container values.
    return [*limited[:-1], booking_attempt]


def _split_container_references(reference: str) -> list[str]:
    tokens = [tok.strip().upper() for tok in re.split(r"[,\s]+", reference) if tok.strip()]
    return [token for token in tokens if re.match(r"^[A-Z]{4}\d{7}$", token)]


def _dedupe_reference_attempts(attempts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for reference, tracking_mode in attempts:
        if not reference:
            continue
        key = (reference.upper(), tracking_mode)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((reference, tracking_mode))
    return deduped


def _normalize_reference(reference: str) -> str:
    cleaned = reference.strip()
    if not cleaned:
        return cleaned
    tokens = [tok.strip() for tok in re.split(r"[,\s]+", cleaned) if tok.strip()]
    if not tokens:
        return cleaned
    for token in tokens:
        if re.match(r"^[A-Za-z]{4}\d{7}$", token):
            return token
    return tokens[0]


def _status_from_payload(
    *,
    payload: dict[str, Any],
    source: str,
    source_url: str,
    eta_only_mode: bool,
) -> ShipmentStatus:
    if payload.get("IsSuccess") is not True:
        message = (
            extract_first(payload, ["ErrorMessage", "errorMessage", "Message", "message"])
            or _extract_unsuccessful_payload_message(payload)
            or "MSC TrackingInfo returned unsuccessful response"
        )
        raise ValueError(message)

    data = payload.get("Data")
    if not isinstance(data, dict):
        raise ValueError("MSC TrackingInfo payload missing Data object")

    bill = _pick_bill(data)
    container = _pick_container(bill)
    discovered_containers = extract_container_numbers(payload)
    events = _extract_events(container)
    recent_moves = _events_to_moves(events)
    latest_move = recent_moves[0] if recent_moves else None

    eta_local_text = _extract_eta_local_text(container, bill)
    eta_time = parse_event_time(eta_local_text)
    if eta_time is None:
        eta_time, eta_local_text = _derive_eta_from_moves(recent_moves)

    vessel_voyage = _extract_vessel_voyage(container, bill)

    if eta_only_mode:
        return ShipmentStatus(
            status_text=_eta_status_text(eta_time),
            eta_time=eta_time,
            eta_local_text=eta_local_text,
            latest_move=latest_move,
            recent_moves=recent_moves,
            discovered_containers=discovered_containers,
            raw_source=source,
            source_url=source_url,
            vessel_voyage=vessel_voyage,
        )

    return ShipmentStatus(
        status_text=(latest_move.name if latest_move else _eta_status_text(eta_time)),
        location=latest_move.location if latest_move else None,
        event_time=latest_move.event_time if latest_move else None,
        eta_time=eta_time,
        eta_local_text=eta_local_text,
        latest_move=latest_move,
        recent_moves=recent_moves,
        discovered_containers=discovered_containers,
        raw_source=source,
        source_url=source_url,
        movement_details=latest_move.name if latest_move else None,
        vessel_voyage=vessel_voyage,
    )


def _pick_bill(data: dict[str, Any]) -> dict[str, Any]:
    bills = data.get("BillOfLadings")
    if isinstance(bills, list):
        for bill in bills:
            if isinstance(bill, dict):
                return bill
    return {}


def _pick_container(bill: dict[str, Any]) -> dict[str, Any]:
    containers = bill.get("ContainersInfo")
    if isinstance(containers, list):
        for container in containers:
            if isinstance(container, dict):
                return container
    return {}


def _extract_events(container: dict[str, Any]) -> list[dict[str, Any]]:
    events = container.get("Events")
    if not isinstance(events, list):
        return []
    return [event for event in events if isinstance(event, dict)]


def _events_to_moves(events: list[dict[str, Any]]) -> list[MovementEvent]:
    moves: list[MovementEvent] = []
    for event in events:
        description = _extract_description(event)
        detail = _extract_detail(event)
        fallback_name = description
        if detail and description:
            fallback_name = f"{description} ({detail})"

        local_time_text = extract_first(event, ["Date", "EventDate", "eventDateTime", "date"])
        location = extract_first(event, ["Location", "location", "LocationName", "locationName", "Port", "port"])
        state_hint = extract_event_state_hint(event, extra_keys=["Detail"])

        moves.append(
            MovementEvent(
                name=to_dcsa_movement_name(event=event, fallback_name=fallback_name),
                location=location,
                event_time=parse_event_time(local_time_text),
                event_time_local_text=local_time_text,
                event_state=_normalize_event_state(state_hint),
                vessel_voyage=extract_event_vessel_voyage(event),
            )
        )

    return sorted(
        moves,
        key=lambda m: (m.event_time is not None, m.event_time.isoformat() if m.event_time else ""),
        reverse=True,
    )


def _extract_description(event: dict[str, Any]) -> str | None:
    return extract_first(event, ["Description", "description", "EventName", "eventName", "LatestMove", "latestMove"])


def _extract_detail(event: dict[str, Any]) -> str | None:
    detail = event.get("Detail")
    if isinstance(detail, list):
        parts = [str(item).strip() for item in detail if str(item).strip()]
        if parts:
            return ", ".join(parts)
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return None


def _extract_eta_local_text(container: dict[str, Any], bill: dict[str, Any]) -> str | None:
    eta = extract_first(container, ["PodEtaDate", "POD ETA", "Eta", "ETA"])
    if eta:
        return eta
    general = bill.get("GeneralTrackingInfo")
    if isinstance(general, dict):
        return extract_first(general, ["FinalPodEtaDate", "PodEtaDate", "ETA", "Eta"])
    return None


def _derive_eta_from_moves(moves: list[MovementEvent]) -> tuple[datetime | None, str | None]:
    for move in moves:
        if "arriv" in move.name.lower() and move.event_time is not None:
            return move.event_time, move.event_time_local_text
    return None, None


def _extract_vessel_voyage(container: dict[str, Any], bill: dict[str, Any]) -> str | None:
    vessel = extract_first(
        container,
        ["FinalPodVesselName", "PodVesselName", "VesselName", "Vessel", "vesselName", "vessel"],
    ) or extract_first(
        bill,
        ["FinalPodVesselName", "PodVesselName", "VesselName", "Vessel", "vesselName", "vessel"],
    )
    voyage = extract_first(
        container,
        ["FinalPodVoyage", "PodVoyage", "VoyageNumber", "VoyageNo", "Voyage", "voyageNumber", "voyage"],
    ) or extract_first(
        bill,
        ["FinalPodVoyage", "PodVoyage", "VoyageNumber", "VoyageNo", "Voyage", "voyageNumber", "voyage"],
    )
    return render_vessel_voyage(vessel, voyage)


def _eta_status_text(eta: datetime | None) -> str:
    if eta is None:
        return "ETA unavailable"
    return f"ETA {eta.isoformat()}"


def _append_raw_source(raw_source: str | None, extra: str) -> str:
    base = (raw_source or "").strip()
    if not base:
        return extra
    return f"{base} | {extra}"


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int_env(key: str, *, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _normalize_event_state(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"actual", "act", "completed", "complete", "confirmed"}:
        return "actual"
    if normalized in {"estimated", "estimate", "est", "planned", "plan", "forecast", "expected", "scheduled"}:
        return "estimated"
    return normalized


def _extract_unsuccessful_payload_message(payload: dict[str, Any]) -> str | None:
    data = payload.get("Data")
    if isinstance(data, str):
        cleaned = data.strip()
        return cleaned or None
    return None


def _wait_for_msc_page_ready(page: Any, *, timeout_ms: int, challenge_timeout_seconds: int, reload_attempts: int) -> None:
    deadline = time.monotonic() + max(1, challenge_timeout_seconds)
    reloads_used = 0
    while True:
        try:
            page.wait_for_selector(
                "input[name='__RequestVerificationToken']",
                timeout=min(timeout_ms, 3000),
                state="attached",
            )
            html = page.content().lower()
            if "access denied" not in html and "just a moment" not in html and "security check" not in html:
                return
        except Exception:
            pass

        html = page.content().lower()
        if "access denied" not in html and "just a moment" not in html and "security check" not in html:
            return
        if time.monotonic() >= deadline:
            if reloads_used < max(0, reload_attempts):
                page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
                reloads_used += 1
                deadline = time.monotonic() + max(1, challenge_timeout_seconds)
                continue
            if "access denied" in html:
                raise ValueError("MSC page access denied in browser session")
            raise ValueError("MSC page challenge did not clear in browser session")
        page.wait_for_timeout(1000)


_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'vendor', {get: () => 'Google Inc.'});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4]});
window.chrome = window.chrome || { runtime: {} };
"""
