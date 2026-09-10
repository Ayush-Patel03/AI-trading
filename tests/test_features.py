"""technicals.features() — the S-04 research features, each pinned to a hand-built series
with a known answer; and the plumbing that carries them (backtest records, ic.py
--by-feature) end to end. Nothing here is scored, and a test asserts that too.
"""
import json
import math
import pathlib
import random
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import technicals as t  # noqa: E402


# ------------------------------------------------------------------ builders
def _day(i):
    """A synthetic, monotone calendar: day i -> 'YYYY-MM-DD' (28-day months)."""
    return f"2024-{(i // 28) % 12 + 1:02d}-{i % 28 + 1:02d}" if i < 336 else \
        f"2025-{((i - 336) // 28) % 12 + 1:02d}-{(i - 336) % 28 + 1:02d}"


def _bar(i, close, o=None, h=None, l=None, v=1_000_000):
    return {"begins_at": f"{_day(i)}T00:00:00Z",
            "open_price": str(close if o is None else o),
            "high_price": str(close * 1.01 if h is None else h),
            "low_price": str(close * 0.99 if l is None else l),
            "close_price": str(close), "volume": v, "interpolated": False}


def _from_returns(rets, start=100.0, opens="close"):
    """Bars whose close-to-close returns are `rets`. opens='close': open == close (all the
    move is overnight); opens='prev': open == prior close (all the move is intraday)."""
    bars, c = [], start
    bars.append(_bar(0, c))
    for i, r in enumerate(rets, 1):
        nc = c * (1.0 + r)
        bars.append(_bar(i, nc, o=(nc if opens == "close" else c)))
        c = nc
    return bars


def _flat(n, close=100.0):
    return [_bar(i, close) for i in range(n)]


def _linear(n, start=100.0, step=1.0):
    return [_bar(i, start + i * step) for i in range(n)]


# ------------------------------------------------------------------ nulls, not zeros
def test_short_history_gives_nulls_never_zeros():
    out = t.features(_flat(4))               # too short even for ret_5d
    assert set(out) == set(t.FEATURE_KEYS)
    assert all(v is None for v in out.values()), out
    # ten flat bars DO cover ret_5d, and a flat series really did return zero: that is a
    # measured 0.0, not a placeholder — the two must stay distinguishable
    ten = t.features(_flat(10))
    assert ten["ret_5d"] == 0.0 and ten["ret_1m"] is None


def test_every_key_is_present_even_on_empty_input():
    out = t.features([])
    assert set(out) == set(t.FEATURE_KEYS)
    assert all(v is None for v in out.values())


def test_the_long_windows_are_null_until_the_history_covers_them():
    out = t.features(_linear(100))
    assert out["ret_1m"] is not None and out["ret_5d"] is not None
    assert out["ret_12_7"] is None and out["ret_12_1"] is None
    assert out["ret_6_2"] is None, "126 sessions back needs 127 closes"
    assert out["close_to_52wk_high"] is None
    assert out["beta_252"] is None and out["resid_mom_12_1"] is None


# ------------------------------------------------------------------ window returns
def test_window_returns_use_the_stated_session_offsets():
    bars = _linear(300)                      # close[i] = 100 + i, last index 299
    out = t.features(bars)
    c = lambda k: 100.0 + (299 - k)          # close k sessions before the last bar
    assert out["ret_12_7"] == pytest.approx(c(147) / c(252) - 1)
    assert out["ret_6_2"] == pytest.approx(c(42) / c(126) - 1)
    assert out["ret_12_1"] == pytest.approx(c(21) / c(252) - 1)
    assert out["ret_1m"] == pytest.approx(c(0) / c(21) - 1)
    assert out["ret_5d"] == pytest.approx(c(0) / c(5) - 1)


def test_a_single_ten_percent_day_sets_max_1m_and_the_one_month_return():
    rets = [0.0] * 40
    rets[-3] = 0.10                          # inside the last 21 sessions
    out = t.features(_from_returns(rets))
    assert out["max_1m"] == pytest.approx(0.10)
    assert out["ret_1m"] == pytest.approx(0.10)
    assert out["ret_5d"] == pytest.approx(0.10)


def test_max_1m_ignores_a_spike_older_than_a_month():
    rets = [0.0] * 60
    rets[10] = 0.25
    out = t.features(_from_returns(rets))
    assert out["max_1m"] == pytest.approx(0.0)


def test_close_to_52wk_high_is_close_over_the_max_high():
    bars = _linear(260)                      # rising: the last bar's high is the max
    out = t.features(bars)
    last = 100.0 + 259
    assert out["close_to_52wk_high"] == pytest.approx(last / (last * 1.01))
    # a spike high 100 sessions ago dominates
    bars[-100]["high_price"] = str(10_000.0)
    assert t.features(bars)["close_to_52wk_high"] == pytest.approx(last / 10_000.0)


