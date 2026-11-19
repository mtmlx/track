from datetime import datetime
from types import SimpleNamespace

from shipment_sync.models import ContainerMovementRecord, MovementEvent, ShipmentRef, ShipmentStatus
from shipment_sync.one_departure_notice import publish_one_final_departure


def test_final_departure_correction_posts_once_without_field_writes():
    ref = "ONEU1505734"
    shipment = ShipmentRef("task", "33718", "one", "NB6BI3721300", ref, "list",
                           destination_port="Puerto Quetzal")

    def move(code, time, port, voyage, state="actual"):
        return MovementEvent(f"Movement ({code})", "LAZARO CARDENAS, MEXICO(APM)",
                             datetime.fromisoformat(time), time, state, voyage,
                             location_code=port, transport_mode="VESSEL",
                             container_reference=ref if code == "LOAD" else None)

    load = move("LOAD", "2026-11-18T07:12:00-06:00", "MXLZC", "NYK SILVIA 0443W")
    departure = move("DEPA", "2026-11-18T13:14:00-06:00", "MXLZC", "NYK SILVIA 0443W")
    arrival = move("ARRI", "2026-11-20T08:00:00-06:00", "GTPRQ", "NYK SILVIA 0444E", "estimated")
    moves = [load, departure, arrival]
    status = ShipmentStatus("Tracking", eta_time=arrival.event_time, recent_moves=moves,
                            raw_source="https://apix.one-line.com/v2/events",
                            container_movements=[ContainerMovementRecord(ref, shipment.booking_no, None, moves)])
    comments = ["5. Transport Departed (DEPA) | 2026-11-18 08:00 | LAZARO CARDENAS, MEXICO(APM... | ESTIMATE"]
    client = SimpleNamespace(
        _fetch_recent_task_comment_texts=lambda *a, **kw: comments,
        get_shipments_by_task_ids=lambda *a, **kw: [shipment],
        _post_comment=lambda task, text: comments.insert(0, text),
    )
    now = datetime.fromisoformat("2026-11-18T14:00:00-06:00")
    departure.event_state = "estimated"
    assert publish_one_final_departure(client, shipment, status, now=now) == "unverified"
    departure.event_state = "actual"
    load.vessel_voyage = "NYK SILVIA 0439W"
    assert publish_one_final_departure(client, shipment, status, now=now) == "unverified"
    load.vessel_voyage = departure.vessel_voyage
    assert publish_one_final_departure(client, shipment, status, now=now) == "published"
    assert "13:14-06:00 port local time (ACTUAL)" in comments[0]
    assert publish_one_final_departure(client, shipment, status, now=now) == "unchanged"
    assert len(comments) == 2
