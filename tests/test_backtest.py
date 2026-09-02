"""The backtest harness — and above all, the no-look-ahead guarantee.

A backtest with hindsight in it is worse than no backtest: it produces a confident number
that is wrong in the flattering direction, and nothing downstream can detect it. So the
boundary gets tested directly, several ways, including the adversarial one — bars AFTER the
as-of date are present in the input and must change nothing.
"""
import json

import pytest

from conftest import ENGINE


@pytest.fixture
def bt(run_dir):
    import importlib
    import backtest as b
    importlib.reload(b)
    return b


def _bar(day, close, *, o=None, h=None, l=None, v=1_000_000):
    return {"begins_at": f"{day}T00:00:00Z",
            "open_price": f"{o if o is not None else close}",
            "high_price": f"{h if h is not None else close * 1.01}",
            "low_price": f"{l if l is not None else close * 0.99}",
            "close_price": f"{close}", "volume": v, "interpolated": False}


def _series(n, start=100.0, step=0.5, first_day=1):
    """n consecutive 'sessions' on a synthetic calendar, rising steadily."""
    out = []
    d = 0
    for i in range(n):
        d += 1
        day = f"2025-{(d // 28) + 1:02d}-{(d % 28) + 1:02d}"
        out.append(_bar(day, start + i * step))
    return out


def _bars_file(run_dir, symbols):
    payload = {"data": {"results": [{"symbol": s, "bars": b} for s, b in symbols.items()]}}
    p = run_dir / "bars.json"
    p.write_text(json.dumps(payload))
    return str(p)


# ------------------------------------------------------------------ the boundary
def test_upto_excludes_every_later_bar(bt):
    bars = [_bar("2025-01-01", 10), _bar("2025-01-02", 11), _bar("2025-01-03", 12)]
    kept = bt.upto(bars, "2025-01-02")
    assert [bt.bar_date(b) for b in kept] == ["2025-01-01", "2025-01-02"]


def test_upto_is_inclusive_of_the_as_of_date(bt):
    bars = [_bar("2025-01-02", 11)]
    assert len(bt.upto(bars, "2025-01-02")) == 1


def test_future_bars_change_nothing_about_a_replay(bt, run_dir):
    """The adversarial test. Same as-of date, one input with the future in it and one
    without, and the scored output must be byte-identical."""
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    future = hist + [_bar("2026-12-01", 9999.0), _bar("2026-12-02", 0.01)]

    short = bt.load_bars(_bars_file(run_dir, {"AAA": list(hist), "SPY": list(hist)}))
    a = bt.replay(short, as_of)
    long = bt.load_bars(_bars_file(run_dir, {"AAA": list(future), "SPY": list(future)}))
    b = bt.replay(long, as_of)

    assert a is not None and b is not None
    assert json.dumps(a["results"], sort_keys=True) == json.dumps(b["results"], sort_keys=True), \
        "a bar dated after the as-of date reached the score — this is look-ahead bias"


def test_a_symbol_that_did_not_trade_that_session_is_skipped(bt, run_dir):
    """Its last bar predates the as-of date, so its 'price' would be a stale close carried
    forward as if it were that day's — a silent, systematic mispricing."""
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    stale = hist[:-3]
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "OLD": stale, "SPY": hist}))
    out = bt.replay(bars, as_of)
    assert {r["ticker"] for r in out["results"]} == {"AAA"}


def test_a_name_without_enough_history_is_not_scored(bt, run_dir):
    short = _series(60)
    full = _series(260)
    as_of = bt.bar_date(full[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"NEW": short, "AAA": full, "SPY": full}))
    out = bt.replay(bars, as_of)
    assert {r["ticker"] for r in out["results"]} == {"AAA"}, \
        "no 200-day MA means no trend pillar and no setup classification"


# ------------------------------------------------------------------ the honest subset
def test_only_trend_and_momentum_are_scored(bt, run_dir):
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist}))
    row = bt.replay(bars, as_of)["results"][0]
    assert sorted(row["missing_pillars"]) == ["catalyst", "fundamentals", "intelligence"]
    assert row["coverage_pct"] == 40.0
    assert row["pillars"]["trend"] > 0


def test_a_partial_run_can_never_claim_strong_buy(bt, run_dir):
    """40% coverage is below MIN_COVERAGE_FOR_STRONG, so the cap fires on every row. That
    is correct and it is worth pinning: no backtest row may be quoted as a Strong Buy."""
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist}))
    out = bt.replay(bars, as_of)
    assert all(r["verdict"] != "Strong Buy" for r in out["results"])


