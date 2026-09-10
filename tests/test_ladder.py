"""K-02 — the drawdown ladder. One synthetic book is walked down every rung and back.

The book is deliberately simple: $8,500 cash and 30 shares of XYZ at $50 — 15% of the
book, so the position cap never trims it — and equity is 8500 + 30 × price: every rung
is one price. The stop is parked at $1 so nothing but the ladder can sell. Each step is
a fresh session: the 3% daily kill measures from the session's opening mark, and a
multi-day bleed that never trips it is exactly the case the ladder exists for.
"""
import copy
import datetime as dt
import json

import pytest

import ladder

SCAN_ROW = {"ticker": "SCHW", "name": "Charles Schwab", "price": 92.4, "score": 78.0,
            "setup": "Momentum", "verdict": "Strong Buy", "gics": "Financials",
            "sector": "Financials", "industry": "Capital Markets", "rsi_14": 60.0,
            "atr_14": 2.77, "atr_pct": 3.0, "ma_50": 87.78, "ma_200": 79.46,
            "confidence": "ok", "upside_pct": 12.0, "analyst_target": 103.49,
            "setup_note": "trend intact"}


def _book(cash=8500.0, shares=30.0, price=50.0, **extra):
    b = {"starting_equity": 10000.0, "cash": cash, "realized_pnl": 0.0, "mode": "paper",
         "positions": [{"symbol": "XYZ", "shares": shares, "avg_cost": 50.0, "stop": 1.0,
                        "target": 500.0, "gics": "Industrials", "industry": "Machinery",
                        "opened": "2026-09-01", "intraday_shares": 0.0, "trim_count": 0,
                        "rebalance_count": 0, "last_price": price, "high_water": price}]
         if shares else [],
         "working_orders": [], "closed_trades": [], "day_trades": [], "equity_curve": []}
    b.update(extra)
    return b


def _scan(date):
    return {"meta": {"scan_date": date, "time": "09:30", "slot": "opening range",
                     "macro_events": []}, "results": [copy.deepcopy(SCAN_ROW)]}


def step(pm, book, date, px, slot="opening-range", with_scan=True):
    """One decision slot at 10:45 ET on `date`, XYZ printing `px`."""
    scan = _scan(date) if with_scan else None
    return pm.run(book, scan, {"XYZ": px}, slot, f"{date}T14:45:00Z", "paper")


def _placed(jrn, sym="SCHW"):
    return [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == sym]


@pytest.fixture
def engine(pm):
    pm.PEERS.update({"books": {}, "loaded": [], "missing": []})
    return pm


