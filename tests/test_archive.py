"""archive.py decides run identity and whether a run earns a permanent board.

The fingerprint hashes what the book IS, never what it is WORTH. If marks counted,
"publish when the book changed" would collapse into "publish always" and a real
UNPROTECTED banner would be one of four near-identical boards a day.
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import archive


BOOK = {
    "positions": [{"symbol": "NVDA", "shares": 3.395463, "avg_cost": 218.38,
                   "stop": 205.49, "target": 257.05, "stop_basis_kind": "atr",
                   "trim_count": 0, "last_price": 222.255}],
    "working_orders": [], "realized_pnl": 0.0, "closed_trades": [],
    "mode": "paper", "day": {"halted": False},
}


def test_fingerprint_ignores_a_price_move():
    b = copy.deepcopy(BOOK)
    before = archive.book_fingerprint(b)
    b["positions"][0]["last_price"] = 999.0
    assert archive.book_fingerprint(b) == before


def test_fingerprint_catches_a_share_change():
    b = copy.deepcopy(BOOK)
    before = archive.book_fingerprint(b)
    b["positions"][0]["shares"] = 4.0
    assert archive.book_fingerprint(b) != before


def test_fingerprint_catches_a_stop_change():
    b = copy.deepcopy(BOOK)
    before = archive.book_fingerprint(b)
    b["positions"][0]["stop"] = 200.0
    assert archive.book_fingerprint(b) != before


def test_sentinel_never_publishes_but_keeps_its_reasons():
    jrn = {"sentinel": True, "slot": "sentinel", "desk": "swing",
           "decisions": [{"action": "fill-sell"}], "warnings": [],
           "book_fingerprint": "aaaa", "date": "2026-09-02"}
    publish, reasons = archive.should_publish(jrn, BOOK, {"book_fingerprint": "aaaa"})
    assert publish is False
    assert "1 decision taken" in reasons, "the reasons drive the book snapshot and must survive"


def test_non_swing_desk_never_publishes():
    jrn = {"sentinel": False, "slot": "midday", "desk": "momentum",
           "decisions": [{"action": "place-buy"}], "warnings": [],
           "book_fingerprint": "aaaa", "date": "2026-09-02"}
    publish, reasons = archive.should_publish(jrn, BOOK, {"book_fingerprint": "aaaa"})
    assert publish is False
    assert reasons


def test_swing_decision_slot_does_publish():
    jrn = {"sentinel": False, "slot": "midday", "desk": "swing",
           "decisions": [{"action": "place-buy"}], "warnings": [],
           "book_fingerprint": "aaaa", "date": "2026-09-02"}
    publish, _ = archive.should_publish(jrn, BOOK, {"book_fingerprint": "aaaa"})
    assert publish is True


def test_close_of_day_publishes_an_unchanged_open_book():
    jrn = {"sentinel": False, "slot": "power-hour", "desk": "swing",
           "decisions": [], "warnings": [], "book_fingerprint": "aaaa", "date": "2026-09-02"}
    publish, reasons = archive.should_publish(jrn, BOOK, {"book_fingerprint": "aaaa"})
    assert publish is True
    assert reasons == [archive.CLOSE_REASON]


def test_scheduled_slot_run_id_is_time_free_so_a_rerun_replaces_itself():
    assert archive.pm_run_id("2026-09-02", "midday") == "2026-09-02-midday"
    assert archive.pm_run_id("2026-09-02", "midday", "2026-09-02T17:15:00Z") == "2026-09-02-midday"


def test_sentinel_run_id_carries_the_clock():
    rid = archive.pm_run_id("2026-09-02", "sentinel", "2026-09-02T13:47:47Z")
    assert rid == "2026-09-02-sentinel-1347"
