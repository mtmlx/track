import pytest

from shipment_sync.wan_hai_reviewed import render_register


def evidence():
    histories = {}
    for ref, gate, returned in (
        ("WHSU6183498", "18:12", "17:46"),
        ("WHSU6809560", "14:09", "17:47"),
    ):
        histories[ref] = {
            "booking": "025G721233",
            "pickup_facility": "Terminal De Contenedores Quetzal",
            "empty_facility": "Contenedores Y Servicios, S.A.",
            "pickup_event": "Full Container Withdrawn By Consignee From Pier/Terminal",
            "empty_event": "Empty Container Discharged From Vessel Or Gate In To Pier/Terminal/Off-Dock Depot (Empty Available)",
            "pickup_at": "2026-09-24 " + gate,
            "empty_at": "2026-09-25 " + returned,
            "bl_status_at": "2026-09-25 " + returned,
        }
    return {"bl": "025G721233", "pod": "Puerto Quetzal (GT)", "histories": histories}


def test_both_confirmed_movements_render_with_source_precision():
    data = evidence()
    report = render_register(data["bl"], set(data["histories"]), data)
    assert "Destination gate-out: 2/2. Empty returned to depot: 2/2" in report
    assert "WHSU6809560 | 2026-09-24 14:09 | 2026-09-25 17:47" in report
    assert "no timezone is shown" in report


@pytest.mark.parametrize("key,value", [
    ("booking", "other"), ("empty_facility", "arbitrary terminal"),
    ("bl_status_at", "2026-09-24 10:00"),
    ("pickup_at", "2026-09-26 00:01"),
])
def test_register_rejects_conflicting_evidence(key, value):
    data = evidence()
    data["histories"]["WHSU6183498"][key] = value
    with pytest.raises(ValueError):
        render_register(data["bl"], set(data["histories"]), data)


def test_register_rejects_missing_container():
    data = evidence()
    with pytest.raises(ValueError):
        render_register(data["bl"], {*data["histories"], "WHSU6183499"}, data)
