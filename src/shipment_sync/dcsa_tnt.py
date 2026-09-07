from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import UUID


SUPPORTED_TNT_VERSIONS = frozenset({"2.2", "2.3"})

_EVENT_CODE_FIELD_BY_TYPE = {
    "SHIPMENT": "shipmentEventTypeCode",
    "TRANSPORT": "transportEventTypeCode",
    "EQUIPMENT": "equipmentEventTypeCode",
}
_EVENT_CODE_FIELDS = frozenset(_EVENT_CODE_FIELD_BY_TYPE.values())
_SHIPMENT_CODES_V22 = frozenset(
    {
        "RECE",
        "DRFT",
        "PENA",
        "PENU",
        "REJE",
        "APPR",
        "ISSU",
        "SURR",
        "SUBM",
        "VOID",
        "CONF",
        "REQS",
        "CMPL",
        "HOLD",
        "RELS",
    }
)
_SHIPMENT_CODES_V23 = _SHIPMENT_CODES_V22 | frozenset({"PENC", "CANC"})
_TRANSPORT_CODES = frozenset({"ARRI", "DEPA"})
_EQUIPMENT_CODES = frozenset({"LOAD", "DISC", "GTIN", "GTOT", "STUF", "STRP", "PICK", "DROP", "INSP", "RSEA", "RMVD"})
_EVENT_CLASSIFIERS = frozenset({"ACT", "PLN", "EST"})
_FACILITY_TYPES = frozenset({"BOCR", "CLOC", "COFS", "COYA", "OFFD", "DEPO", "INTE", "POTE", "RAMP"})
_REFERENCE_TYPES_V22 = frozenset({"FF", "SI", "PO", "CR", "AAO", "EQ"})
_REFERENCE_TYPES_V23 = _REFERENCE_TYPES_V22 | frozenset({"ECR", "CSI", "BPR", "BID"})
# CMA's published TNT 2.2 contract explicitly documents these two carrier
# extensions. They are accepted only for CMA, not as generic DCSA 2.2 values.
_CMA_CGM_REFERENCE_TYPES_V22 = frozenset({"LOAD", "ERT"})
_CMA_CGM_CARRIER_ALIASES = frozenset({"cma cgm", "cma-cgm", "cma - cgm"})
_DOCUMENT_REFERENCE_TYPES_V22 = frozenset({"BKG", "TRD"})
_DOCUMENT_REFERENCE_TYPES_V23 = _DOCUMENT_REFERENCE_TYPES_V22 | frozenset({"CBR", "SHI"})
_LEGACY_DOCUMENT_REFERENCE_TYPES = {
    "BKG (BOOKING)": "BKG",
    "TRD (TRANSPORT DOCUMENT)": "TRD",
}
_SENSITIVE_KEY_PARTS = ("authorization", "password", "secret", "token", "api_key", "apikey", "clientsecret")


class DcsaTntValidationError(ValueError):
    """Raised when an event cannot safely enter the canonical DCSA ledger."""


@dataclass(frozen=True)
class DcsaReference:
    reference_type: str
    reference_value: str


@dataclass(frozen=True)
class DcsaDocumentReference:
    reference_type: str
    reference_value: str


@dataclass(frozen=True)
class DcsaLocation:
    un_location_code: str | None = None
    facility_code: str | None = None
    facility_code_list_provider: str | None = None
    facility_type_code: str | None = None


