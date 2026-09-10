"""K-04 — historical-simulation VaR and the named stress windows (engine/var.py).

The pure functions are checked against hand calculations on synthetic return series with
a KNOWN tail; the pm.py wiring is checked on the fixture books with and without a staged
bars.json, and the enforce gate is shown to block entries while a stop still fires.
"""
import datetime as dt
import json
import os
import subprocess
import sys

import pytest

import var
from conftest import ENGINE, ROOT, run_pm
from test_house import synthetic_bars


# ------------------------------------------------------------------ helpers
def _series(n, start="2024-01-02", fn=None):
    """{date: r} over n WEEKDAYS from start; fn(i, date) gives the return."""
    d = dt.date.fromisoformat(start)
    out = {}
    i = 0
    while len(out) < n:
        if d.weekday() < 5:
            out[d.isoformat()] = fn(i, d.isoformat()) if fn else 0.0
            i += 1
        d += dt.timedelta(days=1)
    return out


def _known_tail(n=500):
    """Returns whose sorted order is i/1000 − 0.25: the 5th worst is −0.246 exactly."""
    order = [(i * 7919) % n for i in range(n)]            # a fixed permutation of 0..n-1
    return _series(n, fn=lambda i, d: order[i] / 1000.0 - 0.25)


def _bars_from_series(symbol, series, c0=100.0):
    """A get_equity_historicals result row whose daily returns reproduce `series`. The
    first bar is dated one weekday before the first return."""
    first = dt.date.fromisoformat(min(series))
    d0 = first - dt.timedelta(days=1)
    while d0.weekday() >= 5:
        d0 -= dt.timedelta(days=1)
    bars = [{"begins_at": d0.isoformat() + "T00:00:00Z", "close_price": str(c0),
             "interpolated": False}]
    c = c0
    for d in sorted(series):
        c *= 1.0 + series[d]
        bars.append({"begins_at": d + "T00:00:00Z", "close_price": repr(c), "interpolated": False})
    return {"symbol": symbol, "bars": bars}


SHOCKS = {"2024-08-05": -0.03, "2025-04-03": -0.03, "2025-04-04": -0.04, "2025-04-09": 0.08}


def _spy_series():
    """SPY over 2024-01-02 → late 2025: quiet days plus the four dated shocks."""
    return _series(500, fn=lambda i, d: SHOCKS.get(d, 0.001 * ((i % 5) - 2)))


# ------------------------------------------------------------------ hs_var
def test_hs_var_hits_the_known_99th_percentile():
    r = var.hs_var({"A": 1.0}, {"A": _known_tail()}, alpha=0.99)
    assert r["n_days"] == 500 and r["tail_n"] == 5
    assert r["var_pct"] == pytest.approx(24.6, abs=1e-9)        # 5th worst of 500
    assert r["cvar_pct"] == pytest.approx(24.8, abs=1e-9)       # mean of the 5 worst
    assert r["window"] == {"start": min(_known_tail()), "end": max(_known_tail())}
    assert r["coverage_pct"] == 100.0 and r["uncovered"] == [] and "reason" not in r


def test_var_scales_with_the_weight_and_cash_earns_nothing():
    half = var.hs_var({"A": 0.5}, {"A": _known_tail()}, alpha=0.99)
    assert half["var_pct"] == pytest.approx(12.3, abs=1e-9)


def test_cvar_is_never_below_var():
    for alpha in (0.9, 0.95, 0.99):
        r = var.hs_var({"A": 0.7, "B": 0.3},
                       {"A": _known_tail(), "B": _series(500, fn=lambda i, d: 0.002 * ((i % 7) - 3))},
                       alpha=alpha)
        assert r["var_pct"] is not None and r["cvar_pct"] >= r["var_pct"]


def test_too_short_a_history_is_null_with_a_reason():
    r = var.hs_var({"A": 1.0}, {"A": _known_tail(100)})
    assert r["var_pct"] is None and r["cvar_pct"] is None and r["window"] is None
    assert r["n_days"] == 100 and "120 needed" in r["reason"]


def test_overlap_is_the_intersection_and_a_name_without_bars_is_named():
    a = _known_tail(500)
    b = dict(list(sorted(a.items()))[-300:])          # only the newest 300 days
    r = var.hs_var({"A": 0.4, "B": 0.4, "NOBARS": 0.2}, {"A": a, "B": {d: 0.0 for d in b}})
    assert r["n_days"] == 300
    assert r["uncovered"] == ["NOBARS"] and r["coverage_pct"] == 80.0


