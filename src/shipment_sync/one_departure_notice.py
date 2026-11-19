"""Comment-only correction for a confirmed ONE final-leg sailing."""
from datetime import datetime, timezone
import hashlib
import re
import unicodedata

from .destination import same_port
from .one_write_holds import write_hold_reason


def _vessel(move):
    return (move.vessel_voyage or "").upper().rsplit(" ", 1)[0]


def _place(value):
    return unicodedata.normalize("NFKD", (value or "").split("(")[0]).encode(
        "ascii", "ignore").decode().strip().casefold()


def _final_departure(shipment, status, now):
    if (status.raw_source != "https://apix.one-line.com/v2/events" or status.booking_only
            or not status.eta_time or not shipment.destination_port):
        return None
    moves = [m for m in status.recent_moves if m.event_time and m.event_time.tzinfo]
    arrivals = [m for m in moves if m.name.endswith("(ARRI)") and m.event_time == status.eta_time
                and m.transport_mode == "VESSEL" and _vessel(m)
                and same_port(shipment.destination_port, m.location_code or m.location)]
    if len(arrivals) != 1:
        return None
    arrival = arrivals[0]
    route = [m for m in moves if m.event_time < arrival.event_time and m.transport_mode == "VESSEL"
             and m.name.endswith(("(DEPA)", "(LOAD)", "(DISC)", "(ARRI)"))]
    if not route:
        return None
    departure = max(route, key=lambda m: m.event_time)
    if (not departure.name.endswith("(DEPA)") or departure.event_state != "actual"
            or departure.event_time > now or _vessel(departure) != _vessel(arrival)
            or not departure.location_code or not departure.vessel_voyage
            or not departure.event_time_local_text
            or same_port(shipment.destination_port, departure.location_code)):
        return None
    records = status.container_movements
    expected = set(re.findall(r"\b[A-Z]{4}\d{7}\b", (shipment.container_no or "").upper()))
    if (not expected or len(records) != len(expected) or {r.container for r in records} != expected
            or shipment.expected_container_count not in (None, len(expected))):
        return None
    for record in records:
        calls = [m for m in record.movements if m.name.endswith(("(LOAD)", "(DISC)"))
                 and m.container_reference == record.container and m.event_time and m.event_time.tzinfo
                 and m.event_time <= departure.event_time]
        last = max(calls, key=lambda m: m.event_time, default=None)
        if (not last or not last.name.endswith("(LOAD)") or last.event_state != "actual"
                or last.location_code != departure.location_code
                or last.vessel_voyage != departure.vessel_voyage):
            return None
    return departure


def publish_one_final_departure(client, shipment, status, *, now=None):
    if shipment.shipping_line.strip().lower() != "one" or write_hold_reason(shipment):
        return "ineligible"
    move = _final_departure(shipment, status, now or datetime.now(timezone.utc))
    if not move:
        return "unverified"
    local = datetime.fromisoformat(move.event_time_local_text)
    if not local.tzinfo or local != move.event_time:
        return "unverified"
    fingerprint = hashlib.sha256(
        f"{shipment.booking_no}|{shipment.container_no}|{move.location_code}|{move.vessel_voyage}|{move.event_time.isoformat()}".encode()
    ).hexdigest()
    marker = f"ONE final-leg departure fingerprint: {fingerprint}"
    comments = client._fetch_recent_task_comment_texts(shipment.task_id, limit=25)
    if any(marker in text.splitlines() for text in comments):
        return "unchanged"
    previous = None
    for text in comments:
        for line in text.splitlines():
            match = re.search(r"Transport Departed \(DEPA\)\s*\|\s*(\d{4}-\d\d-\d\d \d\d:\d\d)\s*\|\s*([^|]+)\|\s*ESTIMATE\s*$", line)
            if (match and match[1][:10] == local.date().isoformat()
                    and _place(match[2]) and _place(match[2]) == _place(move.location)):
                previous = match[1]
                break
        if previous:
            break
    if not previous:
        return "no_matching_estimate"
    refs = client.get_shipments_by_task_ids([shipment.task_id], allowed_list_ids={shipment.list_id})
    def identity(ref):
        return (ref.shipping_line, ref.booking_no, ref.container_no,
                ref.expected_container_count, ref.destination_port, ref.current_task_status)
    if len(refs) != 1 or identity(refs[0]) != identity(shipment) or write_hold_reason(refs[0]):
        return "identity_changed"
    client._post_comment(shipment.task_id,
        f"ONE final-leg departure correction: {move.vessel_voyage} departed "
        f"{_place(move.location).title()} ({move.location_code}) at "
        f"{local.isoformat(sep=' ', timespec='minutes')} port local time (ACTUAL). "
        f"This replaces the earlier {previous} estimate for that departure. "
        "Origin ETD, destination ETA and shipment status are not changed by this correction.\n"
        f"Carrier source: {status.source_url or status.raw_source}\n{marker}")
    return "published"