@dataclass(frozen=True)
class DcsaTntEvent:
    carrier: str
    tnt_version: str
    event_type: str
    event_code: str
    event_classifier_code: str | None
    event_id: str | None
    conformance_warnings: tuple[str, ...]
    idempotency_key: str
    event_created_at: datetime
    event_at: datetime | None
    document_id: str | None
    document_type_code: str | None
    equipment_reference: str | None
    empty_indicator_code: str | None
    location: DcsaLocation
    transport_call_id: str | None
    transport_call_sequence_number: int | None
    vessel_name: str | None
    export_voyage_number: str | None
    import_voyage_number: str | None
    references: tuple[DcsaReference, ...]
    document_references: tuple[DcsaDocumentReference, ...]
    source_url: str | None
    raw_payload: dict[str, Any]

    @property
    def is_pending_confirmation(self) -> bool:
        return self.event_type == "SHIPMENT" and self.event_code == "PENC"

    @property
    def is_cancelled(self) -> bool:
        return self.event_type == "SHIPMENT" and self.event_code == "CANC"

    @property
    def initial_projection_state(self) -> str:
        if self.is_cancelled:
            return "halted"
        if self.is_pending_confirmation:
            return "requires_review"
        return "pending"

    def normalized_record(self) -> dict[str, Any]:
        return {
            "carrier": self.carrier,
            "tnt_version": self.tnt_version,
            "event_type": self.event_type,
            "event_code": self.event_code,
            "event_classifier_code": self.event_classifier_code,
            "event_id": self.event_id,
            "conformance_warnings": list(self.conformance_warnings),
            "idempotency_key": self.idempotency_key,
            "event_created_at": self.event_created_at.isoformat(),
            "event_at": self.event_at.isoformat() if self.event_at else None,
            "document_id": self.document_id,
            "document_type_code": self.document_type_code,
            "equipment_reference": self.equipment_reference,
            "empty_indicator_code": self.empty_indicator_code,
            "location": {
                "un_location_code": self.location.un_location_code,
                "facility_code": self.location.facility_code,
                "facility_code_list_provider": self.location.facility_code_list_provider,
                "facility_type_code": self.location.facility_type_code,
            },
            "transport_call_id": self.transport_call_id,
            "transport_call_sequence_number": self.transport_call_sequence_number,
            "vessel_name": self.vessel_name,
            "export_voyage_number": self.export_voyage_number,
            "import_voyage_number": self.import_voyage_number,
            "references": [reference.__dict__ for reference in self.references],
            "document_references": [reference.__dict__ for reference in self.document_references],
            "source_url": self.source_url,
            "initial_projection_state": self.initial_projection_state,
        }


