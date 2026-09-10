from shipment_sync.models import ShipmentRef, ShipmentUpdatePlan, ShipmentWriteResult
import json

import pytest

from shipment_sync.msc_browser_assisted import (
    build_queue,
    consolidate_browser_statuses,
    is_msc_line,
    read_import_batch,
    status_from_browser_capture,
)
from shipment_sync.msc_browser_assisted import MscBrowserCapture
from shipment_sync.msc_browser_assisted import validate_browser_journey
from shipment_sync.msc_browser_assisted_main import _download_import_batch, _import_batch


def test_build_queue_uses_container_references_before_booking() -> None:
    items = build_queue(
        [
            ShipmentRef(
                task_id="task-1",
                task_name="MSC shipment",
                shipping_line="MSC",
                booking_no="BOOK-1",
                container_no="TRHU5066421, CAIU7832977",
                list_id="list-1",
            )
        ]
    )

    assert len(items) == 1
    assert items[0].container_numbers == ["TRHU5066421", "CAIU7832977"]
    assert items[0].booking_no == "BOOK-1"


def test_msc_line_normalization_accepts_registered_aliases() -> None:
    assert is_msc_line("MSC")
    assert is_msc_line("Mediterranean Shipping Company")
    assert not is_msc_line("ONE")


def test_browser_capture_maps_visible_msc_result_to_existing_status_model() -> None:
    capture = """\
CONTAINER NUMBER: TRHU5066421
POD ETA
19/09/2026
Date
Location
Description
Empty/Laden/Vessel/Voyage
Equipment handling facility name
19/09/2026
Miami, US
Estimated Time of Arrival
ANTWERP 81W
Pomtoc Terminal
12/08/2026
Busan, KR
Full Intended Transshipment
ANTWERP 81E
Busan Container Terminal - Bct
08/08/2026
Busan, KR
Full Transshipment Discharged
MSC YOKOHAMA GY631A
Pusan New Port International Terminal (Pnit)
06/08/2026
Ningbo, CN
Export Loaded on Vessel
MSC YOKOHAMA GY631A
Gangji Terminal (Phase Iv)
"""

    status = status_from_browser_capture(capture)

    assert status.eta_time is not None
    assert status.eta_time.date().isoformat() == "2026-09-19"
    assert status.vessel_voyage == "ANTWERP 81W"
    assert status.discovered_containers == ["TRHU5066421"]
    assert not status.container_discovery_authoritative
    assert any(move.name == "Container Loaded (LOAD)" for move in status.recent_moves)


def test_browser_capture_without_pod_eta_uses_actual_movement_history() -> None:
    capture = """\
CONTAINER NUMBER: MSCU5320889
Date
Location
Description
03/08/2026
Charleston, US
Empty received at CY
"""

    status = status_from_browser_capture(capture)

    assert status.latest_move is not None
    assert status.latest_move.name == "Container Gated In (GTIN)"


def test_read_import_batch_keeps_captures_and_failures_separate(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "captures": [{"task_id": "task-1", "capture": "captured page"}],
                "failures": [{"task_id": "task-2", "reference": "MSCU1234567", "error": "No result found"}],
            }
        ),
        encoding="utf-8",
    )

    captures, failures = read_import_batch(path)

    assert captures[0].task_id == "task-1"
    assert failures[0].reference == "MSCU1234567"


def test_read_import_batch_allows_a_capture_and_diagnostic_for_one_task(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "captures": [{"task_id": "task-1", "capture": "captured page"}],
                "failures": [{"task_id": "task-1", "reference": "MSCU1234567", "error": "No result found"}],
            }
        ),
        encoding="utf-8",
    )

    captures, failures = read_import_batch(path)

    assert captures[0].task_id == failures[0].task_id == "task-1"


def test_read_import_batch_allows_multiple_failed_containers_for_one_task(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "failures": [
                    {"task_id": "task-1", "reference": "MSCU1234567", "error": "No result found"},
                    {"task_id": "task-1", "reference": "MSCU7654321", "error": "No result found"},
                ]
            }
        ),
        encoding="utf-8",
    )

    _, failures = read_import_batch(path)

    assert [failure.reference for failure in failures] == ["MSCU1234567", "MSCU7654321"]


