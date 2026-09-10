"""D-03 — the stocks-in-play opening-range-breakout desk (engine/orb.py, E24).

Pure-function tests on synthetic 5-minute bars, then the pm.py wiring driven the way the
09:35 / 10:35 sentinels and the power-hour slot would drive it, on the INACTIVE orb
template with --allow-inactive semantics (pm.use_desk(..., allow_inactive=True)).
"""
import datetime as dt
import json
import pathlib

import pytest

from conftest import ENGINE, FIX

import orb

DAY = "2026-09-10"          # a Thursday; EDT, so 09:30 ET is 13:30Z


# ------------------------------------------------------------------ synthetic bars
def _utc(date, hhmm):
    """RFC-3339 UTC for an ET wall-clock time on an EDT date (UTC−4)."""
    h, m = map(int, hhmm.split(":"))
    return f"{date}T{h + 4:02d}:{m:02d}:00Z"


def bar(date, hhmm, o, h, l, c, v):
    return {"begins_at": _utc(date, hhmm), "open_price": o, "high_price": h, "low_price": l,
            "close_price": c, "volume": v, "interpolated": False}


def session_dates(n, end=DAY):
    """The `n` weekday dates ending at `end`, oldest first."""
    d = dt.date.fromisoformat(end)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def flat_day(date, px=50.0, vol=10_000, buckets=None):
    """A quiet session: every bucket a flat bar at `px` with `vol` shares."""
    buckets = buckets or ["09:30", "09:35", "09:40", "09:45", "10:30", "12:00", "15:45", "15:55"]
    return [bar(date, b, px, px, px, px, vol) for b in buckets]


def history(sym, days=14, px=50.0, vol=10_000):
    """14 quiet prior sessions for one symbol (the RVOL baseline)."""
    return [b for d in session_dates(days + 1)[:-1] for b in flat_day(d, px, vol)]


def rh_shape(by_sym, interval="5minute"):
    return {"data": {"results": [{"symbol": s, "interval": interval, "bars": bars}
                                 for s, bars in by_sym.items()]}}


def daily_bars(sym, n=30, px=50.0, rng=1.0, vol=2_000_000, end=DAY):
    """`n` daily bars STRICTLY before `end` with a constant true range `rng` (so ATR14 =
    rng) and volume `vol` (so ADV14 = vol)."""
    out = []
    for d in session_dates(n + 1, end)[:-1]:
        out.append({"begins_at": f"{d}T04:00:00Z", "open_price": px, "high_price": px + rng / 2,
                    "low_price": px - rng / 2, "close_price": px, "volume": vol})
    return out


@pytest.fixture
def universe():
    """Three names with 14 quiet sessions, then today's opening bar:
    BIG   — 3× its bucket average, green candle 49.5→50.5 (high 50.6, low 49.4)
    MID   — 2× average, RED candle
    FLAT  — 1× average, doji."""
    b5 = {
        "BIG": history("BIG") + [bar(DAY, "09:30", 49.5, 50.6, 49.4, 50.5, 30_000)],
        "MID": history("MID") + [bar(DAY, "09:30", 50.5, 50.7, 49.8, 49.9, 20_000)],
        "FLAT": history("FLAT") + [bar(DAY, "09:30", 50.0, 50.3, 49.7, 50.0, 10_000)],
    }
    daily = {s: daily_bars(s, rng=1.0) for s in b5}
    return b5, daily


# ------------------------------------------------------------------ 1. RVOL and ranking
def test_opening_rvol_ranks_the_three_times_name_first(universe):
    b5, daily = universe
    rv = orb.opening_rvol(b5, DAY)
    assert rv["BIG"]["rvol"] == pytest.approx(3.0)
    assert rv["MID"]["rvol"] == pytest.approx(2.0)
    assert rv["FLAT"]["rvol"] == pytest.approx(1.0)
    assert rv["BIG"]["n_sessions"] == 14
    ranked = orb.stocks_in_play(["FLAT", "MID", "BIG"], b5, daily, today=DAY)
    assert [s["symbol"] for s in ranked] == ["BIG", "MID", "FLAT"]
    assert ranked[0]["rank"] == 1 and ranked[0]["or_high"] == 50.6 and ranked[0]["or_low"] == 49.4
    assert ranked[0]["atr14"] == pytest.approx(1.0) and ranked[0]["adv14"] == 2_000_000
    assert ranked[0]["stats_source"] == "daily_bars"


