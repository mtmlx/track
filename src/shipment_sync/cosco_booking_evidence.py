"""One scoped, read-only official COSCO booking observation.

This is not a sync engine or a DCSA event converter. It has no writer or archive
client. The caller must retain the returned native bytes and exact receipt before
connecting an independently verified consumer. No inferred vessel/carrier events.
"""
from __future__ import annotations
import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from shipment_sync.carriers.common import bounded_response_bytes

PRODUCTION_ORIGIN = 'https://api.lines.coscoshipping.com'
MAX_BYTES = 2_000_000
TENANT = 'b99e1c1281a18ff33519f4fd8079b397e0b27e3bc5e35b39b796782ed0791987'
TASKS = frozenset({'86e3bvrv7'})  # Explicit 33762 pilot; never infer enrollment.
FIELDS = ('sequenceNumber', 'vesselName', 'voyageNo', 'portOfLoading',
          'portOfDischarge', 'actualDepartureDate', 'expectedDateOfDeparture',
          'actualArrivalDate', 'estimatedDateOfArrival', 'actualShippingDate',
          'actualDischargeDate')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _at(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp timezone required')
    return parsed


def _scope(scope, now):
    if scope.get('query_held') is not False or scope.get('active') is not True:
        raise ValueError('Held or inactive source prohibited')
    binding = scope.get('binding') or {}
    if (binding.get('tenant') != TENANT or binding.get('task_id') not in TASKS
            or binding.get('customer_crm_task_id') != '86dwkvu5b'
            or binding.get('workspace_id') != '8451352'
            or binding.get('list_id') != '901704769513'
            or binding.get('carrier') != 'COSCO'
            or binding.get('coverage_kind') != 'BOOKING_ITINERARY'):
        raise ValueError('Outside exact reviewed Movesa COSCO scope')
    if not re.fullmatch(r'[A-Z0-9]{5,24}', binding.get('booking_reference', '')):
        raise ValueError('Exact current booking required')
    observed = _at(scope.get('native_scope_verified_at'))
    expires = _at(scope.get('expires_at'))
    if not timedelta(0) <= now-observed <= timedelta(hours=1) or not now < expires <= observed+timedelta(hours=1):
        raise ValueError('Current native CRM/carrier/booking scope expired')
    if not re.fullmatch(r'[a-f0-9]{64}', scope.get('native_source_sha256', '')) or not scope.get('authority_ref'):
        raise ValueError('Native scope proof and authority required')
    containers = binding.get('assigned_containers')
    if not isinstance(containers, list) or len(containers) != len(set(containers)) or any(not re.fullmatch(r'[A-Z]{4}\d{7}', c) for c in containers):
        raise ValueError('Current container population required')
    return binding


def _normal(value):
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def native_booking(raw, binding):
    """Literal native observations with JSON pointers; never movement completion."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError('Bounded native JSON bytes required')
    data = json.loads(raw)
    if data.get('code') not in (0, '0') or isinstance(data.get('code'), bool):
        raise ValueError('COSCO success code required')
    envelope = data.get('data') or {}
    if envelope.get('type') != 'bkg' or envelope.get('numberType') not in ('bkg', 'both'):
        raise ValueError('Native response must be a booking lookup')
    content = envelope.get('content') or {}
    tracking = content.get('trackingPath') or {}
    # COP docs explicitly define billOfladingRefCode as the booking number.
    if str(tracking.get('billOfladingRefCode', '')) != binding['booking_reference']:
        raise ValueError('Native COSCO booking identity differs')
    destination = tracking.get('pod')
    accepted = {binding.get('destination_port_label'), *binding.get('destination_port_codes', [])} - {None, ''}
    destination_verified = bool(destination and _normal(destination) in {_normal(v) for v in accepted})
    legs = content.get('actualShipment') or []
    if not isinstance(legs, list) or len(legs) > 100:
        raise ValueError('Bounded native itinerary required')
    observations = []
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise ValueError('Invalid native itinerary row')
        fields = {}
        for field in FIELDS:
            value = leg.get(field)
            if value is None or value == '':
                continue
            if isinstance(value, (dict, list, bool)) or len(str(value)) > 500:
                raise ValueError('Unexpected native itinerary field shape')
            fields[field] = {'value': value, 'json_pointer': f'/data/content/actualShipment/{index}/{field}'}
        observations.append({'native_row': index, 'fields': fields})
    assigned = []
    groups = content.get('containerStatus') or []
    if not isinstance(groups, list) or len(groups) > 100:
        raise ValueError('Bounded native container inventory required')
    for group in groups:
        rows = group.get('containers') or []
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError('Bounded native container group required')
        for row in rows:
            unit = row.get('containerNumber')
            if unit:
                if not isinstance(unit, str) or not re.fullmatch(r'[A-Z]{4}\d{7}', unit):
                    raise ValueError('Ambiguous native container identity')
                assigned.append(unit)
    assigned = sorted(set(assigned))
    required = binding['assigned_containers']
    container_binding_verified = not required or set(required) == set(assigned)
    complete = bool(observations) and destination_verified and container_binding_verified
    result = {
        'native_format': 'COSCO_COP_BOOKING_JSON', 'booking_reference': binding['booking_reference'],
        'booking_identity_json_pointer': '/data/content/trackingPath/billOfladingRefCode',
        'destination': destination, 'destination_verified': destination_verified,
        'observed_container_assignments': assigned, 'container_binding_verified': container_binding_verified,
        'native_itinerary_rows': observations, 'native_payload_sha256': hashlib.sha256(raw).hexdigest(),
        'state': 'NATIVE_BOOKING_ITINERARY_OBSERVED' if complete else 'BOOKING_IDENTIFIED_INCOMPLETE_EVIDENCE',
        'not_a_dcsa_event': True, 'container_movements_completed': False,
        'date_timezone_inferred': False, 'atlas_document_approval': False,
        'source_field_writes': 0,
    }
    result['proof_sha256'] = _digest(result)
    return result


def capture(scope, *, adapter_factory, now=None):
    """Exactly one official HMAC GET, injected for tests; no fallback/replay."""
    now = now or datetime.now(timezone.utc)
    binding = _scope(scope, now)
    adapter = adapter_factory()
    if not adapter._has_cop_credentials():
        return {'state': 'ADAPTER_CREDENTIALS_MISSING', 'provider_query_attempts': 0, 'source_field_writes': 0}
    if (adapter.mode != 'cop' or adapter.cop_base_url != PRODUCTION_ORIGIN
            or adapter.cop_service_prefix != '/service' or adapter.cop_booking_number_type != 'bkg'):
        raise ValueError('Exact production COSCO COP route required; no UAT or fallback')
    url = f"{PRODUCTION_ORIGIN}/service/info/tracking/{quote(binding['booking_reference'])}?numberType=bkg"
    original_get = adapter.session.get
    receipt = {'provider_query_attempts': 0}

    def once(requested_url, **kwargs):
        if receipt['provider_query_attempts'] or requested_url != url or kwargs.get('allow_redirects') is not False:
            raise ValueError('Outside the single reviewed native booking GET')
        receipt['provider_query_attempts'] = 1
        response = original_get(requested_url, **kwargs)
        raw = bounded_response_bytes(response, max_bytes=MAX_BYTES)
        if response.status_code != 200 or response.url != url:
            raise ValueError('Exact successful native response required')
        receipt.update(raw=raw, http_status=response.status_code, url=response.url)
        return response

    adapter.max_retries = 0
    adapter.timeout_seconds = min(max(1, adapter.timeout_seconds), 45)
    adapter.session.get = once
    try:
        payload, source = adapter._fetch_payload_cop(reference=binding['booking_reference'], logical_type='booking')
        raw = receipt.get('raw')
        if not raw or json.loads(raw) != payload or source != 'cosco-cop:' + url:
            raise ValueError('Native response changed before verification')
        observation = native_booking(raw, binding)
        return {'state': observation['state'], 'binding': binding, 'observation': observation,
                'native_response': {'body_base64': base64.b64encode(raw).decode(), 'sha256': hashlib.sha256(raw).hexdigest(),
                                    'url': url, 'http_status': 200, 'content_kind': 'RAW_HTTP_JSON'},
                'observed_at': now.isoformat(), 'scope_sha256': _digest(scope),
                'provider_query_attempts': 1, 'source_field_writes': 0, 'archive_writes': 0}
    except Exception as error:
        # Preserve uncertainty without leaking headers, HMAC credentials or a
        # provider-supplied error body. Caller reconciles; never replay here.
        result = {'state': 'NATIVE_READ_GAP', 'provider_query_attempts': receipt['provider_query_attempts'],
                'error_type': type(error).__name__, 'source_field_writes': 0, 'archive_writes': 0,
                'retry_allowed': False, 'requires_reconciliation': True}
        # A successful HTTP body that fails semantic matching is retained as
        # discrepant private evidence, never silently discarded or approved.
        if receipt.get('raw'):
            result['native_response'] = {'body_base64': base64.b64encode(receipt['raw']).decode(),
                'sha256': hashlib.sha256(receipt['raw']).hexdigest(), 'url': receipt['url'],
                'http_status': receipt['http_status'], 'content_kind': 'RAW_HTTP_JSON'}
        return result
    finally:
        adapter.session.get = original_get
