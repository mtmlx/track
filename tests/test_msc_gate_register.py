from datetime import date
from types import SimpleNamespace

from shipment_sync.models import ShipmentRef
from shipment_sync.msc_browser_assisted import MscBrowserCapture, _date_only_gate_register


def test_msc_register_preserves_date_precision_and_rejects_incomplete_roster():
    refs = ["MSMU6969316", "MSNU5802884"]
    shipment = ShipmentRef("task", "test", "msc", "BOOK", ", ".join(refs), "list",
                           expected_container_count=2, destination_port="Puerto Cortes")
    entries = []
    for ref, gate, returned in zip(refs, ("30/09/2026", "29/09/2026"), ("01/10/2026", "29/09/2026")):
        capture = MscBrowserCapture("task", "\n".join([
            f"CONTAINER NUMBER: {ref}", returned, "Puerto Cortes, HN", "Empty received at CY", "EMPTY",
            gate, "Puerto Cortes, HN", "Import to consignee", "LADEN",
            "13/09/2026", "Puerto Cortes, HN", "Import Discharged from Vessel", "MSC WESER LF637R",
        ]), ref)
        entries.append((capture, SimpleNamespace(destination_port="Puerto Cortes, HN")))
    report = _date_only_gate_register(shipment, entries, date(2026, 11, 18))
    assert "Port gate-out: 2/2 confirmed" in report
    assert "Empty returned: 2/2 confirmed" in report
    assert "MSNU5802884 | gate-out: 2026-09-29 | empty return: 2026-09-29" in report
    assert "Same-day event order is not verified" in report
    assert "00:00" not in report
    assert report == _date_only_gate_register(shipment, entries[::-1], date(2026, 11, 18))
    assert _date_only_gate_register(shipment, entries[:1], date(2026, 11, 18)) is None
    assert _date_only_gate_register(shipment, entries, date(2026, 9, 28)) is None
