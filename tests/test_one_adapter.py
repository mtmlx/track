from __future__ import annotations

from datetime import datetime, timezone
import json

from shipment_sync.clickup_client import ClickUpClient
from shipment_sync.config import Settings
from shipment_sync.carriers.one import (
    OneAdapter,
    _extract_eta_from_cargo_events,
    _extract_eta_from_search_item,
    _extract_booking_status_text,
    _extract_final_discharge_vessel_voyage,
    _enrich_moves_with_voyage_legs,
    _latest_move_from_search_item,
    _pick_latest_move,
    _pick_search_reference,
)
from shipment_sync.models import MovementEvent, ShipmentRef, ShipmentStatus


class _StubResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.content = json.dumps(payload).encode()
        self._content_consumed = True
        self.headers = {}

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _PayloadResponse:
    def __init__(self, payload: bytes, url: str) -> None:
        self.content = payload
        self.headers = {"content-type": "application/json"}
        self.url = url
        self._content_consumed = True


class _StubSession:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get(self, url: str, params: dict[str, str], timeout: int, **kwargs) -> _StubResponse:
        return _StubResponse(self.payload)


class _RouteStubSession:
    def __init__(self, *, search_payload: dict, voyage_payload: dict) -> None:
        self.search_payload = search_payload
        self.voyage_payload = voyage_payload
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url: str, json: dict, timeout: int, **kwargs) -> _StubResponse:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return _StubResponse(self.search_payload)

    def get(self, url: str, params: dict[str, str], timeout: int, **kwargs) -> _StubResponse:
        self.gets.append({"url": url, "params": params, "timeout": timeout})
        return _StubResponse(self.voyage_payload)


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


def test_fetch_recent_moves_uses_trigger_type_for_actual_events() -> None:
    adapter = OneAdapter()
    adapter.session = _StubSession(
        {
            "data": [
                {
                    "eventName": "Unloaded from Vessel at Port of Discharging",
                    "eventLocalPortDate": "2026-04-09T17:26:00+00:00",
                    "triggerType": "ACTUAL",
                    "location": {"locationName": "PUERTO QUETZAL"},
                }
            ]
        }
    )

    moves = adapter._fetch_recent_moves("BOOK-1", "CONT-1")

    assert len(moves) == 1
    assert moves[0].name == "Container Discharged (DISC)"
    assert moves[0].location == "PUERTO QUETZAL"
    assert moves[0].event_time == datetime(2026, 4, 9, 17, 26, tzinfo=timezone.utc)
    assert moves[0].event_state == "actual"


def test_fetch_recent_moves_prefers_one_outbound_vessel_voyage() -> None:
    adapter = OneAdapter()
    adapter.session = _StubSession(
        {
            "data": [
                {
                    "eventName": "Vessel Departure from Port of Loading",
                    "eventLocalPortDate": "2026-06-20T05:07:00.000Z",
                    "triggerType": "ACTUAL",
                    "location": {"locationName": "NINGBO, ZHEJIANG"},
                    "edhVessel": {
                        "name": "ONE SPARKLE",
                        "voyNo": "2624",
                        "dirCode": "E",
                        "inboundConsortiumVoyage": "2611W",
                        "outboundConsortiumVoyage": "2624E",
                    },
                }
            ]
        }
    )

    moves = adapter._fetch_recent_moves("NB5BI4208300", "TGCU5293003")

    assert moves[0].vessel_voyage == "ONE SPARKLE 2624E"


def test_extract_eta_from_cargo_events_uses_trigger_type_for_estimated_events() -> None:
    eta_time, eta_raw = _extract_eta_from_cargo_events(
        [
            {
                "triggerType": "ESTIMATED",
                "locationName": "PUERTO QUETZAL",
                "localPortDate": "2026-05-20T04:00:00+00:00",
            }
        ],
        matrix_ids=set(),
        pod_name="PUERTO QUETZAL",
    )

    assert eta_time == datetime(2026, 5, 20, 4, 0, tzinfo=timezone.utc)
    assert eta_raw == "2026-05-20T04:00:00+00:00"


def test_extract_eta_from_search_item_prefers_actual_pod_arrival_over_stale_estimated_delivery_event() -> None:
    eta_time, eta_raw = _extract_eta_from_search_item(
        {
            "pod": {"locationName": "PUERTO QUETZAL"},
            "cargoEvents": [
                {
                    "matrixId": "E105",
                    "locationName": "PUERTO QUETZAL",
                    "localPortDate": "2026-07-06T10:33:00.000Z",
                    "trigger": "ESTIMATED",
                },
                {
                    "matrixId": "E089",
                    "locationName": "PUERTO QUETZAL",
                    "localPortDate": "2026-08-05T21:18:00.000Z",
                    "trigger": "ACTUAL",
                },
            ],
        }
    )

    assert eta_time == datetime(2026, 8, 5, 21, 18, tzinfo=timezone.utc)
    assert eta_raw == "2026-08-05T21:18:00.000Z"