def test_both_warnings_ride_on_every_record(bt, run_dir):
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist}))
    warnings = bt.replay(bars, as_of)["meta"]["data_warnings"]
    assert any("SURVIVORSHIP" in w for w in warnings)
    assert any("PARTIAL MODEL" in w for w in warnings)


# ------------------------------------------------------------------ regime
def test_breadth_is_computed_from_the_universe_itself(bt, run_dir):
    up, down = _series(260), _series(260, start=200.0, step=-0.5)
    as_of = bt.bar_date(up[-1])
    syms = {f"U{i}": list(up) for i in range(8)}
    syms.update({f"D{i}": list(down) for i in range(4)})
    syms["SPY"] = list(up)
    out = bt.replay(bt.load_bars(_bars_file(run_dir, syms)), as_of)
    breadth = out["regime"].get("breadth") or {}
    assert breadth.get("pct_above_50dma") == pytest.approx(66.7, abs=0.5), \
        "8 of 12 names above their own 50-day"


def test_vix_is_absent_rather_than_proxied(bt, run_dir):
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    out = bt.replay(bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist})), as_of)
    assert out["regime"].get("vix") is None, \
        "a volatility proxy in the vix field is a number that means something else"


# ------------------------------------------------------------------ point-in-time fundamentals
def test_financials_without_an_availability_date_are_refused(bt, run_dir):
    p = run_dir / "fin.json"
    p.write_text(json.dumps({"AAA": [{"revenue_growth_pct": 20.0}]}))
    with pytest.raises(SystemExit) as e:
        bt.load_financials(str(p))
    assert "available_from" in str(e.value)


def test_financials_use_only_what_was_public(bt):
    rows = [{"available_from": "2025-02-01", "revenue_growth_pct": 10.0},
            {"available_from": "2025-05-01", "revenue_growth_pct": 20.0}]
    assert bt.financials_asof(rows, "2025-01-15") == {}
    assert bt.financials_asof(rows, "2025-03-01")["revenue_growth_pct"] == 10.0
    assert bt.financials_asof(rows, "2025-06-01")["revenue_growth_pct"] == 20.0


def test_financials_restore_the_fundamentals_pillar_when_supplied(bt, run_dir):
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist}))
    fin = {"AAA": [{"available_from": "2025-01-01", "revenue_growth_pct": 30.0,
                    "profit_margin_pct": 25.0}]}
    out = bt.replay(bars, as_of, financials=fin)
    row = out["results"][0]
    assert "fundamentals" not in row["missing_pillars"]
    assert row["coverage_pct"] > 40.0


# ------------------------------------------------------------------ the calendar and the CLI
def test_rebalance_dates_walk_the_benchmarks_own_calendar(bt, run_dir):
    hist = _series(260)
    bars = bt.load_bars(_bars_file(run_dir, {"SPY": hist}))
    every5 = bt.rebalance_dates(bars, "SPY", "2025-01-01", "2026-12-31", 5)
    every1 = bt.rebalance_dates(bars, "SPY", "2025-01-01", "2026-12-31", 1)
    assert len(every1) == 260
    assert len(every5) == 52
    assert every5 == every1[::5]


def test_the_cli_writes_records_validate_can_read(bt, run_dir):
    hist = _series(260)
    bars_path = _bars_file(run_dir, {"AAA": list(hist), "BBB": list(hist), "SPY": list(hist)})
    start, end = bt.bar_date(hist[230]), bt.bar_date(hist[-1])
    rc = bt.main(["--bars", bars_path, "--start", start, "--end", end, "--every", "5",
                  "--out-records", str(run_dir / "records")])
    assert rc == 0
    import validate
    recs = validate.load_records(str(run_dir / "records"))
    assert recs, "validate.py must be able to load what the backtest wrote — same format"
    obs = validate.observations(recs)
    assert obs, "and turn them into observations without any adaptation"


def test_it_refuses_without_the_benchmark(bt, run_dir):
    hist = _series(260)
    bars_path = _bars_file(run_dir, {"AAA": hist})
    assert bt.main(["--bars", bars_path, "--start", "2025-01-01", "--end", "2026-01-01",
                    "--out-records", str(run_dir / "r2")]) == 2
    assert not (run_dir / "r2" / "").exists() or not list((run_dir / "r2").glob("*.json"))


def test_a_replay_is_deterministic(bt, run_dir):
    hist = _series(260)
    as_of = bt.bar_date(hist[-1])
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": list(hist), "SPY": list(hist)}))
    a = json.dumps(bt.replay(bars, as_of)["results"], sort_keys=True)
    b = json.dumps(bt.replay(bars, as_of)["results"], sort_keys=True)
    assert a == b
