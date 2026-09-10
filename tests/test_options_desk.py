"""D-02 — the paper options desk (engine/options_desk.py, E27).

Every test runs on a SYNTHETIC chain built from Black–Scholes at a known IV surface, so
the numbers the desk is expected to produce are computable by hand from the rules. The
desk is paper only and ships inactive; the CLI tests prove both.
"""
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))

import options_desk as od   # noqa: E402

TODAY = dt.date(2026, 9, 10)
NOW = "2026-09-10T14:15:00Z"
SPOT = 650.0
R = 0.04


# ------------------------------------------------------------------ synthetic chain
def _iv(K, S, base=0.17):
    """A put skew: IV rises as the strike falls."""
    return base + 0.9 * max(0.0, (S - K) / S)


def synthetic_chain(spot=SPOT, today=TODAY, symbol="XSP", dtes=(10, 38, 66), lo=560, hi=700,
                    step=1, spread=0.04, with_greeks=True, with_oi=True, base_iv=0.17,
                    wide_strikes=(), zero_bid_strikes=(), include_spot=True):
    """A Robinhood-style option_chains.json payload: flat rows with chain_symbol,
    expiration_date, strike_price, type, bid/ask, implied_volatility, greeks, OI."""
    rows = []
    for d in dtes:
        exp = (today + dt.timedelta(days=d)).isoformat()
        T = d / 365.0
        for K in range(lo, hi + 1, step):
            for typ in ("put", "call"):
                iv = _iv(K, spot, base_iv)
                g = od.bs_greeks(spot, K, T, R, iv, put=(typ == "put"))
                mid = g["price"]
                half = spread / 2.0
                if K in wide_strikes:
                    half = mid * 0.12               # 24% of mid — over the 10% gate
                bid = round(max(mid - half, 0.0), 2)
                ask = round(mid + half, 2)
                if K in zero_bid_strikes:
                    bid = 0.0
                row = {"chain_symbol": symbol, "expiration_date": exp, "strike_price": K,
                       "type": typ, "bid_price": bid, "ask_price": ask,
                       "implied_volatility": round(iv, 4), "volume": 100}
                if with_greeks:
                    row.update({"delta": round(g["delta"], 4), "gamma": round(g["gamma"], 6),
                                "theta": round(g["theta"], 4), "vega": round(g["vega"], 4)})
                if with_oi:
                    row["open_interest"] = 5000 if typ == "call" else 3000
                if include_spot:
                    row["underlying_price"] = spot
                rows.append(row)
    return {"results": rows}


def chain_rows(payload):
    import snapshots
    ch = snapshots.normalise_chain(payload)
    rows = []
    for sym in ch:
        rows.extend(ch[sym]["contracts"])
    return rows


def desk_rules(**over):
    return od.merge_rules(over)


def chain_on(day, **kw):
    """The same three expiries as the day-one chain, seen from `day`."""
    dtes = tuple((TODAY + dt.timedelta(days=d) - day).days for d in (10, 38, 66))
    return synthetic_chain(today=day, dtes=dtes, **kw)


def open_via_run(run_dir, book=None, chain=None, vix=(17.0, 19.0), slot="opening-range", peers=None,
                 now=NOW, rules=None):
    chain = chain if chain is not None else synthetic_chain()
    (run_dir / "option_chains.json").write_text(json.dumps(chain), encoding="utf-8")
    if vix is not None:
        (run_dir / "vix.json").write_text(json.dumps({"vix": vix[0], "vix3m": vix[1],
                                                       "as_of": TODAY.isoformat()}), encoding="utf-8")
    elif (run_dir / "vix.json").exists():
        os.remove(run_dir / "vix.json")
    book = book if book is not None else od.new_book()
    return od.run_slot(str(run_dir), book, slot, now, rules=rules, peers=peers or {"books": {}})


