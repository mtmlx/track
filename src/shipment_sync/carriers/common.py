from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
import re
import time
from typing import Any

import requests

_DCSA_TRANSPORT_LABELS: dict[str, str] = {
    "ARRI": "Transport Arrived (ARRI)",
    "DEPA": "Transport Departed (DEPA)",
}

_DCSA_EQUIPMENT_LABELS: dict[str, str] = {
    "LOAD": "Container Loaded (LOAD)",
    "DISC": "Container Discharged (DISC)",
    "GTIN": "Container Gated In (GTIN)",
    "GTOT": "Container Gated Out (GTOT)",
    "STUF": "Container Stuffed (STUF)",
    "STRP": "Container Stripped (STRP)",
    "PICK": "Container Picked Up (PICK)",
    "DROP": "Container Dropped Off (DROP)",
    "INSP": "Container Inspected (INSP)",
    "RSEA": "Container Resealed (RSEA)",
    "RMVD": "Seal Removed (RMVD)",
}

_DCSA_SHIPMENT_LABELS: dict[str, str] = {
    "RECE": "Shipment Received (RECE)",
    "DRFT": "Shipment Drafted (DRFT)",
    "PENA": "Shipment Pending Approval (PENA)",
    "PENU": "Shipment Pending Update (PENU)",
    "REJE": "Shipment Rejected (REJE)",
    "APPR": "Shipment Approved (APPR)",
    "ISSU": "Shipment Issued (ISSU)",
    "SURR": "Shipment Surrendered (SURR)",
    "SUBM": "Shipment Submitted (SUBM)",
    "VOID": "Shipment Voided (VOID)",
    "CONF": "Shipment Confirmed (CONF)",
    "REQS": "Shipment Requested (REQS)",
    "CMPL": "Shipment Completed (CMPL)",
    "HOLD": "Shipment On Hold (HOLD)",
    "RELS": "Shipment Released (RELS)",
}

DEFAULT_CARRIER_RESPONSE_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_CARRIER_PAYLOAD_MAX_NODES = 100_000
DEFAULT_CARRIER_PAYLOAD_MAX_DEPTH = 64


class CarrierResponseLimitError(ValueError):
    """A configured carrier response exceeded the application's safety budget."""


class CarrierPayloadLimitError(ValueError):
    """A decoded carrier payload exceeded the application's traversal budget."""


def carrier_response_max_bytes() -> int:
    return _positive_env_int(
        "CARRIER_RESPONSE_MAX_BYTES",
        default=DEFAULT_CARRIER_RESPONSE_MAX_BYTES,
    )


def reject_response_redirect(response: requests.Response, **kwargs: Any) -> requests.Response:
    """Reject redirects before requests prepares a redirect and buffers its body."""
    if 300 <= response.status_code < 400:
        response.close()
        raise requests.HTTPError("Carrier endpoint returned an unexpected redirect", response=response)
    return response