def test_an_empty_book_or_no_bars_at_all_is_null():
    assert var.hs_var({}, {"A": _known_tail()})["reason"] == "no positions to measure"
    assert "no held name has bars" in var.hs_var({"Z": 1.0}, {"A": _known_tail()})["reason"]


def test_two_year_cap_on_the_window():
    r = var.hs_var({"A": 1.0}, {"A": _known_tail(700)})
    assert r["n_days"] == var.MAX_DAYS == 504


# ------------------------------------------------------------------ stress
def test_stress_replays_each_holdings_own_bars_when_they_cover_the_window():
    spy = _spy_series()
    a = {d: 1.5 * r for d, r in spy.items()}
    st = var.stress({"A": 0.5}, {"A": a, "SPY": spy}, betas={"A": 1.5})
    # 2025-04-03/04, cumulative, from A's own bars: (1−0.045)(1−0.06) − 1 = −10.23%, × 50%
    tw = st["2025-04-03/04"]
    assert tw["coverage"] == "own" and tw["side"] == "long"
    assert tw["pnl_pct"] == pytest.approx(0.5 * ((0.955 * 0.94) - 1) * 100, abs=1e-6)
    assert st["2024-08-05"]["pnl_pct"] == pytest.approx(0.5 * -4.5, abs=1e-6)
    assert st["2024-08-05"]["benchmark_basis"] == "bars"
    # the squeeze is the SHORT-side number: the long book gains, short_pnl_pct is the loss
    sq = st["2025-04-09"]
    assert sq["side"] == "short" and sq["pnl_pct"] == pytest.approx(0.5 * 12.0, abs=1e-6)
    assert sq["short_pnl_pct"] == pytest.approx(-6.0, abs=1e-6)


def test_stress_falls_back_to_beta_times_the_index_return_and_says_so():
    spy = _spy_series()
    a = {d: 1.5 * r for d, r in spy.items()}
    st = var.stress({"A": 0.5}, {"A": a, "SPY": spy}, betas={"A": 1.5})
    for name in ("2020-03-16", "2022"):
        row = st[name]
        assert row["coverage"] == "proxy" and row["benchmark_basis"] == "index"
        assert row["by_symbol"]["A"]["coverage"] == "proxy"
        assert row["pnl_pct"] == pytest.approx(0.5 * 1.5 * var.BENCH_WINDOW_RET[name] * 100, abs=1e-6)


def test_a_name_with_neither_bars_nor_beta_is_unmeasured_never_beta_one():
    spy = _spy_series()
    st = var.stress({"A": 0.3, "Q": 0.2}, {"A": {d: r for d, r in spy.items()}, "SPY": spy},
                    betas={"A": 1.0})
    row = st["2020-03-16"]
    assert row["coverage"] == "partial" and row["measured_pct"] == 60.0
    assert row["by_symbol"]["Q"] == {"ret_pct": None, "coverage": "none"}
    assert row["pnl_pct"] == pytest.approx(0.3 * var.BENCH_WINDOW_RET["2020-03-16"] * 100, abs=1e-6)
    none = var.stress({"Q": 0.2}, {}, betas={})
    assert all(r["coverage"] == "none" and r["pnl_pct"] is None for r in none.values())
    assert var.worst_stress(none) == (None, None)


def test_worst_stress_is_the_most_negative_long_window_only():
    spy = _spy_series()
    a = {d: 1.5 * r for d, r in spy.items()}
    st = var.stress({"A": 1.0}, {"A": a, "SPY": spy}, betas={"A": 1.5})
    name, pnl = var.worst_stress(st)
    assert name == "2022" and pnl == pytest.approx(1.5 * -19.44, abs=1e-6)
    assert st["2025-04-09"]["pnl_pct"] > 0      # never the worst, whatever its size


def test_window_return_needs_both_bounds():
    s = {"2025-04-03": -0.03, "2025-04-04": -0.04}
    assert var.window_return(s, "2025-04-03", "2025-04-04") == pytest.approx(0.97 * 0.96 - 1)
    assert var.window_return(s, "2025-04-02", "2025-04-04") is None
    assert var.window_return({}, "2025-04-03", "2025-04-03") is None


