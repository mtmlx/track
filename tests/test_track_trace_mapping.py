from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.carriers.common import to_dcsa_movement_name
from shipment_sync.carriers.one import _extract_departure_move_from_voyage_list_data
from shipment_sync.date_utils import format_port_local_time
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus


def _settings(**overrides: object) -> Settings:
    base = dict(
        clickup_api_token="token",
        clickup_oauth_access_token=None,
        clickup_oauth_client_id=None,
        clickup_oauth_client_secret=None,
        clickup_oauth_redirect_uri=None,
        clickup_list_id="list-1",
        clickup_list_ids=["list-1"],
        clickup_team_id=None,
        clickup_space_ids=[],
        clickup_folder_ids=[],
        clickup_discover_from_spaces=False,
        clickup_discover_from_team=False,
        cf_container_no="container-field",
        cf_booking_no="booking-field",
        cf_shipping_line="carrier-field",
        cf_shipment_status=None,
        cf_status_last_checked="last-checked-field",
        cf_track_trace_snapshot=None,
        cf_eta="eta-field",
        cf_etd="etd-field",
        cf_discharge_date="disc-field",
        cf_gate_in_full="gtin-full-field",
        cf_gate_out_empty="gtot-empty-field",
        cf_gate_out_delivery="gtot-delivery-field",
        cf_gate_in_empty="gtin-empty-field",
    )
    base.update(overrides)
    return Settings(**base)


def _days_from_now(offset: int) -> datetime:
    now = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    return now + timedelta(days=offset)


def _ms_days_from_now(offset: int) -> str:
    return str(int(_days_from_now(offset).timestamp() * 1000))


def test_plan_shipment_update_maps_origin_and_destination_events_to_fields() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="maersk",
        booking_no="BOOK-1",
        container_no="CONT-1",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        eta_time=datetime(2026, 3, 25, 8, 0, tzinfo=timezone.utc),
        eta_local_text="2026-03-25 14:00",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 23, 12, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 22, 9, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 6, 10, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 4, 8, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETA"].field_id == "eta-field"
    assert updates["ETA"].value.date().isoformat() == "2026-03-25"
    assert updates["ETD"].field_id == "etd-field"
    assert updates["ETD"].value.date().isoformat() == "2026-02-10"
    assert updates["Discharge date"].field_id == "disc-field"
    assert updates["Discharge date"].value.date().isoformat() == "2026-03-22"
    assert updates["Gate-in full"].field_id == "gtin-full-field"
    assert updates["Gate-in full"].value.date().isoformat() == "2026-02-06"
    assert updates["Gate out empty"].field_id == "gtot-empty-field"
    assert updates["Gate out empty"].value.date().isoformat() == "2026-02-04"
    assert updates["Gate out delivery"].field_id == "gtot-delivery-field"
    assert updates["Gate out delivery"].value.date().isoformat() == "2026-03-23"
    assert updates["Gate in empty"].field_id == "gtin-empty-field"
    assert updates["Gate in empty"].value.date().isoformat() == "2026-03-23"
    assert plan.comment_text is not None
    assert "Last checked (UTC): " in plan.comment_text
    assert "ETA (port local time): 2026-03-25 14:00" in plan.comment_text