def test_rvol_uses_the_same_bucket_only_and_needs_a_baseline(universe):
    b5, _ = universe
    # a name with only three prior sessions has no honest baseline
    short = {"NEW": [b for d in session_dates(4)[:-1] for b in flat_day(d)]
             + [bar(DAY, "09:30", 50, 51, 49, 50.5, 90_000)]}
    assert orb.opening_rvol(short, DAY)["NEW"]["rvol"] is None
    # a huge 09:35 bar does not move the 09:30 baseline
    b5["FLAT"].append(bar(DAY, "09:35", 50, 50, 50, 50, 900_000))
    assert orb.opening_rvol(b5, DAY)["FLAT"]["rvol"] == pytest.approx(1.0)


def test_robinhood_shape_and_interval_guard(universe):
    b5, _ = universe
    by_sym, notes = orb.bars_by_symbol(rh_shape(b5))
    assert set(by_sym) == {"BIG", "MID", "FLAT"}
    assert by_sym["BIG"][-1]["bucket"] == "09:30" and by_sym["BIG"][-1]["date"] == DAY
    # a list of responses concatenates; a daily block is refused with a note
    two = [rh_shape({"BIG": b5["BIG"][:10]}), rh_shape({"BIG": b5["BIG"][10:]})]
    assert len(orb.bars_by_symbol(two)[0]["BIG"]) == len(b5["BIG"])
    _, notes = orb.bars_by_symbol(rh_shape({"BIG": b5["BIG"]}, interval="day"))
    assert notes and "not 5minute" in notes[0]


# ------------------------------------------------------------------ 2. filters and direction
def test_filters_price_adv_and_atr(universe):
    b5, daily = universe
    b5["CHEAP"] = history("CHEAP", px=4.0) + [bar(DAY, "09:30", 4.0, 4.6, 3.9, 4.5, 30_000)]
    daily["CHEAP"] = daily_bars("CHEAP", px=4.0, rng=1.0)
    b5["THIN"] = history("THIN") + [bar(DAY, "09:30", 49.5, 50.6, 49.4, 50.5, 30_000)]
    daily["THIN"] = daily_bars("THIN", vol=500_000)
    b5["DEAD"] = history("DEAD") + [bar(DAY, "09:30", 49.5, 50.6, 49.4, 50.5, 30_000)]
    daily["DEAD"] = daily_bars("DEAD", rng=0.3)
    in_play, rejected = orb.screen(list(b5), b5, daily, today=DAY)
    assert {s["symbol"] for s in in_play} == {"BIG", "MID", "FLAT"}
    why = {r["symbol"]: r["reason"] for r in rejected}
    assert "price" in why["CHEAP"] and "$5.00 floor" in why["CHEAP"]
    assert "ADV" in why["THIN"]
    assert "ATR14" in why["DEAD"]


def test_scan_row_stats_are_the_fallback_when_no_daily_bars(universe):
    b5, _ = universe
    rows = [{"ticker": "BIG", "atr_14": 0.8, "avg_volume_20d": 3_000_000, "gics": "Energy"}]
    s = orb.stocks_in_play(rows, b5, None, today=DAY)
    assert s[0]["atr14"] == 0.8 and s[0]["adv14"] == 3_000_000
    assert s[0]["stats_source"] == "scan_row" and s[0]["gics"] == "Energy"
    # no stats anywhere: rejected, not sized on a guess
    _, rej = orb.screen(["BIG"], b5, None, today=DAY)
    assert rej and "no daily bars" in rej[0]["reason"]


def test_direction_green_long_red_skip_doji_skip(universe):
    b5, daily = universe
    s = {x["symbol"]: x for x in orb.stocks_in_play(list(b5), b5, daily, today=DAY)}
    assert s["BIG"]["direction"] == "long"
    assert s["MID"]["direction"] == "short"
    assert s["FLAT"]["direction"] == "doji"
    orders, notes = orb.size_entries(list(s.values()), 5000.0)
    assert [o["symbol"] for o in orders] == ["BIG"]
    why = {n["symbol"]: n["reason"] for n in notes}
    assert "long-only" in why["MID"] and "red" in why["MID"]
    assert "doji" in why["FLAT"]


def test_top_n_cut(universe):
    b5, daily = universe
    in_play, rej = orb.screen(list(b5), b5, daily, {"top_n": 2}, today=DAY)
    assert [s["symbol"] for s in in_play] == ["BIG", "MID"]
    assert any(r["symbol"] == "FLAT" and "top 2" in r["reason"] for r in rej)