# ------------------------------------------------------------------ Black–Scholes
def test_bs_price_and_iv_round_trip():
    for S, K, T, sigma, put in ((650, 620, 40 / 365, 0.18, True), (100, 110, 0.5, 0.35, True),
                                (100, 90, 0.25, 0.22, False), (650, 700, 1.0, 0.15, True)):
        p = od.bs_price(S, K, T, R, sigma, put=put)
        assert p > 0
        iv = od.implied_vol(p, S, K, T, R, put=put)
        assert abs(iv - sigma) < 1e-5
    # put-call parity
    c = od.bs_price(100, 100, 0.5, R, 0.2, put=False)
    p = od.bs_price(100, 100, 0.5, R, 0.2, put=True)
    import math
    assert abs((c - p) - (100 - 100 * math.exp(-R * 0.5))) < 1e-9
    # out of the no-arbitrage band -> None, expired -> intrinsic
    assert od.implied_vol(-1, 100, 100, 0.5, R) is None
    assert od.bs_price(90, 100, 0.0, R, 0.2, put=True) == 10.0


def test_bs_greeks_signs_and_units():
    g = od.bs_greeks(650, 620, 40 / 365, R, 0.18, put=True)
    assert -0.5 < g["delta"] < 0 and g["gamma"] > 0 and g["vega"] > 0 and g["theta"] < 0
    # vega per vol point: bumping sigma by 0.01 moves the price by ~vega
    p0 = od.bs_price(650, 620, 40 / 365, R, 0.18)
    p1 = od.bs_price(650, 620, 40 / 365, R, 0.19)
    assert abs((p1 - p0) - g["vega"]) < 0.03 * g["vega"]      # second-order (volga) residual
    # theta per day: one day less to expiry costs ~theta
    p2 = od.bs_price(650, 620, 39 / 365, R, 0.18)
    assert abs((p2 - p0) - g["theta"]) < 0.03 * abs(g["theta"])


# ------------------------------------------------------------------ selection
def test_select_structure_picks_20_delta_in_window_5_wide():
    rows = chain_rows(synthetic_chain())
    cand, why = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    assert why is None, why
    assert cand["dte"] == 38                     # the 30–45 window, nearest 40
    assert cand["width"] == 5.0
    assert cand["long_strike"] == cand["short_strike"] - 5.0
    assert abs(abs(cand["short_greeks"]["delta"]) - 0.20) <= 0.10
    # nearest |delta| to 0.20 among the OTM puts at that expiry
    exp38 = (TODAY + dt.timedelta(days=38)).isoformat()
    puts = [c for c in rows if c["type"] == "put" and c["expiry"] == exp38 and c["strike"] < SPOT]
    best = min(puts, key=lambda c: (abs(abs(c["delta"]) - 0.20), -c["strike"]))
    assert cand["short_strike"] == best["strike"]


def test_select_structure_derives_delta_from_iv_when_missing():
    rows = chain_rows(synthetic_chain(with_greeks=False))
    cand, why = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    assert why is None, why
    assert cand["short_greeks"]["source"] == "model"
    rows2 = chain_rows(synthetic_chain())
    cand2, _ = od.select_structure(rows2, SPOT, od.RULES, TODAY, "XSP")
    assert cand["short_strike"] == cand2["short_strike"]


def test_select_structure_narrows_the_width_to_fit_the_risk_cap():
    rows = chain_rows(synthetic_chain())
    full, _ = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    assert full["width"] == 5.0 and not full["narrowed"]
    credit5 = full["short_row"]["mid"] - full["long_row"]["mid"] - 0.04
    assert (5.0 - credit5) * 100 > 400          # a 20Δ 5-wide cannot fit under $400
    cand, why = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP", max_loss_per_contract=400.0)
    assert why is None, why
    assert cand["narrowed"] and cand["width"] < 5.0 and cand["width_max"] == 5.0
    credit = cand["short_row"]["mid"] - cand["long_row"]["mid"] - 0.04
    assert (cand["width"] - credit) * 100 <= 400
    # the widest that fits: one step wider does not
    wider = cand["width"] + 1.0
    lrow = od.find_leg(rows, "XSP", cand["expiry"], cand["short_strike"] - wider)
    assert (wider - (cand["short_row"]["mid"] - lrow["mid"] - 0.04)) * 100 > 400
    # with the fallback off the desk does not trade under the cap
    strict = od.merge_rules({"width_fallback": False})
    cand2, why2 = od.select_structure(rows, SPOT, strict, TODAY, "XSP", max_loss_per_contract=400.0)
    assert cand2 is None and "no width fits" in why2


