"""D-01 — the sector-rotation desk (E26).

Three promises:
  * the rule in rotation.py does what the mandate says — 12-1 rank, absolute filter, top-3 or
    TLT, month-end power-hour decision on the holiday-adjusted calendar, a weekly check that
    acts only on a rank-6 drop or a filter flip;
  * pm.py sizes and journals the desk's proposals through the existing pipeline — three
    equal sleeves, marketable next-session fills, rotation-exit for what leaves the set,
    house caps that skip the ETFs but still count them in exposure;
  * the harness replays the same rule and the three live desks write the same bytes as
    before (tests/test_stops.py's golden already pins that; the suite runs unchanged).
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pytest

from conftest import ENGINE, run_pm

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 12-1 returns by construction: closes are 100 until the last 100 sessions, then 100·(1+g),
# so close[-22] / close[-253] - 1 == g exactly. SPY's g decides the absolute filter.
GROWTH = {"XLK": 0.30, "XLF": 0.25, "XLV": 0.20, "XLY": 0.15, "XLP": 0.10, "XLE": 0.05,
          "XLI": 0.00, "XLB": -0.05, "XLU": -0.10, "XLRE": -0.15, "XLC": -0.20, "VEU": 0.12,
          "SPY": 0.18, "TLT": 0.02}


def _sessions(n, last):
    """n trading days (weekday calendar + NYSE holidays) ending on `last`, oldest first."""
    import rotation as r
    out, d = [], last
    while len(out) < n:
        if r.is_trading_day(d):
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def synthetic_bars(last=dt.date(2026, 9, 30), n=300, growth=None, geometric=False):
    """`geometric=False` (the PM tests): closes are 100, then step to 100·(1+g) for the last
    100 sessions — exact prices to assert fills against. `geometric=True` (the harness): a
    constant daily rate so that (1+r)^252 == 1+g — every 12-1 rank and the absolute filter
    are the same on every date of the window, and the equity path is a closed form."""
    growth = growth or GROWTH
    days = _sessions(n, last)
    results = []
    for sym, g in growth.items():
        bars = []
        for i, d in enumerate(days):
            if geometric:
                c = round(100.0 * (1 + g) ** (i / 252.0), 6)
            else:
                c = 100.0 if i < n - 100 else round(100.0 * (1 + g), 4)
            bars.append({"begins_at": d.isoformat() + "T00:00:00Z", "open_price": str(c),
                         "high_price": str(round(c * 1.01, 4)), "low_price": str(round(c * 0.99, 4)),
                         "close_price": str(c), "volume": "1000000", "interpolated": False})
        results.append({"symbol": sym, "bars": bars})
    return {"data": {"results": results}}


@pytest.fixture(autouse=True)
def _restore_shared_rules():
    """pm.use_desk("rotation") writes the desk's caps into portfolio.RULES — a module-level
    dict every later test in this process would otherwise inherit. Snapshot and restore."""
    import copy
    import portfolio
    before = copy.deepcopy(portfolio.RULES)
    yield
    portfolio.RULES.clear()
    portfolio.RULES.update(before)


@pytest.fixture
def rot(run_dir):
    import importlib
    import rotation as r
    importlib.reload(r)
    return r


@pytest.fixture
def bars(rot):
    return rot.load_bars(synthetic_bars())


# ------------------------------------------------------------------ the rule
def test_rank_recovers_the_constructed_12_1_returns(rot, bars):
    ranks = rot.rank(bars, "2026-09-30")
    assert [r["symbol"] for r in ranks[:3]] == ["XLK", "XLF", "XLV"]
    assert ranks[0]["rank"] == 1 and ranks[-1]["symbol"] == "XLC" and ranks[-1]["rank"] == 12
    for r in ranks:
        assert r["ret_12_1"] == pytest.approx(GROWTH[r["symbol"]], abs=1e-9)
    assert "SPY" not in [r["symbol"] for r in ranks] and "TLT" not in [r["symbol"] for r in ranks]


def test_a_short_history_is_unranked_not_zero(rot, bars):
    short = dict(bars, XLK=bars["XLK"][-100:])
    ranks = rot.rank(short, "2026-09-30")
    xlk = [r for r in ranks if r["symbol"] == "XLK"][0]
    assert xlk["ret_12_1"] is None and xlk["rank"] is None
    assert ranks[0]["symbol"] == "XLF" and ranks[-1]["symbol"] == "XLK"


def test_absolute_filter_on_and_off(rot, bars):
    # SPY's 12-month return is +18%: on against a 4% bill, off against a 20% bill.
    assert rot.absolute_filter(bars["SPY"], 4.0, "2026-09-30") is True
    assert rot.absolute_filter(bars["SPY"], 20.0, "2026-09-30") is False
    d = rot.filter_detail(bars["SPY"], None, "2026-09-30")
    assert d["on"] is True and d["tbill_default_used"] is True and "defaulted to 0.0%" in d["note"]
    assert d["spy_ret_12m"] == pytest.approx(0.18, abs=1e-9)
    # an unmeasurable SPY is not a bull market
    assert rot.absolute_filter(bars["SPY"][-50:], 0.0, "2026-09-30") is False


def test_target_book_top3_or_bond(rot, bars):
    ranks = rot.rank(bars, "2026-09-30")
    assert rot.target_book(ranks, True) == ["XLK", "XLF", "XLV"]
    assert rot.target_book(ranks, True, top_n=2) == ["XLK", "XLF"]
    assert rot.target_book(ranks, False) == ["TLT"]
    assert rot.target_book(ranks, False, bond="AGG") == ["AGG"]


def test_rebalance_slot_is_the_last_trading_day_at_power_hour(rot):
    assert rot.is_rebalance_slot("2026-09-30", "power-hour")
    assert not rot.is_rebalance_slot("2026-09-30", "midday")
    assert not rot.is_rebalance_slot("2026-09-29", "power-hour")
    # 2026-05-31 is a Sunday, the 29th a Friday
    assert rot.is_rebalance_slot("2026-05-29", "power-hour")
    # 2026-11-30 is a Monday and a trading day; Thanksgiving does not move month-end
    assert rot.is_rebalance_slot("2026-11-30", "power-hour")
    # a holiday on the last weekday moves the slot back: make Sept 30 a closure
    assert rot.is_rebalance_slot("2026-09-29", "power-hour", calendar={"2026-09-30"})
    assert not rot.is_rebalance_slot("2026-09-30", "power-hour", calendar={"2026-09-30"})
    assert rot.next_rebalance_date("2026-09-10") == dt.date(2026, 9, 30)
    assert rot.next_rebalance_date("2026-09-30") == dt.date(2026, 10, 30)


def test_nyse_holidays_are_the_exchanges_rules(rot):
    h = rot.nyse_holidays(2026)
    assert {dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16), dt.date(2026, 4, 3),
            dt.date(2026, 5, 25), dt.date(2026, 6, 19), dt.date(2026, 7, 3), dt.date(2026, 9, 7),
            dt.date(2026, 11, 26), dt.date(2026, 12, 25)} == h
    assert dt.date(2025, 4, 18) in rot.nyse_holidays(2025)          # Good Friday 2025
    assert dt.date(2022, 1, 1).weekday() == 5 and dt.date(2021, 12, 31) not in rot.nyse_holidays(2021)
    assert not rot.is_trading_day("2026-09-07") and rot.is_trading_day("2026-09-08")


def test_weekly_slot_and_weekly_check(rot, bars):
    assert rot.is_weekly_slot("2026-09-11", "power-hour")          # Friday
    assert not rot.is_weekly_slot("2026-09-10", "power-hour")
    assert rot.is_weekly_slot("2026-04-02", "power-hour")          # Good Friday week: Thursday
    ranks = rot.rank(bars, "2026-09-30")
    # nothing to do: held names in the top 6, filter matches the holdings
    assert rot.weekly_check(["XLK", "XLF", "XLV"], ranks, True) is False
    assert rot.weekly_check(["XLK", "XLP"], ranks, True) is False    # XLP is rank 6: still in
    # a held ETF below rank 6
    assert rot.weekly_check(["XLK", "XLE"], ranks, True) is True     # XLE is rank 7
    # the filter flipped against the holdings
    assert rot.weekly_check(["XLK", "XLF", "XLV"], ranks, False) is True
    assert rot.weekly_check(["TLT"], ranks, True) is True
    assert rot.weekly_check(["TLT"], ranks, False) is False
    # an empty book never triggers the weekly check
    assert rot.weekly_check([], ranks, True) is False


def test_proposals_decide_only_at_the_slot_and_emit_pm_shaped_rows(rot, bars):
    p = rot.proposals(bars, "2026-09-29", ["XLE", "TLT"], "power-hour")
    assert p["acts"] is False and p["rows"] == [] and p["mode"] is None
    assert "next monthly decision 2026-09-30" in p["why"]
    p = rot.proposals(bars, "2026-09-30", ["XLE", "TLT"], "power-hour", macro={"tbill_3m_pct": 4.0})
    assert p["acts"] and p["mode"] == "monthly"
    assert p["target"] == ["XLK", "XLF", "XLV"] and p["exits"] == ["XLE", "TLT"]
    assert p["entries"] == ["XLK", "XLF", "XLV"] and p["tbill_default_used"] is False
    assert p["sleeves_by_symbol"] == {"XLK": 1, "XLF": 1, "XLV": 1}
    for row in p["rows"]:
        assert row["setup"] == "Sector Rotation" and row["verdict"] == "Buy"
        assert row["coverage_pct"] == 100.0 and row["_source"] == "rotation"
        assert row["score"] >= 60 and row["price"] > 0 and row["atr_14"] > 0
        assert row["gics"] == rot.SECTOR_OF[row["ticker"]]
    # the bond leg takes every sleeve, and the default bill rate is logged
    p = rot.proposals(bars, "2026-09-30", ["XLK"], "power-hour", macro={"tbill_3m_pct": 25.0})
    assert p["target"] == ["TLT"] and p["sleeves_by_symbol"] == {"TLT": 3} and p["exits"] == ["XLK"]
    p = rot.proposals(bars, "2026-09-15", [], "midday", force=True)
    assert p["acts"] and p["mode"] == "forced" and any("defaulted to 0.0%" in n for n in p["notes"])
    # the weekly check acts on a Friday only when it has a reason to
    p = rot.proposals(bars, "2026-09-25", ["XLK", "XLF", "XLV"], "power-hour")
    assert p["acts"] is False and "weekly check found nothing" in p["why"]
    p = rot.proposals(bars, "2026-09-25", ["XLK", "XLC"], "power-hour")
    assert p["acts"] and p["mode"] == "weekly" and p["exits"] == ["XLC"]


# ------------------------------------------------------------------ pm.py wiring
def _stage_rotation(run_dir, now, growth=None, positions=None, bars_last=None,
                    activate=False):
    """A rotation run directory: desks.json (inactive unless told otherwise), a seeded book,
    bars_etf.json ending at `bars_last` and fresh broker quotes at the last close."""
    import rotation as r
    d = json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))
    if activate:
        d["desks"]["rotation"]["inactive"] = False
    (run_dir / "desks.json").write_text(json.dumps(d), encoding="utf-8")
    book = r.seed_book(today=now.date())
    for p in positions or []:
        book["positions"].append(p)
        book["cash"] = round(book["cash"] - p["shares"] * p["avg_cost"], 2)
    (run_dir / "paper_book_rotation.json").write_text(json.dumps(book), encoding="utf-8")
    payload = synthetic_bars(last=bars_last or (now.date() - dt.timedelta(days=1)),
                             growth=growth)
    (run_dir / "bars_etf.json").write_text(json.dumps(payload), encoding="utf-8")
    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    quotes = {"data": {"results": []}}
    for res in payload["data"]["results"]:
        px = float(res["bars"][-1]["close_price"])
        quotes["data"]["results"].append({"quote": {
            "symbol": res["symbol"], "state": "active", "has_traded": True,
            "last_trade_price": str(px), "venue_last_trade_time": ts,
            "bid_price": str(round(px * 0.9995, 4)), "ask_price": str(round(px * 1.0005, 4)),
            "venue_bid_time": ts, "venue_ask_time": ts}})
    (run_dir / "pm_quotes.json").write_text(json.dumps(quotes), encoding="utf-8")
    return book


def _run_cli(run_dir, *args):
    env = dict(os.environ, SCAN_DIR=str(run_dir))
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args],
                          capture_output=True, text=True, env=env)


def _journal(run_dir):
    return json.loads((run_dir / "pm_journal_next-rotation.json").read_text(encoding="utf-8"))


def test_pm_cli_places_three_equal_sleeves_at_the_month_end_slot(run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)      # 15:45 ET
    _stage_rotation(run_dir, now)
    r = _run_cli(run_dir, "--allow-inactive", "--desk", "rotation", "--slot", "power-hour",
                 "--now", now.isoformat())
    assert r.returncode == 0, r.stderr + r.stdout
    jrn = _journal(run_dir)["entries"][-1]
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert [d["symbol"] for d in placed] == ["XLK", "XLF", "XLV"]
    notionals = [round(d["shares"] * d["price"], 2) for d in placed]
    assert all(abs(n - 5000.0 / 3) < 1.0 for n in notionals), notionals
    assert all("Sector Rotation" in d["reason"] and "rotation (monthly)" in d["reason"]
               for d in placed)
    assert jrn["rotation"]["acts"] and jrn["rotation"]["mode"] == "monthly"
    assert jrn["rotation"]["target"] == ["XLK", "XLF", "XLV"]
    assert jrn["rotation"]["tbill_default_used"] is True
    assert any("defaulted to 0.0%" in w for w in jrn["warnings"])
    assert jrn["stop_policy"] == "time_catastrophe"
    book = json.loads((run_dir / "pm_book_next-rotation.json").read_text(encoding="utf-8"))
    for o in book["working_orders"]:
        assert o["expires"] == "next-session" and o["fill_rule"] == "marketable"
        assert o["meta"]["stop_policy"] == "time_catastrophe" and o["meta"]["target"] is None
        assert o["meta"]["stop"] == pytest.approx(o["limit_price"] - 3.5 * o["meta"]["atr_14"], abs=0.02)
        assert o["meta"]["rotation"]["sleeves"] == 1 and o["meta"]["rotation"]["of"] == 3
    state = json.loads((run_dir / "pm_state-rotation.json").read_text(encoding="utf-8"))
    assert state["rotation"]["target"] == ["XLK", "XLF", "XLV"]
    # the ETFs are exempt from the house caps and still counted in the house's holdings
    assert not any("House cap" in s["reason"] for s in jrn["skipped"])


def test_pm_refuses_to_enter_on_a_bar_close_without_a_quote(run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now)
    (run_dir / "pm_quotes.json").unlink()
    r = _run_cli(run_dir, "--allow-inactive", "--desk", "rotation", "--slot", "power-hour",
                 "--now", now.isoformat())
    assert r.returncode == 0, r.stderr
    jrn = _journal(run_dir)["entries"][-1]
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert sum(1 for s in jrn["skipped"] if "no fresh broker quote" in s["reason"]) == 3


def test_pm_does_nothing_mid_month_and_force_rebalance_overrides(run_dir):
    now = dt.datetime(2026, 9, 15, 17, 15, tzinfo=dt.timezone.utc)      # midday
    _stage_rotation(run_dir, now)
    r = _run_cli(run_dir, "--allow-inactive", "--desk", "rotation", "--slot", "midday",
                 "--now", now.isoformat())
    assert r.returncode == 0, r.stderr
    jrn = _journal(run_dir)["entries"][-1]
    assert jrn["decisions"] == []
    assert any(s["reason"].startswith("rotation: not a decision slot") for s in jrn["skipped"])
    assert jrn["rotation"]["acts"] is False and jrn["rotation"]["next_rebalance"] == "2026-09-30"
    r = _run_cli(run_dir, "--allow-inactive", "--desk", "rotation", "--slot", "midday",
                 "--now", now.isoformat(), "--force-rebalance")
    assert r.returncode == 0, r.stderr
    jrn = _journal(run_dir)["entries"][-1]
    assert [d["symbol"] for d in jrn["decisions"] if d["action"] == "place-buy"] == ["XLK", "XLF", "XLV"]
    assert jrn["rotation"]["mode"] == "forced"


def test_marketable_orders_survive_the_roll_and_fill_at_the_next_slot(pm, run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now)
    book, jrn, _ = _run_engine(pm, run_dir, "power-hour", now)
    assert len(book["working_orders"]) == 3
    # the next session's first slot: the roll keeps the orders, the fill pass takes them
    nxt = dt.datetime(2026, 10, 1, 12, 45, tzinfo=dt.timezone.utc)
    _restamp_quotes(run_dir, nxt)
    book, jrn2, _ = _run_engine(pm, run_dir, "pre-market", nxt, book=book)
    fills = [d for d in jrn2["decisions"] if d["action"] == "fill-buy"]
    assert [d["symbol"] for d in fills] == ["XLK", "XLF", "XLV"]
    assert not [d for d in jrn2["decisions"] if d["action"] == "expire"]
    assert book["working_orders"] == []
    for p in book["positions"]:
        assert p["stop_policy"] == "time_catastrophe" and p["hold_from"] == "2026-10-01"
        assert p["rotation"]["mode"] == "monthly" and p["target"] is None
    slip = 1 + pm.PM_RULES["exit_slippage_pct"] / 100
    for d in fills:
        assert "marketable" in d["detail"]
    xlk = [p for p in book["positions"] if p["symbol"] == "XLK"][0]
    assert xlk["avg_cost"] == pytest.approx(130.0 * slip, abs=0.01)
    # the desk is now fully deployed in three roughly equal sleeves; the last fill is
    # clamped to the cash the slippage left, which is the pessimism working as designed
    vals = sorted(p["shares"] * p["avg_cost"] for p in book["positions"])
    assert vals[-1] - vals[0] < 15.0 and sum(vals) > 4950 and book["cash"] >= 0
    # a second roll expires nothing (there is nothing left) and nothing new is placed mid-month
    later = dt.datetime(2026, 10, 2, 14, 45, tzinfo=dt.timezone.utc)
    _restamp_quotes(run_dir, later)
    book, jrn3, _ = _run_engine(pm, run_dir, "opening-range", later, book=book)
    assert jrn3["decisions"] == [] and len(book["positions"]) == 3
    assert not any("unjudged" in w for w in jrn3["warnings"])


def test_an_unfilled_next_session_order_expires_on_the_second_roll(pm, run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now)
    book, jrn, _ = _run_engine(pm, run_dir, "power-hour", now)
    assert len(book["working_orders"]) == 3
    # no run at all on Oct 1; Oct 2 rolls a second time
    later = dt.datetime(2026, 10, 2, 14, 45, tzinfo=dt.timezone.utc)
    book["day"]["date"] = "2026-10-01"
    for o in book["working_orders"]:
        o["rolled"] = "2026-10-01"
    _restamp_quotes(run_dir, later)
    book, jrn2, _ = _run_engine(pm, run_dir, "opening-range", later, book=book)
    assert [d["symbol"] for d in jrn2["decisions"] if d["action"] == "expire"] == ["XLK", "XLF", "XLV"]
    assert book["working_orders"] == [] and book["positions"] == []


def test_rotation_exit_sells_what_left_the_set_and_reaffirms_what_stayed(pm, run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    held = [_pos("XLK", 12.0, 130.0, "2026-08-31"), _pos("XLE", 15.0, 105.0, "2026-08-31"),
            _pos("TLT", 16.0, 102.0, "2026-08-31")]
    _stage_rotation(run_dir, now, positions=held)
    book, jrn, state = _run_engine(pm, run_dir, "power-hour", now)
    sells = {d["symbol"]: d for d in jrn["decisions"] if d["action"] == "fill-sell"}
    assert set(sells) == {"XLE", "TLT"}
    assert all(d["reason"] == "rotation-exit" for d in sells.values())
    assert "rank 7" in sells["XLE"]["detail"] and "bond leg is replaced" in sells["TLT"]["detail"]
    closed = {c["symbol"]: c for c in book["closed_trades"]}
    assert closed["XLE"]["reason"] == "rotation-exit"
    placed = [d["symbol"] for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert placed == ["XLF", "XLV"], "XLK is held and re-affirmed, not bought again"
    xlk = [p for p in book["positions"] if p["symbol"] == "XLK"][0]
    assert xlk["hold_from"] == "2026-09-30", "the time stop restarts at the decision"
    assert any("re-affirmed" in s["reason"] for s in jrn["skipped"] if s["symbol"] == "XLK")
    assert jrn["rotation"]["exits"] == ["XLE", "TLT"] and jrn["rotation"]["affirmed"] == ["XLK"]
    # the two new sleeves are sized on the post-exit equity, a third each
    eq = state["book"]["equity"]
    for d in [d for d in jrn["decisions"] if d["action"] == "place-buy"]:
        assert d["shares"] * d["price"] == pytest.approx(eq / 3, abs=2.0)


def test_filter_off_moves_the_whole_book_to_the_bond_leg(pm, run_dir):
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now, positions=[_pos("XLK", 12.0, 130.0, "2026-08-31")])
    (run_dir / "macro.json").write_text(json.dumps({"tbill_3m_pct": 25.0}), encoding="utf-8")
    book, jrn, state = _run_engine(pm, run_dir, "power-hour", now)
    assert [d["symbol"] for d in jrn["decisions"] if d["action"] == "fill-sell"] == ["XLK"]
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert [d["symbol"] for d in placed] == ["TLT"]
    assert placed[0]["shares"] * placed[0]["price"] == pytest.approx(state["book"]["equity"], abs=2.0)
    assert jrn["rotation"]["filter_on"] is False and jrn["rotation"]["tbill_3m_pct"] == 25.0
    assert not any("defaulted" in w for w in jrn["warnings"])


def test_sleeves_scale_by_the_desk_vol_scalar_when_vol_target_is_on(pm, run_dir):
    """Equal thirds × portfolio.desk_vol_scalar(SPY rv_20d). Off by default (scalar 1.0);
    on, a choppy SPY tape shrinks every sleeve by the same factor and the journal carries it."""
    import portfolio
    import technicals
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now)
    # give SPY a ±0.5% saw-tooth over its last 30 sessions (a 1% swing a day, rv_20d ≈ 16%,
    # above the 12% target) so the scalar lands strictly inside the (0.5, 1.5) clamp
    payload = json.loads((run_dir / "bars_etf.json").read_text(encoding="utf-8"))
    spy = [r for r in payload["data"]["results"] if r["symbol"] == "SPY"][0]
    for i, b in enumerate(spy["bars"][-30:]):
        c = round(118.0 * (1 + 0.005 * (1 if i % 2 else -1)), 4)
        b["close_price"] = b["open_price"] = str(c)
        b["high_price"], b["low_price"] = str(round(c * 1.01, 4)), str(round(c * 0.99, 4))
    (run_dir / "bars_etf.json").write_text(json.dumps(payload), encoding="utf-8")
    rv = technicals.features(spy["bars"])["rv_20d"]
    scalar = portfolio.desk_vol_scalar(rv, 12.0, 0.5, 1.5)
    assert 0.5 < scalar < 1.0, (rv, scalar)

    # off: three full thirds, scalar 1.0
    _, jrn, _ = _run_engine(pm, run_dir, "power-hour", now)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert jrn["rotation"]["vol_scalar"] == 1.0
    assert all(abs(d["shares"] * d["price"] - 5000.0 / 3) < 1.0 for d in placed)

    # on: every sleeve is a third times the scalar
    portfolio.RULES["vol_target"] = {"enabled": True, "target_vol_pct": 12.0, "lo": 0.5, "hi": 1.5}
    _stage_rotation(run_dir, now)
    (run_dir / "bars_etf.json").write_text(json.dumps(payload), encoding="utf-8")
    book, jrn, _ = _run_engine(pm, run_dir, "power-hour", now)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert [d["symbol"] for d in placed] == ["XLK", "XLF", "XLV"]
    assert jrn["rotation"]["vol_scalar"] == pytest.approx(scalar, abs=1e-4)
    assert jrn["rotation"]["spy_rv_20d"] == pytest.approx(rv, abs=1e-9)
    for d in placed:
        assert d["shares"] * d["price"] == pytest.approx(5000.0 / 3 * scalar, abs=1.0)
    for o in book["working_orders"]:
        assert o["meta"]["rotation"]["vol_scalar"] == pytest.approx(scalar, abs=1e-4)


def test_house_caps_skip_the_etfs_but_exposure_counts_them(pm, run_dir, quotes):
    # the swing desk, with the rotation book staged as a peer holding a large XLK sleeve
    d = json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))
    d["desks"]["rotation"]["inactive"] = False
    (run_dir / "desks.json").write_text(json.dumps(d), encoding="utf-8")
    import rotation as r
    rb = r.seed_book(today=dt.date(2026, 9, 2))
    rb["positions"] = [_pos("XLK", 30.0, 130.0, "2026-08-31")]
    rb["cash"] = 5000.0 - 30.0 * 130.0
    (run_dir / "paper_book_rotation.json").write_text(json.dumps(rb), encoding="utf-8")
    _, _, state = run_pm(pm, run_dir, slot="sentinel")
    h = state["house"]
    assert "rotation" in h["peers_loaded"] and h["desk_count"] == 4
    assert "XLK" not in h["by_symbol"], "an ETF is not a name the per-name cap counts"
    assert h["by_sector"].get("Information Technology", 0) == pytest.approx(
        sum(x["notional"] for x in h["holdings"]
            if x["sector"] == "Information Technology" and x["symbol"] != "XLK"), abs=0.05)
    assert [x for x in h["holdings"] if x["symbol"] == "XLK"], "exposure still sees it"
    assert h["exposure"]["names"] >= 1
    assert h["equity"] == pytest.approx(14926.57 + 3900.0 + 1100.0, abs=0.05)
    # and on the rotation desk's own entries the cap never bites, however tight
    assert pm.house_block(h, "XLK", "Information Technology", 1e9) is not None
    now = dt.datetime(2026, 9, 30, 19, 45, tzinfo=dt.timezone.utc)
    _stage_rotation(run_dir, now, activate=True)
    pm.PM_RULES["house_max_symbol_pct"] = 0.5
    pm.PM_RULES["house_max_sector_pct"] = 0.5
    book, jrn, _ = _run_engine(pm, run_dir, "power-hour", now)
    assert [d["symbol"] for d in jrn["decisions"] if d["action"] == "place-buy"] == ["XLK", "XLF", "XLV"]


def test_the_three_live_desks_never_see_the_rotation_path(pm, run_dir, quotes, scan):
    """A scan row that claims to be a rotation row is just a row to the swing desk, and the
    swing desk's power-hour still places nothing."""
    s = json.loads((run_dir / "scan_results.json").read_text(encoding="utf-8"))
    s["results"][0]["_source"] = "rotation"
    s["rotation"] = {"acts": True, "target": ["SCHW"], "mode": "forced"}
    (run_dir / "scan_results.json").write_text(json.dumps(s), encoding="utf-8")
    _, jrn, _ = run_pm(pm, run_dir, slot="power-hour", with_scan=True)
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert any("power-hour places no new entries" in x["reason"] for x in jrn["skipped"])
    assert "rotation" not in jrn
    assert pm.rotation_desk() is False


