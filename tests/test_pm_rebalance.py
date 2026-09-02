"""Rebalance discipline — the mirror image of the trim cap.

Trim has been capped at once per session since day one, and PM.md gives the reason: the
manager nibbles the same losing position every slot and calls it risk management.
`rebalance_pass` had no equivalent guard and nibbled the WINNING position instead — NVDA
was shaved four times on 2026-09-02. Two guards now: a count (once per name per session)
and a size deadband (a name oscillating around 15.0% is not a breach).
"""
import json

import pytest

from conftest import run_pm


def _resize(run_dir, symbol, shares, book="paper_book.json"):
    b = json.loads((run_dir / book).read_text())
    for p in b["positions"]:
        if p["symbol"] == symbol:
            p["shares"] = shares
    (run_dir / book).write_text(json.dumps(b))


def _rebalances(jrn, symbol="NVDA"):
    return [d for d in jrn["decisions"]
            if d["action"] == "fill-sell" and d["reason"] == "rebalance" and d["symbol"] == symbol]


def test_a_position_inside_the_deadband_is_not_shaved(run_dir, quotes, pm):
    """The fixture book holds NVDA at 15.2% of equity — over the cap, inside the deadband.

    This is the exact state that produced four shaves in one day for a combined $0.86.
    """
    _, jrn, _ = run_pm(pm, run_dir)
    assert _rebalances(jrn) == [], "a 0.2-point wobble over the cap is not a breach"
    assert any("deadband" in s["reason"] for s in jrn["skipped"] if s["symbol"] == "NVDA"), \
        "and the run must say why it left it alone"


def test_a_real_breach_is_still_trimmed_back_to_the_cap(run_dir, quotes, pm):
    _resize(run_dir, "NVDA", 4.0)                 # ~17.5% of equity, clear of the deadband
    book, jrn, _ = run_pm(pm, run_dir)
    sells = _rebalances(jrn)
    assert len(sells) == 1, "a real breach must still be corrected"
    live = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    equity = book["cash"] + sum(
        p["shares"] * (p.get("last_price") or p["avg_cost"]) for p in book["positions"])
    pct = live["shares"] * live["last_price"] / equity * 100
    assert pct == pytest.approx(15.0, abs=0.2), \
        "the cap is still the cap — the deadband only decides WHEN to act"


def test_a_name_rebalanced_today_is_not_rebalanced_again(run_dir, quotes, pm):
    _resize(run_dir, "NVDA", 4.0)
    book, jrn, _ = run_pm(pm, run_dir)
    assert len(_rebalances(jrn)) == 1
    # Same session, next slot: push it back over the trigger and run again.
    book["positions"] = [dict(p, shares=4.0) if p["symbol"] == "NVDA" else p
                         for p in book["positions"]]
    (run_dir / "paper_book.json").write_text(json.dumps(book))
    _, jrn2, _ = run_pm(pm, run_dir, slot="midday")
    assert _rebalances(jrn2) == [], "one rebalance per name per session"
    assert any("already rebalanced today" in s["reason"]
               for s in jrn2["skipped"] if s["symbol"] == "NVDA")


def test_the_rebalance_is_booked_on_the_position(run_dir, quotes, pm):
    _resize(run_dir, "NVDA", 4.0)
    book, jrn, _ = run_pm(pm, run_dir)
    live = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    assert live["rebalance_count"] == 1
    assert live["last_rebalance_date"] == jrn["date"]


def test_a_new_session_may_rebalance_again(run_dir, quotes, pm):
    """The cap is per session, not per position lifetime — a name that keeps running
    away from the cap on a later day is a real breach again."""
    _resize(run_dir, "NVDA", 4.0)
    book, _, _ = run_pm(pm, run_dir)
    live = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    live["last_rebalance_date"] = "2020-01-01"
    live["shares"] = 4.0
    (run_dir / "paper_book.json").write_text(json.dumps(book))
    _, jrn2, _ = run_pm(pm, run_dir, slot="midday")
    assert len(_rebalances(jrn2)) == 1
