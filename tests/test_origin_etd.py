from datetime import datetime, timezone

import pytest

from shipment_sync.clickup_client import _pick_etd_move
from shipment_sync.models import MovementEvent


def event(code, day, location=None, location_code=None):
    return MovementEvent(
        name=f"Event ({code})", event_time=datetime.fromisoformat(day).replace(tzinfo=timezone.utc),
        location=location, location_code=location_code, event_state="actual",
    )


def test_chongqing_origin_not_pusan_transshipment():
    moves = [
        event("GTIN", "2026-06-26", "CHONGQING, CHONGQING, CHINA(CHONGQING GUOYUAN TERMINAL)"),
        event("DEPA", "2026-07-01", "CHONGQING, CHONGQING, CHINA(CHONGQING GUOYUAN C..."),
        event("LOAD", "2026-08-01", "SHANGHAI, CHINA(TERMINAL)"),
        event("DEPA", "2026-08-02", "SHANGHAI, CHINA(TERMINAL)"),
        event("DISC", "2026-08-09", "PUSAN, KOREA(TERMINAL)"),
        event("LOAD", "2026-09-07", "PUSAN, KOREA(PUSAN NEWPORT INTERNATIONAL TERMINAL)"),
        event("DEPA", "2026-09-08", "PUSAN, KOREA(PUSAN NEWPORT INTERN..."),
    ]
    for _ in range(3):
        assert _pick_etd_move(moves) is moves[1]


def test_authoritative_code_matches_different_terminal_names():
    moves = [event("GTIN", "2026-06-26", "Origin terminal", "CNCKG"),
             event("DEPA", "2026-07-01", "Other terminal display...", "CNCKG"),
             event("LOAD", "2026-09-07", "PUSAN", "KRPUS"),
             event("DEPA", "2026-09-08", "PUSAN", "KRPUS")]
    assert _pick_etd_move(moves) is moves[1]


@pytest.mark.parametrize("location", [None, "UNKNOWN", "CHONG...", "CHONGQING / SHANGHAI", "ORIGIN TERMINAL"])
def test_unknown_location_does_not_infer_barge(location):
    moves = [event("LOAD", "2026-06-26", location),
             event("DEPA", "2026-07-01", "SHANGHAI, CN"),
             event("LOAD", "2026-09-07", "PUSAN"),
             event("DEPA", "2026-09-08", "Other display...")]
    assert _pick_etd_move(moves) is moves[1]


def test_origin_barge_preserved_before_first_departure():
    moves = [event("GTIN", "2026-06-21", "HEFEI, CN"),
             event("LOAD", "2026-06-22", "HEFEI, CN"),
             event("DISC", "2026-06-28", "SHANGHAI, CN"),
             event("DEPA", "2026-07-01", "SHANGHAI, CN")]
    assert _pick_etd_move(moves) is moves[1]


def test_facility_codes_cannot_infer_different_ports():
    moves = [event("LOAD", "2026-06-26", "CHONGQING, CN(TERMINAL A)", "CNCKGAA"),
             event("DEPA", "2026-07-01", "CHONGQING, CN(TERMINAL B)", "CNCKGBB")]
    assert _pick_etd_move(moves) is moves[1]


@pytest.mark.parametrize("code", ["AMBIGUOUS", "UNKNOWN", "N/A", "TBD"])
def test_unknown_code_sentinels_cannot_establish_port_identity(code):
    moves = [event("LOAD", "2026-06-26", None, code),
             event("DEPA", "2026-07-01", None, code),
             event("LOAD", "2026-09-07", "PUSAN, KR"),
             event("DEPA", "2026-09-08", "SHANGHAI, CN")]
    assert _pick_etd_move(moves) is moves[1]


def test_maersk_ambiguous_code_falls_back_to_same_geographic_port():
    moves = [event("LOAD", "2026-06-26", "SHANGHAI, CN", "AMBIGUOUS"),
             event("DEPA", "2026-07-01", "SHANGHAI, CN", "CNSHA")]
    assert _pick_etd_move(moves) is moves[1]


def test_later_return_to_origin_cannot_replace_first_departure():
    moves = [event("GTIN", "2026-06-26", "CHONGQING, CN"),
             event("DEPA", "2026-07-01", None),
             event("LOAD", "2026-09-07", "PUSAN"),
             event("DEPA", "2026-09-08", "CHONGQING, CN")]
    assert _pick_etd_move(moves) is moves[1]