# ------------------------------------------------------------------ volatility
def test_rv_20d_matches_the_annualised_sample_std_of_log_returns():
    a = 0.01
    rets = [math.exp(a * (1 if i % 2 else -1)) - 1 for i in range(40)]
    out = t.features(_from_returns(rets))
    logs = [a * (1 if i % 2 else -1) for i in range(20, 40)]
    m = sum(logs) / 20
    sd = math.sqrt(sum((x - m) ** 2 for x in logs) / 19)
    assert out["rv_20d"] == pytest.approx(sd * math.sqrt(252))


def test_atr_pct_is_the_wilder_atr_over_close_as_a_fraction():
    bars = _flat(40)                         # every bar: high 101, low 99, close 100
    out = t.features(bars)
    assert out["atr_pct"] == pytest.approx(2.0 / 100.0, abs=1e-9)
    # and the row-level derive() reports the same quantity in percent
    assert t.derive(bars)["atr_pct"] == pytest.approx(2.0, abs=1e-9)


def test_turnover_needs_shares_outstanding():
    bars = _flat(40)
    assert t.features(bars)["turnover_20d"] is None
    assert t.features(bars, shares_outstanding=100_000_000)["turnover_20d"] == pytest.approx(0.01)
    assert t.features(bars, shares_outstanding=0)["turnover_20d"] is None


# ------------------------------------------------------------------ relative strength
def test_relative_strength_features_are_differences_of_20_session_returns():
    stock = _from_returns([0.0] * 30 + [0.10] + [0.0] * 5)      # +10% inside 20 sessions
    spy = _from_returns([0.0] * 30 + [0.04] + [0.0] * 5)
    sec = _from_returns([0.0] * 30 + [0.07] + [0.0] * 5)
    out = t.features(stock, spy_bars=spy, sector_bars=sec)
    assert out["rs_20d_vs_spy"] == pytest.approx(0.06)
    assert out["industry_rs_20d"] == pytest.approx(0.03)
    assert out["stock_vs_industry_rs_20d"] == pytest.approx(0.03)
    alone = t.features(stock)
    assert alone["rs_20d_vs_spy"] is None
    assert alone["industry_rs_20d"] is None and alone["stock_vs_industry_rs_20d"] is None
    no_sector = t.features(stock, spy_bars=spy)
    assert no_sector["rs_20d_vs_spy"] == pytest.approx(0.06)
    assert no_sector["industry_rs_20d"] is None


# ------------------------------------------------------------------ residual momentum
def _spy_returns(n, seed=11):
    rng = random.Random(seed)
    return [rng.gauss(0.0004, 0.01) for _ in range(n)]


def test_a_stock_that_is_exactly_1_5x_spy_has_beta_1_5_and_zero_residuals():
    spy_r = _spy_returns(300)
    spy = _from_returns(spy_r)
    stock = _from_returns([1.5 * r for r in spy_r])
    out = t.features(stock, spy_bars=spy)
    assert out["beta_252"] == pytest.approx(1.5, abs=1e-9)
    assert out["resid_mom_12_1"] == pytest.approx(0.0, abs=1e-9)
    assert out["ivol_20d"] == pytest.approx(0.0, abs=1e-9)


def test_residual_momentum_is_the_idiosyncratic_return_excluding_the_last_month():
    spy_r = _spy_returns(300, seed=5)
    alpha = 0.001                              # a constant daily excess over 1.2x SPY
    stock = _from_returns([1.2 * r + alpha for r in spy_r])
    out = t.features(stock, spy_bars=_from_returns(spy_r))
    # no intercept (see the docstring): the alpha stays in the residual, so the 12-1 sum
    # is ~231 sessions of it, and the slope is 1.2 up to the alpha's tiny leverage on it
    assert out["beta_252"] == pytest.approx(1.2, abs=0.02)
    assert out["resid_mom_12_1"] == pytest.approx(231 * alpha, rel=0.05)
    # a one-off +5% idiosyncratic day 100 sessions ago adds ~0.05 to the 12-1 sum ...
    rets = [1.2 * r + alpha for r in spy_r]
    rets[-100] += 0.05
    out2 = t.features(_from_returns(rets), spy_bars=_from_returns(spy_r))
    assert out2["resid_mom_12_1"] - out["resid_mom_12_1"] == pytest.approx(0.05, abs=0.005)
    # ... and one 10 sessions ago (inside the skipped month) adds nothing to it but
    # shows up in the 20-day idiosyncratic vol
    rets = [1.2 * r + alpha for r in spy_r]
    rets[-10] += 0.05
    out3 = t.features(_from_returns(rets), spy_bars=_from_returns(spy_r))
    assert out3["resid_mom_12_1"] - out["resid_mom_12_1"] == pytest.approx(0.0, abs=0.005)
    assert out3["ivol_20d"] > out["ivol_20d"]