def test_select_structure_refuses_outside_window_or_without_long_leg():
    rows = chain_rows(synthetic_chain(dtes=(10, 60)))
    cand, why = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    assert cand is None and "30 and 45 DTE" in why
    rows = chain_rows(synthetic_chain(step=10))       # no strike 5 under the short
    cand, why = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    assert cand is None and "no width fits" in why and "no 6" in why
    assert od.select_structure(rows, None, od.RULES, TODAY, "XSP")[1] == "no spot price for the underlying"


# ------------------------------------------------------------------ gates
def test_gates_each_condition():
    ok, why = od.gates(17.0, 19.0, 5e9, od.RULES)
    assert ok and why == []
    ok, why = od.gates(31.0, 33.0, 5e9, od.RULES)
    assert not ok and any("> 30" in r for r in why)
    ok, why = od.gates(22.0, 20.0, 5e9, od.RULES)
    assert not ok and any("backwardation" in r for r in why)
    ok, why = od.gates(17.0, 19.0, -1e9, od.RULES)
    assert not ok and any("GEX" in r and "< 0" in r for r in why)
    ok, why = od.gates(None, None, 5e9, od.RULES)
    assert not ok and any("VIX unavailable" in r for r in why)
    ok, why = od.gates(17.0, None, 5e9, od.RULES)
    assert not ok and any("VIX3M unavailable" in r for r in why)
    # GEX unavailable: skipped and said so, not failed
    ok, why = od.gates(17.0, 19.0, None, od.RULES)
    assert ok and any("SKIPPED" in r for r in why)


def test_gex_from_chain_sign_and_absence():
    rows = chain_rows(synthetic_chain())
    gex = od.gex_from_chain(rows, SPOT)
    assert gex is not None and gex > 0            # more call OI than put OI in the fixture
    rows = chain_rows(synthetic_chain(with_oi=False))
    assert od.gex_from_chain(rows, SPOT) is None


# ------------------------------------------------------------------ limits
def _structure(max_loss=380.0, contracts=1, sector="Index", delta=0.03, vega=-0.12, theta=0.04,
               spot=SPOT, sid="s"):
    return {"id": sid, "underlying": "XSP", "expiry": "2026-10-18", "short_strike": 620.0,
            "long_strike": 615.0, "width": 5.0, "contracts": contracts, "multiplier": 100,
            "credit": 1.2, "max_loss": max_loss, "status": "open", "sector": sector, "spot": spot,
            "beta": 1.0, "value": 1.2,
            "short_greeks": {"delta": -delta, "vega": -vega, "theta": -theta, "gamma": 0.0},
            "long_greeks": {"delta": 0.0, "vega": 0.0, "theta": 0.0, "gamma": 0.0}}


def test_limits_per_structure_8pct():
    book = od.new_book()
    ok, why = od.limits_ok(book, _structure(max_loss=399.0), od.RULES, equity=5000.0)
    assert ok, why
    ok, why = od.limits_ok(book, _structure(max_loss=401.0), od.RULES, equity=5000.0)
    assert not ok and any("risk per structure" in r for r in why)


def test_limits_total_40pct_and_concurrency_5():
    book = od.new_book()
    flat = dict(delta=0.0, vega=0.0, theta=0.0)
    book["structures"] = [_structure(max_loss=390.0, sid=f"s{i}", sector=f"S{i}", **flat) for i in range(4)]
    ok, why = od.limits_ok(book, _structure(max_loss=390.0, sector="S9", **flat), od.RULES, equity=5000.0)
    assert ok, why
    book["structures"] = [_structure(max_loss=390.0, sid=f"s{i}", sector=f"S{i}", **flat) for i in range(5)]
    ok, why = od.limits_ok(book, _structure(max_loss=390.0, sector="S9", **flat), od.RULES, equity=5000.0)
    assert not ok
    assert any("5 structures open" in r for r in why)
    assert any("total defined risk" in r for r in why)
    # sector cap: 2 per sector
    book["structures"] = [_structure(max_loss=100.0, sid=f"s{i}") for i in range(2)]
    ok, why = od.limits_ok(book, _structure(max_loss=100.0), od.RULES, equity=5000.0)
    assert not ok and any("sector Index" in r for r in why)


