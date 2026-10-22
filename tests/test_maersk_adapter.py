from __future__ import annotations

from datetime import datetime, timezone

import requests

from shipment_sync.carriers import maersk
from shipment_sync.carriers.maersk import MaerskAdapter, _status_from_events, _status_from_public_tracking_text
from shipment_sync.models import MovementEvent, ShipmentRef


def _shipment() -> ShipmentRef:
    return ShipmentRef(
        task_id="task-1",
        task_name="Maersk shipment",
        shipping_line="maersk",
        booking_no=None,
        container_no="CAAU8312730",
        list_id="list-1",
        destination_port="MANZANILLO MX",
    )


def _http_error(status_code: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    response.url = "https://api.maersk.com/track-and-trace-private/events"
    return requests.HTTPError(f"{status_code} error", response=response)


def test_maersk_events_404_uses_public_browser_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.maersk.com/track-and-trace-private/events")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "token")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("MAERSK_WEB_FALLBACK_ON_API_ERROR", "true")

    def fake_get_with_retries(*args, **kwargs):
        assert kwargs["non_retry_statuses"] == {401, 403, 404}
        raise _http_error(404)

    monkeypatch.setattr(maersk, "get_with_retries", fake_get_with_retries)
    adapter = MaerskAdapter()
    monkeypatch.setattr(
        adapter,
        "_try_public_browser_fallback",
        lambda reference: maersk.ShipmentStatus(
            status_text="ETA 2026-08-16",
            eta_time=datetime(2026, 8, 16, 23, tzinfo=timezone.utc),
            eta_local_text="2026-08-16 23:00",
            recent_moves=[
                MovementEvent(
                    name="Transport Departed (DEPA)",
                    location="SAN ANTONIO",
                    event_time=datetime(2026, 7, 8, 23, 29, tzinfo=timezone.utc),
                    event_state="actual",
                ),
                MovementEvent(
                    name="Transport Arrived (ARRI)",
                    location="MANZANILLO MX",
                    event_time=datetime(2026, 8, 16, 23, tzinfo=timezone.utc),
                    event_time_local_text="2026-08-16 23:00",
                    event_state="estimated",
                    vessel_voyage="MAERSK EVORA 632W",
                    transport_mode="VESSEL",
                ),
            ],
            vessel_voyage="MAERSK EVORA 632W",
            raw_source=f"maersk-public-browser:https://www.maersk.com/tracking/{reference}",
        ),
    )

    status = adapter.fetch_status(_shipment())

    assert status.status_text == "ETA 2026-08-16T23:00:00+00:00"
    assert status.raw_source == "maersk-public-browser:https://www.maersk.com/tracking/CAAU8312730"
    assert status.vessel_voyage == "MAERSK EVORA 632W"


def test_maersk_public_tracking_parser_extracts_events_and_final_vessel() -> None:
    status = _status_from_public_tracking_text(
        """
        Estimated arrival date
        16 Aug 2026 23:00
        Latest event
        Vessel departure • SAN ANTONIO, CHILE • 08 Jul 2026
        SAN ANTONIO
        Gate out Empty
        02 Jul 2026 17:37
        Gate in
        06 Jul 2026 19:15
        Load on POLAR COLOMBIA / 627N
        08 Jul 2026 16:38
        Vessel departure (POLAR COLOMBIA / 627N)
        08 Jul 2026 23:29
        BALBOA
        Vessel arrival (POLAR COLOMBIA / 627N)
        22 Jul 2026 01:00
        Vessel departure (MAERSK EVORA / 632W)
        09 Aug 2026 21:00
        MANZANILLO
        Vessel arrival (MAERSK EVORA / 632W)
        16 Aug 2026 23:00
        TCKU6860166
        """,
        source_url="https://www.maersk.com/tracking/272684825",
    )

    assert status is not None
    assert status.eta_local_text == "2026-08-16 23:00"
    assert status.vessel_voyage == "MAERSK EVORA 632W"
    assert status.latest_move is not None
    assert status.latest_move.name == "Transport Departed (DEPA)"
    assert status.latest_move.location == "SAN ANTONIO, CHILE"
    assert status.latest_move.event_time is not None
    assert status.latest_move.event_time.date().isoformat() == "2026-07-08"
    assert status.recent_moves[2].location == "SAN ANTONIO"
    assert status.recent_moves[3].location == "SAN ANTONIO"
    assert [move.name for move in status.recent_moves] == [
        "Empty Container Release to Shipper (GTOT)",
        "Container Gated In (GTIN)",
        "Container Loaded (LOAD)",
        "Transport Departed (DEPA)",
        "Transport Arrived (ARRI)",
        "Transport Departed (DEPA)",
        "Transport Arrived (ARRI)",
    ]


def test_maersk_events_401_still_raises_without_web_fallback(monkeypatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.maersk.com/track-and-trace-private/events")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "token")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "consumer")
    monkeypatch.setenv("MAERSK_WEB_FALLBACK_ON_API_ERROR", "true")

    def fake_get_with_retries(*args, **kwargs):
        raise _http_error(401)

    def fail_web_fallback(*args, **kwargs):
        raise AssertionError("401 should not use Maersk web fallback")

    monkeypatch.setattr(maersk, "get_with_retries", fake_get_with_retries)
    adapter = MaerskAdapter()
    monkeypatch.setattr(adapter, "_try_web_fallback", fail_web_fallback)

    try:
        adapter.fetch_status(_shipment())
    except requests.HTTPError as exc:
        assert exc.response is not None
        assert exc.response.status_code == 401
    else:
        raise AssertionError("Expected 401 HTTPError")


def test_maersk_tries_later_containers_when_first_has_no_events(monkeypatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.maersk.com/track-and-trace-private/events")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "token")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "consumer")

    calls: list[tuple[str, str]] = []

    def fake_fetch_payload(self, reference: str, ref_type: str, credentials):
        calls.append((reference, ref_type))
        if reference == "MRKU0516710":
            return {"events": []}, "maersk-events-api:not-found"
        return {
            "events": [
                {
                    "eventType": "TRANSPORT",
                    "transportEventTypeCode": "DEPA",
                    "eventDateTime": "2026-05-01T10:00:00Z",
                    "locationName": "SHANGHAI",
                }
            ]
        }, f"maersk-events-api:{reference}"

    monkeypatch.setattr(MaerskAdapter, "_fetch_payload", fake_fetch_payload)

    status = MaerskAdapter().fetch_status(
        ShipmentRef(
            task_id="task-1",
            task_name="Maersk shipment",
            shipping_line="maersk",
            booking_no="269822607",
            container_no="MRKU0516710, MRKU0931970, MSKU6547177",
            list_id="list-1",
        )
    )

    assert calls == [("MRKU0516710", "container"), ("MRKU0931970", "container")]
    assert status.latest_move is not None
    assert status.latest_move.name == "Transport Departed (DEPA)"
    assert status.raw_source == "maersk-events-api:MRKU0931970"


def test_maersk_uses_mexico_credentials_for_configured_tago_mago_list(monkeypatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.maersk.com/track-and-trace-private/events")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "default-consumer")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "default-token")
    monkeypatch.setenv("MAERSK_MEXICO_LIST_IDS", "901703461634")
    monkeypatch.setenv("MAERSK_MEXICO_CREDENTIAL_PROFILE", "mexico")
    monkeypatch.setenv("MAERSK_MEXICO_CONSUMER_KEY", "mexico-consumer")
    monkeypatch.setenv("MAERSK_MEXICO_BEARER_TOKEN", "mexico-token")

    def fake_fetch_all_events(self, reference, ref_type, headers):
        assert reference == "TCKU6860166"
        assert ref_type == "container"
        assert headers["Consumer-Key"] == "mexico-consumer"
        assert headers["Authorization"] == "Bearer mexico-token"
        return [
            {
                "eventType": "TRANSPORT",
                "transportEventTypeCode": "DEPA",
                "eventDateTime": "2026-07-08T10:00:00Z",
                "locationName": "SAN ANTONIO",
            }
        ]

    monkeypatch.setattr(MaerskAdapter, "_fetch_all_events", fake_fetch_all_events)
    status = MaerskAdapter().fetch_status(
        ShipmentRef(
            task_id="task-1",
            task_name="TagoMago Maersk shipment",
            shipping_line="maersk",
            booking_no=None,
            container_no="TCKU6860166",
            list_id="901703461634",
        )
    )

    assert status.latest_move is not None
    assert status.latest_move.location == "SAN ANTONIO"


def test_maersk_uses_default_credentials_for_mexico_list_by_default(monkeypatch) -> None:
    monkeypatch.setenv("MAERSK_API_MODE", "events")
    monkeypatch.setenv("MAERSK_TRACKING_API_URL", "https://api.maersk.com/track-and-trace-private/events")
    monkeypatch.setenv("MAERSK_CONSUMER_KEY", "default-consumer")
    monkeypatch.setenv("MAERSK_BEARER_TOKEN", "default-token")
    monkeypatch.setenv("MAERSK_MEXICO_LIST_IDS", "901703461634")
    monkeypatch.setenv("MAERSK_MEXICO_CONSUMER_KEY", "rejected-mexico-consumer")
    monkeypatch.setenv("MAERSK_MEXICO_BEARER_TOKEN", "rejected-mexico-token")

    def fake_fetch_all_events(self, reference, ref_type, headers):
        assert reference == "TCKU6860166"
        assert ref_type == "container"
        assert headers["Consumer-Key"] == "default-consumer"
        assert headers["Authorization"] == "Bearer default-token"
        return []

    monkeypatch.setattr(MaerskAdapter, "_fetch_all_events", fake_fetch_all_events)
    MaerskAdapter().fetch_status(
        ShipmentRef(
            task_id="task-1",
            task_name="TagoMago Maersk shipment",
            shipping_line="maersk",
            booking_no=None,
            container_no="TCKU6860166",
            list_id="901703461634",
        )
    )


def test_maersk_status_sets_final_arrival_vessel_voyage() -> None:
    status = _status_from_events(
        [
            {
                "transportEventTypeCode": "DEPA",
                "eventDateTime": "2026-05-01T10:00:00Z",
                "locationName": "NINGBO",
                "vesselName": "MAERSK ORIGIN",
                "carrierExportVoyageNumber": "601E",
            },
            {
                "transportEventTypeCode": "ARRI",
                "eventDateTime": "2026-06-04T07:00:00Z",
                "locationName": "PUERTO QUETZAL",
                "vesselName": "MAERSK FINAL",
                "carrierExportVoyageNumber": "621E",
            },
        ],
        "maersk-events-api:test",
        source_url="https://www.maersk.com/tracking/CONT",
    )

    assert status.vessel_voyage == "MAERSK FINAL 621E"


def test_maersk_status_uses_estimated_final_arrival_for_eta_and_vessel() -> None:
    status = _status_from_events(
        [
            {
                "eventType": "TRANSPORT",
                "eventClassifierCode": "ACT",
                "transportEventTypeCode": "ARRI",
                "eventDateTime": "2026-07-14T23:55:00-05:00",
                "locationName": "BALBOA",
                "vesselName": "POLAR PERU",
                "carrierExportVoyageNumber": "626N",
            },
            {
                "eventType": "TRANSPORT",
                "eventClassifierCode": "EST",
                "transportEventTypeCode": "DEPA",
                "eventDateTime": "2026-07-31T23:00:00-05:00",
                "locationName": "BALBOA",
                "vesselName": "MAERSK SEQUOIA",
                "carrierExportVoyageNumber": "631N",
            },
            {
                "eventType": "TRANSPORT",
                "eventClassifierCode": "EST",
                "transportEventTypeCode": "ARRI",
                "eventDateTime": "2026-08-03T15:00:00-06:00",
                "locationName": "PUERTO QUETZAL",
                "vesselName": "MAERSK SEQUOIA",
                "carrierExportVoyageNumber": "631N",
            },
        ],
        "maersk-events-api:test",
        source_url="https://www.maersk.com/tracking/272124460",
        destination_port="GTPRQ",
    )

    assert status.eta_time == datetime(2026, 8, 3, 21, tzinfo=timezone.utc)
    assert status.eta_local_text == "2026-08-03T15:00:00-06:00"
    assert status.vessel_voyage == "MAERSK SEQUOIA 631N"
    assert status.final_vessel_voyage == "MAERSK SEQUOIA 631N"
    assert status.recent_moves[0].event_state == "estimated"