def test_inactive_by_default_and_activation_is_one_line(run_dir):
    d = json.loads((ENGINE / "desks.json").read_text(encoding="utf-8"))["desks"]["rotation"]
    assert d["inactive"] is True
    assert d["filter"] == {"universe": "sector-etfs"}
    assert d["rules"]["stop_policy"] == "time_catastrophe"
    assert d["rules"]["stop_params"] == {"k_cat": 3.5, "max_sessions": 25, "target_pct": None}
    assert d["rules"]["rotation"] == {"top_n": 3, "sleeves": 3, "bond_sleeves": 3,
                                      "drop_below_rank": 6, "bond": "TLT"}
    r = _run_cli(run_dir, "--desk", "rotation")
    assert r.returncode == 2 and "INACTIVE" in r.stderr


# ------------------------------------------------------------------ the harness
def _close_on(bars, sym, date):
    return [float(b["close_price"]) for b in bars[sym] if b["begins_at"][:10] == date][0]


def test_replay_reproduces_the_expected_equity_path(rot):
    """Geometric growth at a constant daily rate per symbol: every 12-1 rank and the absolute
    filter are the same on every date, so the rule decides once — the January month-end
    (2026-01-30) — buys XLK/XLF/XLV a third each at the next open (2026-02-02) and re-affirms
    them at every later decision. The equity path is the equal-weighted mix of the three
    price paths from that open, and the whole summary follows from the bars in closed form."""
    bars = rot.load_bars(synthetic_bars(last=dt.date(2026, 9, 30), n=500, geometric=True))
    out = rot.replay(bars, "2026-01-01", "2026-09-30", tbill_series=4.0, cost_bps=0.0)
    assert out["start"] == "2026-01-02" and out["end"] == "2026-09-30"
    assert out["n_month_ends"] == 9 and out["n_decisions"] == 9 and out["n_rebalances"] == 1
    assert out["bond_months"] == 0, "SPY +18% beats a 4% bill in every month here"
    log = out["log"]
    assert len(log) == 1 and log[0]["date"] == "2026-02-02" and log[0]["mode"] == "monthly"
    assert log[0]["target"] == ["XLK", "XLF", "XLV"] and log[0]["turnover"] == pytest.approx(0.5)
    # the book is cash through January, then tracks the three from the Feb 2 open
    curve = {p["date"]: p["equity"] for p in out["equity_curve"]}
    assert curve["2026-01-30"] == 1.0 and out["monthly_returns"]["2026-01"] == 0.0
    expected = sum(_close_on(bars, s, "2026-09-30") / _close_on(bars, s, "2026-02-02")
                   for s in ("XLK", "XLF", "XLV")) / 3
    assert curve["2026-09-30"] == pytest.approx(expected, abs=1e-6)
    assert out["total_return_pct"] == pytest.approx((expected - 1) * 100, abs=1e-3)
    assert out["monthly_returns"]["2026-02"] > 0 and out["n_months"] == 9
    assert out["max_dd_pct"] == 0.0, "three rising legs never draw down"
    assert out["turnover"] == {"mean_one_way": 0.5, "total_one_way": 0.5, "n": 1}
    assert out["corr_with_spy"] is None or -1.0 <= out["corr_with_spy"] <= 1.0
    assert out["tbill_default_used"] is False
    for key in ("monthly_returns", "equity_curve", "max_dd", "n_rebalances", "corr_with_spy",
                "turnover"):
        assert key in out
    # no Sharpe, no win rate: the only place the words appear is the warning that says so
    keys = " ".join(k.lower() for k in out) + " " + " ".join(k.lower() for k in out["turnover"])
    assert "sharpe" not in keys and "win" not in keys
    assert any("No Sharpe and no win rate" in w for w in out["_warnings"])
    # a bill rate above SPY's return sends the book to TLT (all three sleeves) at every decision
    out2 = rot.replay(bars, "2026-01-01", "2026-09-30", tbill_series=25.0, cost_bps=0.0)
    assert out2["bond_months"] == out2["n_month_ends"] == 9 and out2["n_rebalances"] == 1
    assert out2["log"][0]["target"] == ["TLT"] and out2["log"][0]["turnover"] == pytest.approx(0.5)
    expected2 = _close_on(bars, "TLT", "2026-09-30") / _close_on(bars, "TLT", "2026-02-02")
    assert out2["total_return_pct"] == pytest.approx((expected2 - 1) * 100, abs=1e-3)
    assert out2["tbill_default_used"] is False
    # a {date: pct} series is looked up as-of; no series means 0 and the summary says so
    out3 = rot.replay(bars, "2026-01-01", "2026-09-30",
                      tbill_series={"2025-01-01": 4.0, "2026-06-01": 25.0}, cost_bps=0.0)
    assert out3["bond_months"] == 4 and out3["n_rebalances"] == 2
    assert [e["target"] for e in out3["log"]] == [["XLK", "XLF", "XLV"], ["TLT"]]
    # the flip lands mid-month, so it is the WEEKLY check that moves the book to the bond
    # leg (the first Friday after the bill jumped), not the June month-end
    assert out3["log"][1]["mode"] == "weekly" and out3["log"][1]["date"] == "2026-06-08"
    assert rot.replay(bars, "2026-01-01", "2026-09-30", weekly=False,
                      tbill_series={"2025-01-01": 4.0, "2026-06-01": 25.0},
                      cost_bps=0.0)["log"][1]["date"] == "2026-07-01"
    out4 = rot.replay(bars, "2026-01-01", "2026-09-30")
    assert out4["tbill_default_used"] is True and any("defaulted" in w for w in out4["_warnings"])
    assert out4["bond_months"] == 0
    # without SPY there is no calendar and no filter
    with pytest.raises(ValueError):
        rot.replay({k: v for k, v in bars.items() if k != "SPY"}, "2026-01-01", "2026-09-30")