def test_limits_greeks():
    book = od.new_book()
    # delta: ±0.5% of NAV per 1% move. NAV 5000 -> ±$25. 0.08 × 100 × 650 × 1% = $52 → over
    ok, why = od.limits_ok(book, _structure(max_loss=100.0, delta=0.08, vega=0.0, theta=0.0),
                           od.RULES, equity=5000.0, house_nav=5000.0)
    assert not ok and any("β-weighted delta" in r for r in why)
    # the same structure inside a $20,000 house (±$100) passes
    ok, why = od.limits_ok(book, _structure(max_loss=100.0, delta=0.08, vega=0.0, theta=0.0),
                           od.RULES, equity=5000.0, house_nav=20000.0)
    assert ok, why
    # vega: net short vega ≤ 0.5% of 5000 = $25/pt. 0.30 × 100 = $30 → over
    ok, why = od.limits_ok(book, _structure(max_loss=100.0, delta=0.0, vega=-0.30, theta=0.0),
                           od.RULES, equity=5000.0)
    assert not ok and any("short vega" in r for r in why)
    # theta: ≤ 0.3% of 5000 = $15/day. 0.20 × 100 = $20 → over
    ok, why = od.limits_ok(book, _structure(max_loss=100.0, delta=0.0, vega=0.0, theta=0.20),
                           od.RULES, equity=5000.0)
    assert not ok and any("theta" in r for r in why)


# ------------------------------------------------------------------ paper fill and fees
def test_paper_fill_credit_is_mid_less_two_cents_per_leg_and_rejects_wide_legs():
    rows = chain_rows(synthetic_chain())
    cand, _ = od.select_structure(rows, SPOT, od.RULES, TODAY, "XSP")
    f = od.paper_fill([{"row": cand["short_row"], "side": "sell"},
                       {"row": cand["long_row"], "side": "buy"}], "open", od.RULES)
    assert f["ok"] and f["credit"]
    assert abs(f["net"] - (cand["short_row"]["mid"] - cand["long_row"]["mid"] - 0.04)) < 1e-9
    # a wide short leg is refused at entry ...
    wide = chain_rows(synthetic_chain(wide_strikes=(int(cand["short_strike"]),)))
    c2, _ = od.select_structure(wide, SPOT, od.RULES, TODAY, "XSP")
    f2 = od.paper_fill([{"row": c2["short_row"], "side": "sell"}, {"row": c2["long_row"], "side": "buy"}],
                       "open", od.RULES)
    assert not f2["ok"] and any("spread" in r for r in f2["reasons"])
    # ... and so is a zero bid
    zb = chain_rows(synthetic_chain(zero_bid_strikes=(int(cand["long_strike"]),)))
    c3, _ = od.select_structure(zb, SPOT, od.RULES, TODAY, "XSP")
    f3 = od.paper_fill([{"row": c3["short_row"], "side": "sell"}, {"row": c3["long_row"], "side": "buy"}],
                       "open", od.RULES)
    assert not f3["ok"] and any("no bid" in r for r in f3["reasons"])
    # a close is never refused: the debit is reported with the width noted
    f4 = od.paper_fill([{"row": c2["short_row"], "side": "buy"}, {"row": c2["long_row"], "side": "sell"}],
                       "close", od.RULES, strict=False)
    assert f4["ok"] and f4["net"] < 0 and f4["reasons"]


def test_fee_table():
    assert od.fees_for("XSP", 1, 2, od.RULES) == pytest.approx(2 * (0.04 + 0.35))
    assert od.fees_for("SPY", 1, 2, od.RULES) == pytest.approx(2 * 0.04)
    assert od.fees_for("XSP", 3, 2, od.RULES) == pytest.approx(3 * 2 * 0.39)