# ------------------------------------------------------------------ 3. sizing
def test_stop_is_ten_percent_of_atr_and_size_is_one_percent_risk():
    s = {"symbol": "X", "rank": 1, "rvol": 3.0, "or_high": 100.0, "or_low": 98.0, "price": 99.5,
         "direction": "long", "atr14": 4.0, "adv14": 2e6, "date": DAY}
    orders = orb.stop_entries([s], 100_000.0)
    o = orders[0]
    assert o["type"] == "stop-buy" and o["side"] == "buy" and o["status"] == "working"
    assert o["trigger_price"] == 100.01 and o["limit_price"] == 100.01
    assert o["meta"]["stop"] == pytest.approx(100.01 - 0.4, abs=1e-6)
    assert o["meta"]["initial_risk"] == pytest.approx(0.4, abs=1e-6)
    # 1% of 100k = $1,000 at $0.40 a share = 2,500 sh = $250k, over the 25% cap = $25k
    assert o["meta"]["orb"]["notional_cap_bound"] is True
    assert o["shares"] == 249.0 and o["notional"] == pytest.approx(249 * 100.01, abs=0.01)
    assert o["meta"]["stop_policy"] == "chandelier"
    assert o["meta"]["stop_params"]["k_init"] == 0.1 and o["meta"]["stop_params"]["k_trail"] == 0.5
    assert o["cancel_after_et"] == "10:30" and o["valid_from_et"] == "09:35"


def test_notional_cap_binds_on_a_low_atr_name_and_is_logged():
    lo = {"symbol": "LO", "rank": 1, "rvol": 3.0, "or_high": 19.99, "or_low": 19.5, "price": 19.9,
          "direction": "long", "atr14": 0.6, "adv14": 2e6, "date": DAY}
    hi = {"symbol": "HI", "rank": 2, "rvol": 2.0, "or_high": 99.99, "or_low": 95.0, "price": 99.0,
          "direction": "long", "atr14": 20.0, "adv14": 2e6, "date": DAY}
    orders, notes = orb.size_entries([lo, hi], 5000.0, avail_cash=10_000.0)
    by = {o["symbol"]: o for o in orders}
    # LO: risk $50 / $0.06 = 833 sh = $16,667 → capped at $1,250 → 62 whole shares
    assert by["LO"]["meta"]["orb"]["notional_cap_bound"] is True
    assert by["LO"]["shares"] == 62.0 and by["LO"]["notional"] <= 1250.0
    assert any(n["symbol"] == "LO" and "notional cap binds" in n["reason"] for n in notes)
    # HI: risk $50 / $2.00 = 25 sh = $2,500 > cap $1,250 → 12 shares; risk well under 1%
    assert by["HI"]["shares"] == 12.0
    assert by["HI"]["meta"]["dollar_risk"] == pytest.approx(24.0)
    # whole shares only: Robinhood takes no fractional stop orders
    assert all(float(o["shares"]).is_integer() for o in orders)


def test_max_concurrent_and_cash_bound():
    names = [{"symbol": f"S{i}", "rank": i, "rvol": 9 - i, "or_high": 10.0, "or_low": 9.5,
              "price": 9.9, "direction": "long", "atr14": 1.0, "adv14": 2e6, "date": DAY}
             for i in range(1, 8)]
    orders, notes = orb.size_entries(names, 5000.0, avail_cash=5000.0)
    assert len(orders) == 5 and sum(o["notional"] for o in orders) <= 5000.0
    assert orders[-1]["meta"]["orb"]["cash_bound"] and orders[-1]["shares"] < 10, \
        "25% × 5 names is 125% of equity: cash binds at the fifth"
    orders, _ = orb.size_entries(names, 5000.0, avail_cash=4_970.0)
    assert len(orders) == 4, "no cash left for a single share: skipped, not sized to zero"
    orders, notes = orb.size_entries(names, 5000.0, avail_cash=50_000.0, open_count=3)
    assert len(orders) == 2
    assert sum("max 5 concurrent" in n["reason"] for n in notes) == 5
    orders, _ = orb.size_entries(names, 5000.0, avail_cash=50_000.0, max_new=2)
    assert len(orders) == 2, "PM_RULES max_new_entries_per_run still caps a run"


# ------------------------------------------------------------------ 4. stop-buy fills
def _order(trigger=50.61, stop=50.51):
    return {"symbol": "BIG", "type": "stop-buy", "trigger_price": trigger, "limit_price": trigger,
            "shares": 10.0, "valid_from_et": "09:35", "cancel_after_et": "10:30",
            "placed": _utc(DAY, "09:35"), "meta": {"stop": stop, "orb": {"date": DAY}}}