def test_read_import_batch_rejects_duplicate_failure_task_reference_pairs(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "failures": [
                    {"task_id": "task-1", "reference": "MSCU1234567", "error": "No result found"},
                    {"task_id": "task-1", "reference": "MSCU1234567", "error": "Still no result"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate failure task/reference pairs"):
        read_import_batch(path)


def test_read_import_batch_allows_multiple_container_captures_for_one_task(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "captures": [
                    {"task_id": "task-1", "reference": "MSCU1234567", "capture": "CONTAINER NUMBER: MSCU1234567"},
                    {"task_id": "task-1", "reference": "MSCU7654321", "capture": "CONTAINER NUMBER: MSCU7654321"},
                ]
            }
        ),
        encoding="utf-8",
    )

    captures, _ = read_import_batch(path)

    assert [capture.reference for capture in captures] == ["MSCU1234567", "MSCU7654321"]


def test_read_import_batch_rejects_duplicate_capture_task_reference_pairs(tmp_path) -> None:
    path = tmp_path / "batch.json"
    path.write_text(
        json.dumps(
            {
                "captures": [
                    {"task_id": "task-1", "reference": "MSCU1234567", "capture": "captured page one"},
                    {"task_id": "task-1", "reference": "MSCU1234567", "capture": "captured page two"},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate capture task/reference pairs"):
        read_import_batch(path)


def test_consolidate_browser_statuses_requires_all_known_containers() -> None:
    shipment = ShipmentRef("task-1", "MSC shipment", "MSC", "BOOK-1", "MSCU1234567, MSCU7654321", "list-1")
    status = status_from_browser_capture(_capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"))

    with pytest.raises(ValueError, match="missing container capture"):
        consolidate_browser_statuses(shipment, [(MscBrowserCapture("task-1", "", "MSCU1234567"), status)])


def test_consolidate_browser_statuses_rejects_inconsistent_shipment_facts() -> None:
    shipment = ShipmentRef("task-1", "MSC shipment", "MSC", "BOOK-1", "MSCU1234567, MSCU7654321", "list-1")
    shipment.destination_port = "Miami"
    first = status_from_browser_capture(_capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"))
    second = status_from_browser_capture(_capture("MSCU7654321", "20/09/2026", "ANTWERP 81W"))

    with pytest.raises(ValueError, match="disagree across containers"):
        consolidate_browser_statuses(
            shipment,
            [
                (MscBrowserCapture("task-1", _capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"), "MSCU1234567"), first),
                (MscBrowserCapture("task-1", _capture("MSCU7654321", "20/09/2026", "ANTWERP 81W"), "MSCU7654321"), second),
            ],
        )


def test_consolidate_browser_statuses_merges_consistent_container_results() -> None:
    shipment = ShipmentRef("task-1", "MSC shipment", "MSC", "BOOK-1", "MSCU1234567, MSCU7654321", "list-1")
    shipment.destination_port = "Miami"
    first = status_from_browser_capture(_capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"))
    second = status_from_browser_capture(_capture("MSCU7654321", "19/09/2026", "ANTWERP 81W"))

    status = consolidate_browser_statuses(
        shipment,
        [
            (MscBrowserCapture("task-1", _capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"), "MSCU1234567"), first),
            (MscBrowserCapture("task-1", _capture("MSCU7654321", "19/09/2026", "ANTWERP 81W"), "MSCU7654321"), second),
        ],
    )

    assert status.discovered_containers == ["MSCU1234567", "MSCU7654321"]
    assert not status.container_discovery_authoritative


def test_import_batch_continues_after_invalid_capture() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.updated: list[str] = []
            self.failures: list[tuple[str, str]] = []

        def plan_shipment_update(self, shipment, status):
            return ShipmentUpdatePlan(changed=True, status_value="transito", snapshot_hash="hash")

        def update_shipment_status(self, shipment, status):
            self.updated.append(shipment.task_id)
            return ShipmentWriteResult(changed=True, status_value="transito", snapshot_hash="hash")

        def report_msc_tracking_failure(self, shipment, *, reference, error):
            self.failures.append((shipment.task_id, error))
            return True

        def report_msc_container_review_issue(self, shipment, *, error):
            self.failures.append((shipment.task_id, error))
            return True

    shipments = [
        ShipmentRef("bad", "Bad", "msc", "BOOK-BAD", None, "list-1"),
        ShipmentRef("good", "Good", "msc", "BOOK-GOOD", None, "list-1", destination_port="Charleston"),
    ]
    captures = [
        MscBrowserCapture(task_id="bad", capture="CONTAINER NUMBER: MSCU1234567"),
        MscBrowserCapture(
            task_id="good",
            capture="""BOOKING NUMBER: BOOK-GOOD 1 Bill of Lading found
CONTAINER NUMBER: MSCU7654321
Port of Discharge
Charleston, US
Date
Location
Description
03/08/2026
Charleston, US
Empty received at CY
""",
        ),
    ]
    client = FakeClient()

    _import_batch(client, shipments, captures, [], apply=True)

    assert client.updated == ["good"]
    assert client.failures == [("bad", "MSC browser capture does not contain any dated tracking events")]


def test_import_batch_projects_one_consistent_result_for_all_containers() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.updated: list[str] = []

        def plan_shipment_update(self, shipment, status):
            assert status.discovered_containers == ["MSCU1234567", "MSCU7654321"]
            return ShipmentUpdatePlan(changed=True, status_value="transito", snapshot_hash="hash")

        def update_shipment_status(self, shipment, status):
            self.updated.append(shipment.task_id)
            return ShipmentWriteResult(changed=True, status_value="transito", snapshot_hash="hash")

        def report_msc_container_review_issue(self, shipment, *, error):
            raise AssertionError(error)

        def report_msc_tracking_failure(self, shipment, *, reference, error):
            raise AssertionError(error)

    shipment = ShipmentRef(
        "task-1",
        "MSC shipment",
        "MSC",
        "BOOK-1",
        "MSCU1234567, MSCU7654321",
        "list-1",
        destination_port="Miami",
    )
    captures = [
        MscBrowserCapture("task-1", _capture("MSCU1234567", "19/09/2026", "ANTWERP 81W"), "MSCU1234567"),
        MscBrowserCapture("task-1", _capture("MSCU7654321", "19/09/2026", "ANTWERP 81W"), "MSCU7654321"),
    ]
    client = FakeClient()

    _import_batch(client, [shipment], captures, [], apply=True)

    assert client.updated == ["task-1"]


def test_download_import_batch_writes_private_response_to_temporary_file(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"captures": []}'

    monkeypatch.setattr("shipment_sync.msc_browser_assisted_main.urlopen", lambda url, timeout: Response())

    path = _download_import_batch("https://example.invalid/msc-batch")
    try:
        assert path.read_bytes() == b'{"captures": []}'
    finally:
        path.unlink(missing_ok=True)


def _capture(container: str, eta: str, vessel_voyage: str) -> str:
    return f"""BOOKING NUMBER: BOOK-1 1 Bill of Lading found
CONTAINER NUMBER: {container}
Port of Discharge
Miami, US
POD ETA
{eta}
Date
Location
Description
Empty/Laden/Vessel/Voyage
Equipment handling facility name
{eta}
Miami, US
Estimated Time of Arrival
{vessel_voyage}
Pomtoc Terminal
"""


@pytest.mark.parametrize("destination", ["Charleston", "USCHS", "Charleston, US"])
def test_booking_journey_accepts_destination_aliases(destination):
    shipment = ShipmentRef("9748", "9748", "msc", "177WGSGSN7A192A", "TRHU8012033", "list", destination_port=destination)
    capture = MscBrowserCapture("9748", "BOOKING NUMBER: 177WGSGSN7A192A 1 Bill of Lading found\nContainer\nTRHU8012033\nPort of Discharge\nCharleston, US\nShipped To\nCharleston, US")
    validate_browser_journey(shipment, capture)


@pytest.mark.parametrize("destination", [None, "KANSAS, USA", "Charleston / Antwerp", "Antwerp, BE"])
def test_booking_journey_rejects_unknown_or_conflicting_destination(destination):
    shipment = ShipmentRef("9748", "9748", "msc", "177WGSGSN7A192A", "TRHU8012033", "list", destination_port=destination)
    capture = MscBrowserCapture("9748", "BOOKING NUMBER: 177WGSGSN7A192A\nContainer\nTRHU8012033\nPort of Discharge\nCharleston, US")
    with pytest.raises(ValueError, match="shipment destination"):
        validate_browser_journey(shipment, capture)


def test_inland_destination_is_not_required_to_equal_discharge_port():
    shipment = ShipmentRef("s", "s", "msc", "BOOK-1", "MSCU1234567", "list", destination_port="Kansas City")
    capture = MscBrowserCapture("s", "BOOKING NUMBER: BOOK-1\nContainer\nMSCU1234567\nPort of Discharge\nLong Beach, US\nShipped To\nKansas City, US")
    validate_browser_journey(shipment, capture)


@pytest.mark.parametrize("header", ["CONTAINER NUMBER: TRHU8012033", "BOOKING NUMBER: ANOTHER", "BOOKING NUMBER: 177WGSGSN7A192AX"])
def test_reused_container_cannot_supply_another_journey(header):
    shipment = ShipmentRef("9748", "9748", "msc", "177WGSGSN7A192A", "TRHU8012033", "list", destination_port="Charleston")
    capture = MscBrowserCapture("9748", header + "\nContainer\nTRHU8012033\nPort of Discharge\nAntwerp, BE")
    for _ in range(2):
        with pytest.raises(ValueError, match="visible booking/BL"):
            validate_browser_journey(shipment, capture)


def test_matching_mbl_header_is_accepted():
    shipment = ShipmentRef("s", "s", "msc", "MEDUAAH59944", "TRHU8012033", "list", destination_port="Charleston")
    validate_browser_journey(shipment, MscBrowserCapture("s", "Bill of Lading:\nMEDUAAH59944\nContainer\nTRHU8012033\nPort of Discharge\nCharleston, US"))


def test_combined_container_histories_are_rejected():
    shipment = ShipmentRef("s", "s", "msc", "BOOK-1", "MSCU1234567", "list", destination_port="Miami")
    with pytest.raises(ValueError, match="isolated container"):
        validate_browser_journey(shipment, MscBrowserCapture("s", _capture("MSCU1234567", "19/09/2026", "SHIP 1E") + "\nMSCU7654321"))


def test_invalid_journey_never_reaches_write_planner_even_on_repeat():
    class NoWrites:
        def plan_shipment_update(self, *args):
            raise AssertionError("Unsafe capture reached planner")
        def report_msc_container_review_issue(self, shipment, *, error):
            assert "visible booking/BL" in error
            return True
    shipment = ShipmentRef("s", "s", "msc", "BOOK-1", "MSCU1234567", "list", destination_port="Miami")
    capture = MscBrowserCapture("s", _capture("MSCU1234567", "19/09/2026", "SHIP 1E").replace("BOOK-1", "OTHER"))
    for _ in range(2):
        _import_batch(NoWrites(), [shipment], [capture], [], apply=True)


def test_report_retains_rejection_and_skips_newly_ineligible_task(tmp_path):
    path = tmp_path / "progress.jsonl"
    shipment = ShipmentRef("s", "s", "msc", "BOOK-1", "MSCU1234567", "list", destination_port="Miami")
    bad = MscBrowserCapture("s", _capture("MSCU1234567", "19/09/2026", "SHIP 1E").replace("BOOK-1", "OTHER"))
    missing = MscBrowserCapture("closed", bad.capture)
    _import_batch(object(), [shipment], [bad, missing], [], apply=False, report_path=path)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(x['task_id'], x['outcome']) for x in rows] == [('closed', 'ineligible'), ('s', 'rejected')]
    assert path.stat().st_mode & 0o777 == 0o600


def test_uncertain_write_is_reported_without_retry(tmp_path):
    import requests
    class Client:
        calls = 0
        def plan_shipment_update(self, shipment, status):
            return ShipmentUpdatePlan(True, "status", "hash")
        def update_shipment_status(self, shipment, status):
            self.calls += 1
            raise requests.Timeout('sensitive transport details not logged')
    client = Client()
    shipment = ShipmentRef("s", "s", "msc", "BOOK-1", "MSCU1234567", "list", destination_port="Miami")
    capture = MscBrowserCapture("s", _capture("MSCU1234567", "19/09/2026", "SHIP 1E"))
    path = tmp_path / 'progress.jsonl'
    _import_batch(client, [shipment], [capture], [], apply=True, report_path=path)
    assert client.calls == 1
    assert [json.loads(x)['outcome'] for x in path.read_text().splitlines()] == ['write_started', 'write_uncertain']