def test_latest_move_from_search_item_uses_trigger_type() -> None:
    move = _latest_move_from_search_item(
        {
            "latestEvent": {
                "eventName": "Vessel Arrival at Port of Discharge",
                "locationName": "PUERTO QUETZAL",
                "date": "2026-04-09T11:20:00+00:00",
                "triggerType": "ACTUAL",
            }
        }
    )

    assert move is not None
    assert move.name == "Transport Arrived (ARRI)"
    assert move.location == "PUERTO QUETZAL"
    assert move.event_time == datetime(2026, 4, 9, 11, 20, tzinfo=timezone.utc)
    assert move.event_state == "actual"


def test_extract_booking_status_text_from_one_processing_search_item() -> None:
    status_text = _extract_booking_status_text(
        {
            "bookingNo": "TAOGD4882500",
            "latestEvent": {
                "eventName": "Processing",
                "locationName": "",
                "date": "",
            },
        }
    )

    assert status_text == "Processing"


def test_pick_latest_move_prefers_newest_actual_over_future_estimates() -> None:
    moves = [
        MovementEvent(
            name="Container Gated In (GTIN)",
            location="PUERTO QUETZAL",
            event_time=datetime(2026, 6, 4, 19, 0, tzinfo=timezone.utc),
            event_state="estimated",
        ),
        MovementEvent(
            name="Container Gated Out (GTOT)",
            location="PUERTO QUETZAL",
            event_time=datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc),
            event_state="estimated",
        ),
        MovementEvent(
            name="Transport Departed (DEPA)",
            location="LAZARO CARDENAS",
            event_time=datetime(2026, 5, 31, 23, 25, tzinfo=timezone.utc),
            event_state="actual",
        ),
    ]

    move = _pick_latest_move(moves)

    assert move is not None
    assert move.name == "Transport Departed (DEPA)"
    assert move.location == "LAZARO CARDENAS"


def test_extract_final_discharge_vessel_voyage_uses_final_pod_leg() -> None:
    vessel_voyage = _extract_final_discharge_vessel_voyage(
        [
            {
                "vesselEngName": "ONE SPLENDOUR",
                "scheduleVoyageNumber": "2616",
                "scheduleDirectionCode": "E",
                "outboundConsortiumVoyage": "2616E",
                "pod": {"locationCode": "MXLZC", "locationName": "LAZARO CARDENAS, MEXICO"},
            },
            {
                "vesselEngName": "SAN ALFONSO",
                "scheduleVoyageNumber": "2621",
                "scheduleDirectionCode": "E",
                "outboundConsortiumVoyage": "26021E",
                "pod": {"locationCode": "GTPRQ", "locationName": "PUERTO QUETZAL, GUATEMALA"},
            },
        ],
        {"pod": {"code": "GTPRQ", "locationName": "PUERTO QUETZAL"}},
    )

    assert vessel_voyage == "SAN ALFONSO 26021E"


def test_enrich_moves_with_voyage_legs_restores_actual_vessel_name() -> None:
    moves = [
        MovementEvent(
            name="Transport Arrived (ARRI)",
            location="MANZANILLO",
            event_time=datetime(2026, 7, 14, 15, 28, tzinfo=timezone.utc),
            event_state="actual",
            vessel_voyage="2623E",
        )
    ]
    voyage_legs = [
        {
            "vesselEngName": "IQUIQUE EXPRESS",
            "scheduleVoyageNumber": "2623",
            "scheduleDirectionCode": "E",
            "inboundConsortiumVoyage": "2623E",
            "outboundConsortiumVoyage": "2623E",
        },
        {
            "vesselEngName": "ALIAGA EXPRESS",
            "scheduleVoyageNumber": "0371",
            "scheduleDirectionCode": "E",
            "outboundConsortiumVoyage": "0371E",
        },
    ]

    enriched = _enrich_moves_with_voyage_legs(moves, voyage_legs)

    assert enriched[0].vessel_voyage == "IQUIQUE EXPRESS 2623E"