# ---------------------------------------------------------------- the drill
def test_the_ladder_drill(engine):
    pm = engine
    book = _book()

    # -3%: nothing changes. HWM seeded from the seed capital, full-size entry.
    book, jrn, state = step(pm, book, "2026-09-14", 40.0)            # 9,700
    lad = jrn["ladder"]
    assert book["hwm"] == 10000.0
    assert lad["dd_pct"] == pytest.approx(3.0)
    assert lad["rung"] == 0 and lad["entry_size_mult"] == 1.0 and not lad["entries_blocked"]
    full = _placed(jrn)
    assert full, "a 3% drawdown must not touch entries"
    order = book["working_orders"][0]
    assert order["meta"]["ladder_mult"] == 1.0
    assert state["book"]["ladder"] == lad and state["book"]["hwm"] == 10000.0

    # -4.5%: rung 1, the entry is sized at half of what the sizing math produced.
    book, jrn, _ = step(pm, book, "2026-09-15", 35.0)            # 9,550: -4.5%
    lad = jrn["ladder"]
    assert lad["rung"] == 1 and lad["entry_size_mult"] == 0.5
    assert not lad["entries_blocked"]
    halved = _placed(jrn)
    assert halved, "rung 1 resizes, it does not refuse"
    order = book["working_orders"][0]
    assert order["meta"]["ladder_mult"] == 0.5
    assert order["shares"] == pytest.approx(order["meta"]["unscaled_shares"] * 0.5, rel=1e-6)
    assert order["notional"] == pytest.approx(order["shares"] * order["limit_price"], abs=0.01)
    assert any("LADDER" in w and "rung 1" in w for w in jrn["warnings"])
    assert not book["day"]["halted"], "the 3% kill must not be involved at -4.5% over two days"

    # -6.5%: rung 2, the entry is refused and the journal says why. Exits still ran.
    book, jrn, _ = step(pm, book, "2026-09-16", 28.5)            # 9,355: -6.45%
    lad = jrn["ladder"]
    assert lad["rung"] == 2 and lad["entry_size_mult"] == 0.0 and lad["entries_blocked"]
    assert not _placed(jrn)
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "*"]
    assert any("ladder rung 2" in r and "no new entries" in r for r in reasons), reasons
    assert book["positions"], "rung 2 does not sell anything"
    assert not book["day"]["halted"]

    # -8.5%: rung 3. The halt fires through the kill-switch path, the book is flattened,
    # the cool-off is set five business days out. The 3% daily kill is NOT what fired:
    # this session opened at the -8.5% mark, so its day P&L is zero.
    book, jrn, state = step(pm, book, "2026-09-17", 21.5)            # 9,145: -8.55%
    lad = jrn["ladder"]
    assert lad["rung"] == 3 and lad["halt"] and lad["flatten"]
    assert lad["dd_pct"] == pytest.approx(8.55)
    assert book["day"]["halted"] is True
    assert "Drawdown ladder halt" in book["day"]["halt_reason"]
    assert jrn["daily_pnl_pct"] == pytest.approx(0.0, abs=0.05), "not the daily kill"
    assert not any("KILL SWITCH" in w for w in jrn["warnings"])
    assert any("DRAWDOWN LADDER HALT" in w for w in jrn["warnings"])
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert len(sells) == 1 and sells[0]["symbol"] == "XYZ" and sells[0]["reason"] == "flatten"
    assert sells[0]["price"] == pytest.approx(round(21.5 * (1 - 0.0025), 4), abs=1e-6), "slippage applies"
    assert book["positions"] == []
    assert book["cool_until"] == "2026-09-24"           # 18, 21, 22, 23, 24
    assert lad["cool_until"] == "2026-09-24"
    assert book["ladder_halt"]["hwm"] == 10000.0
    assert book["ladder_halt"]["equity_after"] == pytest.approx(8500 + 30 * 21.5 * 0.9975, abs=0.01)
    assert state["book"]["cool_until"] == "2026-09-24"
    assert not _placed(jrn)
    flat_equity = jrn["equity"]

    # Inside the cool-off (a new session, so the day halt itself has cleared): entries
    # are still refused, and the reason is the cool-off, not the day halt.
    book, jrn, _ = step(pm, book, "2026-09-21", 21.5)
    lad = jrn["ladder"]
    assert book["day"]["halted"] is False, "the day halt clears at the roll — the ladder does not"
    assert lad["cool_active"] and lad["entries_blocked"] and lad["reentry_active"]
    assert not _placed(jrn)
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "*"]
    assert any("cool-off" in r and "2026-09-24" in r for r in reasons), reasons

    # After cool_until, still under the HWM: entries come back at the re-entry multiplier.
    book, jrn, _ = step(pm, book, "2026-09-25", 21.5)
    lad = jrn["ladder"]
    assert jrn["equity"] == pytest.approx(flat_equity, abs=0.01)
    assert lad["dd_pct"] > 8.0, "still well under the HWM"
    assert lad["reentry_active"] and not lad["cool_active"] and not lad["entries_blocked"]
    assert lad["entry_size_mult"] == 0.5 and lad["rung"] == 0
    reentry = _placed(jrn)
    assert reentry, "re-entry must be allowed after the cool-off"
    order = book["working_orders"][0]
    assert order["meta"]["ladder_mult"] == 0.5
    assert order["shares"] == pytest.approx(order["meta"]["unscaled_shares"] * 0.5, rel=1e-6)
    assert book["hwm"] == 10000.0, "the HWM does not move down through any of this"

    # HWM regained: full size, cool-off record cleared, the ladder is back at rung 0.
    book["cash"] = 10100.0
    book, jrn, state = step(pm, book, "2026-09-28", 21.5)
    lad = jrn["ladder"]
    assert book["hwm"] == 10100.0
    assert lad["dd_pct"] == 0.0 and lad["rung"] == 0 and lad["entry_size_mult"] == 1.0
    assert not lad["reentry_active"] and not lad["entries_blocked"]
    assert lad["cool_until"] is None and book["cool_until"] is None and book["ladder_halt"] is None
    assert any("regained" in w for w in jrn["warnings"])
    order = book["working_orders"][0]
    assert order["meta"]["ladder_mult"] == 1.0


def test_after_a_halt_the_ladder_re_arms_from_the_post_halt_equity(engine):
    """A re-entered book that loses another 8% from where it re-entered halts again;
    the old HWM is not the yardstick for a second cliff that could never be reached."""
    pm = engine
    book = _book(cash=7800.0, shares=30.0, price=45.0,          # 9,150 = the base
                 hwm=10000.0, cool_until="2026-09-24",
                 ladder_halt={"date": "2026-09-17", "hwm": 10000.0, "dd_pct": 8.5,
                              "equity_after": 9150.0, "cool_until": "2026-09-24"})
    book, jrn, _ = step(pm, book, "2026-09-25", 45.0)             # base: 9150, no move
    assert jrn["ladder"]["rung"] == 0 and jrn["ladder"]["entry_size_mult"] == 0.5
    book, jrn, _ = step(pm, book, "2026-09-28", 18.5)             # 8,355: -8.69% from the base
    lad = jrn["ladder"]
    assert lad["base_dd_pct"] == pytest.approx(8.69, abs=0.01)
    assert lad["rung"] == 3 and lad["halt"]
    assert book["day"]["halted"] and book["positions"] == []
    assert book["cool_until"] == "2026-10-05"
    assert book["hwm"] == 10000.0