def test_plan_shipment_update_accepts_dated_msc_delivery_events_without_state() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-msc-terminal",
        task_name="MSC terminal events",
        shipping_line="msc",
        booking_no="181AY0266397564A1",
        container_no="MSMU4766813",
        list_id="list-1",
        list_name="RTA Shipments",
        current_task_status="near arrival",
    )
    status = ShipmentStatus(
        status_text="ETA unavailable",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="MIAMI, US",
                event_time=_days_from_now(-1),
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="MIAMI, US",
                event_time=_days_from_now(-7),
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="MIAMI, US",
                event_time=_days_from_now(-11),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Discharge date"].value.date() == _days_from_now(-11).date()
    assert updates["Gate out delivery"].value.date() == _days_from_now(-7).date()
    assert updates["Gate in empty"].value.date() == _days_from_now(-1).date()
    assert plan.task_status_update == "empty returned"


def test_plan_shipment_update_does_not_update_task_status_even_when_enabled() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            clickup_task_status_on_update="tránsito",
        )
    )
    shipment = ShipmentRef(
        task_id="task-status-disabled",
        task_name="Shipment status disabled",
        shipping_line="maersk",
        booking_no="BOOK-STATUS",
        container_no="CONT-STATUS",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        eta_time=datetime(2026, 3, 25, 8, 0, tzinfo=timezone.utc),
        eta_local_text="2026-03-25 14:00",
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.changed is True
    assert plan.task_status_update is None


def test_plan_shipment_update_moves_pending_booking_to_booking_confirmed() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-bk",
        task_name="Shipment status BK",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="Pendiente de booking",
    )
    status = ShipmentStatus(
        status_text="Booking confirmed",
        eta_time=_days_from_now(30),
        eta_local_text=_days_from_now(30).date().isoformat(),
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI",
                event_time=_days_from_now(5),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "BK confirmado"


def test_plan_shipment_update_keeps_one_processing_booking_pending() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-one-processing",
        task_name="Shipment status ONE processing",
        shipping_line="one",
        booking_no="TAOGD4882500",
        container_no=None,
        list_id="list-1",
        current_task_status="Pendiente de booking",
    )
    status = ShipmentStatus(
        status_text="ETA 2026-07-22T04:00:00+00:00",
        eta_time=_days_from_now(30),
        eta_local_text=_days_from_now(30).date().isoformat(),
        booking_status_text="Processing",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="QINGDAO, SHANDONG",
                event_time=_days_from_now(5),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update is None
    assert plan.comment_text is not None
    assert "Booking status: Processing" in plan.comment_text


def test_plan_shipment_update_keeps_one_unavailable_booking_pending() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-one-unavailable",
        task_name="Shipment status ONE unavailable",
        shipping_line="one",
        booking_no="NB6BFP529300",
        container_no=None,
        list_id="list-1",
        current_task_status="Pendiente de booking",
        current_field_values={
            "etd-field": str(int(_days_from_now(13).timestamp() * 1000)),
            "eta-field": str(int(_days_from_now(48).timestamp() * 1000)),
        },
    )
    status = ShipmentStatus(status_text="ETA unavailable")

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update is None