def _bars(*specs):
    return [orb._norm_bar(bar(DAY, *s)) for s in specs]


def test_stop_buy_fills_at_the_trigger_when_the_bar_trades_through_it():
    bars = _bars(("09:30", 49.5, 50.6, 49.4, 50.5, 1), ("09:35", 50.5, 50.55, 50.3, 50.4, 1),
                 ("09:40", 50.45, 50.9, 50.4, 50.8, 1))
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "10:35")),
                             {"fill_half_spread_bps": 0.0})
    assert res["status"] == "fill" and res["price"] == 50.61
    assert res["bar"].startswith(f"{DAY}T09:40")


def test_stop_buy_fills_at_the_open_on_a_gap_and_pays_the_haircut():
    bars = _bars(("09:35", 50.5, 50.55, 50.3, 50.4, 1), ("09:40", 51.0, 51.3, 50.9, 51.2, 1))
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "10:35")),
                             {"fill_half_spread_bps": 5.0, "fill_k": 1.0})
    assert res["base"] == 51.0 and res["price"] == pytest.approx(51.0 * (1 + 5 / 10_000), abs=1e-4)
    # with a two-sided quote the haircut is k half-spreads of THAT quote
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "10:35")), None,
                             bid=50.98, ask=51.02)
    assert res["price"] == pytest.approx(51.0 + 0.02, abs=1e-6)


def test_stop_buy_does_not_fill_before_the_trigger_or_on_the_opening_bar():
    # the 09:30 bar's own high is the OR high — it cannot fill the order placed above it
    bars = _bars(("09:30", 49.5, 50.6, 49.4, 50.5, 1), ("09:35", 50.5, 50.6, 50.3, 50.4, 1),
                 ("09:40", 50.45, 50.55, 50.4, 50.5, 1))
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "09:50")))
    assert res["status"] == "working"
    # a bar still forming at `now` is not evidence
    bars = _bars(("09:35", 50.5, 50.55, 50.3, 50.4, 1), ("09:40", 50.45, 50.9, 50.4, 50.8, 1))
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "09:42")))
    assert res["status"] == "working"


def test_stop_buy_unfilled_is_cancelled_after_1030_and_a_late_hit_does_not_count():
    bars = _bars(("09:35", 50.5, 50.55, 50.3, 50.4, 1), ("10:25", 50.4, 50.5, 50.3, 50.4, 1),
                 ("10:30", 50.4, 50.9, 50.3, 50.8, 1))     # the 10:30 bar is past validity
    res = orb.check_stop_buy(_order(), bars, orb.to_et(_utc(DAY, "10:35")))
    assert res["status"] == "cancel" and "10:30" in res["reason"]
    res = orb.check_stop_buy(_order(), bars[:2], orb.to_et(_utc(DAY, "10:29")))
    assert res["status"] == "working"


# ------------------------------------------------------------------ 5. management
def test_chandelier_trails_half_atr_under_the_highest_five_minute_high():
    pos = {"symbol": "BIG", "avg_cost": 50.61, "stop": 50.51, "atr_14": 1.0,
           "highest_high": 50.61}
    bars = _bars(("09:45", 50.7, 51.0, 50.6, 50.9, 1), ("09:50", 50.9, 51.6, 50.8, 51.5, 1),
                 ("09:55", 51.4, 51.5, 51.15, 51.2, 1))
    m = orb.manage_position(pos, bars, {"k_trail": 0.5})
    # the 09:50 HIGH 51.6 is the anchor (its close 51.5 would have put the stop at 51.0)
    assert m["highest_high"] == 51.6 and m["stop"] == 51.1 and m["raised"] is True
    assert m["exit_reason"] is None
    assert pos["stop"] == 50.51, "manage_position mutates nothing"
    # the trail never comes down: a lower bar later leaves the stop where it was
    m2 = orb.manage_position(dict(pos, stop=51.1, highest_high=51.6, stop_basis_kind="trail"),
                             _bars(("10:00", 51.2, 51.3, 51.15, 51.1, 1)), {"k_trail": 0.5})
    assert m2["stop"] == 51.1 and m2["raised"] is False
    # …and a low through it is a trail exit at the stop
    m3 = orb.manage_position(dict(pos, stop=51.1, highest_high=51.6, stop_basis_kind="trail"),
                             _bars(("10:05", 51.15, 51.2, 50.9, 50.95, 1)), {"k_trail": 0.5})
    assert m3["exit_reason"] == "trail" and m3["exit_price"] == 51.1
    # within ONE bar the low is tested before the high can raise the trail: a bar that
    # spikes to 53 and then breaks 51.1 is a stop-out, not a raise
    m4 = orb.manage_position(dict(pos, stop=51.1, highest_high=51.6, stop_basis_kind="trail"),
                             _bars(("10:10", 51.5, 53.0, 51.0, 52.0, 1)), {"k_trail": 0.5})
    assert m4["exit_reason"] == "trail" and m4["exit_price"] == 51.1
    # a position that pre-dates the field seeds the anchor from highest_close, then the entry
    m5 = orb.manage_position({"avg_cost": 50.61, "stop": 50.51, "atr_14": 1.0,
                              "highest_close": 51.0}, _bars(("10:15", 51.0, 51.0, 51.0, 51.0, 1)))
    assert m5["highest_high"] == 51.0 and m5["stop"] == 50.51