def bounded_response_bytes(
    response: requests.Response,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Read and cache a carrier response without allowing an unbounded body."""
    limit = max_bytes if max_bytes is not None else carrier_response_max_bytes()
    if limit < 1:
        raise ValueError("Carrier response byte limit must be positive")

    headers = getattr(response, "headers", {}) or {}
    raw_content_length = headers.get("content-length") or headers.get("Content-Length")
    if raw_content_length:
        try:
            declared_length = int(str(raw_content_length).strip())
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit:
            _close_response(response)
            raise CarrierResponseLimitError(
                f"Carrier response declares {declared_length} bytes, exceeding the {limit}-byte limit"
            )

    if getattr(response, "_content_consumed", False):
        cached = getattr(response, "content", b"")
        if not isinstance(cached, bytes):
            cached = bytes(cached)
        if len(cached) > limit:
            _close_response(response)
            raise CarrierResponseLimitError(
                f"Carrier response contains {len(cached)} bytes, exceeding the {limit}-byte limit"
            )
        return cached

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        raw_body = getattr(response, "content", None)
        if raw_body is None:
            raw_body = str(getattr(response, "text", "")).encode("utf-8")
        if not isinstance(raw_body, bytes):
            raw_body = bytes(raw_body)
        if len(raw_body) > limit:
            _close_response(response)
            raise CarrierResponseLimitError(
                f"Carrier response contains {len(raw_body)} bytes, exceeding the {limit}-byte limit"
            )
        return raw_body

    chunks: list[bytes] = []
    total = 0
    for chunk in iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            _close_response(response)
            raise CarrierResponseLimitError(
                f"Carrier response exceeds the {limit}-byte limit while streaming"
            )
        chunks.append(chunk)

    body = b"".join(chunks)
    try:
        response._content = body
        response._content_consumed = True
    except Exception:
        pass
    return body


def bounded_response_text(
    response: requests.Response,
    *,
    max_bytes: int | None = None,
) -> str:
    body = bounded_response_bytes(response, max_bytes=max_bytes)
    encoding = getattr(response, "encoding", None) or "utf-8"
    return body.decode(encoding, errors="replace")


def bounded_response_json(
    response: requests.Response,
    *,
    max_bytes: int | None = None,
) -> Any:
    return json.loads(bounded_response_text(response, max_bytes=max_bytes))


def to_dcsa_movement_name(
    *,
    event: dict[str, Any] | None = None,
    fallback_name: str | None = None,
    event_type: str | None = None,
    transport_event_code: str | None = None,
    equipment_event_code: str | None = None,
    shipment_event_code: str | None = None,
) -> str:
    event_type_code = _normalized_code(
        event_type
        or (extract_first(event, ["eventType"]) if event is not None else None)
    )
    transport_code = _normalized_code(
        transport_event_code
        or (extract_first(event, ["transportEventTypeCode"]) if event is not None else None)
    )
    equipment_code = _normalized_code(
        equipment_event_code
        or (extract_first(event, ["equipmentEventTypeCode"]) if event is not None else None)
    )
    shipment_code = _normalized_code(
        shipment_event_code
        or (extract_first(event, ["shipmentEventTypeCode"]) if event is not None else None)
    )

    if equipment_code and equipment_code in _DCSA_EQUIPMENT_LABELS:
        return _DCSA_EQUIPMENT_LABELS[equipment_code]
    if transport_code and transport_code in _DCSA_TRANSPORT_LABELS:
        return _DCSA_TRANSPORT_LABELS[transport_code]
    if shipment_code and shipment_code in _DCSA_SHIPMENT_LABELS:
        return _DCSA_SHIPMENT_LABELS[shipment_code]

    guessed = _guess_dcsa_label_from_text(fallback_name)
    if guessed:
        return guessed

    if event_type_code == "TRANSPORT":
        return "Transport Event"
    if event_type_code == "EQUIPMENT":
        return "Equipment Event"
    if event_type_code == "SHIPMENT":
        return "Shipment Event"

    if fallback_name and fallback_name.strip():
        return fallback_name.strip()
    return "Unknown move"


def extract_first(payload: Any, candidate_keys: list[str]) -> str | None:
    wanted = {k.lower() for k in candidate_keys}
    for item in _iter_payload_items(payload):
        if isinstance(item, dict):
            for key, value in item.items():
                if key.lower() in wanted and isinstance(value, (str, int, float)):
                    return str(value)
    return None


def extract_event_state_hint(event: dict[str, Any], extra_keys: list[str] | None = None) -> str | None:
    actual_flag = _extract_first_bool(
        event,
        ["actualIndicator", "isActual", "actual", "hasOccurred", "isConfirmed"],
    )
    if actual_flag is True:
        return "actual"

    estimated_flag = _extract_first_bool(
        event,
        ["estimatedIndicator", "isEstimated", "estimated", "isPlanned"],
    )
    if estimated_flag is True:
        return "estimated"

    candidate_keys = [
        "triggerType",
        "eventClassifierCode",
        "trigger",
        "eventType",
        "status",
        "state",
        "eventStatus",
        "eventState",
        "milestoneStatus",
        "shipmentStatus",
    ]
    if extra_keys:
        candidate_keys.extend(extra_keys)
    return extract_first(event, candidate_keys)


def extract_container_numbers(payload: Any) -> list[str]:
    pattern = re.compile(r"\b([A-Za-z]{4}\d{7})\b")
    found: list[str] = []
    seen_tokens: set[str] = set()

    for item in _iter_payload_items(payload):
        if not isinstance(item, str):
            continue

        for match in pattern.findall(item):
            token = match.upper()
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            found.append(token)

    return found


def parse_event_time(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        dt = _parse_datetime_fallback(candidate)
        if dt is None:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_eta_time(payload: Any) -> datetime | None:
    eta_raw = extract_first(
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
    return parse_event_time(eta_raw)


def render_vessel_voyage(vessel: str | None, voyage: str | None) -> str | None:
    parts = [_clean_vessel_voyage_part(part) for part in (vessel, voyage)]
    rendered = " ".join(part for part in parts if part)
    return rendered or None


def extract_event_vessel_voyage(event: dict[str, Any]) -> str | None:
    nested_vessel_voyage = event.get("vesselVoyage")
    if not isinstance(nested_vessel_voyage, dict):
        nested_vessel_voyage = event.get("vessel_voyage")
    if isinstance(nested_vessel_voyage, dict):
        vessel = extract_first(
            nested_vessel_voyage,
            ["vesselName", "vesselEngName", "vesselEnglishName", "VesselName", "Vessel"],
        )
        voyage = extract_first(
            nested_vessel_voyage,
            ["voyageNo", "voyageNumber", "scheduleVoyageNumber", "VoyageNo", "Voyage"],
        )
        rendered = render_vessel_voyage(vessel, voyage)
        if rendered:
            return rendered

    vessel = extract_first(
        event,
        [
            "vesselName",
            "vesselEngName",
            "vesselEnglishName",
            "nameOfVessel",
            "transportName",
            "transport",
            "vessel",
            "VesselName",
            "Vessel",
        ],
    )
    voyage = extract_first(
        event,
        [
            "carrierExportVoyageNumber",
            "carrierImportVoyageNumber",
            "exportVoyageNumber",
            "importVoyageNumber",
            "carrierVoyageNumber",
            "voyageNumber",
            "scheduleVoyageNumber",
            "outboundConsortiumVoyage",
            "inboundConsortiumVoyage",
            "voyageNo",
            "voyage",
            "VoyageNumber",
            "VoyageNo",
            "Voyage",
        ],
    )
    return render_vessel_voyage(vessel, voyage)


def extract_final_destination_vessel_voyage(
    events: list[dict[str, Any]],
    *,
    final_location: str | None = None,
) -> str | None:
    candidates: list[tuple[int, datetime | None, int, str]] = []
    normalized_final_location = _normalize_location_token(final_location)

    for idx, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        vessel_voyage = extract_event_vessel_voyage(event)
        if not vessel_voyage:
            continue

        event_location = _event_location(event)
        location_score = 0
        if normalized_final_location:
            normalized_event_location = _normalize_location_token(event_location)
            if normalized_event_location and (
                normalized_event_location == normalized_final_location
                or normalized_final_location in normalized_event_location
                or normalized_event_location in normalized_final_location
            ):
                location_score = 2
        relevant_score = _final_destination_event_score(event)
        if normalized_final_location and not location_score:
            continue
        if not normalized_final_location and not relevant_score:
            continue

        event_time = parse_event_time(
            extract_first(
                event,
                [
                    "eventDateTime",
                    "eventLocalPortDate",
                    "eventTime",
                    "actualTime",
                    "timestamp",
                    "dateTime",
                    "eventDate",
                    "date",
                    "locationDateTime",
                    "actualArrivalDate",
                    "estimatedDateOfArrival",
                    "plannedArrivalDateTime",
                    "scheduledArrivalDateTime",
                    "cgoAvailTm",
                ],
            )
        )
        candidates.append((location_score + relevant_score, event_time, idx, vessel_voyage))

    if not candidates:
        return None
    best = max(
        candidates,
        key=lambda item: (
            item[0],
            item[1] is not None,
            item[1].isoformat() if item[1] is not None else "",
            item[2],
        ),
    )
    return best[3]


def _extract_first_bool(payload: Any, candidate_keys: list[str]) -> bool | None:
    wanted = {k.lower() for k in candidate_keys}
    queue = [payload]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for key, value in item.items():
                if key.lower() in wanted:
                    parsed = _coerce_bool(value)
                    if parsed is not None:
                        return parsed
                queue.append(value)
        elif isinstance(item, list):
            queue.extend(item)
    return None


def _clean_vessel_voyage_part(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    if not cleaned or cleaned in {"---", "-", "N/A", "NA"}:
        return None
    return cleaned


def _event_location(event: dict[str, Any]) -> str | None:
    return extract_first(
        event,
        [
            "locationName",
            "location",
            "city",
            "port",
            "eventLocation",
            "nodeName",
            "unLocCode",
            "UNLocationCode",
            "portOfDischarge",
            "placeOfDelivery",
            "destination",
        ],
    )


def _final_destination_event_score(event: dict[str, Any]) -> int:
    transport_code = _normalized_code(extract_first(event, ["transportEventTypeCode"]))
    equipment_code = _normalized_code(extract_first(event, ["equipmentEventTypeCode"]))
    if equipment_code == "DISC":
        return 3
    if transport_code == "ARRI":
        return 2

    raw_name = extract_first(
        event,
        [
            "eventName",
            "eventDescription",
            "milestoneName",
            "status",
            "movement",
            "description",
            "eventType",
            "containerNumberStatus",
            "label",
        ],
    )
    normalized = (raw_name or "").strip().lower()
    if not normalized:
        return 0
    if "discharg" in normalized:
        return 3
    if "arriv" in normalized or "arrival" in normalized:
        return 2
    return 0


def _normalize_location_token(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", str(value)).strip().lower()
    return re.sub(r"\s+", " ", normalized) or None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "actual", "act", "confirmed", "complete", "completed", "a"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "estimated", "estimate", "est", "planned", "plan", "expected", "scheduled", "e"}:
            return False
    return None


def _parse_datetime_fallback(candidate: str) -> datetime | None:
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


class _ScriptPayloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[bool, str]] = []
        self._parts: list[str] | None = None
        self._is_next = False

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._parts = []
            self._is_next = any(name == "id" and (value or "").casefold() == "__next_data__" for name, value in attrs)

    def handle_startendtag(self, tag, attrs):
        # In text/html a script slash does not close its raw-text payload.
        self.handle_starttag(tag, attrs)
        if tag == "script":
            self.set_cdata_mode(tag)

    def handle_data(self, data):
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._parts is not None:
            self.scripts.append((self._is_next, "".join(self._parts)))
            self._parts = None


def extract_json_from_http_response(response: requests.Response) -> dict:
    content_type = (response.headers.get("content-type") or "").lower()
    body = bounded_response_text(response)
    if "json" in content_type:
        data = json.loads(body)
        if isinstance(data, dict):
            return data
        return {"data": data}

    parser = _ScriptPayloadParser()
    parser.feed(body)
    # Only complete script elements count. Do not flush incomplete markup:
    # older supported HTMLParser versions rescan malformed suffixes at close.
    next_payload = next((text for is_next, text in parser.scripts if is_next), None)
    if next_payload is not None:
        data = json.loads(next_payload)
        if isinstance(data, dict):
            return data
        return {"data": data}

    for _, text in parser.scripts:
        script_body = text.strip()
        if not script_body:
            continue
        if not (script_body.startswith("{") or script_body.startswith("[")):
            continue
        try:
            data = json.loads(script_body)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
        return {"data": data}

    raise ValueError("Could not parse JSON payload from tracking response")


def get_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: int = 45,
    max_retries: int = 2,
    retry_delay_seconds: float = 2.0,
    non_retry_statuses: set[int] | None = None,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
            if non_retry_statuses and response.status_code in non_retry_statuses:
                response.raise_for_status()
            response.raise_for_status()
            bounded_response_bytes(response)
            return response
        except requests.RequestException as exc:
            last_error = exc
            response = getattr(exc, "response", None)
            if response is not None and non_retry_statuses and response.status_code in non_retry_statuses:
                break
            if attempt >= max_retries:
                break
            time.sleep(retry_delay_seconds * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError("Request failed without specific error")


def _iter_payload_items(payload: Any):
    max_nodes = _positive_env_int(
        "CARRIER_PAYLOAD_MAX_NODES",
        default=DEFAULT_CARRIER_PAYLOAD_MAX_NODES,
    )
    max_depth = _positive_env_int(
        "CARRIER_PAYLOAD_MAX_DEPTH",
        default=DEFAULT_CARRIER_PAYLOAD_MAX_DEPTH,
    )
    queue: deque[tuple[Any, int]] = deque([(payload, 0)])
    seen_containers: set[int] = set()
    nodes_seen = 0

    while queue:
        item, depth = queue.popleft()
        nodes_seen += 1
        if nodes_seen > max_nodes:
            raise CarrierPayloadLimitError(
                f"Carrier payload exceeds the {max_nodes}-node traversal limit"
            )
        if depth > max_depth:
            raise CarrierPayloadLimitError(
                f"Carrier payload exceeds the {max_depth}-level nesting limit"
            )

        yield item

        if isinstance(item, dict):
            item_id = id(item)
            if item_id in seen_containers:
                continue
            seen_containers.add(item_id)
            queue.extend((value, depth + 1) for value in item.values())
        elif isinstance(item, (list, tuple)):
            item_id = id(item)
            if item_id in seen_containers:
                continue
            seen_containers.add(item_id)
            queue.extend((value, depth + 1) for value in item)


def _positive_env_int(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _close_response(response: requests.Response) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _normalized_code(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().upper()
    return normalized or None


def _guess_dcsa_label_from_text(name: str | None) -> str | None:
    if not name:
        return None
    normalized = name.strip().lower()
    if not normalized:
        return None

    if "booking confirmed" in normalized:
        return _DCSA_SHIPMENT_LABELS["CONF"]
    if "empty container release to shipper" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTOT"]
    if "empty container returned from customer" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTIN"]
    # MSC uses these terminal messages instead of DCSA equipment codes. They
    # represent the physical handoff to the consignee and the empty return.
    if "import to consignee" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTOT"]
    if "empty received at cy" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTIN"]
    if "export received at cy" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTIN"]
    if "empty to shipper" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTOT"]
    if "arrival at port of discharge" in normalized or normalized.startswith("vessel arrival"):
        return _DCSA_TRANSPORT_LABELS["ARRI"]
    if "departure from port of loading" in normalized or normalized.startswith("vessel departure"):
        return _DCSA_TRANSPORT_LABELS["DEPA"]
    if "gate in" in normalized or "gated in" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTIN"]
    if "gate out" in normalized or "gated out" in normalized:
        return _DCSA_EQUIPMENT_LABELS["GTOT"]
    if "unloaded from vessel" in normalized or "discharge" in normalized or "unloaded" in normalized:
        return _DCSA_EQUIPMENT_LABELS["DISC"]
    if "loaded on vessel" in normalized or "load on board" in normalized or "loaded" in normalized:
        return _DCSA_EQUIPMENT_LABELS["LOAD"]
    if "stuff" in normalized:
        return _DCSA_EQUIPMENT_LABELS["STUF"]
    if "strip" in normalized:
        return _DCSA_EQUIPMENT_LABELS["STRP"]
    if "pick up" in normalized or "pickup" in normalized:
        return _DCSA_EQUIPMENT_LABELS["PICK"]
    if "drop off" in normalized or normalized.startswith("drop "):
        return _DCSA_EQUIPMENT_LABELS["DROP"]
    if "inspect" in normalized:
        return _DCSA_EQUIPMENT_LABELS["INSP"]
    if "reseal" in normalized:
        return _DCSA_EQUIPMENT_LABELS["RSEA"]
    if "seal removed" in normalized or ("remove" in normalized and "seal" in normalized):
        return _DCSA_EQUIPMENT_LABELS["RMVD"]
    if "arriv" in normalized:
        return _DCSA_TRANSPORT_LABELS["ARRI"]
    if "depart" in normalized or "sail" in normalized:
        return _DCSA_TRANSPORT_LABELS["DEPA"]
    return None
