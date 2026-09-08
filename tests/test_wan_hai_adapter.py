from __future__ import annotations

from shipment_sync.carriers.wan_hai import (
    _build_reference_attempts,
    _extract_booking_detail_containers,
    _looks_like_anti_bot_page,
    _status_from_parsed,
)
from shipment_sync.clickup_client import _extract_wan_hai_reference_hints
from shipment_sync.models import ShipmentRef


def test_wan_hai_prefers_booking_before_existing_containers() -> None:
    attempts = _build_reference_attempts(
        ShipmentRef(
            task_id="task-1",
            task_name="Wan Hai shipment",
            shipping_line="wan hai",
            booking_no="031G539204",
            container_no="WHSU4002923, WHSU4041560, BEAU5922059",
            list_id="list-1",
        )
    )

    assert attempts[:2] == [("031G539204", "2"), ("WHSU4002923", "1")]


def test_wan_hai_ignores_unverifiable_hints_without_known_container() -> None:
    attempts = _build_reference_attempts(
        ShipmentRef(
            task_id="task-1",
            task_name="Wan Hai shipment",
            shipping_line="wan hai",
            booking_no="027G709927",
            container_no=None,
            list_id="list-1",
            reference_hints=["WI666V70037", "027G709927"],
        )
    )

    assert attempts == [("027G709927", "2")]


def test_wan_hai_extracts_booking_and_mbl_hints_from_clickup_comment() -> None:
    text = (
        "change the booking to booking confirmed. "
        "HBL#GOSZX26062614  MBL#027G709927  BK#WI666V70037  PO F243264003Z-2.pdf"
    )

    assert _extract_wan_hai_reference_hints(text) == [
        "WI666V70037",
        "027G709927",
        "GOSZX26062614",
    ]


def test_wan_hai_detects_incapsula_anti_bot_page() -> None:
    html = '<script src="/_Incapsula_Resource?SWJIYLWA=abc"></script>'

    assert _looks_like_anti_bot_page(html)
    assert _looks_like_anti_bot_page("<html></html>", status_code=403)


def test_wan_hai_extracts_containers_from_booking_detail_table_only() -> None:
    html = """
    <html>
      <body>
        <script>var unrelated = "BEAU5922059";</script>
        <table><tr><th>Ctnr No.</th><th>Seal No.</th></tr>
          <tr><td>WHSU4002923</td><td>A</td></tr>
          <tr><td>WHSU4020402</td><td>B</td></tr>
          <tr><td>WHSU4041560</td><td>C</td></tr>
        </table>
      </body>
    </html>
    """

    assert _extract_booking_detail_containers(html) == [
        "WHSU4002923",
        "WHSU4020402",
        "WHSU4041560",
    ]


def test_wan_hai_status_sets_vessel_voyage_from_booking_detail() -> None:
    status = _status_from_parsed(
        cargo_type="2",
        result={"vessel": "KOTA SANTOS", "voyage": "020E", "booking_reference": "031G539204"},
        detail={
            "vessel": "KOTA SANTOS",
            "voyage": "020E",
            "booking_status": "CONFIRMED",
            "estimated_arrival_date": "2026/06/24",
            "port_of_loading": "NINGBO (CN)",
            "port_of_discharging": "PUERTO QUETZAL (GT)",
        },
        discovered_containers=["WHSU4002923"],
        source_url="https://www.wanhai.com/views/cargo_track_v2/tracking_data_list.xhtml",
    )

    assert status.vessel_voyage == "KOTA SANTOS 020E"
