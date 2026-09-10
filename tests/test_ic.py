"""ic.py — per-date rank IC, the Newey-West t-statistic and the quantile spread.

The statistics are checked against hand-computed cases, not against each other.
"""
import json
import math
import random
import statistics

import pytest

from conftest import ENGINE


@pytest.fixture
def icm(run_dir):
    import importlib
    import ic as m
    importlib.reload(m)
    return m


# ------------------------------------------------------------------ statistics
def test_spearman_matches_a_hand_computed_case(icm):
    # d = rank(x) - rank(y) = [-1, 1, -1, 1, 0]; sum d^2 = 4; rho = 1 - 6*4 / (5*24) = 0.8
    rho, p, n = icm.spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5])
    assert n == 5
    assert rho == pytest.approx(0.8)
    assert 0 < p < 1


def test_spearman_with_ties_uses_average_ranks(icm):
    # ranks of y = [1, 2, 3.5, 5, 3.5]; sxy = 8, sxx = 10, syy = 9.5 -> 8 / sqrt(95)
    rho, _, _ = icm.spearman([1, 2, 3, 4, 5], [5, 6, 7, 8, 7])
    assert rho == pytest.approx(8 / math.sqrt(95))


def test_newey_west_with_lag_zero_is_the_plain_variance(icm):
    rng = random.Random(3)
    xs = [rng.gauss(0, 1) for _ in range(50)]
    assert icm.newey_west_variance(xs, 0) == pytest.approx(statistics.pvariance(xs))
    # and the t-statistic is then the ordinary one
    t = icm.nw_tstat(xs, 0)
    assert t == pytest.approx(statistics.fmean(xs) / math.sqrt(statistics.pvariance(xs) / len(xs)))


def test_newey_west_lag_one_matches_the_formula_by_hand(icm):
    # xs = [1, 3, 2, 4]: m = 2.5, d = [-1.5, .5, -.5, 1.5]
    # gamma_0 = 1.25, gamma_1 = (-.75 - .25 - .75) / 4 = -0.4375
    # S = gamma_0 + 2 * (1 - 1/2) * gamma_1 = 1.25 - 0.4375 = 0.8125
    assert icm.newey_west_variance([1, 3, 2, 4], 1) == pytest.approx(0.8125)


def test_newey_west_variance_is_never_negative_and_lag_is_capped(icm):
    xs = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]          # perfectly negatively autocorrelated
    assert icm.newey_west_variance(xs, 100) >= 0.0
    assert icm.newey_west_variance(xs, 100) == icm.newey_west_variance(xs, len(xs) - 1)


def test_positive_autocorrelation_widens_the_variance(icm):
    # a slow sine: strongly positively autocorrelated, so the HAC variance must exceed
    # the naive one — the whole reason the correction exists for overlapping horizons
    xs = [math.sin(i / 10.0) for i in range(120)]
    assert icm.newey_west_variance(xs, 10) > icm.newey_west_variance(xs, 0)


def test_block_bootstrap_is_deterministic_and_brackets_the_mean(icm):
    rng = random.Random(7)
    s = [rng.gauss(1.0, 0.5) for _ in range(40)]
    lo, hi = icm.block_bootstrap_ci(s, block=5)
    assert lo < statistics.fmean(s) < hi
    assert icm.block_bootstrap_ci(s, block=5) == (lo, hi), "same input, same interval"
    assert icm.block_bootstrap_ci([1.0], block=5) is None


# ------------------------------------------------------------------ the table
def _monotone(n_dates=20, n_syms=15, noise=0.0, seed=1, sign=1.0):
    """Observations whose forward return is a monotone function of the score."""
    rng = random.Random(seed)
    obs = []
    for d in range(n_dates):
        date = f"2025-01-{d + 1:02d}"
        for s in range(n_syms):
            score = float(rng.randrange(0, 100))
            fwd = sign * score * 0.1 + rng.gauss(0, noise)
            obs.append({"date": date, "symbol": f"S{s}", "score": score,
                        "fwd": {5: fwd, 10: fwd * 2},
                        "features": {"good": fwd, "bad": -fwd, "flat": 1.0}})
    return obs


