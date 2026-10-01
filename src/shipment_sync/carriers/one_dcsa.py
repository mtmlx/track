"""ONE DCSA T&T on the production apix domain; no operational writes."""
import os
import re
import time
from datetime import datetime, timezone

import requests

from shipment_sync.carriers.common import bounded_response_json, reject_response_redirect, parse_event_time, to_dcsa_movement_name
from shipment_sync.destination import same_port
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus


class OneDcsaClient:
    def __init__(self):
        self.key = os.getenv('ONE_DCSA_API_KEY', '').strip()
        self.secret = os.getenv('ONE_DCSA_API_SECRET', '').strip()
        self.base = 'https://apix.one-line.com'
        self.timeout = int(os.getenv('ONE_TIMEOUT_SECONDS', '60'))
        self.session = requests.Session()
        self.token = None
        self.expires = 0.0

    def _request(self, method, path, **kwargs):
        for attempt in range(3):
            try:
                response = self.session.request(method, self.base + path, timeout=self.timeout,
                    stream=True, allow_redirects=False, hooks={'response': reject_response_redirect}, **kwargs)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError('ONE DCSA connection failed') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                response.close()
                time.sleep(2 ** attempt)
                continue
            try:
                if response.status_code >= 400:
                    raise RuntimeError(f'ONE DCSA HTTP {response.status_code}')
                return bounded_response_json(response), dict(response.headers)
            finally:
                response.close()
        raise RuntimeError('ONE DCSA request failed')

    def _token(self):
        if not self.key or not self.secret:
            raise RuntimeError('ONE DCSA credentials are missing')
        if self.token and time.monotonic() < self.expires:
            return self.token
        payload, _ = self._request('POST', '/v1/oauth/accesstoken?grant_type=client_credentials',
            auth=(self.key, self.secret), headers={'apikey': self.key, 'Content-Type': 'application/x-www-form-urlencoded'}, data='')
        products = payload.get('api_product_list_json', payload.get('api_product_list', ''))
        if 'DCSA TNT_PROD' not in products or not payload.get('access_token'):
            raise RuntimeError('ONE token lacks DCSA Track & Trace entitlement')
        self.token = payload['access_token']
        self.expires = time.monotonic() + max(0, int(payload.get('expires_in', 0)) - 60)
        return self.token

    def events(self, shipment):
        containers = re.findall(r'\b[A-Z]{4}\d{7}\b', (shipment.container_no or '').upper())
        if len(set(containers)) > 1:
            raise ValueError('ONE DCSA needs a single container per shipment record')
        if containers:
            params = {'equipmentReference': containers[0], 'limit': 100}
        elif shipment.booking_no:
            params = {'carrierBookingReference': shipment.booking_no.strip(), 'limit': 100}
        else:
            raise ValueError('ONE DCSA requires a container or booking reference')
        events, seen_cursors = [], set()
        for _ in range(20):
            token = self._token()
            try:
                page, headers = self._request('GET', '/v2/events', params=params,
                    headers={'Authorization': 'Bearer ' + token, 'apikey': self.key})
            except RuntimeError as exc:
                if str(exc) != 'ONE DCSA HTTP 401':
                    raise
                self.token = None
                page, headers = self._request('GET', '/v2/events', params=params,
                    headers={'Authorization': 'Bearer ' + self._token(), 'apikey': self.key})
            if not isinstance(page, list) or any(not isinstance(e, dict) for e in page):
                raise ValueError('ONE DCSA returned an invalid event list')
            for event in page:
                refs = [event.get('equipmentReference')] + [r.get('referenceValue') for r in event.get('references', []) if r.get('referenceType') == 'EQ']
                bookings = [r.get('documentReferenceValue') for r in event.get('documentReferences', []) if r.get('documentReferenceType') == 'BKG']
                if containers and any(ref and ref != containers[0] for ref in refs):
                    raise ValueError('ONE DCSA returned a conflicting container')
                if shipment.booking_no and bookings and shipment.booking_no.strip() not in bookings:
                    raise ValueError('ONE DCSA returned a conflicting booking')
            events.extend(page)
            cursor = next((v for k, v in headers.items() if k.lower() in ('next-page-cursor', 'nextpagecursor')), None)
            if not cursor:
                if any(k.lower() == 'link' and 'rel="next"' in v for k, v in headers.items()):
                    raise ValueError('ONE DCSA returned unsupported link pagination')
                if len(page) >= 100:
                    raise ValueError('ONE DCSA full page without next cursor; completeness uncertain')
                break
            if cursor in seen_cursors:
                raise ValueError('ONE DCSA repeated a pagination cursor')
            seen_cursors.add(cursor)
            params['cursor'] = cursor
        else:
            raise ValueError('ONE DCSA pagination limit reached')
        unique = {}
        for event in events:
            identity = event.get('eventID')
            if not identity:
                raise ValueError('ONE DCSA event has no identity')
            old = unique.get(identity)
            if old is None or (event.get('eventCreatedDateTime', '') > old.get('eventCreatedDateTime', '')):
                unique[identity] = event
        return list(unique.values()), containers

    def fetch_status(self, shipment: ShipmentRef) -> ShipmentStatus:
        events, requested = self.events(shipment)
        if not events:
            raise ValueError('ONE DCSA returned no events; preserve existing shipment fields')
        discovered = sorted({ref for e in events for ref in ([e.get('equipmentReference')] + [r.get('referenceValue') for r in e.get('references', []) if r.get('referenceType') == 'EQ']) if ref and re.fullmatch(r'[A-Z]{4}\d{7}', ref)})
        # Booking-wide movements must not be projected onto an arbitrary container.
        if not requested and len(discovered) != 1:
            raise ValueError('ONE DCSA booking has multiple/no containers; container-level tracking required')
        now = datetime.now(timezone.utc)
        moves, arrivals = [], []
        for e in events:
            if e.get('eventType') not in ('EQUIPMENT', 'TRANSPORT'):
                continue
            tc = e.get('transportCall') or {}
            loc = e.get('eventLocation') or {}
            code = loc.get('UNLocationCode') or tc.get('UNLocationCode')
            location = loc.get('locationName') or tc.get('otherFacility') or code
            raw_time = e.get('eventDateTime')
            if not isinstance(raw_time, str) or datetime.fromisoformat(raw_time.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('ONE DCSA event lacks an explicit timezone')
            dt = parse_event_time(raw_time)
            if dt is None or dt.tzinfo is None:
                raise ValueError('ONE DCSA event lacks a timezone-aware timestamp')
            classifier = e.get('eventClassifierCode')
            if classifier not in ('ACT', 'EST', 'PLN'):
                raise ValueError('ONE DCSA event has unknown classifier')
            if classifier == 'ACT' and dt > now:
                raise ValueError('ONE DCSA actual event is in the future')
            event_code = e.get('equipmentEventTypeCode') or e.get('transportEventTypeCode')
            vessel = (tc.get('vessel') or {}).get('vesselName')
            voyage = tc.get('exportVoyageNumber') or tc.get('importVoyageNumber')
            vv = ' '.join(str(x) for x in (vessel, voyage) if x) or None
            name = to_dcsa_movement_name(event=e)
            source_name = None
            if event_code == 'GTIN' and e.get('emptyIndicatorCode') == 'EMPTY':
                source_name = 'Empty Container Returned from Customer'
            moves.append(MovementEvent(name=name, location=location, event_time=dt,
                event_time_local_text=raw_time, event_state='actual' if classifier == 'ACT' else 'estimated',
                vessel_voyage=vv, source_event_name=source_name, location_code=code))
            if event_code == 'ARRI' and same_port(shipment.destination_port, code or location):
                arrivals.append((classifier == 'ACT', dt, raw_time, vv))
        moves.sort(key=lambda m: m.event_time, reverse=True)
        actual = [m for m in moves if m.event_state == 'actual']
        latest = actual[0] if actual else None
        arrival = max(arrivals, default=None, key=lambda a: (a[0], a[1]))
        return ShipmentStatus(status_text=latest.name if latest else 'Awaiting actual ONE event',
            location=latest.location if latest else None, event_time=latest.event_time if latest else None,
            eta_time=arrival[1] if arrival else None, eta_local_text=arrival[2] if arrival else None,
            latest_move=latest, recent_moves=moves, discovered_containers=discovered,
            container_discovery_authoritative=bool(requested), raw_source=self.base + '/v2/events',
            source_url=self.base + '/v2/events', vessel_voyage=arrival[3] if arrival else None,
            final_vessel_voyage=arrival[3] if arrival else None,
            destination_port=shipment.destination_port, require_destination_evidence=True)
