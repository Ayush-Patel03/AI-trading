"""Exits protect real decisions. These pin behaviour that already exists — a failure here
means the migration changed something, not that the engine is wrong."""
import json

import pytest

from conftest import run_pm


def _break_stop(run_dir, symbol="NVDA", stop=240.00, book="paper_book.json"):
    b = json.loads((run_dir / book).read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == symbol:
            p["stop"] = stop
    (run_dir / book).write_text(json.dumps(b), encoding="utf-8")


def test_stop_fires_and_closes_the_whole_position(pm, run_dir, quotes):
    _break_stop(run_dir)                      # live fixture print is 222.255
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "NVDA"]
    assert len(sells) == 1, "a broken stop must fire exactly once"
    assert sells[0]["reason"] == "stop"
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"], "position must be closed"


def test_exit_fills_below_the_print_by_the_slippage_assumption(pm, run_dir, quotes):
    _break_stop(run_dir)
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    px = [d["price"] for d in jrn["decisions"] if d["symbol"] == "NVDA"][0]
    assert px == pytest.approx(222.255 * (1 - 0.0025), rel=1e-4), \
        "exits must fill 0.25% against the book, never at the print"


def test_a_position_with_no_fresh_price_is_reported_unprotected_not_sold(pm, run_dir):
    # No quotes fixture: nothing may be sold on a stale mark.
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert any("UNPROTECTED" in w for w in jrn["warnings"])


def test_target_scales_out_half_and_moves_the_stop_to_breakeven(pm, run_dir, quotes):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "NVDA":
            p["target"] = 200.00              # below the live print — target is through
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    d = [x for x in jrn["decisions"] if x["symbol"] == "NVDA"][0]
    assert d["reason"] == "target"
    live = [p for p in book["positions"] if p["symbol"] == "NVDA"][0]
    assert live["scaled_out"] is True
    assert live["stop"] == pytest.approx(round(live["avg_cost"], 2))
    assert live["stop_basis_kind"] == "breakeven"


def test_a_quiet_book_takes_no_decision(pm, run_dir, quotes):
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert jrn["decisions"] == [], "no stop or target is through at fixture prices"