def test_replay_charges_the_stated_cost_on_every_trade(rot):
    bars = rot.load_bars(synthetic_bars(last=dt.date(2026, 9, 30), n=500, geometric=True))
    free = rot.replay(bars, "2026-01-01", "2026-09-30", tbill_series=4.0, cost_bps=0.0)
    paid = rot.replay(bars, "2026-01-01", "2026-09-30", tbill_series=4.0, cost_bps=10.0)
    assert paid["total_return_pct"] < free["total_return_pct"]
    assert paid["equity_curve"][-1]["equity"] == pytest.approx(
        free["equity_curve"][-1]["equity"] * (1 - 0.001), rel=1e-6)


def test_corr_with_another_curve(rot):
    a = [{"date": f"2026-01-{d:02d}", "equity": 100 + d} for d in range(1, 31)]
    b = [{"date": f"2026-01-{d:02d}", "equity": 200 + 2 * d} for d in range(1, 31)]
    c = rot.corr_with(a, b)
    assert c["n"] == 29 and c["corr"] == pytest.approx(1.0, abs=1e-6) and c["not_a_sample"]
    # a paper book's curve keeps the last slot of each date
    book = {"equity_curve": [{"date": "2026-01-01", "slot": "midday", "equity": 5.0},
                             {"date": "2026-01-01", "slot": "power-hour", "equity": 6.0},
                             {"date": "2026-01-02", "slot": "midday", "equity": 7.0}]}
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(book, f)
    assert rot.load_equity_curve(f.name) == [{"date": "2026-01-01", "equity": 6.0},
                                             {"date": "2026-01-02", "equity": 7.0}]