def test_initial_stop_hit_and_gap_through_exit_at_the_open():
    pos = {"symbol": "BIG", "avg_cost": 50.61, "stop": 50.51, "atr_14": 1.0, "highest_high": 50.61}
    m = orb.manage_position(pos, _bars(("09:45", 50.55, 50.6, 50.45, 50.5, 1)))
    assert m["exit_reason"] == "stop" and m["exit_price"] == 50.51
    m = orb.manage_position(pos, _bars(("09:45", 50.2, 50.3, 50.1, 50.2, 1)))
    assert m["exit_reason"] == "stop" and m["exit_price"] == 50.2, "gap through: the open"
    out = orb.manage([pos], {"BIG": _bars(("09:45", 50.2, 50.3, 50.1, 50.2, 1))})
    assert out["BIG"]["exit_reason"] == "stop"


def test_entry_window_is_the_0935_sentinel_only():
    assert orb.in_entry_window(_utc(DAY, "09:35"))
    assert orb.in_entry_window(_utc(DAY, "09:47"))
    assert not orb.in_entry_window(_utc(DAY, "10:35"))
    assert not orb.in_entry_window(_utc(DAY, "09:15"))


# ------------------------------------------------------------------ 6. the harness
def test_replay_on_synthetic_data_with_a_known_outcome(universe):
    b5, daily = universe
    # BIG breaks out at 09:40 (high 50.9 ≥ 50.61 trigger, open 50.5 → fill at the trigger),
    # climbs to a 52.4 high (trail 51.9), never touches it, flattens at the 15:45 open 52.5.
    b5["BIG"] += [bar(DAY, "09:35", 50.5, 50.55, 50.3, 50.4, 5_000),
                  bar(DAY, "09:40", 50.5, 50.9, 50.4, 50.8, 5_000),
                  bar(DAY, "10:00", 50.8, 52.1, 50.7, 52.0, 5_000),
                  bar(DAY, "12:00", 52.0, 52.4, 51.9, 52.3, 5_000),
                  bar(DAY, "15:45", 52.5, 52.6, 52.4, 52.5, 5_000),
                  bar(DAY, "15:55", 52.5, 52.6, 52.4, 52.5, 5_000)]
    b5["MID"] += flat_day(DAY, 49.9)[1:]
    b5["FLAT"] += flat_day(DAY, 50.0)[1:]
    res = orb.replay(b5, daily, DAY, DAY, {"fill_half_spread_bps": 0.0}, starting_equity=5000.0)
    assert res["n_sessions"] == 1 and res["n_trades"] == 1 and res["n_days_traded"] == 1
    t = res["trades"][0]
    assert t["symbol"] == "BIG" and t["entry"] == 50.61 and t["exit"] == 52.5
    # 1% of 5000 = $50 at $0.10 → 500 sh = $25k → 25% cap $1,250 → 24 shares
    assert t["shares"] == 24.0 and t["pnl"] == pytest.approx(24 * (52.5 - 50.61), abs=0.01)
    assert t["r"] == pytest.approx((52.5 - 50.61) / 0.10, abs=0.01) and t["reason"] == "flatten"
    assert res["daily_pnl"][DAY] == t["pnl"] and res["ending_equity"] == 5000.0 + t["pnl"]
    assert res["max_dd"] == 0.0 and res["avg_r"] == t["r"]
    assert res["skips"]["short"] == 1 and res["skips"]["doji"] == 1
    assert res["cost_bps_assumed"] == 0.0
    assert "IEX" in res["note"] and "Long-only" in res["note"]
    assert "sharpe" not in json.dumps(res).lower() and "win_rate" not in json.dumps(res)
    # the default haircut is 1 half-spread of 5 bp on every fill: 10 bp per round trip
    res2 = orb.replay(b5, daily, DAY, DAY, None, starting_equity=5000.0)
    assert res2["cost_bps_assumed"] == 10.0 and res2["trades"][0]["pnl"] < t["pnl"]