def test_with_an_intercept_the_window_sum_would_collapse_to_a_reversal():
    """The reason the regression is through the origin: an intercept forces the residuals
    to sum to zero over the sample, so the 12-1 sum is minus the last month's."""
    spy_r = _spy_returns(300, seed=8)
    rng = random.Random(1)
    y = [1.1 * r + rng.gauss(0, 0.01) for r in spy_r][-252:]
    x = spy_r[-252:]
    _, _, resid = t.ols(y, [x], intercept=True)
    assert sum(resid[:-21]) == pytest.approx(-sum(resid[-21:]), abs=1e-9)
    _, _, resid0 = t.ols(y, [x], intercept=False)
    assert sum(resid0) != pytest.approx(0.0, abs=1e-6)


def test_residual_momentum_is_null_without_the_benchmark():
    out = t.features(_from_returns(_spy_returns(300)))
    assert out["beta_252"] is None and out["resid_mom_12_1"] is None and out["ivol_20d"] is None


def test_the_regression_aligns_on_dates_not_positions():
    spy_r = _spy_returns(300, seed=3)
    spy = _from_returns(spy_r)
    stock = _from_returns([1.5 * r for r in spy_r])
    # the stock misses one session in the middle: an index-based regression would be
    # shifted by a day from there on and the beta would collapse toward zero
    del stock[150]
    out = t.features(stock, spy_bars=spy)
    # not exactly 1.5: the stock's two-day compounded return across the gap is not 1.5x
    # SPY's compounded return. Index-based alignment would give a beta near zero.
    assert out["beta_252"] == pytest.approx(1.5, abs=1e-3)


# ------------------------------------------------------------------ overnight share
def test_overnight_share_is_one_when_all_the_move_is_overnight():
    rets = [0.01 * (1 if i % 3 else -1) for i in range(40)]
    out = t.features(_from_returns(rets, opens="close"))
    assert out["overnight_share_20d"] == pytest.approx(1.0)


def test_overnight_share_is_zero_when_all_the_move_is_intraday():
    rets = [0.01 * (1 if i % 3 else -1) for i in range(40)]
    out = t.features(_from_returns(rets, opens="prev"))
    assert out["overnight_share_20d"] == pytest.approx(0.0)


def test_overnight_share_is_null_when_the_close_to_close_sum_is_zero():
    out = t.features(_flat(40))
    assert out["overnight_share_20d"] is None


# ------------------------------------------------------------------ input shapes
def test_the_plain_bar_shape_is_accepted():
    bars = [{"date": _day(i), "open": 100 + i, "high": 101 + i, "low": 99 + i,
             "close": 100 + i, "volume": 1000} for i in range(40)]
    out = t.features(bars)
    assert out["ret_5d"] == pytest.approx(139 / 134 - 1)


def test_interpolated_bars_are_ignored():
    bars = _flat(40)
    bars[-1]["close_price"] = "999"
    bars[-1]["interpolated"] = True
    assert t.features(bars)["ret_5d"] == pytest.approx(0.0)


# ------------------------------------------------------------------ the CLI path
def test_the_cli_emits_features_per_symbol(tmp_path, monkeypatch, capsys):
    spy_r = _spy_returns(300, seed=2)
    payload = {"data": {"results": [
        {"symbol": "AAA", "bars": _from_returns([1.5 * r for r in spy_r])},
        # the sector ETF must not be an exact multiple of SPY: a collinear design is
        # singular and the regression (rightly) returns nothing
        {"symbol": "XLK", "bars": _from_returns(_spy_returns(300, seed=7))},
        {"symbol": "SPY", "bars": _from_returns(spy_r)},
    ]}}
    (tmp_path / "bars.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "sectors.json").write_text(json.dumps({"AAA": "XLK"}), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["technicals.py", "--bars", str(tmp_path / "bars.json"),
                                      "--sector-map", str(tmp_path / "sectors.json"),
                                      "--out", str(tmp_path / "technicals.json")])
    t.main()
    out = json.loads((tmp_path / "technicals.json").read_text(encoding="utf-8"))
    f = out["AAA"]["features"]
    assert set(f) == set(t.FEATURE_KEYS)
    assert f["beta_252"] == pytest.approx(1.5, abs=1e-6)
    assert f["industry_rs_20d"] is not None
    assert out["AAA"]["rs_20d_vs_SPY"] == pytest.approx(f["rs_20d_vs_spy"] * 100.0)