# ------------------------------------------------------------------ run_slot end to end
def test_run_slot_opens_a_structure_and_writes_book_and_journal(run_dir):
    book, jrn, state = open_via_run(run_dir)
    assert jrn["gates"]["ok"], jrn["gates"]
    opens = [d for d in jrn["decisions"] if d["action"] == "open-spread"]
    assert len(opens) == 1, jrn["skipped"]
    s = book["structures"][0]
    for k in ("id", "underlying", "expiry", "short_strike", "long_strike", "width", "contracts",
              "credit", "max_loss", "opened", "opened_slot", "greeks_at_open", "status", "closed",
              "exit_reason", "pnl"):
        assert k in s
    assert s["underlying"] == "XSP" and s["status"] == "open" and 2.0 <= s["width"] <= 5.0
    assert s["max_loss"] <= 0.08 * 5000 + 1e-6
    assert any("narrowed to" in w for w in jrn["warnings"])
    assert s["max_loss"] == pytest.approx((s["width"] - s["credit"]) * 100 * s["contracts"], abs=0.01)
    # cash took the credit less fees; equity is cash less the debit to close AT MID, so the
    # $0.02/leg slippage shows up as a loss the moment the structure is marked
    assert book["cash"] == pytest.approx(5000 + s["credit"] * 100 * s["contracts"] - s["fees"], abs=0.01)
    assert state["book"]["equity"] == pytest.approx(5000 - s["fees"] - 0.04 * 100 * s["contracts"], abs=0.01)
    assert s["value_source"] == "chain"
    assert book["revision"] == 1 and book["based_on_revision"] == 0
    assert jrn["run_id"].endswith("-options") and jrn["book_fingerprint"]
    assert jrn["shadow"]["applicable"] is False
    assert state["broker_policy"]["name"] == "intraday_margin"
    assert state["book"]["maintenance_requirement"] == pytest.approx(s["max_loss"])
    assert state["stress"] and len(state["stress"]["scenarios"]) == 3   # weekly stress ran on day one
    assert book["equity_curve"][-1]["slot"] == "opening-range"


def test_run_slot_marks_and_takes_50pct_profit(run_dir):
    book, jrn, _ = open_via_run(run_dir)
    s = book["structures"][0]
    # the same chain a day later marks to nearly the same value: no exit
    book, jrn, _ = open_via_run(run_dir, book=book, now="2026-09-11T14:15:00Z",
                                chain=chain_on(dt.date(2026, 9, 11)))
    assert not jrn["exits"] and book["structures"][0]["value_source"] == "chain"
    # IV collapses and spot rallies: the spread is worth under half the credit -> profit take
    later = chain_on(dt.date(2026, 9, 18), spot=690.0, base_iv=0.10)
    book, jrn, state = open_via_run(run_dir, book=book, now="2026-09-18T14:15:00Z", chain=later)
    assert jrn["exits"] and jrn["exits"][0]["reason"] == "profit-take"
    closed = book["closed_trades"][-1]
    assert closed["id"] == s["id"] and closed["pnl"] > 0 and closed["status"] == "closed"
    assert closed["debit"] <= 0.5 * closed["credit"] + 0.05
    assert book["realized_pnl"] == pytest.approx(closed["pnl"])
    # the exit ran before the entry pass, so the same slot re-deployed into a fresh structure
    assert all(x["id"] != s["id"] for x in book["structures"])


def test_mark_falls_back_to_model_when_the_chain_is_missing(run_dir):
    book, _, _ = open_via_run(run_dir)
    os.remove(run_dir / "option_chains.json")
    book, jrn, _ = od.run_slot(str(run_dir), book, "sentinel", "2026-09-10T15:35:00Z",
                               peers={"books": {}})
    assert book["structures"][0]["value_source"] == "model"
    assert any("No option chain staged" in w for w in jrn["warnings"])
    assert not [d for d in jrn["decisions"] if d["action"] == "open-spread"]


def test_21_dte_exit(run_dir):
    book, _, _ = open_via_run(run_dir)
    exp = dt.date.fromisoformat(book["structures"][0]["expiry"])
    day = exp - dt.timedelta(days=21)
    book, jrn, _ = open_via_run(run_dir, book=book, now=f"{day.isoformat()}T14:15:00Z", chain=chain_on(day))
    assert jrn["exits"] and jrn["exits"][0]["reason"] == "dte-exit"
    assert book["closed_trades"][-1]["exit_reason"] == "dte-exit"