def test_replay_stop_out_and_drawdown(universe):
    b5, daily = universe
    b5["BIG"] += [bar(DAY, "09:40", 50.5, 50.9, 50.4, 50.8, 5_000),
                  bar(DAY, "09:45", 50.7, 50.75, 50.3, 50.35, 5_000),   # low 50.3 ≤ stop 50.51
                  bar(DAY, "15:45", 50.3, 50.3, 50.3, 50.3, 5_000)]
    b5["MID"] += flat_day(DAY, 49.9)[1:]
    b5["FLAT"] += flat_day(DAY, 50.0)[1:]
    res = orb.replay(b5, daily, DAY, DAY, {"fill_half_spread_bps": 0.0})
    t = res["trades"][0]
    assert t["reason"] == "stop" and t["exit"] == 50.51 and t["r"] == pytest.approx(-1.0, abs=0.01)
    assert res["max_dd"] > 0


def test_cli_writes_an_e24_ledger_row(tmp_path, universe, capsys):
    b5, daily = universe
    b5["BIG"] += [bar(DAY, "09:40", 50.5, 50.9, 50.4, 50.8, 5_000),
                  bar(DAY, "15:45", 51.0, 51.0, 51.0, 51.0, 5_000)]
    p5 = tmp_path / "bars_5m.json"
    p5.write_text(json.dumps(rh_shape(b5)), encoding="utf-8")
    pd = tmp_path / "bars.json"
    pd.write_text(json.dumps({"data": {"results": [{"symbol": s, "bars": b}
                                                    for s, b in daily.items()]}}), encoding="utf-8")
    led = tmp_path / "ledger.jsonl"
    out = tmp_path / "e24.json"
    res = orb.main(["--bars-5m", str(p5), "--bars", str(pd), "--start", DAY, "--end", DAY,
                    "--json", str(out), "--ledger", str(led)])
    assert res["n_trades"] == 1
    import ledger
    rows = ledger.trials(str(led))
    assert len(rows) == 1 and rows[0]["id"] == "E24" and rows[0]["n_trials_to_date"] == 1
    assert rows[0]["in_sample"]["cost_bps_assumed"] == 10.0
    assert "IEX" in rows[0]["in_sample"]["data_caveat"]
    assert rows[0]["out_of_sample"] is None and rows[0]["decision"] is None
    assert "trial 1 of the ledger" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["n_trades"] == 1


# ------------------------------------------------------------------ 7. the pm.py wiring
def _stage_orb(run_dir, pm, book=None):
    """Select the inactive orb template deliberately and stage a flat $5,000 book."""
    pm.use_desk("orb", "desks.json", allow_inactive=True)
    b = book or {"mode": "paper", "revision": 0, "starting_equity": 5000.0, "cash": 5000.0,
                 "realized_pnl": 0.0, "positions": [], "working_orders": [], "closed_trades": [],
                 "day_trades": [], "equity_curve": [], "seeded": DAY}
    (run_dir / "paper_book_orb.json").write_text(json.dumps(b), encoding="utf-8")
    return b


def _scan_rows(*syms):
    return {"meta": {"scan_date": DAY, "time": "08:00"},
            "results": [{"ticker": s, "price": 50.0, "score": 60, "setup": "Momentum",
                         "verdict": "buy", "gics": "Energy", "atr_14": 1.0,
                         "avg_volume_20d": 2_000_000} for s in syms]}


def _run(pm, run_dir, slot, hhmm, book, prices=None, scan=None):
    return pm.run(book, scan, prices or {}, slot, _utc(DAY, hhmm), "paper")


def test_pm_refuses_the_inactive_desk_without_allow_inactive(pm, run_dir):
    with pytest.raises(pm.InactiveDesk):
        pm.use_desk("orb", "desks.json")
    cfg = pm.use_desk("orb", "desks.json", allow_inactive=True)
    assert cfg["inactive"] is True and pm._orb_rules()["k_trail"] == 0.5
    assert pm.PM_RULES["max_new_entries_per_run"] == 5
    # the three live desks carry no orb block and never enter an ORB branch
    for d in ("swing", "pullback", "momentum"):
        pm.use_desk(d, "desks.json")
        assert pm._orb_rules() is None