def test_plan_shipment_update_moves_one_explicit_confirmation_to_booking_confirmed() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-one-confirmed",
        task_name="Shipment status ONE confirmed",
        shipping_line="one",
        booking_no="NB6BFP529300",
        container_no=None,
        list_id="list-1",
        current_task_status="Pendiente de booking",
        current_field_values={
            "etd-field": str(int(_days_from_now(13).timestamp() * 1000)),
            "eta-field": str(int(_days_from_now(48).timestamp() * 1000)),
        },
    )
    status = ShipmentStatus(
        status_text="ETA 2026-08-13T07:00:00+00:00",
        booking_status_text="Booking Confirmed",
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "BK confirmado"


def test_plan_shipment_update_moves_booking_confirmed_to_collected() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-collected",
        task_name="Shipment status collected",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="BK confirmado",
    )
    status = ShipmentStatus(
        status_text="Empty out",
        recent_moves=[
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="SHANGHAI",
                event_time=_days_from_now(-2),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "Recolectado"


def test_plan_shipment_update_moves_rta_pending_to_bk_confirmed() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-rta-bk",
        task_name="RTA booking",
        shipping_line="msc",
        booking_no="177EBBPPXVNN2502",
        container_no="MSNU9927513",
        list_id="169175872",
        list_name="RTA Shipments",
        current_task_status="bk pending to confirm",
        current_field_values={
            "eta-field": _ms_days_from_now(30),
            "etd-field": _ms_days_from_now(5),
        },
    )
    status = ShipmentStatus(
        status_text="ETA 2026-07-23",
        eta_time=_days_from_now(30),
        eta_local_text=_days_from_now(30).date().isoformat(),
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "bk confirmed"


def test_plan_shipment_update_moves_rta_to_at_port_after_actual_discharge() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-rta-port",
        task_name="RTA port",
        shipping_line="msc",
        booking_no="BOOK-RTA",
        container_no="CONT-RTA",
        list_id="169175872",
        list_name="RTA Shipments",
        current_task_status="near arrival",
    )
    status = ShipmentStatus(
        status_text="Discharged",
        recent_moves=[
            MovementEvent(
                name="Container Discharged (DISC)",
                location="CHARLESTON, US",
                event_time=_days_from_now(-1),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "at port"


def test_plan_shipment_update_moves_rta_to_at_rail_after_actual_rail_handoff() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-rta-rail",
        task_name="RTA rail",
        shipping_line="msc",
        booking_no="BOOK-RTA",
        container_no="CONT-RTA",
        list_id="169175872",
        list_name="RTA Shipments",
        current_task_status="at port",
    )
    status = ShipmentStatus(
        status_text="Rail departure",
        recent_moves=[
            MovementEvent(
                name="Rail Departure",
                location="CHARLESTON INTERMODAL",
                event_time=_days_from_now(-1),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "at rail"


def test_plan_shipment_update_moves_rta_to_arrived_ramp_after_actual_rail_ramp_arrival() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-rta-ramp",
        task_name="RTA ramp",
        shipping_line="msc",
        booking_no="BOOK-RTA",
        container_no="CONT-RTA",
        list_id="169175872",
        list_name="RTA Shipments",
        current_task_status="at rail",
    )
    status = ShipmentStatus(
        status_text="Rail ramp arrival",
        recent_moves=[
            MovementEvent(
                name="Container Arrived at Rail Ramp",
                location="DALLAS RAMP",
                event_time=_days_from_now(0),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "container arrived at ramp"


def test_plan_shipment_update_does_not_move_rta_rail_status_from_estimated_rail_event() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-rta-rail-estimated",
        task_name="RTA rail estimated",
        shipping_line="msc",
        booking_no="BOOK-RTA",
        container_no="CONT-RTA",
        list_id="169175872",
        list_name="RTA Shipments",
        current_task_status="at port",
    )
    status = ShipmentStatus(
        status_text="Rail departure estimate",
        recent_moves=[
            MovementEvent(
                name="Rail Departure",
                location="CHARLESTON INTERMODAL",
                event_time=_days_from_now(2),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update is None


def test_plan_shipment_update_moves_origin_port_to_transit_when_etd_passes() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-transit",
        task_name="Shipment status transit",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="En puerto Origen",
    )
    status = ShipmentStatus(
        status_text="In transit",
        eta_time=_days_from_now(20),
        eta_local_text=_days_from_now(20).date().isoformat(),
        recent_moves=[
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="SHANGHAI",
                event_time=_days_from_now(-7),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="SHANGHAI",
                event_time=_days_from_now(-5),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI",
                event_time=_days_from_now(-1),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "Tránsito"


def test_plan_shipment_update_moves_origin_port_to_transit_for_origin_barge_leg() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-status-barge-transit",
        task_name="Shipment status barge transit",
        shipping_line="msc",
        booking_no="BOOK-BARGE",
        container_no="CONT-BARGE",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "gtot-empty-field": _ms_days_from_now(-10),
            "gtin-full-field": _ms_days_from_now(-9),
            "etd-field": _ms_days_from_now(6),
            "eta-field": _ms_days_from_now(42),
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        recent_moves=[
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO CORTES, HN",
                event_time=_days_from_now(42),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(-2),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="YANGZHOU, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.task_status_update == "Tránsito"
    assert updates["Vessel/Voyage"].value == "BARGE"


def test_plan_shipment_update_moves_to_transit_for_actual_barge_load_without_gate_events() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-status-barge-no-gates",
        task_name="Shipment status barge without gates",
        shipping_line="msc",
        booking_no="BOOK-BARGE-NO-GATES",
        container_no="CONT-BARGE-NO-GATES",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "etd-field": _ms_days_from_now(-10),
            "eta-field": _ms_days_from_now(42),
            "vessel-voyage-field": "MAIN VESSEL / V001",
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        recent_moves=[
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO CORTES, HN",
                event_time=_days_from_now(42),
                event_state="estimated",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(-2),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="HEFEI, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.task_status_update == "Tránsito"
    assert updates["Vessel/Voyage"].value == "BARGE"


def test_plan_shipment_update_moves_to_transit_for_actual_laden_barge_yard_event() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-hefei-laden-barge",
        task_name="MSC Hefei actual barge yard",
        shipping_line="msc",
        booking_no="177WJVJVJ400167T",
        container_no="MEDU4972003",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "eta-field": _ms_days_from_now(42),
            "vessel-voyage-field": "PENDING",
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
                event_state="estimated",
            ),
            MovementEvent(
                name="Export at barge yard (LADEN)",
                location="HEFEI, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="HEFEI, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert plan.task_status_update == "Tránsito"
    assert updates["Vessel/Voyage"].value == "BARGE"


def test_plan_shipment_update_does_not_infer_transit_from_load_and_planned_departure() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-hefei-unconfirmed-feeder",
        task_name="MSC Hefei unconfirmed feeder",
        shipping_line="msc",
        booking_no="177WJVJVJ400167T",
        container_no="MEDU4972003",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "eta-field": _ms_days_from_now(42),
            "vessel-voyage-field": "PENDING",
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="HEFEI, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert plan.task_status_update is None
    assert "Vessel/Voyage" not in updates


def test_plan_shipment_update_keeps_barge_until_a_later_actual_vessel_event() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-status-final-vessel",
        task_name="Shipment status final vessel",
        shipping_line="msc",
        booking_no="BOOK-FINAL",
        container_no="CONT-FINAL",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "gtot-empty-field": _ms_days_from_now(-10),
            "gtin-full-field": _ms_days_from_now(-9),
            "etd-field": _ms_days_from_now(6),
            "eta-field": _ms_days_from_now(42),
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        vessel_voyage="MSC FINAL FV123A",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="YANGZHOU, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
            MovementEvent(
                name="Export at barge yard (LADEN)",
                location="YANGZHOU, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.task_status_update == "Tránsito"
    assert updates["Vessel/Voyage"].value == "BARGE"


def test_plan_shipment_update_replaces_barge_with_latest_actual_mother_vessel() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-barge-mother-vessel",
        task_name="MSC feeder barge to mother vessel",
        shipping_line="msc",
        booking_no="BOOK-BARGE-MOTHER",
        container_no="CONT-BARGE-MOTHER",
        list_id="list-1",
        current_field_values={"vessel-voyage-field": "BARGE"},
    )
    status = ShipmentStatus(
        status_text="In transit",
        vessel_voyage="MSC PLANNED FINAL 900E",
        final_vessel_voyage="MSC PLANNED FINAL 900E",
        recent_moves=[
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="HEFEI, CN",
                event_time=_days_from_now(-8),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(-2),
                event_state="actual",
                vessel_voyage="MSC MOTHER 123E",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["Vessel/Voyage"].value == "MSC MOTHER 123E"


def test_msc_transshipment_discharge_updates_voyage_without_destination_arrival() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-field"))
    shipment = ShipmentRef(
        task_id="msc-discharge", task_name="MSC", shipping_line="msc",
        booking_no="BOOK", container_no="CONT", list_id="list-1",
        destination_port="Puerto Cortes", current_task_status="transito",
        current_field_values={"vessel-field": "BARGE"},
    )
    status = ShipmentStatus(
        status_text="In transit", destination_port="Puerto Cortes",
        require_destination_evidence=True,
        recent_moves=[
            MovementEvent(name="Container Loaded (LOAD)", event_time=_days_from_now(-8),
                          event_state="actual", location="Ningbo", vessel_voyage="MSC RANIA VIII GN625E"),
            MovementEvent(name="Container Discharged (DISC)", event_time=_days_from_now(-2),
                          event_state="actual", location="Colon", vessel_voyage="MSC RANIA VIII NH631R"),
        ],
    )
    plan = client.plan_shipment_update(shipment, status)
    fields = {f.field_id: f.value for f in plan.custom_field_updates}
    assert fields["vessel-field"] == "MSC RANIA VIII NH631R"
    assert "disc-field" not in fields
    assert plan.task_status_update is None
    shipment.current_field_values.update(fields)
    repeat = client.plan_shipment_update(shipment, status)
    assert not any(f.field_id in {"vessel-field", "disc-field"} for f in repeat.custom_field_updates)


def test_msc_latest_vessel_rejects_unconfirmed_future_and_equipment_labels() -> None:
    from shipment_sync.clickup_client import _latest_actual_event_vessel_voyage
    now = datetime.now(timezone.utc)
    good = MovementEvent(name="Container Loaded (LOAD)", event_time=now-timedelta(days=8),
                         event_state="actual", vessel_voyage="MSC VALID 1E")
    for state, when, vessel in [
        ("estimated", now-timedelta(days=1), "MSC BAD 2E"),
        (None, now-timedelta(days=1), "MSC BAD 2E"),
        ("actual", None, "MSC BAD 2E"),
        ("actual", now+timedelta(days=1), "MSC BAD 2E"),
        ("actual", now-timedelta(days=1), "EMPTY"),
        ("actual", now-timedelta(days=1), "LADEN"),
    ]:
        bad = MovementEvent(name="Container Discharged (DISC)", event_time=when,
                            event_state=state, vessel_voyage=vessel)
        status = ShipmentStatus(status_text="Transit", recent_moves=[bad, good])
        assert _latest_actual_event_vessel_voyage(status, now_utc=now, include_discharge=True) == "MSC VALID 1E"


def test_plan_shipment_update_does_not_label_normal_one_ocean_leg_as_barge() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-one-normal-ocean-leg",
        task_name="ONE normal ocean leg",
        shipping_line="one",
        booking_no="TAOGC8049300",
        container_no="ONEU2407096",
        list_id="list-1",
        current_task_status="Por arribar",
        current_field_values={
            "gtot-empty-field": _ms_days_from_now(-60),
            "gtin-full-field": _ms_days_from_now(-55),
            "eta-field": _ms_days_from_now(5),
            "vessel-voyage-field": "BARGE",
        },
    )
    status = ShipmentStatus(
        status_text="In transit",
        eta_time=_days_from_now(5),
        vessel_voyage="NYK SILVIA 0440E",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="QINGDAO, CN",
                event_time=_days_from_now(-55),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="QINGDAO, CN",
                event_time=_days_from_now(-54),
                event_state="actual",
                vessel_voyage="IQUIQUE EXPRESS 2623E",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="QINGDAO, CN",
                event_time=_days_from_now(-53),
                event_state="actual",
                vessel_voyage="IQUIQUE EXPRESS 2623E",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="MANZANILLO, MX",
                event_time=_days_from_now(-1),
                event_state="actual",
                vessel_voyage="NYK SILVIA 0440E",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="MANZANILLO, MX",
                event_time=_days_from_now(0),
                event_state="actual",
                vessel_voyage="NYK SILVIA 0440E",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.task_status_update is None
    assert updates["Vessel/Voyage"].value == "NYK SILVIA 0440E"


def test_plan_shipment_update_enriches_one_actual_voyage_with_matching_vessel_name() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-one-seaspan-bravo",
        task_name="ONE transshipment vessel",
        shipping_line="one",
        booking_no="NB5BI3647900",
        container_no="ONEU5053340",
        list_id="list-1",
        current_field_values={"vessel-voyage-field": "0103E"},
    )
    status = ShipmentStatus(
        status_text="ETA future",
        vessel_voyage="SEASPAN BRAVO 0103E",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PUSAN",
                event_time=_days_from_now(-2),
                event_state="actual",
                vessel_voyage="0103E",
            )
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Vessel/Voyage"].value == "SEASPAN BRAVO 0103E"


def test_plan_shipment_update_prefers_confirmed_final_vessel_before_departure() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-maersk-final-leg",
        task_name="Maersk final leg",
        shipping_line="maersk",
        booking_no="272124460",
        container_no=None,
        list_id="list-1",
        current_field_values={"vessel-voyage-field": "POLAR PERU 626N"},
    )
    status = ShipmentStatus(
        status_text="ETA future",
        vessel_voyage="MAERSK SEQUOIA 631N",
        final_vessel_voyage="MAERSK SEQUOIA 631N",
        recent_moves=[
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="BALBOA",
                event_time=_days_from_now(-2),
                event_state="actual",
                vessel_voyage="POLAR PERU 626N",
            )
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Vessel/Voyage"].value == "MAERSK SEQUOIA 631N"


def test_plan_shipment_update_keeps_one_canonical_final_voyage_over_schedule_event() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-one-sc-montana",
        task_name="ONE canonical final voyage",
        shipping_line="one",
        booking_no="SZPGH2579600",
        container_no="ONEU0000001",
        list_id="list-1",
        current_field_values={"vessel-voyage-field": "SC MONTANA 2630E"},
    )
    status = ShipmentStatus(
        status_text="ETA 2026-08-05",
        vessel_voyage="SC MONTANA 0M34IS1MA",
        final_vessel_voyage="SC MONTANA 0M34IS1MA",
        recent_moves=[
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO QUETZAL, GUATEMALA",
                event_time=_days_from_now(-1),
                event_state="actual",
                vessel_voyage="SC MONTANA 2630E",
            )
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Vessel/Voyage"].value == "SC MONTANA 0M34IS1MA"


def test_plan_shipment_update_does_not_move_origin_port_for_future_etd_without_barge_leg() -> None:
    client = ClickUpClient(
        _settings(
            clickup_use_task_status=True,
            cf_vessel_voyage="vessel-voyage-field",
        )
    )
    shipment = ShipmentRef(
        task_id="task-status-no-barge",
        task_name="Shipment status no barge",
        shipping_line="msc",
        booking_no="BOOK-NO-BARGE",
        container_no="CONT-NO-BARGE",
        list_id="list-1",
        current_task_status="En puerto Origen",
        current_field_values={
            "gtot-empty-field": _ms_days_from_now(-10),
            "gtin-full-field": _ms_days_from_now(-9),
            "etd-field": _ms_days_from_now(6),
            "eta-field": _ms_days_from_now(42),
        },
    )
    status = ShipmentStatus(
        status_text="ETA future",
        eta_time=_days_from_now(42),
        recent_moves=[
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO CORTES, HN",
                event_time=_days_from_now(42),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=_days_from_now(6),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.task_status_update is None
    assert "Vessel/Voyage" not in updates


def test_plan_shipment_update_moves_transit_to_arriving_inside_eta_window() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-arriving",
        task_name="Shipment status arriving",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="Tránsito",
    )
    status = ShipmentStatus(
        status_text="Arriving soon",
        eta_time=_days_from_now(7),
        eta_local_text=_days_from_now(7).date().isoformat(),
        recent_moves=[
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="SHANGHAI",
                event_time=_days_from_now(-14),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="SHANGHAI",
                event_time=_days_from_now(-12),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI",
                event_time=_days_from_now(-10),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "Por arribar"


def test_plan_shipment_update_does_not_move_arriving_to_arrived_when_eta_passes() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-arrived",
        task_name="Shipment status arrived",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="Por arribar",
        current_field_values={"eta-field": _ms_days_from_now(-1)},
    )
    status = ShipmentStatus(status_text="Arrived")

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update is None


def test_plan_shipment_update_moves_arrived_to_en_route_after_gate_out_delivery() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-route",
        task_name="Shipment status route",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="arribado en puerto",
        current_field_values={"gtot-delivery-field": _ms_days_from_now(0)},
    )
    status = ShipmentStatus(status_text="Gate out delivery")

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "en ruta a almacén"


def test_plan_shipment_update_moves_warehouse_to_empty_returned_after_gate_in_empty() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-empty",
        task_name="Shipment status empty",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="en almacén",
        current_field_values={"gtin-empty-field": _ms_days_from_now(0)},
    )
    status = ShipmentStatus(status_text="Empty returned")

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update == "Vacío devuelto"


def test_plan_shipment_update_does_not_change_status_after_empty_returned() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-status-terminal",
        task_name="Shipment status terminal",
        shipping_line="one",
        booking_no="BOOK-STATUS",
        container_no=None,
        list_id="list-1",
        current_task_status="VACIO DEVUELTO",
        current_field_values={
            "eta-field": _ms_days_from_now(7),
            "gtot-empty-field": _ms_days_from_now(-14),
            "gtin-full-field": _ms_days_from_now(-12),
            "etd-field": _ms_days_from_now(-10),
        },
    )
    status = ShipmentStatus(status_text="Late carrier update")

    plan = client.plan_shipment_update(shipment, status)

    assert plan.task_status_update is None


def test_plan_shipment_update_skips_destination_events_until_discharge_exists() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-2",
        task_name="Shipment 2",
        shipping_line="maersk",
        booking_no="BOOK-2",
        container_no="CONT-2",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="Origin leg",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 6, 10, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 4, 8, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert "Gate-in full" in updates
    assert "Gate out empty" in updates
    assert "ETD" in updates
    assert "Discharge date" not in updates
    assert "Gate out delivery" not in updates
    assert "Gate in empty" not in updates


def test_plan_shipment_update_skips_estimated_destination_dates() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-2b",
        task_name="Shipment 2B",
        shipping_line="maersk",
        booking_no="BOOK-2B",
        container_no="CONT-2B",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="Destination ETA pending",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 24, 18, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 10, 18, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert "ETD" in updates
    assert "Discharge date" not in updates
    assert "Gate out delivery" not in updates
    assert "Gate in empty" not in updates


def test_plan_shipment_update_clears_estimated_destination_dates_already_in_clickup() -> None:
    client = ClickUpClient(_settings())

    def ms(year: int, month: int, day: int) -> str:
        return str(int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp() * 1000))

    shipment = ShipmentRef(
        task_id="task-2c",
        task_name="Shipment 2C",
        shipping_line="maersk",
        booking_no="BOOK-2C",
        container_no="CONT-2C",
        list_id="list-1",
        current_field_values={
            "disc-field": ms(2026, 3, 23),
            "gtot-delivery-field": ms(2026, 3, 24),
            "gtin-empty-field": ms(2026, 3, 24),
        },
    )
    status = ShipmentStatus(
        status_text="Destination ETA pending",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 24, 18, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 3, 23, 9, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert plan.changed is True
    assert updates["Discharge date"].value is None
    assert updates["Gate out delivery"].value is None
    assert updates["Gate in empty"].value is None


def test_plan_shipment_update_only_refreshes_last_checked_when_all_values_match() -> None:
    client = ClickUpClient(_settings())
    def ms(year: int, month: int, day: int) -> str:
        return str(int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp() * 1000))

    shipment = ShipmentRef(
        task_id="task-3",
        task_name="Shipment 3",
        shipping_line="one",
        booking_no="BOOK-3",
        container_no="CONT-3",
        list_id="list-1",
        current_field_values={
            "eta-field": ms(2026, 5, 5),
            "etd-field": ms(2026, 2, 25),
            "disc-field": ms(2026, 5, 5),
            "gtin-full-field": ms(2026, 2, 13),
            "gtot-empty-field": ms(2026, 2, 13),
            "gtot-delivery-field": ms(2026, 5, 5),
            "gtin-empty-field": ms(2026, 5, 6),
        },
    )
    status = ShipmentStatus(
        status_text="ETA 2026-05-05",
        eta_time=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
        eta_local_text="2026-05-05",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 5, 5, 0, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 25, 0, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 13, 0, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Empty Container Release to Shipper",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 13, 0, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.changed is False
    assert [update.label for update in plan.custom_field_updates] == ["Last T&T Update"]
    assert plan.comment_text is None


def test_plan_shipment_update_can_optionally_post_no_change_comment() -> None:
    client = ClickUpClient(_settings(shipment_comment_on_no_change=True))

    def ms(year: int, month: int, day: int) -> str:
        return str(int(datetime(year, month, day, 12, 0, tzinfo=timezone.utc).timestamp() * 1000))

    shipment = ShipmentRef(
        task_id="task-3b",
        task_name="Shipment 3B",
        shipping_line="one",
        booking_no="BOOK-3B",
        container_no="CONT-3B",
        list_id="list-1",
        current_field_values={
            "eta-field": ms(2026, 5, 5),
            "etd-field": ms(2026, 2, 25),
        },
    )
    status = ShipmentStatus(
        status_text="ETA 2026-05-05",
        eta_time=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
        eta_local_text="2026-05-05",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 2, 25, 0, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    assert plan.changed is False
    assert plan.comment_text is not None
    assert "No change found" in plan.comment_text
    assert "T&T executed on " in plan.comment_text


def test_plan_shipment_update_prefers_first_load_port_departure_for_etd() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-4",
        task_name="Shipment 4",
        shipping_line="one",
        booking_no="BOOK-4",
        container_no="CONT-4",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 6, 20, 1, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 8, 8, 30, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 11, 8, 28, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 11, 11, 29, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUSAN",
                event_time=datetime(2026, 3, 17, 12, 54, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="PUSAN",
                event_time=datetime(2026, 3, 17, 13, 54, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PUSAN",
                event_time=datetime(2026, 3, 17, 18, 54, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 30, 4, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-03-11"


def test_plan_shipment_update_falls_back_to_first_departure_when_origin_ready_event_is_missing() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-5",
        task_name="Shipment 5",
        shipping_line="one",
        booking_no="BOOK-5",
        container_no="CONT-5",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="JIANGMEN, GUANGDONG",
                event_time=datetime(2026, 3, 28, 23, 20, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="JIANGMEN, GUANGDONG",
                event_time=datetime(2026, 3, 30, 23, 20, tzinfo=timezone.utc),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-03-28"


def test_plan_shipment_update_uses_first_departure_after_gate_in_for_etd() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-5b",
        task_name="Shipment 5B",
        shipping_line="maersk",
        booking_no="BOOK-5B",
        container_no="CONT-5B",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="CLSAI",
                event_time=datetime(2026, 6, 17, 17, 30, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="STI",
                event_time=datetime(2026, 6, 27, 13, 32, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="STI",
                event_time=datetime(2026, 7, 3, 23, 0, tzinfo=timezone.utc),
                event_time_local_text="2026-07-03T23:00:00-04:00",
                event_state="estimated",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PPCBL",
                event_time=datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PPCBL",
                event_time=datetime(2026, 7, 24, 23, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-07-03"


def test_plan_shipment_update_uses_first_origin_departure_cluster_for_etd() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-6",
        task_name="Shipment 6",
        shipping_line="one",
        booking_no="BOOK-6",
        container_no="CONT-6",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 5, 17, 17, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 5, 16, 7, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 5, 14, 4, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="NINGBO, ZHEJIANG",
                event_time=datetime(2026, 4, 12, 20, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="SHEKOU, GUANGDONG",
                event_time=datetime(2026, 4, 3, 23, 40, 30, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHEKOU, GUANGDONG",
                event_time=datetime(2026, 4, 4, 2, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="JIANGMEN, GUANGDONG",
                event_time=datetime(2026, 3, 27, 23, 31, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="JIANGMEN, GUANGDONG",
                event_time=datetime(2026, 3, 27, 11, 58, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-03-27"


def test_plan_shipment_update_uses_origin_barge_load_for_etd() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-barge-etd",
        task_name="MSC origin barge",
        shipping_line="msc",
        booking_no="177WJUJUJ308516W",
        container_no="GAOU7846820",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="YANGZHOU, CN",
                event_time=datetime(2026, 6, 21, 8, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="YANGZHOU, CN",
                event_time=datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="SHANGHAI, CN",
                event_time=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc),
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-06-22"


def test_plan_shipment_update_uses_barge_load_when_msc_omits_main_port_discharge() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-hefei-barge-etd",
        task_name="MSC Hefei origin barge",
        shipping_line="msc",
        booking_no="177WJVJVJ400170T",
        container_no="BMOU6181736",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="In transit",
        recent_moves=[
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="HEFEI, CN",
                event_time=datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, CN",
                event_time=datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-07-07"


def test_plan_shipment_update_skips_transshipment_discharge_until_final_destination() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-7",
        task_name="Shipment 7",
        shipping_line="one",
        booking_no="BOOK-7",
        container_no="CONT-7",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="Transshipment in progress",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUSAN",
                event_time=datetime(2026, 3, 10, 6, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUSAN",
                event_time=datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="PUSAN",
                event_time=datetime(2026, 3, 11, 7, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PUSAN",
                event_time=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["ETD"].value.date().isoformat() == "2026-03-02"
    assert "Discharge date" not in updates
    assert "Gate out delivery" not in updates
    assert "Gate in empty" not in updates


def test_plan_shipment_update_uses_final_discharge_after_transshipment() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-8",
        task_name="Shipment 8",
        shipping_line="one",
        booking_no="BOOK-8",
        container_no="CONT-8",
        list_id="list-1",
    )
    status = ShipmentStatus(
        status_text="Destination leg complete",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="SHANGHAI, SHANGHAI",
                event_time=datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUSAN",
                event_time=datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="PUSAN",
                event_time=datetime(2026, 3, 11, 7, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PUSAN",
                event_time=datetime(2026, 3, 11, 12, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 1, 14, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 2, 13, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 3, 15, 0, tzinfo=timezone.utc),
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["Discharge date"].value.date().isoformat() == "2026-04-01"
    assert updates["Gate out delivery"].value.date().isoformat() == "2026-04-02"
    assert updates["Gate in empty"].value.date().isoformat() == "2026-04-03"


def test_plan_shipment_update_ignores_untimed_discharge_placeholder_after_destination_delivery() -> None:
    client = ClickUpClient(_settings(clickup_use_task_status=True))
    shipment = ShipmentRef(
        task_id="task-one-untimed-placeholder",
        task_name="ONE destination delivery with itinerary placeholder",
        shipping_line="one",
        booking_no="NB5BI3197900",
        container_no="TCLU8540106",
        list_id="list-1",
        current_task_status="arribado en puerto",
    )
    status = ShipmentStatus(
        status_text="ETA 2026-08-11T03:43:00+00:00",
        recent_moves=[
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 8, 11, 6, 3, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 8, 18, 16, 34, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 8, 18, 22, 34, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="MANZANILLO",
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="MANZANILLO",
                event_state="estimated",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)

    updates = {update.label: update for update in plan.custom_field_updates}
    assert updates["Discharge date"].value.date().isoformat() == "2026-08-11"
    assert updates["Gate out delivery"].value.date().isoformat() == "2026-08-18"
    assert updates["Gate in empty"].value.date().isoformat() == "2026-08-18"
    assert plan.task_status_update == "Vacío devuelto"


def test_format_port_local_time_preserves_carrier_clock_time() -> None:
    assert format_port_local_time("2026-03-27T19:16:00.000Z", None) == "2026-03-27 19:16"
    assert format_port_local_time("27/03/2026 19:16", None) == "2026-03-27 19:16"
    assert format_port_local_time("2026-05-02", None) == "2026-05-02"


def test_unloaded_event_maps_to_discharge_not_load() -> None:
    assert (
        to_dcsa_movement_name(fallback_name="Unloaded from Vessel at Port of Discharging")
        == "Container Discharged (DISC)"
    )


def test_vessel_arrival_at_port_of_discharge_maps_to_arrival() -> None:
    assert (
        to_dcsa_movement_name(fallback_name="Vessel Arrival at Port of Discharge")
        == "Transport Arrived (ARRI)"
    )


def test_one_booking_only_voyage_list_produces_first_leg_departure_move() -> None:
    move = _extract_departure_move_from_voyage_list_data(
        [
            {
                "pol": {
                    "locationName": "HONG KONG, HONG KONG, CHINA",
                    "date": "2026-04-19T19:30:00.000Z",
                },
                "pod": {
                    "locationName": "MANZANILLO, MEXICO",
                    "berthingDate": "2026-05-17T16:00:00.000Z",
                },
            },
            {
                "pol": {
                    "locationName": "MANZANILLO, MEXICO",
                    "date": "2026-05-20T00:00:00.000Z",
                },
                "pod": {
                    "locationName": "PUERTO QUETZAL, GUATEMALA",
                    "berthingDate": "2026-05-23T07:00:00.000Z",
                },
            },
        ]
    )

    assert move is not None
    assert move.name == "Transport Departed (DEPA)"
    assert move.location == "HONG KONG, HONG KONG, CHINA"
    assert move.event_time is not None
    assert move.event_time.date().isoformat() == "2026-04-19"
    assert move.event_state == "estimated"