def test_breach_defensive_close(run_dir):
    book, _, _ = open_via_run(run_dir)
    short = book["structures"][0]["short_strike"]
    crash = chain_on(dt.date(2026, 9, 11), spot=short - 3.0, base_iv=0.35)
    book, jrn, _ = open_via_run(run_dir, book=book, now="2026-09-11T14:15:00Z", chain=crash)
    assert jrn["exits"] and jrn["exits"][0]["reason"] == "defensive-close"
    closed = book["closed_trades"][-1]
    assert closed["pnl"] < 0 and -closed["pnl"] <= closed["max_loss"] + closed["fees"] + 0.05


def test_manage_settles_at_expiry_intrinsic():
    book = od.new_book()
    s = _structure(max_loss=380.0)
    s.update({"expiry": TODAY.isoformat(), "credit": 1.2, "fees": 0.78})
    book["structures"] = [s]
    book["cash"] = 5000 + 120 - 0.78
    exits = od.manage(book, [], {"XSP": 617.0}, TODAY, od.RULES)
    assert exits[0]["reason"] == "expired"
    assert exits[0]["debit"] == pytest.approx(3.0)      # 620 − 617 in the money, long leg OTM


# ------------------------------------------------------------------ stress
def test_stress_aug_2024_loses_money(run_dir):
    book, _, _ = open_via_run(run_dir)
    res = od.stress(book, None, od.RULES, TODAY)
    by = {s["name"]: s for s in res["scenarios"]}
    assert by["2024-08-05"]["pnl"] < 0
    assert by["2024-08-05"]["pct_equity"] < 0
    ml = book["structures"][0]["max_loss"]
    assert -by["2024-08-05"]["pnl"] <= ml + 1e-6         # defined risk bounds it
    assert res["worst"] in by
    # a bigger shock costs more
    assert by["2024-08-05"]["pnl"] <= by["2025-04-08"]["pnl"]


# ------------------------------------------------------------------ exclusions and gates in a run
def test_held_underlying_exclusion(run_dir):
    peers = {"books": {"swing": {"cash": 1000.0, "positions": [{"symbol": "SPY", "shares": 3,
                                                                  "last_price": 650.0}]}}}
    book, jrn, _ = open_via_run(run_dir, peers=peers)
    assert not book["structures"]
    assert any("stock desk holds" in s["reason"] for s in jrn["skipped"])
    assert "XSP" in od.held_underlyings(peers) and "SPY" in od.held_underlyings(peers)


def test_regime_gate_blocks_entries_in_a_run(run_dir):
    book, jrn, _ = open_via_run(run_dir, vix=(32.0, 30.0))
    assert not book["structures"] and jrn["gates"]["ok"] is False
    assert any("regime gate" in s["reason"] for s in jrn["skipped"])
    # no vix.json at all: no entries, and the journal says why
    book, jrn, _ = open_via_run(run_dir, vix=None)
    assert not book["structures"] and any("vix.json" in w for w in jrn["warnings"])


def test_gex_gate_skipped_when_chain_has_no_oi(run_dir):
    book, jrn, _ = open_via_run(run_dir, chain=synthetic_chain(with_oi=False))
    assert jrn["gates"]["gex"] is None and jrn["gates"]["ok"]
    assert any("SKIPPED" in r for r in jrn["gates"]["reasons"])
    assert book["structures"]


def test_sentinel_takes_no_entries_and_no_stress(run_dir):
    book, jrn, _ = open_via_run(run_dir, slot="sentinel")
    assert not book["structures"] and jrn["gates"] is None and jrn["stress"] is None
    assert book["equity_curve"] == []


def test_spy_fallback_when_xsp_absent(run_dir):
    book, jrn, _ = open_via_run(run_dir, chain=synthetic_chain(symbol="SPY"))
    assert book["structures"] and book["structures"][0]["underlying"] == "SPY"
    assert book["structures"][0]["fees"] == pytest.approx(0.08)      # no index fee on SPY
    assert any(s["symbol"] == "XSP" for s in jrn["skipped"])


def test_ladder_halt_flattens_structures(run_dir):
    book, _, _ = open_via_run(run_dir)
    book["hwm"] = 6000.0             # equity ~5000 is >8% under: rung 3
    book, jrn, state = open_via_run(run_dir, book=book, now="2026-09-11T14:15:00Z",
                                    chain=chain_on(dt.date(2026, 9, 11)))
    assert any("LADDER HALT" in w for w in jrn["warnings"])
    assert not book["structures"] and book["closed_trades"][-1]["exit_reason"] == "flatten"
    assert book["ladder_halt"] and book["cool_until"]
    assert state["book"]["ladder"]["halt"] is True