def parse_dcsa_tnt_event(
    payload: Mapping[str, Any],
    *,
    carrier: str,
    tnt_version: str,
    source_url: str | None = None,
) -> DcsaTntEvent:
    """Validate and normalize the operationally material TNT 2.2/2.3 contract."""

    event = _as_mapping(payload, field="payload")
    normalized_carrier = _required_text(carrier, field="carrier").lower()
    version = normalize_tnt_version(tnt_version)
    event_type = _required_code(event.get("eventType"), field="eventType")
    if event_type not in _EVENT_CODE_FIELD_BY_TYPE:
        raise DcsaTntValidationError(f"Unsupported eventType {event_type!r}.")

    event_code = _extract_event_code(event, event_type=event_type, version=version)
    event_created_at = _parse_datetime(event.get("eventCreatedDateTime"), field="eventCreatedDateTime")
    event_at = _optional_datetime(event.get("eventDateTime"), field="eventDateTime")
    event_id, event_id_warning = _optional_event_id(
        value=event.get("eventID"),
        carrier=normalized_carrier,
        version=version,
    )
    event_classifier_code = _optional_code(event.get("eventClassifierCode"), field="eventClassifierCode")
    _validate_event_classifier(event_type, version, event_classifier_code)

    document_id = _optional_text(event.get("documentID"), field="documentID")
    document_type_code = _optional_code(event.get("documentTypeCode"), field="documentTypeCode")
    equipment_reference = _optional_text(event.get("equipmentReference"), field="equipmentReference")
    empty_indicator_code = _optional_code(event.get("emptyIndicatorCode"), field="emptyIndicatorCode")
    transport_call = _optional_mapping(event.get("transportCall"), field="transportCall")
    _validate_type_requirements(
        event_type=event_type,
        document_id=document_id,
        document_type_code=document_type_code,
        equipment_reference=equipment_reference,
        empty_indicator_code=empty_indicator_code,
        transport_call=transport_call,
    )

    location = _extract_location(event, transport_call=transport_call, version=version)
    transport_call_id = _optional_text(
        (transport_call or {}).get("transportCallID") or event.get("transportCallID"),
        field="transportCallID",
    )
    transport_call_sequence_number = _optional_non_negative_int(
        (transport_call or {}).get("transportCallSequenceNumber"),
        field="transportCallSequenceNumber",
    )
    vessel = _optional_mapping((transport_call or {}).get("vessel"), field="transportCall.vessel") or {}
    references = _parse_references(event.get("references"), carrier=normalized_carrier, version=version)
    document_references = _parse_document_references(event.get("documentReferences"), version=version)
    reason = _optional_text(event.get("reason"), field="reason")
    if version == "2.3" and reason is not None and len(reason) > 250:
        raise DcsaTntValidationError("reason must be at most 250 characters for TNT 2.3.")

    sanitized_payload = sanitize_dcsa_payload(event)
    idempotency_key = _idempotency_key(
        carrier=normalized_carrier,
        version=version,
        event_id=event_id,
        payload=sanitized_payload,
    )
    return DcsaTntEvent(
        carrier=normalized_carrier,
        tnt_version=version,
        event_type=event_type,
        event_code=event_code,
        event_classifier_code=event_classifier_code,
        event_id=event_id,
        conformance_warnings=tuple(warning for warning in (event_id_warning,) if warning),
        idempotency_key=idempotency_key,
        event_created_at=event_created_at,
        event_at=event_at,
        document_id=document_id,
        document_type_code=document_type_code,
        equipment_reference=equipment_reference,
        empty_indicator_code=empty_indicator_code,
        location=location,
        transport_call_id=transport_call_id,
        transport_call_sequence_number=transport_call_sequence_number,
        vessel_name=_optional_text(vessel.get("vesselName"), field="transportCall.vessel.vesselName"),
        export_voyage_number=_optional_text((transport_call or {}).get("exportVoyageNumber"), field="exportVoyageNumber"),
        import_voyage_number=_optional_text((transport_call or {}).get("importVoyageNumber"), field="importVoyageNumber"),
        references=references,
        document_references=document_references,
        source_url=_optional_text(source_url, field="source_url"),
        raw_payload=sanitized_payload,
    )


