"""Read-only per-container destination milestones, separate from workflow projection."""
import re
import hashlib
import json
from datetime import datetime, timezone

from .destination import same_port


def container_progress(shipment, status, *, now=None):
    if shipment.shipping_line.strip().lower() != 'one' or not status.container_movements:
        return None
    now = now or datetime.now(timezone.utc)
    recorded = set(re.findall(r'\b[A-Z]{4}\d{7}\b', (shipment.container_no or '').upper()))
    records = sorted(status.container_movements, key=lambda r: r.container)
    population = {r.container for r in records}
    expected = shipment.expected_container_count or len(recorded | population)
    if max(expected, len(recorded), len(population)) <= 1:
        return None
    population_verified = (len(records) == len(population) == expected
                           and recorded.issubset(population))
    rows = []
    for record in records:
        def valid(move):
            return (move.event_time is not None and move.event_time.tzinfo is not None
                    and move.container_reference == record.container
                    and same_port(shipment.destination_port, move.location_code or move.location))
        moves = [m for m in record.movements if valid(m)]
        discharges = [m for m in moves if m.name.endswith('(DISC)')
                      and m.event_state == 'actual' and m.equipment_load_state == 'LADEN'
                      and m.event_time <= now]
        def candidates(code, load, actual):
            return [m for m in moves if m.name.endswith('(' + code + ')')
                    and m.equipment_load_state == load
                    and (m.event_state == 'actual' and m.event_time <= now if actual
                         else m.event_state == 'estimated')
                    and any(d.event_time <= m.event_time for d in discharges)]
        def selected(code, load, actual):
            found = candidates(code, load, actual)
            move = max(found, key=lambda m: m.event_time, default=None)
            if move is None:
                return None
            return dict(timestamp=move.event_time.isoformat(),
                        port_local_timestamp=move.event_time_local_text or move.event_time.isoformat(),
                        location=move.location, location_code=move.location_code,
                        event_id=move.source_event_id, classification=move.event_state)
        rows.append(dict(container=record.container, booking=record.booking,
                         source=record.source_url,
                         gate_out=selected('GTOT', 'LADEN', True),
                         empty_return=selected('GTIN', 'EMPTY', True),
                         estimated_gate_out=selected('GTOT', 'LADEN', False),
                         estimated_empty_return=selected('GTIN', 'EMPTY', False)))
    def completion(key):
        times = [r[key]['timestamp'] for r in rows if r[key]]
        complete = population_verified and len(times) == expected
        return dict(completed=len(times), expected=expected, complete=complete,
                    completed_at=max(times, key=datetime.fromisoformat) if complete else None,
                    outstanding=sorted((recorded | population) - {r['container'] for r in rows if r[key]}))
    return dict(population_verified=population_verified, gate_out=completion('gate_out'),
                empty_return=completion('empty_return'), containers=rows)


def progress_comment(progress):
    if progress is None:
        return ''
    out, returned = progress['gate_out'], progress['empty_return']
    lines = ['Container movement register (carrier evidence)',
             f"Port gate-out: {out['completed']}/{out['expected']} ACTUAL",
             f"Empty returned: {returned['completed']}/{returned['expected']} ACTUAL",
             'Times below retain the carrier port timezone/UTC offset. Estimates never count as completed.']
    if not progress['population_verified']:
        lines.append('Container population incomplete or inconsistent: shipment completion is NOT verified.')
    for row in progress['containers']:
        def label(key):
            item = row[key]
            return item['port_local_timestamp'] if item else 'NOT CONFIRMED'
        lines.append(f"{row['container']} | booking {row['booking'] or 'not available'} | "
                     f"gate-out ACTUAL: {label('gate_out')} | empty return ACTUAL: {label('empty_return')}")
        for key in ('estimated_gate_out', 'estimated_empty_return'):
            actual_key = 'gate_out' if key == 'estimated_gate_out' else 'empty_return'
            if row[key] and not row[actual_key]:
                lines.append(f"  {key.replace('_', ' ')}: {label(key)} (ESTIMATE ONLY)")
        if row['source']:
            lines.append(f"  Carrier source: {row['source']}")
        ids = [row[k]['event_id'] for k in ('gate_out', 'empty_return') if row[k] and row[k]['event_id']]
        if ids:
            lines.append('  Actual event IDs: ' + ', '.join(ids))
    fingerprint = hashlib.sha256(json.dumps(progress, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    lines.append('Container register fingerprint: ' + fingerprint)
    return '\n'.join(lines)