def test_backtest_cli_desk_rotation_writes_a_summary_and_a_ledger_row(run_dir):
    import backtest, importlib, ledger
    importlib.reload(backtest)
    payload = synthetic_bars(last=dt.date(2026, 9, 30), n=420)
    (run_dir / "bars_etf.json").write_text(json.dumps(payload), encoding="utf-8")
    other = {"equity_curve": [{"date": b["begins_at"][:10], "equity": float(b["close_price"])}
                              for b in payload["data"]["results"][0]["bars"]]}
    (run_dir / "other.json").write_text(json.dumps(other), encoding="utf-8")
    led = run_dir / "ledger.jsonl"
    argv = ["--desk", "rotation", "--bars", str(run_dir / "bars_etf.json"),
            "--start", "2026-01-01", "--end", "2026-09-30", "--summary", "rotation.json",
            "--tbill-pct", "4.0", "--corr-with", str(run_dir / "other.json"),
            "--ledger", str(led)]
    assert backtest.main(argv) == 0
    out = json.loads((run_dir / "rotation.json").read_text(encoding="utf-8"))
    assert out["n_rebalances"] >= 1 and out["corr_with_other"]["n"] > 0
    assert "acceptance_rho_lt_0_6" in out["corr_with_other"]
    rows = ledger.trials(str(led))
    assert len(rows) == 1 and rows[0]["id"] == "E26" and rows[0]["n_trials_to_date"] == 1
    assert rows[0]["in_sample"]["max_dd_pct"] == out["max_dd_pct"]
    assert rows[0]["in_sample"]["corr_with_other"] == out["corr_with_other"]["corr"]
    assert rows[0]["horizons"] == [] and rows[0]["n"] == out["n_months"]
    assert "Sharpe" not in json.dumps(rows[0]["in_sample"])
    # the ordinary replay still demands --out-records
    with pytest.raises(SystemExit):
        backtest.main(["--bars", str(run_dir / "bars_etf.json"), "--start", "2026-01-01",
                       "--end", "2026-09-30"])


