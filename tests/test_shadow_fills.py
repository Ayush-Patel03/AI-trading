"""K-06 — shadow fills, implementation shortfall and the execution-cost budget.

Two promises, pinned in opposite directions:
  * with PM_RULES["shadow"]["enabled"] False the engine writes the SAME BYTES it wrote
    before K-06 existed (tests/fixtures/pm_golden_prechange_shadow.json, generated from the
    engine at d02bcee by tests/shadow_sequence.py);
  * with it on, the only difference is the new keys — booked prices and P&L do not move.
"""
import copy
import datetime as dt
import json
import pathlib

import pytest

import shadow_sequence as ss

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"
NEW_KEYS = {"shadow", "shadow_price", "shadow_gap_usd", "implementation_shortfall_bps",
            "decision_mid", "shadow_basis", "equity_shadow", "cost_budget", "adv_usd",
            "cost_budget_bps_per_year"}


@pytest.fixture
def fills(run_dir):
    import importlib
    import fills as f
    importlib.reload(f)
    return f


def _strip(obj):
    """Drop every K-06 key, recursively, so what is left must equal the pre-change output."""
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in NEW_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _canon(obj):
    return json.dumps(obj, sort_keys=True)


# ------------------------------------------------------------------ the arithmetic
def test_shadow_price_by_slot(fills):
    """Buys at ask + k×half_spread + add_on, sells at bid − k×half_spread − add_on, k by slot."""
    bid, ask, last = 100.00, 100.10, 100.05
    hs, mid = 0.05, 100.05
    add = mid * 2.0 / 10_000
    for slot, k in (("pre-market", 1.0), ("opening-range", 1.0), ("midday", 0.0),
                    ("power-hour", 1.0), ("sentinel", 0.5), ("ad-hoc", 0.5)):
        b = fills.shadow_fill("buy", bid, ask, last, slot)
        s = fills.shadow_fill("sell", bid, ask, last, slot)
        assert b["k"] == k and s["k"] == k
        assert b["price"] == pytest.approx(ask + k * hs + add, abs=1e-6), slot
        assert s["price"] == pytest.approx(bid - k * hs - add, abs=1e-6), slot
        assert b["half_spread"] == pytest.approx(hs)
        assert b["add_on_bps"] == 2.0
        assert "ask" in b["basis"] and "bid" in s["basis"]


def test_a_buy_never_fills_at_or_inside_the_mid(fills):
    """The reason the model exists: filling at mid is a fantasy on every slot."""
    for slot in ("pre-market", "opening-range", "midday", "power-hour", "sentinel", "ad-hoc"):
        b = fills.shadow_fill("buy", 50.00, 50.20, 50.10, slot)
        s = fills.shadow_fill("sell", 50.00, 50.20, 50.10, slot)
        assert b["price"] > 50.10 and s["price"] < 50.10


def test_small_cap_add_on(fills):
    """A name under $10m of dollar ADV pays the 15 bp add-on; a large cap pays 2 bp."""
    big = fills.shadow_fill("buy", 20.00, 20.04, 20.02, "midday", adv_usd=500e6)
    small = fills.shadow_fill("buy", 20.00, 20.04, 20.02, "midday", adv_usd=3e6)
    unknown = fills.shadow_fill("buy", 20.00, 20.04, 20.02, "midday")
    assert big["add_on_bps"] == 2.0 and unknown["add_on_bps"] == 2.0
    assert small["add_on_bps"] == 15.0
    assert small["price"] - big["price"] == pytest.approx(20.02 * 13.0 / 10_000, abs=1e-6)
    assert "small cap" in small["basis"] and "large cap" in big["basis"]


def test_adv_from_a_scan_row(fills):
    assert fills.adv_usd({"adv_usd": 4e6, "price": 10.0, "volume": 1e9}) == 4e6
    assert fills.adv_usd({"price": 10.0, "avg_volume_20d": 200_000}) == 2e6
    assert fills.adv_usd({"price": 10.0, "volume": 300_000}) == 3e6
    assert fills.adv_usd({"price": 10.0}) is None
    assert fills.adv_usd(None) is None