# ---------------------------------------------------------------- soft daily level
def test_soft_daily_level_blocks_entries_but_not_exits_below_the_3pct_kill(engine):
    pm = engine
    book = _book()
    book, jrn, _ = step(pm, book, "2026-09-14", 50.0)             # opens the session at 10,000
    assert book["day"]["open_equity"] == 10000.0
    assert _placed(jrn), "a flat day places normally"
    # The entry from the opening slot must not fill on the next one (it is a limit at the
    # scan price and the scan price is unchanged) — cancel it so the book stays legible.
    book["working_orders"] = []
    for p in book["positions"]:
        p["stop"] = 49.0                                           # a stop the -2.5% print breaks
    book, jrn, _ = step(pm, book, "2026-09-14", 41.5, slot="midday")    # 9,745: -2.55% on the day
    lad = jrn["ladder"]
    assert lad["soft_daily_hit"] and lad["entries_blocked"] and lad["rung"] == 0
    assert lad["dd_pct"] == pytest.approx(2.55)
    assert not _placed(jrn)
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "*"]
    assert any("soft daily level" in r for r in reasons), reasons
    assert any(w.startswith("SOFT DAILY LEVEL") for w in jrn["warnings"])
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert sells and sells[0]["reason"] == "stop", "exits still run under the soft level"
    assert book["day"]["halted"] is False, "-2.5% is under the 3% kill; it must not trip"
    assert not any("KILL SWITCH" in w for w in jrn["warnings"])
    assert book["day"]["ladder_soft_hit"] is True, "sticky for the rest of the session"


def test_soft_daily_level_stays_shut_for_the_session_and_clears_at_the_roll(engine):
    pm = engine
    book = _book()
    book, _, _ = step(pm, book, "2026-09-14", 50.0)
    book["working_orders"] = []
    book, jrn, _ = step(pm, book, "2026-09-14", 41.5, slot="midday")
    assert jrn["ladder"]["soft_daily_hit"]
    # Price recovers inside the same session: still no entries — the flag is sticky.
    book["working_orders"] = []
    book, jrn, _ = step(pm, book, "2026-09-14", 49.5, slot="midday")
    assert jrn["daily_pnl_pct"] > -2.0
    assert jrn["ladder"]["soft_daily_hit"] and jrn["ladder"]["entries_blocked"]
    assert not _placed(jrn)
    # Next session: clean slate.
    book, jrn, _ = step(pm, book, "2026-09-15", 49.5)
    assert not jrn["ladder"]["soft_daily_hit"] and not jrn["ladder"]["entries_blocked"]
    assert _placed(jrn)


def test_the_3pct_kill_switch_is_untouched(engine):
    pm = engine
    book = _book()
    book, _, _ = step(pm, book, "2026-09-14", 50.0)
    book["working_orders"] = []
    book, jrn, _ = step(pm, book, "2026-09-14", 39.0, slot="midday")     # 9,670: -3.3% on the day
    assert book["day"]["halted"] is True
    assert "kill switch" in book["day"]["halt_reason"]
    assert any("KILL SWITCH TRIPPED" in w for w in jrn["warnings"])
    assert jrn["ladder"]["soft_daily_hit"], "the soft level is also through; both are reported"


# ---------------------------------------------------------------- the high-water mark
def test_hwm_is_seeded_from_the_existing_equity_curve(engine):
    pm = engine
    book = _book(equity_curve=[{"date": "2026-09-01", "slot": "midday", "equity": 10050.0},
                               {"date": "2026-09-02", "slot": "midday", "equity": 9900.0}])
    book, jrn, state = step(pm, book, "2026-09-14", 50.0)
    assert book["hwm"] == 10050.0
    assert jrn["ladder"]["hwm"] == 10050.0
    assert jrn["ladder"]["dd_pct"] == pytest.approx((10050 - 10000) / 10050 * 100, abs=0.01)
    assert state["book"]["hwm"] == 10050.0


def test_hwm_seeds_from_the_seed_capital_when_there_is_no_curve():
    assert ladder.seed_hwm({"starting_equity": 5000.0, "equity_curve": []}) == 5000.0
    assert ladder.seed_hwm({"equity_curve": [{"equity": 4800.0}, {"equity": 5100.0}]}) == 5100.0
    assert ladder.seed_hwm({"hwm": 5200.0, "equity_curve": [{"equity": 5100.0}],
                            "starting_equity": 5000.0}) == 5200.0
    assert ladder.seed_hwm({}) is None