# ------------------------------------------------------------------ assess
def test_assess_flags_each_breach_and_only_blocks_when_enforced():
    v = {"var_pct": 3.5, "alpha": 0.99}
    st = {"2022": {"pnl_pct": -17.0, "side": "long"}, "2025-04-09": {"pnl_pct": 30.0, "side": "short"}}
    a = var.assess(v, st, {"enforce": False})
    assert a["flags"] == ["var", "stress"] and a["block_new_entries"] is False
    assert a["worst_stress"] == -17.0 and a["worst_stress_window"] == "2022"
    a = var.assess(v, st, {"enforce": True})
    assert a["block_new_entries"] is True and len(a["reasons"]) == 2
    clean = var.assess({"var_pct": 2.9}, {"2022": {"pnl_pct": -16.0, "side": "long"}}, {"enforce": True})
    assert clean["flags"] == [] and clean["block_new_entries"] is False
    # unmeasured never flags — an absent VaR is not a low one and is not a high one
    nul = var.assess({"var_pct": None, "reason": "no bars"}, {}, {"enforce": True})
    assert nul["flags"] == [] and nul["block_new_entries"] is False


def test_compact_and_console_line():
    v = var.hs_var({"A": 1.0}, {"A": _known_tail()})
    st = {"2022": {"pnl_pct": -5.0, "side": "long", "coverage": "proxy"}}
    c = var.compact(v, st, var.assess(v, st))
    assert c["var_pct"] == 24.6 and c["worst_stress"] == -5.0 and c["worst_stress_window"] == "2022"
    assert c["stress"] == {"2022": {"pnl_pct": -5.0, "coverage": "proxy"}}
    line = var.console_line(c)
    assert "VaR99 24.60%" in line and "worst stress 2022 -5.0% (proxy)" in line
    assert "FLAGS var  [reported]" in line
    assert "n/a" in var.console_line({"var_pct": None, "reason": "no bars.json staged this run"})
    assert var.console_line(None) == "risk: VaR not measured"


# ------------------------------------------------------------------ pm.py wiring
def test_pm_rules_carry_the_documented_defaults(pm):
    assert pm.PM_RULES["var"] == {"alpha": 0.99, "max_var_pct_of_desk": 3.0,
                                  "max_stress_multiple_of_halt": 2.0, "halt_pct": 8.0,
                                  "enforce": False}


def test_no_bars_staged_is_null_with_a_reason_in_state_and_journal(pm, run_dir, quotes):
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range")
    r = state["risk"]
    assert r["bars"] == "absent" and r["var"]["var_pct"] is None
    assert r["var"]["reason"] == "no bars.json staged this run"
    assert r["stress"] == {} and r["flags"] == [] and r["block_new_entries"] is False
    assert r["worst_stress"] is None
    assert jrn["risk"]["var_pct"] is None and jrn["risk"]["reason"] == r["var"]["reason"]
    assert state["pm_rules"]["var"]["enforce"] is False


def _stage_bars(pm, run_dir):
    held = ["MU", "SNDK", "NVDA", "HOOD", "MRVL", "DELL", "MDB"]
    bars = synthetic_bars(extra={s: 1.2 for s in held})
    (run_dir / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    pm.load_bars()
    assert pm.BARS["rows"] is not None
    return bars


def test_pm_run_measures_var_and_stress_when_bars_are_staged(pm, run_dir, quotes):
    _stage_bars(pm, run_dir)
    _, jrn, state = run_pm(pm, run_dir, slot="sentinel")
    r = state["risk"]
    assert r["bars"] == "staged"
    v = r["var"]
    assert v["var_pct"] is not None and v["cvar_pct"] >= v["var_pct"]
    assert v["n_days"] >= 120 and v["alpha"] == 0.99 and v["coverage_pct"] == 100.0
    assert set(r["weights_pct"]) == {"MU", "SNDK", "NVDA", "HOOD"}
    st = r["stress"]
    assert set(st) == set(var.SCENARIOS)
    # the synthetic bars start 2025-01-01: the April 2025 windows replay the names' own
    # bars, 2020 / 2022 / 2024 fall back to β × the index return and are labelled so
    assert st["2025-04-03/04"]["coverage"] == "own" and st["2025-04-09"]["coverage"] == "own"
    for name in ("2020-03-16", "2022", "2024-08-05"):
        assert st[name]["coverage"] == "proxy" and st[name]["benchmark_basis"] == "index"
        assert all(s["coverage"] == "proxy" for s in st[name]["by_symbol"].values())
    assert r["worst_stress_window"] is not None and r["worst_stress"] < 0
    assert r["enforce"] is False and r["block_new_entries"] is False
    # the compact journal block agrees with the state
    assert jrn["risk"]["var_pct"] == v["var_pct"]
    assert jrn["risk"]["worst_stress"] == r["worst_stress"]
    assert set(jrn["risk"]["stress"]) == set(var.SCENARIOS)


def test_enforce_blocks_new_entries_but_never_an_exit(pm, run_dir, quotes, scan):
    _stage_bars(pm, run_dir)
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "NVDA":
            p["stop"] = 240.00          # above the quote: the exit pass MUST sell it
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    pm.PM_RULES["var"]["enforce"] = True
    pm.PM_RULES["var"]["max_var_pct_of_desk"] = 0.0001     # any measured VaR breaches
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    r = state["risk"]
    assert r["flags"] == ["var"] and r["enforce"] is True and r["block_new_entries"] is True
    assert [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "NVDA"], \
        "the stop must fire whatever the VaR says"
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "*"]
    assert any(x.startswith("VaR / stress (enforced)") and "VaR" in x for x in reasons), reasons
    assert any(w.startswith("VAR / STRESS") for w in jrn["warnings"])
    assert jrn["risk"]["block_new_entries"] is True