def test_missing_quote_means_shadow_equals_booked(fills):
    for bid, ask in ((None, None), (0.0, 0.0), (101.0, 99.0), (None, 100.0)):
        s = fills.shadow_fill("sell", bid, ask, 99.75, "opening-range")
        assert s["price"] == 99.75
        assert s["basis"] == "no-quote"
        assert s["half_spread"] is None
    assert fills.decision_mid(None, None) is None
    assert fills.shortfall_bps(99.75, None) is None


def test_gap_through_stop_uses_min_of_last_and_stop(fills):
    """A stop is not resting anywhere. When the tape has printed through a stale quote,
    the shadow's reference is min(last, stop) — never the stop price and never the bid
    above it."""
    stop, last = 100.0, 90.0
    s = fills.shadow_fill("sell", 95.0, 96.0, 89.775, "midday", cap=min(last, stop))
    assert s["price"] == pytest.approx(90.0 - 95.5 * 2.0 / 10_000, abs=1e-6)
    assert s["price"] < 95.0 < stop
    # and when the quote is already below the print, the bid is the touch
    s2 = fills.shadow_fill("sell", 89.9, 90.1, 89.775, "midday", cap=min(last, stop))
    assert s2["price"] == pytest.approx(89.9 - 90.0 * 2.0 / 10_000, abs=1e-6)
    # without the cap a stale quote would have been trusted — that is the naive model
    s3 = fills.shadow_fill("sell", 95.0, 96.0, 89.775, "midday")
    assert s3["price"] == pytest.approx(95.0 - 95.5 * 2.0 / 10_000, abs=1e-6)


def test_shortfall_bps_by_hand(fills):
    # bid 222.24 / ask 222.27 → mid 222.255; a midday sale at bid − 2bp of mid
    s = fills.shadow_fill("sell", 222.24, 222.27, 222.255, "midday")
    mid = fills.decision_mid(222.24, 222.27)
    assert mid == pytest.approx(222.255)
    expected_px = 222.24 - 222.255 * 2.0 / 10_000
    assert s["price"] == pytest.approx(expected_px, abs=1e-6)
    assert fills.shortfall_bps(s["price"], mid) == pytest.approx(
        abs(expected_px - 222.255) / 222.255 * 1e4, abs=1e-3)
    # by hand: half-spread 0.015 = 0.675 bp of mid, plus 2 bp add-on = 2.675 bp
    assert fills.shortfall_bps(s["price"], mid) == pytest.approx(2.675, abs=1e-3)


def test_book_cum_gap_accumulates_and_gap_share_of_realized(fills):
    """Two fills, one sale that flattered the book and one buy that slandered it."""
    book = {"realized_pnl": 50.0}
    today = dt.date(2026, 9, 10)
    # sale booked at 100.00, shadow says 99.50 → the book took $0.50 × 10 too much
    r1 = fills.record_shadow(book, "sell", 10.0, 100.00, {"price": 99.50, "basis": "bid"},
                             100.05, today)
    assert r1["shadow_gap_usd"] == pytest.approx(5.0)
    assert book["shadow"]["cum_gap_usd"] == pytest.approx(5.0)
    assert book["shadow"]["n_fills"] == 1
    assert book["shadow"]["gap_share_of_realized_pct"] == pytest.approx(10.0)
    # buy booked at 50.00, shadow says 49.90 → the book overpaid: gap −0.10 × 20 = −2.00
    r2 = fills.record_shadow(book, "buy", 20.0, 50.00, {"price": 49.90, "basis": "ask"},
                             49.95, today)
    assert r2["shadow_gap_usd"] == pytest.approx(-2.0)
    assert book["shadow"]["cum_gap_usd"] == pytest.approx(3.0)
    assert book["shadow"]["n_fills"] == 2
    assert book["shadow"]["gap_share_of_realized_pct"] == pytest.approx(6.0)
    # shortfall × notional lands in the year's cost accumulator
    y = book["shadow"]["by_year"]["2026"]
    assert y["n_fills"] == 2 and y["n_quoted"] == 2
    exp = (abs(99.5 - 100.05) / 100.05) * 1000.0 + (abs(49.9 - 49.95) / 49.95) * 1000.0
    assert y["shortfall_usd"] == pytest.approx(exp, abs=1e-4)
    # null share when nothing is realised
    book["realized_pnl"] = 0.0
    fills.record_shadow(book, "sell", 1.0, 10.0, {"price": 10.0, "basis": "no-quote"}, None, today)
    assert book["shadow"]["gap_share_of_realized_pct"] is None
    assert fills.shadow_summary(book)["gap_share_of_realized_pct"] is None