def test_no_bars_is_a_no_op_that_says_so(pm, run_dir, universe):
    book = _stage_orb(run_dir, pm)
    pm.ORB.update({"bars_5m": None, "universe": _scan_rows("BIG")["results"], "daily": None})
    book, jrn, _ = _run(pm, run_dir, "sentinel", "09:35", book)
    assert book["working_orders"] == [] and book["positions"] == []
    assert any("no intraday bars — no ORB today" in s["reason"] for s in jrn["skipped"])
    assert any("no ORB today" in w for w in jrn["warnings"])


def test_0935_sentinel_places_stop_buys_and_no_other_sentinel_does(pm, run_dir, universe):
    b5, daily = universe
    book = _stage_orb(run_dir, pm)
    pm.ORB.update({"bars_5m": b5, "universe": _scan_rows("BIG", "MID", "FLAT")["results"],
                   "daily": daily})
    # not the entry window: nothing placed
    book, jrn, _ = _run(pm, run_dir, "sentinel", "10:35", book)
    assert book["working_orders"] == []
    book, jrn, _ = _run(pm, run_dir, "sentinel", "09:35", book)
    wo = book["working_orders"]
    assert len(wo) == 1 and wo[0]["symbol"] == "BIG" and wo[0]["type"] == "stop-buy"
    assert wo[0]["trigger_price"] == 50.61 and wo[0]["meta"]["stop"] == 50.51
    assert wo[0]["meta"]["stop_policy"] == "chandelier"
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert placed and "stop-buy" in placed[0]["detail"] and "notional cap binds" in placed[0]["detail"]
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-buy"], \
        "a stop-buy never fills in the run that placed it"
    why = {s["symbol"]: s["reason"] for s in jrn["skipped"]}
    assert "long-only" in why["MID"] and "doji" in why["FLAT"]
    assert jrn["orb"]["n_in_play"] == 3 and jrn["orb"]["in_play"][0]["symbol"] == "BIG"
    assert jrn["stop_policy"] == "chandelier" and jrn["broker_policy"] == "intraday_margin"


def test_decision_slot_entry_pass_places_nothing_on_the_orb_desk(pm, run_dir, quotes, scan):
    book = _stage_orb(run_dir, pm)
    from conftest import _fresh_scan_meta
    s = _fresh_scan_meta(json.loads((FIX / "scan.json").read_text(encoding="utf-8")))
    book, jrn, _ = pm.run(book, s, {}, "opening-range", None, "paper")
    assert book["working_orders"] == []
    assert any("ORB desk: entries are the 09:35 sentinel" in x["reason"] for x in jrn["skipped"])


def _breakout_day(b5):
    b5["BIG"] += [bar(DAY, "09:35", 50.5, 50.55, 50.3, 50.4, 5_000),
                  bar(DAY, "09:40", 50.5, 50.9, 50.4, 50.8, 5_000),
                  bar(DAY, "09:45", 50.8, 51.2, 50.7, 51.1, 5_000),
                  bar(DAY, "10:00", 51.1, 52.1, 51.0, 52.0, 5_000),
                  bar(DAY, "10:25", 52.0, 52.2, 51.9, 52.1, 5_000)]
    return b5