def test_enforce_with_no_breach_places_normally(pm, run_dir, quotes, scan):
    _stage_bars(pm, run_dir)
    pm.PM_RULES["var"]["enforce"] = True
    pm.PM_RULES["var"]["max_var_pct_of_desk"] = 100.0
    pm.PM_RULES["var"]["halt_pct"] = 100.0
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state["risk"]["flags"] == [] and state["risk"]["block_new_entries"] is False
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]


def test_reported_only_flag_raises_no_warning_and_still_places(pm, run_dir, quotes, scan):
    _stage_bars(pm, run_dir)
    pm.PM_RULES["var"]["max_var_pct_of_desk"] = 0.0001
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state["risk"]["flags"] == ["var"] and state["risk"]["block_new_entries"] is False
    assert not any("VAR" in w for w in jrn["warnings"])
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]


def test_the_stress_verdict_can_block_on_its_own(pm, run_dir, quotes, scan):
    _stage_bars(pm, run_dir)
    pm.PM_RULES["var"]["enforce"] = True
    pm.PM_RULES["var"]["max_var_pct_of_desk"] = 100.0
    pm.PM_RULES["var"]["halt_pct"] = 0.01           # 2 × 0.01% — any stress loss breaches
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state["risk"]["flags"] == ["stress"] and state["risk"]["block_new_entries"] is True
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]


def _cli(run_dir, *args):
    env = dict(os.environ)
    env["SCAN_DIR"] = str(run_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args], cwd=run_dir,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_heartbeat_and_coverage_row_carry_var_and_worst_stress(pm, run_dir, quotes):
    _stage_bars(pm, run_dir)
    r = _cli(run_dir, "--slot", "sentinel")
    assert "SENTINEL QUIET" in r.stdout, r.stdout + r.stderr
    assert "risk: VaR99 " in r.stdout and "worst stress" in r.stdout
    hb = json.loads((run_dir / "pm_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["quiet"] is True
    assert isinstance(hb["var_pct"], float) and hb["var_pct"] > 0
    assert isinstance(hb["worst_stress"], float) and hb["worst_stress"] < 0
    assert hb["worst_stress_window"] in var.SCENARIOS
    sys.path.insert(0, str(ROOT / "runner"))
    import run as runner
    row = runner.coverage_row_from_results(
        [{"desk": "swing", "exit_code": 0, "quiet": True, "heartbeat": hb, "check": None}],
        "sentinel", "rid", "sha", hb["ts"])
    assert row["desks"]["swing"]["var_pct"] == hb["var_pct"]
    assert row["desks"]["swing"]["worst_stress"] == hb["worst_stress"]
    assert row["desks"]["swing"]["worst_stress_window"] == hb["worst_stress_window"]


def test_heartbeat_without_bars_carries_nulls_not_zeros(run_dir, quotes):
    r = _cli(run_dir, "--slot", "sentinel")
    assert "risk: VaR n/a (no bars.json staged this run)" in r.stdout, r.stdout + r.stderr
    hb = json.loads((run_dir / "pm_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["var_pct"] is None and hb["worst_stress"] is None


def test_decision_slot_state_file_carries_the_risk_block(run_dir, quotes, scan):
    r = _cli(run_dir, "--slot", "opening-range")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "risk: VaR n/a" in r.stdout
    st = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    assert st["risk"]["var"]["var_pct"] is None
    jn = json.loads((run_dir / "pm_journal_next.json").read_text(encoding="utf-8"))
    assert jn["entries"][-1]["risk"]["var_pct"] is None


# ------------------------------------------------------------------ runtime constraints
def test_var_module_is_stdlib_only():
    import ast
    src = (ENGINE / "var.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names


def test_var_is_in_the_manifest_and_the_ci_allowlist():
    manifest = (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert "var.py" in manifest
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert '"var"' in ci