# ------------------------------------------------------------------ the engine, OFF and ON
def _golden():
    return json.loads(ss.GOLDEN.read_text(encoding="utf-8"))


def test_off_path_is_byte_identical_to_the_pre_change_engine(pm, run_dir):
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    g = _golden()
    assert _canon(out["book"]) == _canon(g["book"])
    assert _canon(out["journal"]) == _canon(g["journal"])
    assert "shadow" not in out["book"]
    assert "equity_shadow" not in out["state"]["book"]
    assert "shadow" not in out["journal"]["entries"][-1]
    # state differs only in the rules block carrying the new (disabled) config
    assert _canon(_strip(out["state"])) == _canon(_strip(g["state"]))


def test_on_path_differs_only_in_the_new_keys(pm, run_dir):
    out = ss.replay(pm, run_dir)          # default: enabled
    g = _golden()
    assert _canon(out["book"]) != _canon(g["book"])
    assert _canon(_strip(out["book"])) == _canon(_strip(g["book"]))
    assert _canon(_strip(out["journal"])) == _canon(_strip(g["journal"]))
    assert _canon(_strip(out["state"])) == _canon(_strip(g["state"]))
    # booked prices and P&L are the pre-change numbers exactly
    assert out["book"]["realized_pnl"] == g["book"]["realized_pnl"]
    assert [c["exit"] for c in out["book"]["closed_trades"]] == \
        [c["exit"] for c in g["book"]["closed_trades"]]
    assert out["state"]["book"]["equity"] == g["state"]["book"]["equity"]


def test_every_fill_carries_a_shadow_record(pm, run_dir):
    out = ss.replay(pm, run_dir)
    fills_seen = [d for e in out["journal"]["entries"] for d in e["decisions"]
                  if d["action"] in ("fill-buy", "fill-sell")]
    assert {d["symbol"] for d in fills_seen} == {"SCHW", "NVDA", "HOOD"}
    for d in fills_seen:
        for k in ("shadow_price", "shadow_gap_usd", "implementation_shortfall_bps",
                  "decision_mid", "shadow_basis"):
            assert k in d, (d["symbol"], k)
        assert d["shadow_basis"] != "no-quote"
        assert d["implementation_shortfall_bps"] > 0
    # the closed-trade records carry the same fields
    for c in out["book"]["closed_trades"][-2:]:
        assert c["shadow_price"] is not None and c["implementation_shortfall_bps"] is not None
    # the book-level ledger counted all three
    sh = out["book"]["shadow"]
    assert sh["n_fills"] == 3
    assert sh["cum_gap_usd"] == pytest.approx(sum(d["shadow_gap_usd"] for d in fills_seen), abs=0.01)
    assert sh["gap_share_of_realized_pct"] == pytest.approx(
        sh["cum_gap_usd"] / abs(out["book"]["realized_pnl"]) * 100, abs=0.01)


def test_decision_mid_is_captured_at_placement_and_read_at_the_fill(pm, run_dir):
    """The entry is decided on run 1 (bid 92.35 / ask 92.45 → mid 92.40) and fills on run 2
    against a different quote. The shortfall must be against the decision, not the fill."""
    out = ss.replay(pm, run_dir)
    e1, e2 = out["journal"]["entries"]
    placed = next(d for d in e1["decisions"] if d["action"] == "place-buy")
    assert placed["decision_mid"] == pytest.approx(92.40)
    filled = next(d for d in e2["decisions"] if d["action"] == "fill-buy")
    assert filled["decision_mid"] == pytest.approx(92.40)
    # midday k=0: ask 92.35 + 2bp of the run-2 mid 92.30
    assert filled["shadow_price"] == pytest.approx(92.35 + 92.30 * 2.0 / 10_000, abs=1e-3)
    assert filled["implementation_shortfall_bps"] == pytest.approx(
        abs(filled["shadow_price"] - 92.40) / 92.40 * 1e4, abs=0.01)
    # buy gap sign: (shadow − booked) × shares — the book paid the 92.40 limit, the shadow
    # would have paid less, so the paper book slandered itself here (negative gap)
    assert filled["shadow_gap_usd"] == pytest.approx(
        (filled["shadow_price"] - 92.40) * filled["shares"], abs=1e-3)