def test_fetch_status_treats_empty_booking_search_with_voyage_as_processing() -> None:
    adapter = OneAdapter()
    adapter.session = _RouteStubSession(
        search_payload={"status": 200, "code": 1, "message": "Success"},
        voyage_payload={
            "status": 200,
            "code": 1,
            "message": "Success",
            "data": [
                {
                    "vesselEngName": "ONE SERENITY",
                    "scheduleVoyageNumber": "2625",
                    "scheduleDirectionCode": "E",
                    "pol": {
                        "locationName": "NINGBO, ZHEJIANG, CHINA",
                        "isActual": False,
                        "date": "2026-06-23T21:00:00.000Z",
                        "locationCode": "CNNGB",
                    },
                    "pod": {
                        "locationName": "PUERTO QUETZAL, GUATEMALA",
                        "isArrivalActual": False,
                        "arrivalDate": "2026-07-29T03:00:00.000Z",
                        "isBerthingActual": False,
                        "berthingDate": "2026-07-29T04:00:00.000Z",
                        "locationCode": "GTPRQ",
                    },
                    "outboundConsortiumVoyage": "2625E",
                }
            ],
        },
    )

    status = adapter._fetch_status_from_edh("NB6BFM822300", "B")

    assert status is not None
    assert status.booking_status_text == "Data Processing"
    assert status.eta_local_text == "2026-07-29T04:00:00.000Z"
    assert status.vessel_voyage == "ONE SERENITY 2625E"
    assert status.final_vessel_voyage == "ONE SERENITY 2625E"
    assert status.recent_moves[0].name == "Transport Departed (DEPA)"


def test_fetch_status_sets_canonical_final_vessel_separately_from_actual_event(monkeypatch) -> None:
    adapter = OneAdapter()
    adapter.eta_only_mode = True
    adapter.session = _RouteStubSession(
        search_payload={
            "data": [
                {
                    "bookingNo": "SZPGH2579600",
                    "containerNo": "ONEU0000001",
                    "pod": {"locationCode": "GTPRQ", "locationName": "PUERTO QUETZAL, GUATEMALA"},
                }
            ]
        },
        voyage_payload={"data": []},
    )
    monkeypatch.setattr(
        adapter,
        "_fetch_voyage_list",
        lambda booking_no: [
            {
                "vesselEngName": "SC MONTANA",
                "scheduleVoyageNumber": "2630",
                "scheduleDirectionCode": "E",
                "outboundConsortiumVoyage": "0M34IS1MA",
                "pod": {"locationCode": "GTPRQ", "locationName": "PUERTO QUETZAL, GUATEMALA"},
            }
        ],
    )
    monkeypatch.setattr(
        adapter,
        "_fetch_recent_moves",
        lambda booking_no, container_no: [
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO QUETZAL, GUATEMALA",
                event_time=datetime(2026, 8, 5, 21, 18, tzinfo=timezone.utc),
                event_state="actual",
                vessel_voyage="SC MONTANA 2630E",
            )
        ],
    )

    status = adapter._fetch_status_from_edh("SZPGH2579600", "B")

    assert status is not None
    assert status.vessel_voyage == "SC MONTANA 0M34IS1MA"
    assert status.final_vessel_voyage == "SC MONTANA 0M34IS1MA"
    assert status.recent_moves[0].vessel_voyage == "SC MONTANA 2630E"


def test_fetch_status_uses_configured_fallback_when_edh_has_no_result(monkeypatch) -> None:
    adapter = OneAdapter()
    adapter.eta_only_mode = False
    adapter.url_template = "https://tracking.example.test/cargo"
    monkeypatch.setattr(adapter, "_fetch_status_from_edh", lambda *args, **kwargs: None)

    def fake_get_with_retries(session, url, **kwargs):
        assert url == "https://tracking.example.test/cargo"
        assert kwargs["params"] == {"trakNoParam": "NB5BI3647900", "trakNoTpCdParam": "B"}
        return _PayloadResponse(b'{"shipmentStatus":"In transit"}', "https://tracking.example.test/cargo?booking=NB5BI3647900")

    monkeypatch.setattr("shipment_sync.carriers.one.get_with_retries", fake_get_with_retries)
    shipment = ShipmentRef(
        task_id="task-one-fallback",
        task_name="ONE fallback",
        shipping_line="one",
        booking_no="NB5BI3647900",
        container_no=None,
        list_id="list-1",
    )

    status = adapter.fetch_status(shipment)

    assert status.status_text == "In transit"
    assert status.raw_source == "one-web:https://tracking.example.test/cargo?booking=NB5BI3647900"


def test_fetch_status_uses_booking_search_to_discover_sibling_containers() -> None:
    session = _RouteStubSession(
        search_payload={
            "status": 200,
            "code": 1,
            "message": "Success",
            "data": [
                {
                    "bookingNo": "NB6BF9831800",
                    "containerNo": "ONEU4291087",
                    "latestEvent": {
                        "eventName": "Vessel Departure from Port of Loading",
                        "locationName": "NINGBO, ZHEJIANG",
                        "date": "2026-06-20T05:07:00.000Z",
                        "triggerType": "ACTUAL",
                    },
                },
                {
                    "bookingNo": "NB6BF9831800",
                    "containerNo": "ONEU4315002",
                    "latestEvent": {
                        "eventName": "Vessel Departure from Port of Loading",
                        "locationName": "NINGBO, ZHEJIANG",
                        "date": "2026-06-20T05:07:00.000Z",
                        "triggerType": "ACTUAL",
                    },
                },
            ],
        },
        voyage_payload={"status": 200, "code": 1, "message": "Success", "data": []},
    )
    adapter = OneAdapter()
    adapter.session = session
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="one",
        booking_no="NB6BF9831800",
        container_no="ONEU4291087",
        list_id="list-1",
    )

    status = adapter.fetch_status(shipment)

    assert session.posts[0]["json"]["filters"] == {
        "search_text": "NB6BF9831800",
        "search_type": "BKG_NO",
    }
    assert status.discovered_containers == ["ONEU4291087", "ONEU4315002"]
    assert status.latest_move is not None
    assert status.latest_move.name == "Transport Departed (DEPA)"