# ------------------------------------------------------------------ house exposure conversion
def test_house_options_holdings_equity_equivalent(run_dir):
    import house
    book, _, _ = open_via_run(run_dir)
    s = book["structures"][0]
    hold = house.options_holdings(book, "options")
    assert len(hold) == 1 and hold[0]["kind"] == "options-delta" and hold[0]["symbol"] == "XSP"
    want = abs(s["greeks"]["delta"]) * 100 * s["contracts"] * s["spot"] * 1.0
    assert hold[0]["notional"] == pytest.approx(want, abs=0.01)
    assert house.options_equity(book) == pytest.approx(od.desk_equity(book))


def test_pm_house_exposure_folds_an_options_peer_in(pm, run_dir):
    book, _, _ = open_via_run(run_dir)
    swing = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    pm.PEERS.update({"books": {"options": book}, "loaded": ["options"], "missing": []})
    h = pm.house_exposure(swing, {})
    assert h is not None and h["desks"]["options"]["kind"] == "options"
    assert "XSP" in h["by_symbol"] and h["by_sector"].get("Index", 0) > 0
    assert any(x["kind"] == "options-delta" for x in h["holdings"])


# ------------------------------------------------------------------ desks.json, CLI, inactive
def test_desks_json_options_template(run_dir):
    d = json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))["desks"]["options"]
    assert d["inactive"] is True and d["kind"] == "options"
    assert d["book"] == "paper_book_options.json" and d["journal"] == "pm_journal_current_options.json"
    assert d["doc_book"] == "claude/paper-book-options.json"
    assert d["doc_journal"] == "claude/pm-journal-options.json"
    assert d["starting_equity"] == 5000.0
    assert d["rules"]["fees"]["index_per_contract"] == 0.35
    assert "put credit spread" in d["_thesis"].lower()
    for k in ("swing", "pullback", "momentum"):
        d2 = json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))["desks"][k]
        assert d2["rules"] == {"stop_policy": "fixed_atr"} and not d2.get("inactive")


def _cli(run_dir, *args):
    env = dict(os.environ, SCAN_DIR=str(run_dir))
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args],
                          capture_output=True, text=True, env=env)


def test_inactive_refused_on_the_cli_and_no_book_created(run_dir):
    r = _cli(run_dir, "--desk", "options", "--slot", "opening-range")
    assert r.returncode == 2 and "INACTIVE" in r.stderr
    assert not (run_dir / "paper_book_options.json").exists()
    assert not (run_dir / "pm_book_next-options.json").exists()


def test_cli_end_to_end_with_allow_inactive(run_dir):
    (run_dir / "option_chains.json").write_text(json.dumps(synthetic_chain()), encoding="utf-8")
    (run_dir / "vix.json").write_text(json.dumps({"vix": 17.0, "vix3m": 19.0}), encoding="utf-8")
    (run_dir / "paper_book_options.json").write_text(json.dumps(od.new_book()), encoding="utf-8")
    r = _cli(run_dir, "--desk", "options", "--slot", "opening-range", "--allow-inactive", "--now", NOW)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "open-spread" in r.stdout and "COVERAGE options" in r.stdout
    nxt = json.loads((run_dir / "pm_book_next-options.json").read_text(encoding="utf-8"))
    assert nxt["revision"] == 1 and nxt["structures"]
    jn = json.loads((run_dir / "pm_journal_next-options.json").read_text(encoding="utf-8"))
    assert jn["entries"][-1]["desk"] == "options" and jn["entries"][-1]["kind"] == "options"
    assert jn["entries"][-1]["changed"] is True and jn["entries"][-1]["publish_snapshot"] is False
    st = json.loads((run_dir / "pm_state-options.json").read_text(encoding="utf-8"))
    assert st["kind"] == "options" and st["orders_to_place"] == []
    hb = json.loads((run_dir / "pm_heartbeat-options.json").read_text(encoding="utf-8"))
    assert hb["desk"] == "options" and hb["positions"] == 1 and hb["quiet"] is False
    # the chain snapshot was archived on the way through (S-01)
    assert list((run_dir / "archive" / "chain_snapshot").glob("*.jsonl.gz"))
    # the --check protocol: the stored book is still at revision 0, so the write is allowed
    r2 = _cli(run_dir, "--desk", "options", "--allow-inactive", "--book", "pm_book_next-options.json",
              "--check", "paper_book_options.json")
    assert r2.returncode == 0 and "OK: no concurrent write" in r2.stdout
    # and refused when someone else wrote first
    bumped = od.new_book(); bumped["revision"] = 7
    (run_dir / "paper_book_options.json").write_text(json.dumps(bumped), encoding="utf-8")
    r3 = _cli(run_dir, "--desk", "options", "--allow-inactive", "--book", "pm_book_next-options.json",
              "--check", "paper_book_options.json")
    assert r3.returncode == 2 and "REFUSED" in r3.stderr