def test_hwm_never_decreases_and_survives_equity_curve_capping(engine):
    pm = engine
    pm.PM_RULES["equity_curve_max"] = 2
    book = _book()
    book, _, _ = step(pm, book, "2026-09-14", 55.0)               # 10,150: a new high
    assert book["hwm"] == 10150.0
    for date, px in (("2026-09-15", 50.0), ("2026-09-16", 49.0), ("2026-09-17", 48.5)):
        book["working_orders"] = []
        book, jrn, _ = step(pm, book, date, px)
        assert book["hwm"] == 10150.0, "a lower mark must never pull the HWM down"
        assert jrn["ladder"]["hwm"] == 10150.0
    assert len(book["equity_curve"]) == 2
    assert max(c["equity"] for c in book["equity_curve"]) < 10150.0, \
        "the 10,150 point has been capped off the curve — the HWM must not depend on it"
    assert ladder.seed_hwm(book) == 10150.0
    # A stored HWM above anything on the curve or the seed is kept, not re-derived.
    book["hwm"] = 11000.0
    book, jrn, _ = step(pm, book, "2026-09-18", 48.5)
    assert book["hwm"] == 11000.0 and jrn["ladder"]["hwm"] == 11000.0


def test_a_sentinel_run_honours_the_ladder_halt(engine):
    """The rung-3 flatten is an exit, and the sentinel exists to honour exits within
    the hour rather than at the next slot."""
    pm = engine
    book = _book(hwm=10000.0)
    book, jrn, _ = pm.run(book, None, {"XYZ": 21.5}, "sentinel", "2026-09-14T15:30:00Z", "paper")
    assert jrn["ladder"]["rung"] == 3 and book["day"]["halted"] and book["positions"] == []
    assert book["cool_until"] == "2026-09-21"


# ---------------------------------------------------------------- the pure module
def test_state_for_reads_the_rules_it_is_given_and_writes_nothing():
    book = {"starting_equity": 1000.0, "equity_curve": [], "day": {"date": "2026-09-14"}}
    before = json.dumps(book, sort_keys=True)
    rules = {"soft_daily_pct": 1.0,
             "rungs": [{"dd_pct": 2.0, "entry_size_mult": 0.25},
                       {"dd_pct": 3.0, "entry_size_mult": 0.0},
                       {"dd_pct": 5.0, "flatten": True, "cool_sessions": 2}],
             "reentry_size_mult": 0.75}
    today = dt.date(2026, 9, 14)
    st = ladder.state_for(book, 975.0, today, rules)
    assert (st["rung"], st["entry_size_mult"], st["entries_blocked"]) == (1, 0.25, False)
    st = ladder.state_for(book, 965.0, today, rules)
    assert (st["rung"], st["entries_blocked"]) == (2, True)
    st = ladder.state_for(book, 940.0, today, rules)
    assert st["rung"] == 3 and st["halt"] and st["flatten"]
    st = ladder.state_for(book, 995.0, today, rules, day_pnl_pct=-1.2)
    assert st["rung"] == 0 and st["soft_daily_hit"] and st["entries_blocked"]
    st = ladder.state_for(book, 995.0, today, rules, day_pnl_pct=-0.9)
    assert not st["soft_daily_hit"] and not st["entries_blocked"]
    assert set(st) >= {"dd_pct", "hwm", "rung", "entry_size_mult", "entries_blocked", "reason",
                       "soft_daily_hit", "cool_until", "reentry_active"}
    assert json.dumps(book, sort_keys=True) == before, "state_for is pure"
    rec = ladder.halt_record(st, today, 940.0, rules)
    assert rec["cool_until"] == "2026-09-16"
    assert ladder.add_business_days(dt.date(2026, 9, 18), 1) == dt.date(2026, 9, 21)


def test_pm_rules_carry_the_ladder_for_the_board(engine):
    lad = engine.PM_RULES["ladder"]
    assert lad["soft_daily_pct"] == 2.0
    assert [r["dd_pct"] for r in lad["rungs"]] == [4.0, 6.0, 8.0]
    assert lad["rungs"][0]["entry_size_mult"] == 0.5
    assert lad["rungs"][1]["entry_size_mult"] == 0.0
    assert lad["rungs"][2]["flatten"] is True and lad["rungs"][2]["cool_sessions"] == 5
    assert lad["reentry_size_mult"] == 0.5
    _, _, state = step(engine, _book(), "2026-09-14", 50.0)
    assert state["pm_rules"]["ladder"] == lad