def test_fill_then_trail_then_flatten_leaves_no_positions(pm, run_dir, universe):
    b5, daily = universe
    book = _stage_orb(run_dir, pm)
    pm.ORB.update({"bars_5m": b5, "universe": _scan_rows("BIG")["results"], "daily": daily})
    pm.PM_RULES["shadow"]["enabled"] = False
    book, jrn, _ = _run(pm, run_dir, "sentinel", "09:35", book)
    assert len(book["working_orders"]) == 1
    # 10:35 — the bars since 09:35 show the breakout at 09:40: fill at the trigger + haircut
    _breakout_day(b5)
    px = {"BIG": {"price": 52.1, "as_of": _utc(DAY, "10:34"), "bid": 52.08, "ask": 52.12}}
    book, jrn, _ = _run(pm, run_dir, "sentinel", "10:35", book, px)
    fills = [d for d in jrn["decisions"] if d["action"] == "fill-buy"]
    assert fills and fills[0]["price"] == pytest.approx(50.61 + 0.02, abs=1e-6), \
        "max(trigger, bar open) + k half-spreads of the quote"
    assert book["working_orders"] == [] and len(book["positions"]) == 1
    pos = book["positions"][0]
    assert pos["stop_policy"] == "chandelier" and pos["orb_entry_bar"].startswith(f"{DAY}T09:40")
    # the same run manages the bars AFTER the fill bar: highest 5-min HIGH 52.2 → trail 51.7
    # (the highest close is 52.1; the anchor is the high, not the close)
    assert pos["highest_high"] == 52.2 and pos["stop"] == pytest.approx(51.7)
    assert pos["highest_close"] >= 52.2, "the generic field never sits under the anchor"
    raises = [d for d in jrn["decisions"] if d["action"] == "raise-stop"]
    assert raises and "50.51 to 51.70" in raises[0]["detail"] and "5-min high" in raises[0]["detail"]
    # 11:35 — a 5-min low through the trail exits at the stop (× exit slippage), journaled as a stop
    b5["BIG"] += [bar(DAY, "11:00", 52.0, 52.05, 51.5, 51.55, 5_000)]
    px = {"BIG": {"price": 51.55, "as_of": _utc(DAY, "11:34")}}
    book2 = json.loads(json.dumps(book))
    book2, jrn2, _ = _run(pm, run_dir, "sentinel", "11:35", book2, px)
    stops = [d for d in jrn2["decisions"] if d["action"] == "fill-sell"]
    assert stops and stops[0]["reason"] == "stop" and "5-min low broke the 51.70" in stops[0]["detail"]
    assert stops[0]["price"] == pytest.approx(round(51.7 * (1 - 0.0025), 4), abs=1e-4)
    assert book2["positions"] == []
    # …or, with no hit, the power-hour PM flattens: nothing is held overnight
    b5["BIG"].pop()
    px = {"BIG": {"price": 52.3, "as_of": _utc(DAY, "15:44")}}
    book3, jrn3, _ = pm.run(book, _scan_rows("BIG"), px, "power-hour", _utc(DAY, "15:45"), "paper")
    flat = [d for d in jrn3["decisions"] if d["action"] == "fill-sell"]
    assert flat and flat[0]["reason"] == "time" and "Flatten at close" in flat[0]["detail"]
    assert book3["positions"] == [] and book3["working_orders"] == []
    assert not [d for d in jrn3["decisions"] if d["action"] == "place-buy"]
    assert book3["closed_trades"][-1]["symbol"] == "BIG"
    # nothing counts day trades under intraday_margin
    assert jrn3["day_trades_used"] == 1 and not book3.get("freeze_until")


def test_unfilled_stop_buy_is_cancelled_by_the_1035_sentinel(pm, run_dir, universe):
    b5, daily = universe
    book = _stage_orb(run_dir, pm)
    pm.ORB.update({"bars_5m": b5, "universe": _scan_rows("BIG")["results"], "daily": daily})
    book, jrn, _ = _run(pm, run_dir, "sentinel", "09:35", book)
    b5["BIG"] += flat_day(DAY, 50.4)[1:]        # never reaches 50.61
    book, jrn, _ = _run(pm, run_dir, "sentinel", "10:35", book,
                        {"BIG": {"price": 50.4, "as_of": _utc(DAY, "10:34")}})
    cancels = [d for d in jrn["decisions"] if d["action"] == "cancel"]
    assert cancels and "unfilled by 10:30" in cancels[0]["detail"]
    assert book["working_orders"] == [] and book["positions"] == []
    assert book["cash"] == 5000.0, "a working order never debits cash"


def test_load_orb_inputs_reads_the_staged_files(pm, run_dir, universe):
    b5, daily = universe
    _stage_orb(run_dir, pm)
    assert pm.load_orb_inputs()["bars_5m"] is None
    (run_dir / "bars_5m.json").write_text(json.dumps(rh_shape(b5)), encoding="utf-8")
    (run_dir / "scan_results.json").write_text(json.dumps(_scan_rows("BIG")), encoding="utf-8")
    (run_dir / "bars.json").write_text(json.dumps({"data": {"results": [
        {"symbol": s, "bars": b} for s, b in daily.items()]}}), encoding="utf-8")
    inp = pm.load_orb_inputs()
    assert set(inp["bars_5m"]) == {"BIG", "MID", "FLAT"}
    assert inp["universe"][0]["ticker"] == "BIG" and set(inp["daily"]) == {"BIG", "MID", "FLAT"}


def test_orb_is_in_the_manifest_and_the_ci_allowlist():
    assert "orb.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    ci = (ENGINE.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '"orb"' in ci
    desks = json.loads((ENGINE / "desks.json").read_text(encoding="utf-8"))["desks"]
    assert desks["orb"]["inactive"] is True, "the desk ships inactive"
    assert "long-only" in desks["orb"]["label"].lower() or "LONG-ONLY" in desks["orb"]["_thesis"]
    for d in ("swing", "pullback", "momentum"):
        assert "orb" not in (desks[d].get("rules") or {})
