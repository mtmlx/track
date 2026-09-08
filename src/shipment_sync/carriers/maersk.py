import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import requests

from shipment_sync.carriers.base import CarrierAdapter
from shipment_sync.carriers.common import (
    bounded_response_json,
    reject_response_redirect,
    CarrierResponseLimitError,
    extract_container_numbers,
    extract_event_vessel_voyage,
    extract_final_destination_vessel_voyage,
    extract_event_state_hint,
    extract_eta_time,
    extract_first,
    extract_json_from_http_response,
    get_with_retries,
    parse_event_time,
    to_dcsa_movement_name,
)
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from shipment_sync.playwright_runner import run_sync_playwright


@dataclass(frozen=True)
class MaerskCredentialProfile:
    name: str
    api_key: str
    consumer_key: str
    bearer_token: str
    oauth_token_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_scope: str

    @property
    def is_configured(self) -> bool:
        return bool(
            self.bearer_token
            or (
                self.oauth_token_url
                and (self.oauth_client_id or self.consumer_key)
                and (self.oauth_client_secret or self.api_key)
            )
        )


class MaerskAdapter(CarrierAdapter):
    def __init__(self) -> None:
        self.eta_only_mode = _env_bool("SHIPMENT_ETA_ONLY", default=True)
        self.api_mode = os.getenv("MAERSK_API_MODE", "auto").strip().lower()
        self.url_template = os.getenv("MAERSK_TRACKING_URL_TEMPLATE", "").strip()
        self.api_url = os.getenv("MAERSK_TRACKING_API_URL", "").strip()
        self.api_key_header = os.getenv("MAERSK_API_KEY_HEADER", "X-API-Key")
        self.ref_param = os.getenv("MAERSK_REF_PARAM", "reference")
        self.type_param = os.getenv("MAERSK_TYPE_PARAM", "referenceType")
        self.default_credentials = _credentials_from_env("MAERSK", name="default")
        self.mexico_credentials = _credentials_from_env(
            "MAERSK_MEXICO",
            name="mexico",
            oauth_defaults=self.default_credentials,
        )
        self.mexico_list_ids = _csv_set("MAERSK_MEXICO_LIST_IDS")
        self.mexico_credential_profile = (
            os.getenv("MAERSK_MEXICO_CREDENTIAL_PROFILE", "default").strip().lower() or "default"
        )
        self.api_version = os.getenv("MAERSK_API_VERSION", "1").strip()
        self.events_limit = int(os.getenv("MAERSK_EVENTS_LIMIT", "100"))
        if self.events_limit <= 0:
            self.events_limit = 100
        self.fetch_all_events = _env_bool("MAERSK_FETCH_ALL_EVENTS", default=True)
        self.web_fallback_enabled = _env_bool("MAERSK_WEB_FALLBACK_ON_API_ERROR", default=True)
        self.public_browser_fallback_enabled = _env_bool("MAERSK_PUBLIC_BROWSER_FALLBACK", default=True)
        self.public_browser_timeout_seconds = int(os.getenv("MAERSK_PUBLIC_BROWSER_TIMEOUT_SECONDS", "45"))
        self.public_browser_wait_seconds = float(os.getenv("MAERSK_PUBLIC_BROWSER_WAIT_SECONDS", "5"))
        self.public_browser = os.getenv("MAERSK_PUBLIC_BROWSER", "chromium").strip() or "chromium"
        self.public_browser_channel = os.getenv("MAERSK_PUBLIC_BROWSER_CHANNEL", "chrome").strip()
        self.public_browser_user_agent = (
            os.getenv(
                "MAERSK_PUBLIC_BROWSER_USER_AGENT",
                (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            ).strip()
            or (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        self.timeout_seconds = int(os.getenv("MAERSK_TIMEOUT_SECONDS", "60"))
        self.max_retries = int(os.getenv("MAERSK_MAX_RETRIES", "2"))
        self.retry_delay_seconds = float(os.getenv("MAERSK_RETRY_DELAY_SECONDS", "2"))
        self.session = requests.Session()
        self._oauth_tokens: dict[str, tuple[str, datetime]] = {}

    def fetch_status(self, shipment: ShipmentRef) -> ShipmentStatus:
        candidates = _pick_references(shipment)
        credentials = self._credentials_for(shipment)
        fallback_status: ShipmentStatus | None = None
        for reference, ref_type in candidates:
            status = self._fetch_status_for_reference(reference, ref_type, credentials)
            if _status_has_carrier_data(status):
                return status
            if fallback_status is None:
                fallback_status = status

        if fallback_status is not None:
            return fallback_status
        raise ValueError("Missing booking/container number")

    def fetch_dcsa_events(self, shipment: ShipmentRef) -> tuple[list[dict[str, Any]], str]:
        """Fetch official Maersk Events API payloads without any web fallback.

        This method is used exclusively by the DCSA shadow lane. It must not
        convert the public tracking website into events because that would make
        payload-version validation and carrier provenance meaningless.
        """

        credentials = self._credentials_for(shipment)
        if self.api_mode != "events" or not self.api_url or not credentials.is_configured:
            raise ValueError(
                "Maersk DCSA shadow ingestion requires MAERSK_API_MODE=events and MAERSK_TRACKING_API_URL."
            )
        headers = self._build_events_api_headers(credentials)
        for reference, ref_type in _pick_references(shipment):
            events = self._fetch_all_events(reference, ref_type, headers)
            if events:
                return events, self.api_url
        return [], self.api_url

    def _fetch_status_for_reference(
        self,
        reference: str,
        ref_type: str,
        credentials: MaerskCredentialProfile,
    ) -> ShipmentStatus:
        source_url = _build_maersk_tracking_url(reference)
        payload, source = self._fetch_payload(reference, ref_type, credentials)
        discovered_containers = extract_container_numbers(payload)
        events = _extract_events(payload)
        payload_eta = extract_eta_time(payload)
        payload_eta_raw = _extract_eta_raw(payload)

        if events:
            status = _status_from_events(events, source, source_url=source_url)
            status.discovered_containers = discovered_containers
            public_vessel_voyage = payload.get("public_vessel_voyage")
            if isinstance(public_vessel_voyage, str) and public_vessel_voyage.strip():
                status.vessel_voyage = public_vessel_voyage.strip()
            if not status.eta_time:
                status.eta_time = payload_eta
            if not status.eta_local_text:
                status.eta_local_text = payload_eta_raw
            if self.eta_only_mode:
                status.status_text = _eta_status_text(status.eta_time)
            return status

        if self.eta_only_mode:
            return ShipmentStatus(
                status_text=_eta_status_text(payload_eta),
                eta_time=payload_eta,
                eta_local_text=payload_eta_raw,
                discovered_containers=discovered_containers,
                raw_source=source,
                source_url=source_url,
            )

        status_text = extract_first(
            payload,
            ["status", "shipmentStatus", "transportStatus", "latestEvent", "eventDescription"],
        ) or "Unknown"
        location = extract_first(payload, ["location", "locationName", "city", "port", "eventLocation"])
        event_time_raw = extract_first(
            payload,
            ["eventTime", "eventDateTime", "actualTime", "timestamp", "dateTime", "date"],
        )

        movement_details = extract_first(
            payload,
            ["latestEvent", "eventDescription", "transportStatus", "shipmentStatus"],
        )

        return ShipmentStatus(
            status_text=status_text,
            location=location,
            event_time=parse_event_time(event_time_raw),
            eta_time=payload_eta,
            eta_local_text=payload_eta_raw,
            discovered_containers=discovered_containers,
            raw_source=source,
            source_url=source_url,
            movement_details=movement_details,
        )

    def _fetch_payload(
        self,
        reference: str,
        ref_type: str,
        credentials: MaerskCredentialProfile,
    ) -> tuple[dict, str]:
        source_url = _build_maersk_tracking_url(reference)
        headers = {self.api_key_header: credentials.api_key} if credentials.api_key else {}

        if self.url_template:
            url = self.url_template.format(reference=reference, type=ref_type)
            response = get_with_retries(
                self.session,
                url,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                retry_delay_seconds=self.retry_delay_seconds,
            )
            payload = extract_json_from_http_response(response)
            return payload, f"maersk-web:{url}"

        if not self.api_url:
            fallback_payload, fallback_source = self._try_web_fallback(reference, ref_type, reason="missing_api_url")
            if fallback_payload is not None:
                return fallback_payload, fallback_source
            raise ValueError("Set MAERSK_TRACKING_URL_TEMPLATE or MAERSK_TRACKING_API_URL")

        if self._use_maersk_events_api(credentials):
            events_headers = self._build_events_api_headers(credentials)
            try:
                if self.fetch_all_events:
                    all_events = self._fetch_all_events(reference, ref_type, events_headers)
                    return {"events": all_events}, f"maersk-events-api:{self.api_url}"

                params = _events_params(reference, ref_type, self.events_limit, cursor="1")
                response = get_with_retries(
                    self.session,
                    self.api_url,
                    params=params,
                    headers=events_headers,
                    timeout_seconds=self.timeout_seconds,
                    max_retries=self.max_retries,
                    retry_delay_seconds=self.retry_delay_seconds,
                    non_retry_statuses={401, 403, 404},
                )
                payload = response.json()
                if isinstance(payload, list):
                    return {"events": payload}, f"maersk-events-api:{self.api_url}"
                if isinstance(payload, dict):
                    return payload, f"maersk-events-api:{self.api_url}"
                return {"events": []}, f"maersk-events-api:{self.api_url}"
            except requests.HTTPError as exc:
                if _http_status(exc) == 404:
                    public_status = self._try_public_browser_fallback(reference)
                    if public_status is not None:
                        return {
                            "events": _public_status_events(public_status),
                            "public_vessel_voyage": public_status.vessel_voyage,
                        }, public_status.raw_source or source_url
                    return {"events": []}, f"maersk-events-api:not-found:{self.api_url}"
                if not _should_try_web_fallback(exc):
                    raise
                fallback_payload, fallback_source = self._try_web_fallback(reference, ref_type, reason="events_api_error")
                if fallback_payload is not None:
                    return fallback_payload, fallback_source
                raise
            except requests.RequestException:
                fallback_payload, fallback_source = self._try_web_fallback(reference, ref_type, reason="events_api_error")
                if fallback_payload is not None:
                    return fallback_payload, fallback_source
                raise

        params = {
            self.ref_param: reference,
            self.type_param: ref_type,
        }
        response = get_with_retries(
            self.session,
            self.api_url,
            params=params,
            headers=headers,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        payload = extract_json_from_http_response(response)
        return payload, f"maersk-api:{self.api_url}"

    def _try_public_browser_fallback(self, reference: str) -> ShipmentStatus | None:
        if not self.public_browser_fallback_enabled:
            return None

        source_url = _build_maersk_tracking_url(reference)

        def _run() -> ShipmentStatus | None:
            try:
                from playwright.sync_api import sync_playwright
            except Exception:
                return None

            timeout_ms = max(1, self.public_browser_timeout_seconds) * 1000
            wait_ms = max(0, int(self.public_browser_wait_seconds * 1000))
            with sync_playwright() as p:
                browser_type = getattr(p, self.public_browser, None)
                if browser_type is None:
                    return None

                launch_kwargs: dict[str, Any] = {
                    "headless": True,
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if self.public_browser_channel and self.public_browser == "chromium":
                    launch_kwargs["channel"] = self.public_browser_channel

                browser = browser_type.launch(**launch_kwargs)
                try:
                    context = browser.new_context(
                        user_agent=self.public_browser_user_agent,
                        locale="en-US",
                        viewport={"width": 1440, "height": 1000},
                    )
                    context.set_extra_http_headers(
                        {
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                            "Accept-Language": "en-US,en;q=0.9",
                        }
                    )
                    context.add_init_script(_MAERSK_STEALTH_INIT_SCRIPT)
                    page = context.new_page()
                    page.goto(source_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    page.wait_for_function(
                        "document.querySelector('main')?.innerText.includes('Latest event')",
                        timeout=timeout_ms,
                    )
                    if wait_ms:
                        page.wait_for_timeout(wait_ms)
                    return _status_from_public_tracking_text(
                        page.locator("main").inner_text(timeout=timeout_ms),
                        source_url=source_url,
                    )
                finally:
                    browser.close()

        try:
            return run_sync_playwright(_run)
        except Exception as exc:
            print(
                f"Maersk public browser fallback failed for {reference}: {exc}",
                file=sys.stderr,
            )
            return None

    def _try_web_fallback(self, reference: str, ref_type: str, *, reason: str) -> tuple[dict | None, str]:
        if not self.web_fallback_enabled:
            return None, ""
        url_template = self.url_template or "https://www.maersk.com/tracking/{reference}"
        url = url_template.format(reference=reference, type=ref_type)
        response = get_with_retries(
            self.session,
            url,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            retry_delay_seconds=self.retry_delay_seconds,
        )
        payload = extract_json_from_http_response(response)
        return payload, f"maersk-web-fallback:{reason}:{url}"

    def _fetch_all_events(self, reference: str, ref_type: str, headers: dict[str, str]) -> list[dict]:
        all_events: list[dict] = []
        seen_cursors: set[str] = set()
        cursor = "1"

        for _ in range(100):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)

            params = _events_params(reference, ref_type, self.events_limit, cursor=cursor)
            response = get_with_retries(
                self.session,
                self.api_url,
                params=params,
                headers=headers,
                timeout_seconds=self.timeout_seconds,
                max_retries=self.max_retries,
                retry_delay_seconds=self.retry_delay_seconds,
                non_retry_statuses={401, 403, 404},
            )
            payload = response.json()
            event_batch = _extract_events(payload if isinstance(payload, dict) else {"events": payload})
            if event_batch:
                all_events.extend(event_batch)

            next_cursor = _extract_next_cursor(payload, cursor=cursor, page_size=self.events_limit, count=len(event_batch))
            if not next_cursor:
                break
            cursor = next_cursor

        return all_events

    def _credentials_for(self, shipment: ShipmentRef) -> MaerskCredentialProfile:
        if shipment.list_id in self.mexico_list_ids:
            if self.mexico_credential_profile == "default":
                return self.default_credentials
            if self.mexico_credential_profile not in {"mexico", "scoped"}:
                raise ValueError(
                    "MAERSK_MEXICO_CREDENTIAL_PROFILE must be 'default' or 'mexico'."
                )
            if not self.mexico_credentials.is_configured:
                raise ValueError(
                    "Maersk Mexico credential profile is selected for this list but is not fully configured."
                )
            return self.mexico_credentials
        return self.default_credentials

    def _use_maersk_events_api(self, credentials: MaerskCredentialProfile) -> bool:
        if self.api_mode == "events":
            return True
        if self.api_mode == "legacy":
            return False
        return credentials.is_configured

    def _build_events_api_headers(self, credentials: MaerskCredentialProfile) -> dict[str, str]:
        headers: dict[str, str] = {"API-Version": self.api_version}
        if credentials.consumer_key:
            headers["Consumer-Key"] = credentials.consumer_key
        if credentials.bearer_token:
            headers["Authorization"] = f"Bearer {credentials.bearer_token}"
            return headers
        access_token = self._get_oauth_access_token(credentials)
        headers["Authorization"] = f"Bearer {access_token}"
        return headers

    def _get_oauth_access_token(self, credentials: MaerskCredentialProfile) -> str:
        now = datetime.now(timezone.utc)
        cached = self._oauth_tokens.get(credentials.name)
        if cached and now < cached[1]:
            return cached[0]

        token_url = credentials.oauth_token_url
        client_id = credentials.oauth_client_id or credentials.consumer_key
        client_secret = credentials.oauth_client_secret or credentials.api_key
        if not token_url or not client_id or not client_secret:
            raise ValueError(
                "For Maersk events API set either MAERSK_BEARER_TOKEN "
                "or MAERSK_OAUTH_TOKEN_URL + MAERSK_OAUTH_CLIENT_ID/SECRET"
            )

        base_data = {"grant_type": "client_credentials"}
        if credentials.oauth_scope:
            base_data["scope"] = credentials.oauth_scope

        base_headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if credentials.consumer_key:
            base_headers["Consumer-Key"] = credentials.consumer_key

        attempts = [
            {
                "auth": (client_id, client_secret),
                "data": dict(base_data),
                "headers": dict(base_headers),
            },
            {
                "auth": None,
                "data": {
                    **base_data,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                "headers": dict(base_headers),
            },
        ]

        last_error: Exception | None = None
        for attempt in attempts:
            try:
                response = self.session.post(
                    token_url,
                    auth=attempt["auth"],
                    data=attempt["data"],
                    headers=attempt["headers"],
                    timeout=self.timeout_seconds,
                    stream=True,
                    allow_redirects=False,
                    hooks={"response": reject_response_redirect},
                )
                try:
                    response.raise_for_status()
                    payload = bounded_response_json(response)
                finally:
                    response.close()
                token = payload.get("access_token")
                if isinstance(token, str) and token:
                    expires_in = payload.get("expires_in", 300)
                    try:
                        ttl_seconds = int(expires_in)
                    except Exception:
                        ttl_seconds = 300
                    self._oauth_tokens[credentials.name] = (
                        token,
                        now + timedelta(seconds=max(60, ttl_seconds - 30)),
                    )
                    return token
            except CarrierResponseLimitError:
                raise
            except Exception as exc:
                last_error = exc

        if last_error:
            raise ValueError(f"Maersk OAuth token request failed: {last_error}")
        raise ValueError("Maersk OAuth token response missing access_token")


def _pick_references(shipment: ShipmentRef) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []

    if shipment.container_no:
        container_tokens = _split_container_references(shipment.container_no)
        if container_tokens:
            candidates.extend((token, "container") for token in container_tokens)
        else:
            candidates.append((_normalize_reference(shipment.container_no), "container"))

    if shipment.booking_no:
        candidates.append((_normalize_reference(shipment.booking_no), "booking"))

    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for reference, ref_type in candidates:
        if not reference:
            continue
        key = (reference.upper(), ref_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((reference, ref_type))
    return deduped


def _credentials_from_env(
    prefix: str,
    *,
    name: str,
    oauth_defaults: MaerskCredentialProfile | None = None,
) -> MaerskCredentialProfile:
    oauth_token_url = os.getenv(f"{prefix}_OAUTH_TOKEN_URL", "").strip()
    oauth_scope = os.getenv(f"{prefix}_OAUTH_SCOPE", "").strip()
    return MaerskCredentialProfile(
        name=name,
        api_key=os.getenv(f"{prefix}_API_KEY", "").strip(),
        consumer_key=os.getenv(f"{prefix}_CONSUMER_KEY", "").strip(),
        bearer_token=os.getenv(f"{prefix}_BEARER_TOKEN", "").strip(),
        oauth_token_url=oauth_token_url or (oauth_defaults.oauth_token_url if oauth_defaults else ""),
        oauth_client_id=os.getenv(f"{prefix}_OAUTH_CLIENT_ID", "").strip(),
        oauth_client_secret=os.getenv(f"{prefix}_OAUTH_CLIENT_SECRET", "").strip(),
        oauth_scope=oauth_scope or (oauth_defaults.oauth_scope if oauth_defaults else ""),
    )


def _csv_set(key: str) -> set[str]:
    return {value.strip() for value in os.getenv(key, "").split(",") if value.strip()}


def _split_container_references(reference: str) -> list[str]:
    tokens = [tok.strip().upper() for tok in re.split(r"[,\s]+", reference) if tok.strip()]
    return [token for token in tokens if re.match(r"^[A-Z]{4}\d{7}$", token)]


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


def _events_params(reference: str, ref_type: str, limit: int, *, cursor: str) -> dict[str, str]:
    params: dict[str, str] = {"limit": str(limit), "cursor": cursor}
    if ref_type == "container":
        params["equipmentReference"] = reference
    else:
        params["carrierBookingReference"] = reference
    return params


def _public_status_events(status: ShipmentStatus) -> list[dict[str, str]]:
    events: list[dict[str, str]] = []
    for move in status.recent_moves:
        if move.event_time is None:
            continue
        event: dict[str, str] = {
            "eventDateTime": move.event_time.isoformat(),
            "locationName": move.location or "",
            "estimatedArrivalDateTime": status.eta_time.isoformat() if status.eta_time else "",
        }
        if "(DEPA)" in move.name:
            event["eventType"] = "TRANSPORT"
            event["transportEventTypeCode"] = "DEPA"
        elif "(ARRI)" in move.name:
            event["eventType"] = "TRANSPORT"
            event["transportEventTypeCode"] = "ARRI"
        else:
            match = re.search(r"\(([A-Z]{4})\)$", move.name)
            if not match:
                continue
            event["eventType"] = "EQUIPMENT"
            event["equipmentEventTypeCode"] = match.group(1)
        events.append(event)

    if events and status.vessel_voyage:
        vessel, _, voyage = status.vessel_voyage.rpartition(" ")
        events[-1]["vesselName"] = vessel or status.vessel_voyage
        if voyage:
            events[-1]["carrierExportVoyageNumber"] = voyage
    return events


def _status_from_public_tracking_text(text: str, *, source_url: str) -> ShipmentStatus | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    eta_time, eta_local_text = _public_tracking_eta(lines)
    recent_moves = _public_tracking_moves(lines)
    latest_move = _public_tracking_latest_move(lines)
    if latest_move is None and recent_moves:
        actual_moves = [move for move in recent_moves if _event_is_actual_or_today(move)]
        latest_move = actual_moves[-1] if actual_moves else recent_moves[-1]
    if eta_time is None and latest_move is None and not recent_moves:
        return None

    return ShipmentStatus(
        status_text=_eta_status_text(eta_time),
        location=latest_move.location if latest_move else None,
        event_time=latest_move.event_time if latest_move else None,
        eta_time=eta_time,
        eta_local_text=eta_local_text,
        latest_move=latest_move,
        recent_moves=recent_moves,
        discovered_containers=extract_container_numbers(text),
        raw_source=f"maersk-public-browser:{source_url}",
        source_url=source_url,
        movement_details=latest_move.name if latest_move else None,
        vessel_voyage=_public_tracking_final_vessel_voyage(lines),
    )


def _public_tracking_eta(lines: list[str]) -> tuple[datetime | None, str | None]:
    for index, line in enumerate(lines):
        if line.lower() == "estimated arrival date" and index + 1 < len(lines):
            return _parse_public_tracking_datetime(lines[index + 1])
    return None, None


def _public_tracking_latest_move(lines: list[str]) -> MovementEvent | None:
    for index, line in enumerate(lines):
        if line.lower() != "latest event" or index + 1 >= len(lines):
            continue
        parts = [part.strip() for part in lines[index + 1].split("•")]
        if len(parts) < 3:
            return None
        event_time, local_text = _parse_public_tracking_datetime(parts[-1])
        return MovementEvent(
            name=_public_tracking_event_name(parts[0]),
            location=parts[1] or None,
            event_time=event_time,
            event_time_local_text=local_text,
            event_state="actual",
        )
    return None


def _public_tracking_moves(lines: list[str]) -> list[MovementEvent]:
    moves: list[MovementEvent] = []
    for index, line in enumerate(lines):
        normalized = line.lower()
        if normalized == "gate out empty":
            event_name = "Empty Container Release to Shipper (GTOT)"
        elif normalized == "gate in":
            event_name = "Container Gated In (GTIN)"
        elif normalized.startswith("load on "):
            event_name = "Container Loaded (LOAD)"
        elif normalized.startswith("vessel departure") and "•" not in line:
            event_name = "Transport Departed (DEPA)"
        elif normalized.startswith("vessel arrival") and "•" not in line:
            event_name = "Transport Arrived (ARRI)"
        else:
            continue
        event_time, local_text = _next_public_tracking_datetime(lines, index)
        if event_time is None:
            continue
        moves.append(
            MovementEvent(
                name=event_name,
                location=_public_tracking_location_before(lines, index),
                event_time=event_time,
                event_time_local_text=local_text,
                event_state="actual" if event_time.date() <= datetime.now(timezone.utc).date() else "estimated",
            )
        )
    return moves


def _public_tracking_final_vessel_voyage(lines: list[str]) -> str | None:
    vessels: list[str] = []
    for line in lines:
        match = re.search(r"(?:load on|vessel departure|vessel arrival)\s*\(([^)]+)\)", line, re.IGNORECASE)
        if match:
            vessels.append(match.group(1))
        elif line.lower().startswith("load on "):
            vessels.append(line[8:])
    if not vessels:
        return None
    return re.sub(r"\s*/\s*", " ", vessels[-1]).strip() or None


def _public_tracking_event_name(value: str) -> str:
    normalized = value.lower()
    if "departure" in normalized:
        return "Transport Departed (DEPA)"
    if "arrival" in normalized:
        return "Transport Arrived (ARRI)"
    if "load" in normalized:
        return "Container Loaded (LOAD)"
    return value or "Unknown"


def _next_public_tracking_datetime(lines: list[str], index: int) -> tuple[datetime | None, str | None]:
    for candidate in lines[index + 1 : index + 4]:
        parsed, local_text = _parse_public_tracking_datetime(candidate)
        if parsed is not None:
            return parsed, local_text
    return None, None


def _parse_public_tracking_datetime(value: str) -> tuple[datetime | None, str | None]:
    parsed: datetime | None = None
    for date_format in ("%d %b %Y %H:%M", "%d %b %Y"):
        try:
            parsed = datetime.strptime(value.strip(), date_format).replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if parsed is None:
        return None, None
    return parsed, parsed.strftime("%Y-%m-%d %H:%M")


def _public_tracking_location_before(lines: list[str], index: int) -> str | None:
    for candidate in reversed(lines[max(0, index - 8) : index]):
        if candidate.upper() == candidate and any(char.isalpha() for char in candidate):
            return candidate
    return None


def _event_is_actual_or_today(move: MovementEvent) -> bool:
    return move.event_time is not None and move.event_time.date() <= datetime.now(timezone.utc).date()


_MAERSK_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def _extract_events(payload: dict) -> list[dict]:
    if isinstance(payload.get("events"), list):
        return [e for e in payload["events"] if isinstance(e, dict)]
    if isinstance(payload.get("data"), list):
        return [e for e in payload["data"] if isinstance(e, dict)]
    return []


def _extract_next_cursor(payload: object, *, cursor: str, page_size: int, count: int) -> str | None:
    if isinstance(payload, dict):
        direct = _first_text(payload, ["nextCursor", "next_cursor", "nextPage", "next_page", "cursorNext"])
        if direct:
            return direct

        for parent_key in ("pageInfo", "pagination", "meta"):
            parent = payload.get(parent_key)
            if isinstance(parent, dict):
                nested = _first_text(parent, ["nextCursor", "next_cursor", "nextPage", "next_page"])
                if nested:
                    return nested
                has_next = _first_bool(parent, ["hasNext", "has_next", "hasMore", "has_more"])
                if has_next:
                    return _increment_cursor(cursor)

        has_next_payload = _first_bool(payload, ["hasNext", "has_next", "hasMore", "has_more"])
        if has_next_payload:
            return _increment_cursor(cursor)

    if count >= page_size:
        return _increment_cursor(cursor)
    return None


def _status_from_events(events: list[dict], source: str, *, source_url: str | None) -> ShipmentStatus:
    ordered = sorted(events, key=_event_sort_key, reverse=True)
    latest = ordered[0]
    final_arrival = _latest_estimated_transport_arrival(events)
    latest_move = _event_to_movement(latest)
    status_text = latest_move.name if latest_move and latest_move.name else "Unknown"
    location = extract_first(
        latest,
        ["locationName", "UNLocationCode", "facilityCode", "eventLocation", "city", "port"],
    )
    event_time_raw = extract_first(latest, ["eventDateTime", "eventCreatedDateTime", "dateTime", "timestamp"])
    event_time = parse_event_time(event_time_raw)
    event_id = extract_first(latest, ["eventID"])
    raw_source = source if not event_id else f"{source}:{event_id}"
    event_type = extract_first(latest, ["eventType"])
    classifier = extract_first(latest, ["eventClassifierCode"])
    movement_code = (
        extract_first(latest, ["equipmentEventTypeCode"])
        or extract_first(latest, ["shipmentEventTypeCode"])
        or extract_first(latest, ["transportEventTypeCode"])
    )
    detail_parts = [x for x in [event_type, classifier, movement_code] if x]
    movement_details = " / ".join(detail_parts) if detail_parts else None
    recent_moves = [_event_to_movement(event) for event in ordered]
    final_vessel_voyage = extract_event_vessel_voyage(final_arrival) if final_arrival else None
    vessel_voyage = final_vessel_voyage or extract_final_destination_vessel_voyage(events)
    eta_raw = _event_datetime_text(final_arrival) if final_arrival else _extract_eta_raw(latest)
    return ShipmentStatus(
        status_text=status_text,
        location=location,
        event_time=event_time,
        eta_time=parse_event_time(eta_raw) or extract_eta_time(latest),
        eta_local_text=eta_raw,
        latest_move=latest_move,
        recent_moves=recent_moves,
        raw_source=raw_source,
        source_url=source_url,
        movement_details=movement_details,
        vessel_voyage=vessel_voyage,
        final_vessel_voyage=final_vessel_voyage,
    )


def _event_sort_key(event: dict) -> tuple[int, str]:
    raw = (
        extract_first(event, ["eventDateTime"])
        or extract_first(event, ["eventCreatedDateTime"])
        or ""
    )
    dt = parse_event_time(raw)
    if dt:
        return (1, dt.isoformat())
    return (0, raw)


def _event_to_movement(event: dict) -> MovementEvent:
    raw_name = extract_first(
        event,
        [
            "eventDescription",
            "shipmentEventTypeCode",
            "equipmentEventTypeCode",
            "transportEventTypeCode",
            "eventType",
        ],
    )
    name = to_dcsa_movement_name(event=event, fallback_name=raw_name)
    location = extract_first(
        event,
        ["locationName", "UNLocationCode", "facilityCode", "eventLocation", "city", "port"],
    )
    local_time_text = (
        extract_first(event, ["eventDateTime"])
        or extract_first(event, ["eventCreatedDateTime"])
        or extract_first(event, ["dateTime"])
        or extract_first(event, ["timestamp"])
    )
    classifier = _event_classifier(event)
    return MovementEvent(
        name=name,
        location=location,
        event_time=parse_event_time(local_time_text),
        event_time_local_text=local_time_text,
        event_state=_normalize_event_state(classifier),
        vessel_voyage=extract_event_vessel_voyage(event),
    )


def _latest_estimated_transport_arrival(events: list[dict]) -> dict | None:
    candidates = [
        event
        for event in events
        if _event_classifier(event) == "estimated"
        and _normalized_event_code(event.get("transportEventTypeCode")) == "ARRI"
        and parse_event_time(_event_datetime_text(event)) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: parse_event_time(_event_datetime_text(event)).isoformat())


def _event_classifier(event: dict) -> str | None:
    for key in ("eventClassifierCode", "triggerType"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_event_state(value)
    return _normalize_event_state(extract_event_state_hint(event))


def _event_datetime_text(event: dict) -> str | None:
    return extract_first(event, ["eventDateTime", "eventLocalPortDate", "eventTime", "dateTime", "timestamp"])


def _normalized_event_code(value: object) -> str:
    return str(value or "").strip().upper()


def _extract_eta_raw(payload: dict) -> str | None:
    return extract_first(
        payload,
        [
            "eta",
            "etaDate",
            "etaTime",
            "etaDateTime",
            "estimatedArrival",
            "estimatedArrivalDate",
            "estimatedArrivalTime",
            "estimatedArrivalDateTime",
            "estimatedTimeOfArrival",
            "arrivalEstimate",
            "arrivalEstimatedTime",
            "arrivalDateEstimated",
            "plannedArrival",
            "plannedArrivalDate",
            "plannedArrivalTime",
            "plannedArrivalDateTime",
            "scheduledArrival",
            "scheduledArrivalDate",
            "scheduledArrivalTime",
            "scheduledArrivalDateTime",
            "vesselEta",
            "destinationEta",
            "podEta",
            "dischargeEta",
        ],
    )


def _eta_status_text(eta: datetime | None) -> str:
    if eta is None:
        return "ETA unavailable"
    return f"ETA {eta.isoformat()}"


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _first_text(payload: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _first_bool(payload: dict, keys: list[str]) -> bool | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "y", "1"}:
                return True
            if normalized in {"false", "no", "n", "0"}:
                return False
    return None


def _increment_cursor(cursor: str) -> str | None:
    try:
        return str(int(cursor) + 1)
    except Exception:
        return None


def _http_status(exc: requests.HTTPError) -> int | None:
    response = exc.response
    if response is None:
        return None
    return response.status_code


def _should_try_web_fallback(exc: requests.HTTPError) -> bool:
    status = _http_status(exc)
    if status is None:
        return True
    return status >= 500


def _status_has_carrier_data(status: ShipmentStatus) -> bool:
    return bool(
        status.latest_move
        or status.recent_moves
        or status.eta_time
        or status.eta_local_text
        or status.discovered_containers
    )


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


def _build_maersk_tracking_url(reference: str) -> str:
    return f"https://www.maersk.com/tracking/{quote(reference)}"