def test_quintile_spread_is_positive_on_a_monotone_dataset(icm):
    res = icm.summarise(_monotone(noise=0.5), horizons=[5, 10])
    t = res["score"]["5"]
    assert t["n"] == 300 and t["n_dates"] == 20
    assert t["ic_mean"] > 0.9
    assert t["ic_tstat_nw"] > 3
    assert t["spread"]["quantile"] == 5 and t["spread"]["label"] == "quintile"
    assert t["spread"]["mean_pct"] > 0
    lo, hi = t["spread"]["ci90_pct"]
    assert 0 < lo < t["spread"]["mean_pct"] < hi
    assert t["spread"]["block"] == 5
    assert t["nw_lag"] == 5
    # the sign flips with the relationship
    neg = icm.summarise(_monotone(noise=0.5, sign=-1.0), horizons=[5])["score"]["5"]
    assert neg["ic_mean"] < -0.9 and neg["spread"]["mean_pct"] < 0
    assert neg["spread"]["ci90_pct"][1] < 0


def test_small_cross_sections_fall_back_to_terciles(icm):
    res = icm.summarise(_monotone(n_syms=8), horizons=[5])
    assert res["score"]["5"]["spread"]["quantile"] == 3
    assert res["score"]["5"]["spread"]["mean_pct"] > 0


def test_a_date_with_too_few_names_contributes_no_ic(icm):
    obs = _monotone(n_dates=3, n_syms=15) + _monotone(n_dates=1, n_syms=3, seed=9)
    # the second batch reuses date 2025-01-01 -> that date now has 18 names; add a lone date
    obs += [{"date": "2025-02-01", "symbol": "X", "score": 1.0, "fwd": {5: 1.0}, "features": {}},
            {"date": "2025-02-01", "symbol": "Y", "score": 2.0, "fwd": {5: 2.0}, "features": {}}]
    res = icm.summarise(obs, horizons=[5])
    assert res["n_dates"] == 4
    assert res["score"]["5"]["n_dates"] == 3, "the two-name date has no cross-section"


def test_by_feature_mode_tabulates_every_feature(icm):
    res = icm.summarise(_monotone(noise=0.2), horizons=[5], by_feature=True)
    f = res["features"]
    assert set(f) == {"good", "bad", "flat"}
    assert f["good"]["5"]["ic_mean"] == pytest.approx(1.0)
    assert f["bad"]["5"]["ic_mean"] == pytest.approx(-1.0)
    assert f["bad"]["5"]["spread"]["mean_pct"] < 0 < f["good"]["5"]["spread"]["mean_pct"]
    # a constant ranks nothing: no IC, and no spread manufactured out of tie order
    assert f["flat"]["5"]["n_dates"] == 0
    assert f["flat"]["5"]["ic_mean"] is None
    assert f["flat"]["5"]["spread"]["mean_pct"] is None
    assert "features" not in icm.summarise(_monotone(), horizons=[5])


def test_in_sample_metrics_are_horizon_keyed_and_compact(icm):
    res = icm.summarise(_monotone(noise=0.5), horizons=[5, 10])
    m = icm.in_sample_metrics(res)
    assert set(m) == {"5", "10"}
    assert set(m["5"]) == {"n", "n_dates", "ic_mean", "ic_tstat_nw", "pooled_spearman",
                           "pooled_p", "spread_pct", "spread_ci90_pct", "spread_quantile"}


# ------------------------------------------------------------------ loaders and CLI
def _bar(day, close):
    return {"begins_at": f"{day}T00:00:00Z", "close_price": f"{close}", "interpolated": False}