def normalize_tnt_version(value: str) -> str:
    normalized = _required_text(value, field="tnt_version").lower().lstrip("v")
    aliases = {"2.2.0": "2.2", "2.3.0": "2.3"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_TNT_VERSIONS:
        raise DcsaTntValidationError(f"Unsupported DCSA TNT version {value!r}.")
    return normalized


def sanitize_dcsa_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            if _is_sensitive_key(key):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_dcsa_payload(raw_value)
        return sanitized
    if isinstance(value, list):
        return [sanitize_dcsa_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_dcsa_payload(item) for item in value]
    return value


def _extract_event_code(event: Mapping[str, Any], *, event_type: str, version: str) -> str:
    expected_field = _EVENT_CODE_FIELD_BY_TYPE[event_type]
    present_fields = [field for field in _EVENT_CODE_FIELDS if _optional_text(event.get(field), field=field)]
    if present_fields != [expected_field]:
        raise DcsaTntValidationError(
            f"Event must contain exactly one matching event code field for {event_type}; found {present_fields or 'none'}."
        )
    code = _required_code(event.get(expected_field), field=expected_field)
    allowed_codes = _allowed_event_codes(event_type=event_type, version=version)
    if code not in allowed_codes:
        raise DcsaTntValidationError(f"Unsupported {expected_field} {code!r} for TNT {version}.")

    deprecated_code = _optional_code(event.get("eventTypeCode"), field="eventTypeCode")
    if deprecated_code is not None and deprecated_code != code:
        raise DcsaTntValidationError("Deprecated eventTypeCode conflicts with the typed event code.")
    return code


def _allowed_event_codes(*, event_type: str, version: str) -> frozenset[str]:
    if event_type == "SHIPMENT":
        return _SHIPMENT_CODES_V23 if version == "2.3" else _SHIPMENT_CODES_V22
    if event_type == "TRANSPORT":
        return _TRANSPORT_CODES
    return _EQUIPMENT_CODES


def _validate_event_classifier(event_type: str, version: str, value: str | None) -> None:
    if value is None:
        return
    if value not in _EVENT_CLASSIFIERS:
        raise DcsaTntValidationError(f"Unsupported eventClassifierCode {value!r}.")
    if event_type == "SHIPMENT" and version == "2.3" and value != "ACT":
        raise DcsaTntValidationError("TNT 2.3 shipment events must use eventClassifierCode ACT.")


def _validate_type_requirements(
    *,
    event_type: str,
    document_id: str | None,
    document_type_code: str | None,
    equipment_reference: str | None,
    empty_indicator_code: str | None,
    transport_call: Mapping[str, Any] | None,
) -> None:
    if event_type == "SHIPMENT":
        if document_id is None or document_type_code is None:
            raise DcsaTntValidationError("Shipment events require documentID and documentTypeCode.")
        return
    if event_type == "TRANSPORT":
        if transport_call is None:
            raise DcsaTntValidationError("Transport events require transportCall.")
        if _optional_text(transport_call.get("transportCallID"), field="transportCall.transportCallID") is None:
            raise DcsaTntValidationError("Transport events require transportCall.transportCallID.")
        if _optional_code(transport_call.get("modeOfTransport"), field="transportCall.modeOfTransport") is None:
            raise DcsaTntValidationError("Transport events require transportCall.modeOfTransport.")
        return
    if empty_indicator_code is None:
        raise DcsaTntValidationError("Equipment events require emptyIndicatorCode.")
    if equipment_reference is None:
        raise DcsaTntValidationError("Equipment events require equipmentReference for MTM reconciliation.")


def _extract_location(event: Mapping[str, Any], *, transport_call: Mapping[str, Any] | None, version: str) -> DcsaLocation:
    location = _optional_mapping((transport_call or {}).get("location"), field="transportCall.location")
    if location is None:
        location = _optional_mapping(event.get("eventLocation"), field="eventLocation") or {}
    root_facility_type = _optional_code(event.get("facilityTypeCode"), field="facilityTypeCode")
    nested_facility_type = _optional_code((transport_call or {}).get("facilityTypeCode"), field="transportCall.facilityTypeCode")
    if root_facility_type and root_facility_type not in _FACILITY_TYPES:
        raise DcsaTntValidationError(f"Unsupported facilityTypeCode {root_facility_type!r}.")
    if nested_facility_type and nested_facility_type not in _FACILITY_TYPES:
        raise DcsaTntValidationError(f"Unsupported transportCall.facilityTypeCode {nested_facility_type!r}.")
    if root_facility_type and nested_facility_type and root_facility_type != nested_facility_type:
        raise DcsaTntValidationError("Root and transportCall facilityTypeCode conflict.")
    if version == "2.2" and root_facility_type:
        raise DcsaTntValidationError("TNT 2.2 does not define root-level facilityTypeCode.")
    return DcsaLocation(
        un_location_code=_optional_code(location.get("UNLocationCode"), field="location.UNLocationCode"),
        facility_code=_optional_text(location.get("facilityCode"), field="location.facilityCode"),
        facility_code_list_provider=_optional_code(location.get("facilityCodeListProvider"), field="location.facilityCodeListProvider"),
        facility_type_code=root_facility_type or nested_facility_type,
    )


def _parse_references(value: Any, *, carrier: str, version: str) -> tuple[DcsaReference, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DcsaTntValidationError("references must be an array.")
    allowed_types = _allowed_reference_types(carrier=carrier, version=version)
    references: list[DcsaReference] = []
    for index, item in enumerate(value):
        reference = _as_mapping(item, field=f"references[{index}]")
        reference_type = _required_code(reference.get("referenceType"), field=f"references[{index}].referenceType")
        if reference_type not in allowed_types:
            raise DcsaTntValidationError(f"Unsupported referenceType {reference_type!r} for TNT {version}.")
        references.append(
            DcsaReference(
                reference_type=reference_type,
                reference_value=_required_text(reference.get("referenceValue"), field=f"references[{index}].referenceValue"),
            )
        )
    return tuple(references)


def _allowed_reference_types(*, carrier: str, version: str) -> frozenset[str]:
    allowed_types = _REFERENCE_TYPES_V23 if version == "2.3" else _REFERENCE_TYPES_V22
    if version == "2.2" and carrier in _CMA_CGM_CARRIER_ALIASES:
        return allowed_types | _CMA_CGM_REFERENCE_TYPES_V22
    return allowed_types


def _parse_document_references(value: Any, *, version: str) -> tuple[DcsaDocumentReference, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise DcsaTntValidationError("documentReferences must be an array.")
    allowed_types = _DOCUMENT_REFERENCE_TYPES_V23 if version == "2.3" else _DOCUMENT_REFERENCE_TYPES_V22
    references: list[DcsaDocumentReference] = []
    for index, item in enumerate(value):
        reference = _as_mapping(item, field=f"documentReferences[{index}]")
        raw_type = _required_text(reference.get("documentReferenceType"), field=f"documentReferences[{index}].documentReferenceType")
        reference_type = _LEGACY_DOCUMENT_REFERENCE_TYPES.get(raw_type.upper(), raw_type.upper())
        if reference_type not in allowed_types:
            raise DcsaTntValidationError(f"Unsupported documentReferenceType {raw_type!r} for TNT {version}.")
        references.append(
            DcsaDocumentReference(
                reference_type=reference_type,
                reference_value=_required_text(reference.get("documentReferenceValue"), field=f"documentReferences[{index}].documentReferenceValue"),
            )
        )
    return tuple(references)


def _idempotency_key(*, carrier: str, version: str, event_id: str | None, payload: Mapping[str, Any]) -> str:
    if event_id:
        return f"{carrier}:{version}:{event_id}"
    canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return f"{carrier}:{version}:sha256:{sha256(canonical_payload.encode('utf-8')).hexdigest()}"


def _as_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DcsaTntValidationError(f"{field} must be an object.")
    return value


def _optional_mapping(value: Any, *, field: str) -> Mapping[str, Any] | None:
    if value is None:
        return None
    return _as_mapping(value, field=field)


def _required_text(value: Any, *, field: str) -> str:
    normalized = _optional_text(value, field=field)
    if normalized is None:
        raise DcsaTntValidationError(f"{field} is required.")
    return normalized


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DcsaTntValidationError(f"{field} must be a string.")
    normalized = value.strip()
    return normalized or None


def _required_code(value: Any, *, field: str) -> str:
    normalized = _optional_code(value, field=field)
    if normalized is None:
        raise DcsaTntValidationError(f"{field} is required.")
    return normalized


def _optional_code(value: Any, *, field: str) -> str | None:
    text = _optional_text(value, field=field)
    return text.upper() if text else None


def _optional_event_id(*, value: Any, carrier: str, version: str) -> tuple[str | None, str | None]:
    event_id = _optional_text(value, field="eventID")
    if event_id is None:
        return None, None
    if len(event_id) > 256:
        raise DcsaTntValidationError("eventID must be at most 256 characters when supplied.")
    try:
        return str(UUID(event_id)), None
    except ValueError as exc:
        if version == "2.2" and carrier in _CMA_CGM_CARRIER_ALIASES:
            return event_id, "carrier-event-id-not-uuid"
        raise DcsaTntValidationError("eventID must be a UUID when supplied.") from exc


def _parse_datetime(value: Any, *, field: str) -> datetime:
    text = _required_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DcsaTntValidationError(f"{field} must be an ISO-8601 date-time.") from exc
    if parsed.tzinfo is None:
        raise DcsaTntValidationError(f"{field} must include a UTC offset.")
    return parsed.astimezone(timezone.utc)


def _optional_datetime(value: Any, *, field: str) -> datetime | None:
    return None if value is None else _parse_datetime(value, field=field)


def _optional_non_negative_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DcsaTntValidationError(f"{field} must be a non-negative integer.")
    return value


def _is_sensitive_key(value: str) -> bool:
    normalized = value.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)