def test_fetch_status_uses_booking_search_when_declared_count_is_already_met() -> None:
    session = _RouteStubSession(
        search_payload={
            "status": 200,
            "code": 1,
            "message": "Success",
            "data": [{"bookingNo": "SHAGT3664400", "containerNo": "ONEU0005230"}],
        },
        voyage_payload={"status": 200, "code": 1, "message": "Success", "data": []},
    )
    adapter = OneAdapter()
    adapter.session = session
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="one",
        booking_no="SHAGT3664400",
        container_no="ONEU0005230, FFAU3663993",
        list_id="list-1",
        expected_container_count=1,
    )

    status = adapter.fetch_status(shipment)

    assert session.posts[0]["json"]["filters"] == {
        "search_text": "SHAGT3664400",
        "search_type": "BKG_NO",
    }
    assert status.discovered_containers == ["ONEU0005230"]
    assert status.container_discovery_authoritative is False


def test_pick_search_reference_uses_booking_over_stale_container_when_count_is_met() -> None:
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="one",
        booking_no="XMNG98354400",
        container_no="DRYU4262275",
        list_id="list-1",
        expected_container_count=1,
    )

    reference, reference_type, preferred_container = _pick_search_reference(shipment, "B", "C")

    assert (reference, reference_type, preferred_container) == ("XMNG98354400", "B", "DRYU4262275")


def test_plan_shipment_update_writes_vessel_voyage_field() -> None:
    client = ClickUpClient(_settings(cf_vessel_voyage="vessel-voyage-field"))
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="one",
        booking_no="BOOK-1",
        container_no="CONT-1",
        list_id="list-1",
        current_field_values={"vessel-voyage-field": "ONE SPLENDOUR 2616E"},
    )
    status = ShipmentStatus(
        status_text="ETA 2026-06-04",
        eta_time=datetime(2026, 6, 4, 7, 0, tzinfo=timezone.utc),
        vessel_voyage="SAN ALFONSO 26021E",
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Vessel/Voyage"].field_id == "vessel-voyage-field"
    assert updates["Vessel/Voyage"].value == "SAN ALFONSO 26021E"
    assert plan.comment_text is not None
    assert "Vessel/Voyage: SAN ALFONSO 26021E" in plan.comment_text


def test_plan_shipment_update_writes_final_destination_discharge_on_multi_leg_route() -> None:
    client = ClickUpClient(_settings())
    shipment = ShipmentRef(
        task_id="task-1",
        task_name="Shipment 1",
        shipping_line="one",
        booking_no="BOOK-1",
        container_no="CONT-1",
        list_id="list-1",
        current_field_values={"disc-field": None},
    )
    status = ShipmentStatus(
        status_text="ETA 2026-04-09",
        recent_moves=[
            MovementEvent(
                name="Container Gated In (GTIN)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 4, 10, 3, 56, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Gated Out (GTOT)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 4, 9, 21, 56, tzinfo=timezone.utc),
                event_state="estimated",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 4, 9, 17, 26, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="PUERTO QUETZAL",
                event_time=datetime(2026, 4, 9, 11, 20, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Loaded (LOAD)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 4, 5, 21, 31, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Container Discharged (DISC)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 3, 30, 2, 8, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Arrived (ARRI)",
                location="LAZARO CARDENAS",
                event_time=datetime(2026, 3, 29, 18, 25, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="PUSAN",
                event_time=datetime(2026, 3, 9, 17, 24, tzinfo=timezone.utc),
                event_state="actual",
            ),
            MovementEvent(
                name="Feeder Loading at O/B Inland Port",
                location="ZHAPU, ZHEJIANG",
                event_time=None,
                event_state="actual",
            ),
            MovementEvent(
                name="Transport Departed (DEPA)",
                location="LAZARO CARDENAS",
                event_time=None,
                event_state="actual",
            ),
        ],
    )

    plan = client.plan_shipment_update(shipment, status)
    updates = {update.label: update for update in plan.custom_field_updates}

    assert updates["Discharge date"].field_id == "disc-field"
    assert updates["Discharge date"].value.date().isoformat() == "2026-04-09"