def test_archive_records_plus_bars_go_through_validate_loaders(icm, tmp_path):
    """The same record format backtest.py writes and validate.py reads — no adaptation."""
    days = [f"2025-03-{i:02d}" for i in range(1, 28)]
    syms = {f"S{i}": i for i in range(6)}          # S5 rises fastest
    bars = {"data": {"results": [
        {"symbol": s, "bars": [_bar(d, 100 + k * j) for j, d in enumerate(days)]}
        for s, k in syms.items()]}}
    (tmp_path / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    recs = tmp_path / "records"
    recs.mkdir()
    for d in days[:8]:
        rec = {"date": d, "slot": "Backtest", "time": "16:00", "run_id": f"{d}-backtest",
               "results": [{"ticker": s, "score": 10.0 * k, "price": 100.0 + k * days.index(d),
                            "atr_pct": float(k), "rs_20d_vs_spy": -float(k)}
                           for s, k in syms.items()]}
        (recs / f"{d}-backtest.json").write_text(json.dumps(rec), encoding="utf-8")
    obs = icm.load_observations(str(recs), str(tmp_path / "bars.json"), [5, 10])
    assert len(obs) == 48
    assert all(5 in o["fwd"] and 10 in o["fwd"] for o in obs)
    assert obs[0]["features"]["atr_pct"] is not None
    res = icm.summarise(obs, [5], by_feature=True)
    assert res["score"]["5"]["ic_mean"] == pytest.approx(1.0)
    assert res["features"]["rs_20d_vs_spy"]["5"]["ic_mean"] == pytest.approx(-1.0)


def test_archive_records_without_bars_are_refused(icm, tmp_path):
    recs = tmp_path / "records"
    recs.mkdir()
    (recs / "x.json").write_text(json.dumps({"date": "2025-01-01", "results": [
        {"ticker": "A", "score": 1}]}), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        icm.load_observations(str(recs), None, [5])
    assert "--bars" in str(e.value)


def test_cli_writes_json_and_markdown(icm, tmp_path, capsys):
    obs = _monotone(noise=0.5)
    src = tmp_path / "obs.json"
    src.write_text(json.dumps([{**o, "fwd": {str(k): v for k, v in o["fwd"].items()}}
                               for o in obs]), encoding="utf-8")
    out_json, out_md = tmp_path / "ic.json", tmp_path / "ic.md"
    rc = icm.main(["--records", str(src), "--horizons", "5,10", "--by-feature",
                   "--json", str(out_json), "--md", str(out_md)])
    assert rc == 0
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert res["horizons"] == [5, 10]
    assert res["score"]["5"]["ic_mean"] > 0.9
    assert set(res["features"]) == {"good", "bad", "flat"}
    assert len(res["observations"]) == 300
    md = out_md.read_text(encoding="utf-8")
    assert "## 5-session horizon" in md and "| score |" in md and "| good |" in md
    printed = capsys.readouterr().out
    assert "5d" in printed and "3 feature(s)" in printed
    # and the json it wrote is itself a valid --records input (round trip)
    rc2 = icm.main(["--records", str(out_json), "--horizons", "5"])
    assert rc2 == 0


def test_backtest_appends_a_ledger_row_with_the_ic_summary(run_dir, tmp_path, capsys):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    import ledger
    from test_backtest import _series, _bars_file
    hist = _series(260)
    bars_path = _bars_file(run_dir, {"AAA": list(hist), "BBB": list(hist),
                                     "CCC": list(hist), "DDD": list(hist),
                                     "EEE": list(hist), "SPY": list(hist)})
    start, end = bt.bar_date(hist[225]), bt.bar_date(hist[-12])
    led = tmp_path / "exp" / "ledger.jsonl"
    rc = bt.main(["--bars", bars_path, "--start", start, "--end", end, "--every", "5",
                  "--out-records", str(run_dir / "records"), "--ledger", str(led),
                  "--experiment-id", "E10", "--hypothesis", "window split",
                  "--config-diff", '{"features": ["ret_12_7"]}', "--horizons", "5,10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trial 1 of the ledger" in out
    rows = ledger.trials(str(led))
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "E10" and r["hypothesis"] == "window split"
    assert r["config_diff"] == {"features": ["ret_12_7"]}
    assert r["n_trials_to_date"] == 1 and r["decision"] is None
    assert r["window"] == {"start": start, "end": end}
    assert r["universe"]["n_symbols"] == 5
    assert r["horizons"] == [5, 10]
    assert set(r["in_sample"]) == {"5", "10"}
    assert r["n"] > 0
    assert r["harness_cmd"][0] == "backtest.py" and "--ledger" in r["harness_cmd"]
    # a second run counts as trial 2, with an auto id
    rc = bt.main(["--bars", bars_path, "--start", start, "--end", end, "--every", "5",
                  "--out-records", str(run_dir / "records2"), "--ledger", str(led)])
    assert rc == 0
    assert "trial 2 of the ledger" in capsys.readouterr().out
    assert ledger.trials(str(led))[1]["id"].startswith("X-")


def test_backtest_without_ledger_says_it_was_not_counted(run_dir, capsys):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    from test_backtest import _series, _bars_file
    hist = _series(260)
    bars_path = _bars_file(run_dir, {"AAA": list(hist), "SPY": list(hist)})
    rc = bt.main(["--bars", bars_path, "--start", bt.bar_date(hist[240]),
                  "--end", bt.bar_date(hist[-1]), "--out-records", str(run_dir / "r")])
    assert rc == 0
    assert "not counted" in capsys.readouterr().out
    import ledger
    assert ledger.count(str(ENGINE.parent / "experiments" / "ledger.jsonl")) == 4, \
        "a run without --ledger must not touch the repo's ledger"