def test_equity_shadow_in_state(pm, run_dir):
    out = ss.replay(pm, run_dir)
    b = out["state"]["book"]
    assert b["equity_shadow"] == pytest.approx(b["equity"] - b["shadow"]["cum_gap_usd"], abs=0.011)
    assert b["shadow"] == {k: out["book"]["shadow"][k]
                           for k in ("cum_gap_usd", "n_fills", "gap_share_of_realized_pct")}
    assert b["cost_budget"]["budget_bps"] == 200.0
    assert b["cost_budget"]["share_used_pct"] < 100
    j = out["journal"]["entries"][-1]["shadow"]
    assert j["equity_shadow"] == b["equity_shadow"]
    assert j["cost_budget"] == b["cost_budget"]


def test_gap_through_stop_in_the_exit_path(pm, run_dir):
    """A stale quote above the tape: the stop fires on the 90 print, the shadow references
    min(90, stop) and never the stop price or the bid above it."""
    today = dt.date(2026, 9, 10)
    book = {"cash": 1000.0, "realized_pnl": 0.0, "positions": [{
        "symbol": "X", "shares": 10.0, "avg_cost": 105.0, "stop": 100.0, "target": 130.0,
        "opened": "2026-09-01", "intraday_shares": 0.0, "high_water": 105.0}],
        "working_orders": [], "closed_trades": [], "day_trades": [],
        "day": {"date": today.isoformat()}}
    pb = {"X": {"price": 90.0, "fresh": True, "source": "pm-fetch", "bid": 95.0, "ask": 96.0}}
    jrn = {"slot": "midday", "ts": "2026-09-10T17:15:00Z", "decisions": [], "skipped": [],
           "warnings": [], "sentinel": False}
    pm.exit_pass(book, pb, {}, today, 1900.0, jrn)
    sale = next(d for d in jrn["decisions"] if d["action"] == "fill-sell")
    assert sale["reason"] == "stop"
    assert sale["price"] == pytest.approx(90.0 * (1 - 0.0025), abs=1e-4)   # booked: unchanged
    assert sale["shadow_price"] == pytest.approx(90.0 - 95.5 * 2.0 / 10_000, abs=1e-4)
    assert sale["shadow_price"] < 95.0 < 100.0
    # with gap_through_stops off the bid is the touch, which is what the caller asked for
    pm.PM_RULES["shadow"] = dict(pm.PM_RULES["shadow"], gap_through_stops=False)
    book2 = copy.deepcopy(book); book2["positions"] = [{
        "symbol": "X", "shares": 10.0, "avg_cost": 105.0, "stop": 100.0, "target": 130.0,
        "opened": "2026-09-01", "intraday_shares": 0.0, "high_water": 105.0}]
    jrn2 = dict(jrn, decisions=[], skipped=[], warnings=[])
    pm.exit_pass(book2, pb, {}, today, 1900.0, jrn2)
    sale2 = next(d for d in jrn2["decisions"] if d["action"] == "fill-sell")
    assert sale2["shadow_price"] == pytest.approx(95.0 - 95.5 * 2.0 / 10_000, abs=1e-4), \
        "with the rule off the stale bid is trusted — the flag must actually switch something"


def test_no_quote_fill_records_zero_gap(pm, run_dir):
    today = dt.date(2026, 9, 10)
    book = {"cash": 1000.0, "realized_pnl": 0.0, "positions": [{
        "symbol": "X", "shares": 10.0, "avg_cost": 105.0, "stop": 100.0, "target": 130.0,
        "opened": "2026-09-01", "intraday_shares": 0.0, "high_water": 105.0}],
        "working_orders": [], "closed_trades": [], "day_trades": [],
        "day": {"date": today.isoformat()}}
    pb = {"X": {"price": 90.0, "fresh": True, "source": "scan"}}       # no bid/ask
    jrn = {"slot": "midday", "ts": "2026-09-10T17:15:00Z", "decisions": [], "skipped": [],
           "warnings": [], "sentinel": False}
    pm.exit_pass(book, pb, {}, today, 1900.0, jrn)
    sale = next(d for d in jrn["decisions"] if d["action"] == "fill-sell")
    assert sale["shadow_basis"] == "no-quote"
    assert sale["shadow_price"] == sale["price"]
    assert sale["shadow_gap_usd"] == 0.0
    assert sale["implementation_shortfall_bps"] is None
    assert sale["decision_mid"] is None
    assert book["shadow"]["by_year"]["2026"]["n_quoted"] == 0


