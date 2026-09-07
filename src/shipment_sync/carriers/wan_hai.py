from __future__ import annotations

from datetime import datetime
import os
import re
import time
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from shipment_sync.carriers.base import CarrierAdapter
from shipment_sync.carriers.common import extract_container_numbers, parse_event_time, render_vessel_voyage
from shipment_sync.carriers.generic_line import GenericLineAdapter
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus
from shipment_sync.playwright_runner import configured_browser_channel, run_sync_playwright


class WanHaiAntiBotBlocked(ValueError):
    pass


class WanHaiAdapter(CarrierAdapter):
    def __init__(self) -> None:
        self.eta_only_mode = _env_bool("SHIPMENT_ETA_ONLY", default=True)
        self.use_playwright = _env_bool("WAN_HAI_USE_PLAYWRIGHT", default=True)
        self.playwright_required = _env_bool("WAN_HAI_PLAYWRIGHT_REQUIRED", default=False)
        self.playwright_headless = _env_bool("WAN_HAI_PLAYWRIGHT_HEADLESS", default=True)
        self.playwright_timeout_seconds = int(os.getenv("WAN_HAI_PLAYWRIGHT_TIMEOUT_SECONDS", "90"))
        self.playwright_request_delay_seconds = float(os.getenv("WAN_HAI_PLAYWRIGHT_REQUEST_DELAY_SECONDS", "8"))
        self.playwright_browser = os.getenv("WAN_HAI_PLAYWRIGHT_BROWSER", "chromium").strip() or "chromium"
        self.playwright_channel = os.getenv("WAN_HAI_PLAYWRIGHT_CHANNEL", "").strip()
        self.playwright_locale = os.getenv("WAN_HAI_PLAYWRIGHT_LOCALE", "").strip()
        self.playwright_user_agent = os.getenv("WAN_HAI_PLAYWRIGHT_USER_AGENT", "").strip()
        self.tracking_page_url = (
            os.getenv("WAN_HAI_PLAYWRIGHT_TRACKING_URL", "https://vip.wanhai.com/views/cargo_track_v2/tracking_query.xhtml").strip()
            or "https://vip.wanhai.com/views/cargo_track_v2/tracking_query.xhtml"
        )
        self.timeout_seconds = int(os.getenv("WAN_HAI_TIMEOUT_SECONDS", "45"))
        self.max_retries = int(os.getenv("WAN_HAI_MAX_RETRIES", "2"))
        self.retry_delay_seconds = float(os.getenv("WAN_HAI_RETRY_DELAY_SECONDS", "2"))

        self.generic_fallback = GenericLineAdapter(
            env_prefix="WAN_HAI",
            line_label="Wan Hai",
            default_page_url_template="https://www.wanhai.com/views/cargoTrack/CargoTrack.xhtml",
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
                raise ValueError(f"Wan Hai Playwright failed ({playwright_error}); fallback failed ({fallback_error})")
            raise

        if playwright_error is not None:
            status.raw_source = _append_raw_source(status.raw_source, f"wan_hai-playwright-error:{playwright_error}")
        return status

    def _fetch_status_playwright(self, shipment: ShipmentRef) -> ShipmentStatus:
        attempts = _build_reference_attempts(shipment)
        if not attempts:
            raise ValueError("Missing booking/container number")

        last_error: Exception | None = None
        for reference, cargo_type in attempts:
            for attempt in range(self.max_retries + 1):
                try:
                    status = self._playwright_request(reference=reference, cargo_type=cargo_type)
                    structured_booking = _normalize_reference(shipment.booking_no or "").upper()
                    if cargo_type == "2" and reference.upper() != structured_booking:
                        expected = set(extract_container_numbers(shipment.container_no or ""))
                        actual = set(extract_container_numbers(status.discovered_containers))
                        if not expected or not expected.issubset(actual):
                            raise ValueError("Wan Hai comment reference does not match the task containers")
                    return status
                except WanHaiAntiBotBlocked:
                    raise
                except Exception as exc:
                    last_error = exc
                    if attempt >= self.max_retries:
                        break
                    time.sleep(self.retry_delay_seconds * (attempt + 1))

        if last_error is not None:
            raise last_error
        raise ValueError("Wan Hai Playwright request failed without specific error")

    def _playwright_request(self, *, reference: str, cargo_type: str) -> ShipmentStatus:
        def _run() -> ShipmentStatus:
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                raise ValueError("Playwright is not installed. Run: pip install -e .[browser]") from exc

            timeout_ms = max(1, self.playwright_timeout_seconds) * 1000
            with sync_playwright() as p:
                browser_type = getattr(p, self.playwright_browser, None)
                if browser_type is None:
                    raise ValueError(f"Unsupported Playwright browser type: {self.playwright_browser}")

                launch_kwargs: dict[str, Any] = {"headless": self.playwright_headless}
                channel = configured_browser_channel(
                    self.playwright_channel,
                    browser_name=self.playwright_browser,
                )
                if channel:
                    launch_kwargs["channel"] = channel

                browser = browser_type.launch(**launch_kwargs)
                try:
                    context_kwargs: dict[str, Any] = {}
                    if self.playwright_user_agent:
                        context_kwargs["user_agent"] = self.playwright_user_agent
                    if self.playwright_locale:
                        context_kwargs["locale"] = self.playwright_locale
                    context = browser.new_context(**context_kwargs)
                    page = context.new_page()
                    response = page.goto(self.tracking_page_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    html = page.content()
                    if _looks_like_anti_bot_page(html, status_code=response.status if response else None):
                        raise WanHaiAntiBotBlocked(
                            "Wan Hai tracking page blocked by anti-bot protection before the query form loaded"
                        )
                    if self.playwright_request_delay_seconds > 0:
                        page.wait_for_timeout(int(self.playwright_request_delay_seconds * 1000))

                    html = page.content()
                    if _looks_like_anti_bot_page(html):
                        raise WanHaiAntiBotBlocked(
                            "Wan Hai tracking page blocked by anti-bot protection before the query form loaded"
                        )
                    if 'form id="cargoTrackV2Bean"' not in html:
                        raise ValueError("Wan Hai query form not available; browser likely blocked by anti-bot protection")

                    page.select_option("#cargoType", cargo_type)
                    page.fill("#q_ref_no1", reference)
                    with page.expect_popup(timeout=timeout_ms) as popup_info:
                        page.click("#Query")
                    results_page = popup_info.value
                    results_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                    if self.playwright_request_delay_seconds > 0:
                        results_page.wait_for_timeout(int(self.playwright_request_delay_seconds * 1000))

                    result_html = results_page.content()
                    result_data = _parse_result_page(result_html, cargo_type=cargo_type)
                    if result_data is None:
                        raise ValueError(f"Wan Hai returned no results for reference {reference}")
                    discovered_containers = extract_container_numbers(result_html)

                    detail = None
                    detail_html = ""
                    booking_reference = result_data.get("booking_reference")
                    if booking_reference:
                        try:
                            with results_page.expect_popup(timeout=timeout_ms) as detail_popup_info:
                                results_page.get_by_text("Booking Data").first.click()
                            detail_page = detail_popup_info.value
                            detail_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                            if self.playwright_request_delay_seconds > 0:
                                detail_page.wait_for_timeout(int(self.playwright_request_delay_seconds * 1000))
                            detail_html = detail_page.content()
                            detail = _parse_booking_detail_page(detail_html)
                            detail_containers = _extract_booking_detail_containers(detail_html)
                            if detail_containers:
                                discovered_containers = detail_containers
                            elif cargo_type == "1":
                                discovered_containers = extract_container_numbers([discovered_containers, detail])
                        except Exception:
                            detail = None

                    return _status_from_parsed(
                        cargo_type=cargo_type,
                        result=result_data,
                        detail=detail,
                        discovered_containers=discovered_containers,
                        source_url=results_page.url,
                    )
                finally:
                    browser.close()

        return run_sync_playwright(_run)


def _build_reference_attempts(shipment: ShipmentRef) -> list[tuple[str, str]]:
    attempts: list[tuple[str, str]] = []
    if shipment.booking_no:
        ref = _normalize_reference(shipment.booking_no)
        if ref:
            attempts.append((ref, "2"))
    if shipment.container_no:
        for ref in _container_references(shipment.container_no):
            attempts.append((ref, "1"))
    # Comments are suggestions only; a known container is required to bind results.
    if extract_container_numbers(shipment.container_no or ""):
        for hint in shipment.reference_hints:
            ref = _normalize_reference(hint)
            if ref:
                attempts.append((ref, "2"))
    return _dedupe_reference_attempts(attempts)


def _container_references(reference: str) -> list[str]:
    refs = [tok.strip().upper() for tok in re.split(r"[,\s]+", reference) if re.match(r"^[A-Za-z]{4}\d{7}$", tok.strip())]
    if refs:
        return refs
    normalized = _normalize_reference(reference)
    return [normalized] if normalized else []


def _dedupe_reference_attempts(attempts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    deduped: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for reference, cargo_type in attempts:
        if not reference:
            continue
        key = (reference.upper(), cargo_type)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((reference, cargo_type))
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


def _parse_result_page(html: str, *, cargo_type: str) -> dict[str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="tbl-list")
    if table is None:
        return None

    rows = table.find_all("tr")
    for row in rows[1:]:
        cells = row.find_all("td")
        if cargo_type == "1" and len(cells) >= 7:
            booking_reference = _clean_cell_text(cells[6]).split()[0] if _clean_cell_text(cells[6]) else None
            return {
                "reference": _clean_cell_text(cells[0]),
                "event_time_local_text": _parse_wan_hai_datetime(_clean_cell_text(cells[1])),
                "status_text": _clean_cell_text(cells[2]),
                "location": _clean_cell_text(cells[3]),
                "voyage": _clean_cell_text(cells[4]),
                "vessel": _clean_cell_text(cells[5]),
                "booking_reference": booking_reference or "",
            }
        if cargo_type == "2" and len(cells) >= 6:
            return {
                "reference": _clean_cell_text(cells[1]),
                "event_time_local_text": _parse_wan_hai_date(_clean_cell_text(cells[2])),
                "voyage": _clean_cell_text(cells[3]),
                "vessel": _clean_cell_text(cells[4]),
                "booking_reference": _clean_cell_text(cells[1]),
            }
    return None


def _parse_booking_detail_page(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    if "Tracking Information Content [By Book No.]" not in text:
        return {}

    detail: dict[str, str] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    labels = {
        "訂艙號碼": "booking_reference",
        "訂艙日期": "booking_date",
        "船名": "vessel",
        "航次": "voyage",
        "Booking Status": "booking_status",
        "Earliest Return Date": "earliest_return_date",
        "Close Date": "close_date",
        "Doc Cutoff Date": "doc_cutoff_date",
        "Place of Receipt": "place_of_receipt",
        "Port of Loading": "port_of_loading",
        "Port of Discharging": "port_of_discharging",
        "Place of Delivery": "place_of_delivery",
        "Final Destination": "final_destination",
        "Estimated Departure Date": "estimated_departure_date",
        "Estimated Arrival Date": "estimated_arrival_date",
    }

    idx = 0
    while idx < len(lines):
        label = lines[idx]
        key = labels.get(label)
        if key and idx + 1 < len(lines):
            detail[key] = lines[idx + 1]
            idx += 2
            continue
        idx += 1

    return detail


def _extract_booking_detail_containers(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        table_text = table.get_text(" ", strip=True)
        if "Ctnr No." not in table_text and "Cntr No." not in table_text and "Container No." not in table_text:
            continue
        containers = extract_container_numbers(table_text)
        if containers:
            return containers
    return []


def _status_from_parsed(
    *,
    cargo_type: str,
    result: dict[str, str],
    detail: dict[str, str] | None,
    discovered_containers: list[str],
    source_url: str,
) -> ShipmentStatus:
    detail = detail or {}
    latest_status_text = detail.get("booking_status") or result.get("status_text") or "Unknown"
    latest_move_name = _normalize_status_text(latest_status_text)
    latest_move_time_local = result.get("event_time_local_text") or detail.get("estimated_departure_date")
    latest_move_time = parse_event_time(latest_move_time_local)
    latest_move_location = _normalize_location_text(
        result.get("location"),
        detail=detail,
        status_text=latest_status_text,
    )
    eta_local_text = _parse_wan_hai_date(detail.get("estimated_arrival_date"))
    eta_time = parse_event_time(eta_local_text)

    route_bits = [
        detail.get("vessel") or result.get("vessel"),
        detail.get("voyage") or result.get("voyage"),
        _render_route(detail),
    ]
    movement_details = " | ".join(bit for bit in route_bits if bit)
    vessel_voyage = render_vessel_voyage(
        detail.get("vessel") or result.get("vessel"),
        detail.get("voyage") or result.get("voyage"),
    )

    latest_move = None
    if latest_move_name or latest_move_location or latest_move_time_local:
        latest_move = MovementEvent(
            name=latest_move_name or latest_status_text,
            location=latest_move_location,
            event_time=latest_move_time,
            event_time_local_text=latest_move_time_local,
        )

    status_text = latest_move_name or latest_status_text
    if cargo_type == "2" and not detail and eta_time is not None:
        status_text = _eta_status_text(eta_time)

    if cargo_type == "2" and detail and detail.get("booking_status"):
        status_text = _normalize_status_text(detail["booking_status"])

    return ShipmentStatus(
        status_text=status_text,
        location=latest_move_location,
        event_time=latest_move_time,
        eta_time=eta_time,
        eta_local_text=eta_local_text,
        latest_move=latest_move,
        recent_moves=[latest_move] if latest_move is not None else [],
        discovered_containers=discovered_containers,
        raw_source=f"wan_hai-playwright:{source_url}",
        source_url=source_url,
        movement_details=movement_details or None,
        vessel_voyage=vessel_voyage,
    )


def _render_route(detail: dict[str, str]) -> str | None:
    origin = detail.get("port_of_loading") or detail.get("place_of_receipt")
    destination = detail.get("port_of_discharging") or detail.get("place_of_delivery")
    if origin and destination:
        return f"{origin} -> {destination}"
    return origin or destination


def _normalize_location_text(
    value: str | None,
    *,
    detail: dict[str, str],
    status_text: str | None,
) -> str | None:
    if not value:
        return detail.get("port_of_loading") or detail.get("place_of_receipt")

    cleaned = re.sub(r"\s+", " ", value).strip()
    if not cleaned:
        return detail.get("port_of_loading") or detail.get("place_of_receipt")

    translated = _WAN_HAI_LOCATION_TRANSLATIONS.get(cleaned)
    if translated:
        return translated

    if _contains_cjk(cleaned):
        port_loading = detail.get("port_of_loading")
        port_discharging = detail.get("port_of_discharging")
        normalized_status = (status_text or "").strip().upper()
        if "DISC" in normalized_status or "DISCHARG" in normalized_status or "卸船" in cleaned:
            return port_discharging or port_loading or cleaned
        return port_loading or detail.get("place_of_receipt") or cleaned

    return cleaned


def _clean_cell_text(cell: Any) -> str:
    text = cell.get_text(" ", strip=True) if cell is not None else ""
    return re.sub(r"\s+", " ", text).strip()


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in value)


def _parse_wan_hai_datetime(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    for fmt in ("%Y%m%d %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return candidate


def _parse_wan_hai_date(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate or candidate == "---":
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return candidate


def _normalize_status_text(value: str) -> str:
    normalized = (value or "").strip()
    upper = normalized.upper()
    mappings = {
        "LOADED": "Container Loaded (LOAD)",
        "EMPTY RELEASED": "Container Gated Out (GTOT)",
        "GATE IN": "Container Gated In (GTIN)",
        "GATE OUT": "Container Gated Out (GTOT)",
        "DISCHARGED": "Container Discharged (DISC)",
        "重櫃裝船": "Container Loaded (LOAD)",
        "裝船": "Container Loaded (LOAD)",
        "卸船": "Container Discharged (DISC)",
        "進櫃": "Container Gated In (GTIN)",
        "出閘": "Container Gated Out (GTOT)",
        "提領": "Container Gated Out (GTOT)",
    }
    return mappings.get(normalized) or mappings.get(upper) or normalized or "Unknown"


def _eta_status_text(eta_time: datetime | None) -> str:
    if eta_time is None:
        return "Processing"
    return f"ETA {eta_time.isoformat()}"


def _append_raw_source(raw_source: str | None, extra: str) -> str:
    if raw_source and raw_source.strip():
        return f"{raw_source} | {extra}"
    return extra


def _looks_like_anti_bot_page(html: str | None, *, status_code: int | None = None) -> bool:
    if status_code in {401, 403, 429}:
        return True
    if not html:
        return False
    normalized = html.lower()
    markers = (
        "_incapsula_resource",
        "incapsula",
        "imperva",
        "access denied",
        "request unsuccessful",
        "captcha",
    )
    return any(marker in normalized for marker in markers)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


_WAN_HAI_LOCATION_TRANSLATIONS = {
    "青島前灣及裝箱碼頭有限責任公司": "Qingdao Qianwan Container Terminal Co., Ltd.",
    "青島前灣聯合集裝箱碼頭有限責任公司": "Qingdao Qianwan United Container Terminal Co., Ltd.",
    "上海明東集裝箱碼頭有限公司": "Shanghai Mingdong Container Terminals Ltd.",
    "寧波北侖第三集裝箱碼頭有限公司": "Ningbo Beilun Third Container Terminal Co., Ltd.",
    "寧波大榭招商國際碼頭有限公司": "Ningbo Daxie China Merchants International Terminal Co., Ltd.",
    "蛇口集裝箱碼頭有限公司": "Shekou Container Terminals Ltd.",
    "鹽田國際集裝箱碼頭": "Yantian International Container Terminals",
    "廈門遠海集裝箱碼頭有限公司": "Xiamen Ocean Gate Container Terminal Co., Ltd.",
}