def test_cli_sentinel_quiet_when_flat(run_dir):
    (run_dir / "paper_book_options.json").write_text(json.dumps(od.new_book()), encoding="utf-8")
    r = _cli(run_dir, "--desk", "options", "--slot", "sentinel", "--allow-inactive")
    assert r.returncode == 0 and "SENTINEL QUIET" in r.stdout
    assert not (run_dir / "pm_book_next-options.json").exists()
    hb = json.loads((run_dir / "pm_heartbeat-options.json").read_text(encoding="utf-8"))
    assert hb["quiet"] is True


def test_cli_sentinel_quiet_with_open_structures_writes_nothing(run_dir):
    """A sentinel on a book that HOLDS a structure, when nothing fires: heartbeat only, the
    quiet line counts structures (an options book has no `positions` key), no next-book."""
    book, _, _ = open_via_run(run_dir)
    assert book["structures"]
    (run_dir / "paper_book_options.json").write_text(json.dumps(book), encoding="utf-8")
    (run_dir / "option_chains.json").write_text(json.dumps(chain_on(TODAY)), encoding="utf-8")
    r = _cli(run_dir, "--desk", "options", "--slot", "sentinel", "--allow-inactive",
             "--now", "2026-09-10T15:35:00Z")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "SENTINEL QUIET" in r.stdout and "1 structure(s)" in r.stdout
    assert not (run_dir / "pm_book_next-options.json").exists()
    hb = json.loads((run_dir / "pm_heartbeat-options.json").read_text(encoding="utf-8"))
    # the stored book stayed at revision 1 (the opening run's); a quiet run claims no newer one
    assert hb["quiet"] is True and hb["positions"] == 1 and hb["book_revision"] == 1


def test_module_imports_nothing_that_can_place_an_order():
    import ast
    src = (ENGINE / "options_desk.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"broker_policy", "house", "ladder", "snapshots",
                                              "archive", "config"}
    assert names <= allowed, names - allowed
    for bad in ("place_", "order", "urllib", "http", "socket", "requests"):
        assert bad not in src.lower() or bad == "order", bad
    assert "options_desk.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()


def test_vix_json_shapes():
    assert od.parse_vix({"VIX": "17.2", "VIX3M": 19}) == {"vix": 17.2, "vix3m": 19.0, "as_of": None, "source": None}
    p = od.parse_vix({"vix": {"close": 17.2, "date": "2026-09-09"}, "vix3m": {"close": 19.1}})
    assert p["vix"] == 17.2 and p["vix3m"] == 19.1 and p["as_of"] == "2026-09-09"
    p = od.parse_vix({"vix": [{"date": "2026-09-08", "close": 16}, {"date": "2026-09-09", "close": 17}],
                      "vix3m": [{"date": "2026-09-09", "close": 19}]})
    assert p["vix"] == 17.0 and p["as_of"] == "2026-09-09"
    assert od.parse_vix({"foo": 1}) is None


def test_init_book_cli(tmp_path):
    out = tmp_path / "paper_book_options.json"
    r = subprocess.run([sys.executable, str(ENGINE / "options_desk.py"), "--init-book", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    b = json.loads(out.read_text(encoding="utf-8"))
    assert b["kind"] == "options" and b["cash"] == 5000.0 and b["structures"] == []