# ------------------------------------------------------------------ the cost budget
def test_cost_budget_status(fills):
    today = dt.date(2026, 9, 10)
    book = {"cash": 5000.0, "positions": [],
            "shadow": {"by_year": {"2026": {"shortfall_usd": 12.5, "notional_usd": 20000.0,
                                            "n_fills": 8, "n_quoted": 8}}},
            "equity_curve": [{"date": "2026-09-01", "equity": 4800.0},
                             {"date": "2026-09-02", "equity": 5200.0},
                             {"date": "2025-12-31", "equity": 1.0}]}     # last year: ignored
    st = fills.cost_budget_status(book, today, {"cost_budget_bps_per_year": 200})
    assert st["avg_equity"] == 5000.0
    assert st["ytd_cost_bps"] == pytest.approx(12.5 / 5000.0 * 1e4)      # 25 bp
    assert st["budget_bps"] == 200.0
    assert st["share_used_pct"] == pytest.approx(12.5)
    assert st["n_fills"] == 8 and st["year"] == "2026"
    # no curve points: the book's own mark is the denominator; no ledger: zero cost
    st0 = fills.cost_budget_status({"cash": 2500.0, "positions": [
        {"shares": 10.0, "last_price": 250.0}]}, today)
    assert st0["avg_equity"] == 5000.0 and st0["ytd_cost_bps"] == 0.0
    assert st0["share_used_pct"] == 0.0 and st0["budget_bps"] == 200.0


def test_cost_budget_warning_when_over_100_pct(pm, run_dir, quotes):
    from conftest import run_pm
    book = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    yr = str(dt.date.today().year)
    # ~$150 of shortfall on a ~$5,000 book is ~300 bp against a 200 bp budget
    book["shadow"] = {"cum_gap_usd": 0.0, "n_fills": 20, "gap_share_of_realized_pct": None,
                      "by_year": {yr: {"shortfall_usd": 150.0, "notional_usd": 60000.0,
                                       "n_fills": 20, "n_quoted": 20}}}
    (run_dir / "paper_book.json").write_text(json.dumps(book), encoding="utf-8")
    _, jrn, state = run_pm(pm, run_dir, slot="midday")
    cb = state["book"]["cost_budget"]
    assert cb["share_used_pct"] > 100
    assert any("EXECUTION COST BUDGET EXCEEDED" in w for w in jrn["warnings"])
    # and under budget there is no such warning
    book["shadow"]["by_year"][yr]["shortfall_usd"] = 1.0
    (run_dir / "paper_book.json").write_text(json.dumps(book), encoding="utf-8")
    _, jrn2, state2 = run_pm(pm, run_dir, slot="midday")
    assert state2["book"]["cost_budget"]["share_used_pct"] < 100
    assert not any("EXECUTION COST BUDGET" in w for w in jrn2["warnings"])


def test_pm_rules_carry_the_documented_defaults(pm):
    sh = pm.PM_RULES["shadow"]
    assert sh["enabled"] is True
    assert sh["k_by_slot"] == {"pre-market": 1.0, "opening-range": 1.0, "midday": 0.0,
                               "power-hour": 1.0, "sentinel": 0.5, "ad-hoc": 0.5}
    assert sh["add_on_bps_large_cap"] == 2.0 and sh["add_on_bps_small_cap"] == 15.0
    assert sh["small_cap_adv_usd"] == 10e6 and sh["gap_through_stops"] is True
    assert pm.PM_RULES["cost_budget_bps_per_year"] == 200


def test_quote_parser_carries_bid_and_ask(pm, quotes):
    prices, _ = pm.quotes_to_prices(quotes, pm._now())
    assert prices["NVDA"]["bid"] == pytest.approx(222.24)
    assert prices["NVDA"]["ask"] == pytest.approx(222.27)
    crossed = copy.deepcopy(quotes)
    crossed["data"]["results"][0]["quote"]["bid_price"] = "999"
    prices2, _ = pm.quotes_to_prices(crossed, pm._now())
    sym = crossed["data"]["results"][0]["quote"]["symbol"]
    assert prices2[sym]["bid"] is None and prices2[sym]["ask"] is None