# ------------------------------------------------------------------ helpers
def _pos(sym, shares, cost, opened):
    import rotation as r
    return {"symbol": sym, "shares": shares, "avg_cost": cost, "opened": opened,
            "opened_slot": "opening-range", "intraday_shares": 0.0,
            "stop": round(cost - 3.5 * 1.5, 2), "target": None,
            "stop_basis": "catastrophe only: 3.5x ATR", "stop_basis_kind": "catastrophe",
            "stop_basis_short": "catastrophe", "atr_14": 1.5, "atr_pct": 1.2, "stop_pct": 4.0,
            "entry_score": 85.0, "trim_count": 0, "last_trim_date": None,
            "rebalance_count": 0, "last_rebalance_date": None,
            "gics": r.SECTOR_OF[sym], "industry": "Sector ETF", "thesis": None,
            "scaled_out": False, "high_water": cost, "last_price": cost, "last_priced": None,
            "stop_policy": "time_catastrophe", "initial_risk": 5.25,
            "initial_risk_usd": round(5.25 * shares, 2), "sessions_held": 0,
            "highest_close": cost, "trail_level": None,
            "rotation": {"sleeves": 1, "of": 3, "mode": "monthly"}, "hold_from": opened}


def _restamp_quotes(run_dir, now):
    q = json.loads((run_dir / "pm_quotes.json").read_text(encoding="utf-8"))
    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for row in q["data"]["results"]:
        row["quote"]["venue_last_trade_time"] = ts
        row["quote"]["venue_bid_time"] = row["quote"]["venue_ask_time"] = ts
    (run_dir / "pm_quotes.json").write_text(json.dumps(q), encoding="utf-8")


def _run_engine(pm, run_dir, slot, now, book=None):
    """Drive pm.run the way main() does for the rotation desk: the synthetic scan comes from
    bars_etf.json, the quotes from pm_quotes.json, the peers from desks.json."""
    pm.use_desk("rotation", "desks.json", allow_inactive=True)
    pm.PEERS.update({"books": {}, "loaded": [], "missing": []})
    pm.load_peers("desks.json", "rotation", "paper_book_rotation.json")
    if book is None:
        book = json.loads((run_dir / "paper_book_rotation.json").read_text(encoding="utf-8"))
    quotes = json.loads((run_dir / "pm_quotes.json").read_text(encoding="utf-8"))
    prices, _ = pm.quotes_to_prices(quotes, now)
    macro = json.loads((run_dir / "macro.json").read_text(encoding="utf-8")) \
        if (run_dir / "macro.json").exists() else None
    scan = pm.rotation_scan(json.loads((run_dir / "bars_etf.json").read_text(encoding="utf-8")),
                            macro, book, slot, now)
    ts = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return pm.run(book, scan, prices, slot, ts, "paper")