# ------------------------------------------------------------------ scanner pass-through
def test_the_scanner_logs_features_and_scores_none_of_them():
    import scanner
    base = {"name": "AAA", "price": 110.0, "ma_50": 105.0, "ma_200": 100.0, "rsi_14": 60.0,
            "week52_change_pct": 20.0, "week52_high": 112.0, "week52_low": 80.0,
            "avg_volume_20d": 1e6, "volume": 1e6}
    data = lambda c: {"meta": {"scan_date": "2026-09-10", "slot": "Backtest"},
                      "regime": {}, "candidates": {"AAA": c}}
    plain = scanner.scan(data(dict(base)))["results"][0]
    feats = {k: 0.5 for k in t.FEATURE_KEYS}
    with_f = scanner.scan(data({**base, "features": feats}))["results"][0]
    assert "features" not in plain
    assert with_f["features"] == feats
    for k in ("score", "pillars", "raw_score", "verdict", "setup"):
        assert with_f[k] == plain[k], f"{k} moved when features were attached"


# ------------------------------------------------------------------ backtest -> ic.py
def _series(n, seed):
    rng = random.Random(seed)
    return _from_returns([rng.gauss(0.0005, 0.015) for _ in range(n - 1)])


def test_backtest_records_carry_features(run_dir):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    payload = {"data": {"results": [{"symbol": "AAA", "bars": _series(300, 1)},
                                    {"symbol": "BBB", "bars": _series(300, 2)},
                                    {"symbol": "SPY", "bars": _series(300, 3)}]}}
    p = run_dir / "bars.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    start, end = _day(270), _day(299)
    rc = bt.main(["--bars", str(p), "--start", start, "--end", end, "--every", "5",
                  "--out-records", str(run_dir / "records")])
    assert rc == 0
    recs = sorted((run_dir / "records").glob("*.json"))
    assert recs
    rows = json.loads(recs[-1].read_text(encoding="utf-8"))["results"]
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}
    for r in rows:
        f = r["features"]
        assert set(f) == set(t.FEATURE_KEYS)
        assert f["ret_12_7"] is not None and f["beta_252"] is not None
        assert f["rv_20d"] is not None and f["overnight_share_20d"] is not None
        assert f["turnover_20d"] is None, "no shares outstanding were supplied"
        assert f["industry_rs_20d"] is None, "no sector map was supplied"


def test_ic_by_feature_runs_end_to_end_on_backtest_records(run_dir, capsys):
    import importlib
    import backtest as bt
    import ic
    importlib.reload(bt)
    importlib.reload(ic)
    syms = [f"S{i}" for i in range(6)]
    payload = {"data": {"results": [{"symbol": s, "bars": _series(300, 10 + i)}
                                    for i, s in enumerate(syms)]
                        + [{"symbol": "SPY", "bars": _series(300, 99)}]}}
    p = run_dir / "bars.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    rc = bt.main(["--bars", str(p), "--start", _day(255), "--end", _day(290), "--every", "5",
                  "--out-records", str(run_dir / "records")])
    assert rc == 0
    md = run_dir / "ic.md"
    rc = ic.main(["--records", str(run_dir / "records"), "--bars", str(p),
                  "--horizons", "5", "--by-feature", "--md", str(md)])
    assert rc == 0
    text = md.read_text(encoding="utf-8")
    for k in ("ret_12_7", "ret_1m", "max_1m", "beta_252", "rv_20d"):
        assert f"| {k} |" in text, f"{k} missing from the per-feature table"
    assert "feature(s) tabulated" in capsys.readouterr().out


def test_sector_map_and_shares_outstanding_flow_into_backtest_features(run_dir):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    payload = {"data": {"results": [{"symbol": "AAA", "bars": _series(300, 1)},
                                    {"symbol": "XLK", "bars": _series(300, 4)},
                                    {"symbol": "SPY", "bars": _series(300, 3)}]}}
    p = run_dir / "bars.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "sec.json").write_text(json.dumps({"AAA": "XLK"}), encoding="utf-8")
    (run_dir / "sh.json").write_text(json.dumps({"AAA": 50_000_000}), encoding="utf-8")
    out = bt.replay(bt.load_bars(str(p)), _day(299), sector_map=bt.load_sector_map(str(run_dir / "sec.json")),
                    shares_outstanding=bt.load_shares(str(run_dir / "sh.json")))
    rows = {r["ticker"]: r for r in out["results"]}
    assert set(rows) == {"AAA"}, "the sector ETF is a benchmark, not a candidate"
    f = rows["AAA"]["features"]
    assert f["industry_rs_20d"] is not None and f["stock_vs_industry_rs_20d"] is not None
    assert f["turnover_20d"] == pytest.approx(1_000_000 / 50_000_000)
