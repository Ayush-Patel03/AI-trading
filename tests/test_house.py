"""K-03 — house-level concentration and exposure metrics (engine/house.py).

The pure functions are checked against hand calculations; the pm.py wiring is checked on
the three fixture books, whose numbers are recorded here so a change to either the books
or the formulas is a visible diff, not a silent drift.

Fixture books, no bars (what every run sees today):
    7 distinct names, $14,926.57 combined, 42% invested
    N_eff (weights) 5.72 · N_eff (sector proxy, ρ_default 0.3) 1.27 → flag "n_eff"
    β·w n/a · momentum crowd n/a · IT 34.0% · NVDA 10.2% · overlap 57% of names / 33% of equity
"""
import json
import math
import os
import subprocess
import sys

import pytest

import house
from conftest import ENGINE, ROOT, run_pm


# ------------------------------------------------------------------ helpers
def _equal(n, total=1.0):
    return {f"S{i}": total / n for i in range(n)}


def _bars(symbol, closes, start="2025-01-01"):
    """One get_equity_historicals result row with daily bars from a close series."""
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    return {"symbol": symbol,
            "bars": [{"begins_at": (d0 + dt.timedelta(days=i)).isoformat() + "T00:00:00Z",
                      "open_price": str(c), "high_price": str(c), "low_price": str(c),
                      "close_price": str(c), "volume": "1000", "interpolated": False}
                     for i, c in enumerate(closes)]}


def _closes_from_returns(rets, c0=100.0):
    out = [c0]
    for r in rets:
        out.append(out[-1] * (1.0 + r))
    return out


def _bench_returns(n=300, seed=7):
    """Deterministic, non-trivial daily returns — no numpy, no random module state."""
    return [0.01 * math.sin(seed + i * 0.7) + 0.004 * math.cos(i * 1.3) for i in range(n)]


def synthetic_bars(extra=None):
    """SPY plus A (β = 1.5), B (β = 0.5), C (β = −1), MOMO (up >50% over 12-1m), FLAT."""
    rb = _bench_returns()
    rows = [_bars("SPY", _closes_from_returns(rb)),
            _bars("A", _closes_from_returns([1.5 * r for r in rb])),
            _bars("B", _closes_from_returns([0.5 * r for r in rb])),
            _bars("C", _closes_from_returns([-r for r in rb])),
            _bars("MOMO", [100.0 + i for i in range(301)]),
            _bars("FLAT", [100.0] * 301)]
    for sym, beta in (extra or {}).items():
        rows.append(_bars(sym, _closes_from_returns([beta * r for r in rb])))
    return {"data": {"results": rows}}


# ------------------------------------------------------------------ N_eff
def test_n_eff_of_eight_equal_names_is_eight_by_weights_and_about_1_5_at_rho_0_6():
    w = _equal(8)
    assert house.n_eff_weights(w) == pytest.approx(8.0)
    corr = {(a, b): 0.6 for a in w for b in w if a < b}
    # 1 / (1/8 + 0.6 · 7/8) = 1 / 0.65 — eight tickers, one and a half bets.
    assert house.n_eff_corr(w, corr) == pytest.approx(1.538, abs=0.01)


def test_n_eff_corr_with_identity_equals_n_eff_weights():
    w = {"A": 0.10, "B": 0.05, "C": 0.20}          # 35% invested, cash is not a bet
    assert house.n_eff_corr(w, {}) == pytest.approx(house.n_eff_weights(w))
    assert house.n_eff_weights(w) == pytest.approx(1.0 / ((2 / 7) ** 2 + (1 / 7) ** 2 + (4 / 7) ** 2))


def test_sector_proxy_is_rho_1_inside_a_sector_and_rho_default_across():
    w = _equal(3)                                   # S0, S1 in IT; S2 in Financials
    sectors = {"S0": "Information Technology", "S1": "Information Technology", "S2": "Financials"}
    # q = 3·(1/9) + 2·(1/9)·1 + 4·(1/9)·0.3 = 6.2/9
    assert house.n_eff_sector_proxy(w, sectors, 0.3) == pytest.approx(9 / 6.2, abs=1e-6)
    assert house.n_eff_sector_proxy(w, sectors, 0.0) == pytest.approx(9 / 5.0, abs=1e-6)
    # every name its own sector at ρ_default 0 → back to the Herfindahl count
    assert house.n_eff_sector_proxy(w, {"S0": "a", "S1": "b", "S2": "c"}, 0.0) == pytest.approx(3.0)


def test_empty_book_has_no_n_eff():
    assert house.n_eff_weights({}) is None
    assert house.n_eff_corr({}, {}) is None
    assert house.weights([], 10000.0) == {}
    assert house.weights([{"symbol": "A", "notional": 100.0}], 0.0) == {}


# ------------------------------------------------------------------ beta
def test_beta_is_null_without_bars_never_zero():
    pos = [{"symbol": "A", "sector": "IT", "notional": 2000.0, "desk": "swing"}]
    m = house.metrics(pos, 10000.0, bars=None)
    assert m["beta_w"] is None
    assert m["momentum_crowd_pct"] is None
    assert m["n_eff_basis"] == "proxy"
    assert m["bars"] == "absent"
    assert house.beta_exposure({"A": 0.2}, {}) is None
    assert house.beta_exposure({"A": 0.2}, {"A": None}) is None


def test_beta_from_synthetic_bars_matches_the_construction():
    feats, corr, meta = house.features_from_bars(synthetic_bars(), symbols=["A", "B", "C"])
    assert meta["benchmark"] == "SPY"
    assert feats["A"]["beta_252"] == pytest.approx(1.5, abs=1e-6)
    assert feats["B"]["beta_252"] == pytest.approx(0.5, abs=1e-6)
    assert feats["C"]["beta_252"] == pytest.approx(-1.0, abs=1e-6)
    assert corr[("A", "B")] == pytest.approx(1.0, abs=1e-6)
    assert corr[("A", "C")] == pytest.approx(-1.0, abs=1e-6)
    # Σ w·β over combined equity: 20% of A at 1.5 plus 20% of B at 0.5 → 0.40
    assert house.beta_exposure({"A": 0.2, "B": 0.2},
                               {s: f["beta_252"] for s, f in feats.items()}) == pytest.approx(0.4)


def test_metrics_measure_n_eff_from_returns_when_bars_are_staged():
    pos = [{"symbol": "A", "sector": "IT", "notional": 2000.0, "desk": "swing"},
           {"symbol": "B", "sector": "Financials", "notional": 2000.0, "desk": "pullback"}]
    m = house.metrics(pos, 10000.0, bars=synthetic_bars())
    assert m["n_eff_basis"] == "measured"
    assert m["corr_pairs_measured"] == 1
    # A and B are perfectly correlated by construction: two names, one bet.
    assert m["n_eff"] == pytest.approx(1.0, abs=0.01)
    assert m["n_eff_proxy"] == pytest.approx(1.0 / (0.5 + 0.5 * 0.3), abs=0.01)
    assert m["beta_w"] == pytest.approx(0.4, abs=1e-3)
    assert m["beta_coverage_pct"] == pytest.approx(40.0)
    assert m["bars"] == "staged"


def test_a_name_without_bars_falls_back_to_the_proxy_pair_by_pair():
    pos = [{"symbol": "A", "sector": "IT", "notional": 2000.0, "desk": "swing"},
           {"symbol": "ZZZ", "sector": "IT", "notional": 2000.0, "desk": "momentum"}]
    m = house.metrics(pos, 10000.0, bars=synthetic_bars())
    assert m["bars_missing_for"] == ["ZZZ"]
    assert m["corr_pairs_measured"] == 0
    assert m["n_eff_basis"] == "proxy"
    assert m["beta_coverage_pct"] == pytest.approx(20.0)     # only A carries a beta
    assert m["beta_w"] == pytest.approx(0.3, abs=1e-3)        # 0.2 × 1.5; ZZZ is NOT counted as 0


# ------------------------------------------------------------------ momentum crowding
def test_factor_exposure_counts_equity_in_names_up_more_than_50_pct_12_1():
    feats, _, _ = house.features_from_bars(synthetic_bars(), symbols=["MOMO", "FLAT", "A"])
    assert feats["MOMO"]["ret_12_1"] > 0.5
    assert feats["FLAT"]["ret_12_1"] == pytest.approx(0.0)
    pos = [{"symbol": "MOMO", "sector": "IT", "notional": 3000.0},
           {"symbol": "FLAT", "sector": "IT", "notional": 1000.0},
           {"symbol": "A", "sector": "IT", "notional": 1000.0}]
    fx = house.factor_exposure(pos, feats, combined_equity=10000.0)
    assert fx["momentum_crowd_pct"] == pytest.approx(30.0)
    assert fx["measured_pct"] == pytest.approx(50.0)
    assert fx["crowded_names"] == ["MOMO"]
    assert house.factor_exposure(pos, None, combined_equity=10000.0) is None
    assert house.factor_exposure(pos, {}, combined_equity=10000.0) is None


# ------------------------------------------------------------------ concentration & overlap
def test_largest_sector_top_symbol_and_overlap_by_hand():
    pos = [{"symbol": "NVDA", "sector": "IT", "notional": 600.0, "desk": "swing"},
           {"symbol": "NVDA", "sector": "IT", "notional": 400.0, "desk": "momentum"},
           {"symbol": "HOOD", "sector": "Financials", "notional": 500.0, "desk": "swing"},
           {"symbol": "MU", "sector": "IT", "notional": 100.0, "desk": "pullback"},
           {"symbol": "MU", "sector": "IT", "notional": 100.0, "desk": "pullback", "kind": "order"}]
    assert house.largest_sector_share(pos, 10000.0) == ("IT", pytest.approx(0.12))
    assert house.top_symbol_share(house.weights(pos, 10000.0)) == ("NVDA", pytest.approx(0.10))
    ov = house.pairwise_overlap(pos, 10000.0)
    assert ov["names"] == ["NVDA"], "MU twice on ONE desk (position + order) is not overlap"
    assert ov["names_pct"] == pytest.approx(33.33, abs=0.01)
    assert ov["equity_pct"] == pytest.approx(10.0)


def test_overlap_on_the_three_fixture_books(pm, run_dir, quotes):
    _, _, state = run_pm(pm, run_dir, slot="sentinel")
    e = state["house"]["exposure"]
    assert e["names"] == 7
    assert e["overlap_names"] == ["HOOD", "MU", "NVDA", "SNDK"]
    assert e["overlap_pct"] == pytest.approx(57.14, abs=0.01)
    assert e["overlap_equity_pct"] == pytest.approx(33.18, abs=0.05)
    assert e["largest_sector"] == "Information Technology"
    assert e["largest_sector_pct"] == pytest.approx(34.0, abs=0.1)
    assert e["top_symbol"] == "NVDA"
    assert e["top_symbol_pct"] == pytest.approx(10.17, abs=0.05)
    assert e["n_eff_weights"] == pytest.approx(5.72, abs=0.01)
    assert e["n_eff"] == pytest.approx(1.27, abs=0.01)
    assert e["n_eff_basis"] == "proxy"
    assert e["beta_w"] is None and e["momentum_crowd_pct"] is None
    assert e["flags"] == ["n_eff"]
    assert e["enforce"] is False and e["block_new_entries"] is False


# ------------------------------------------------------------------ assess
def test_assess_flags_each_breach_and_only_blocks_when_enforced():
    m = {"n_eff": 1.3, "n_eff_basis": "proxy", "names": 7, "beta_w": 0.95,
         "beta_benchmark": "SPY", "momentum_crowd_pct": 70.0, "momentum_names": ["MOMO"]}
    a = house.assess(m, {"enforce": False})
    assert a["flags"] == ["n_eff", "beta_w", "momentum_crowd"]
    assert a["block_new_entries"] is False and a["enforce"] is False
    assert len(a["reasons"]) == 3
    a = house.assess(m, {"enforce": True})
    assert a["block_new_entries"] is True
    clean = house.assess({"n_eff": 4.0, "beta_w": 0.5, "momentum_crowd_pct": 10.0}, {"enforce": True})
    assert clean["flags"] == [] and clean["block_new_entries"] is False


def test_assess_never_flags_an_unmeasured_metric():
    a = house.assess({"n_eff": None, "beta_w": None, "momentum_crowd_pct": None}, {"enforce": True})
    assert a["flags"] == [] and a["block_new_entries"] is False


def test_thresholds_come_from_the_rules_passed_in():
    m = {"n_eff": 2.5, "names": 5}
    assert house.assess(m, {"min_n_eff": 2.0})["flags"] == []
    assert house.assess(m, {"min_n_eff": 3.0})["flags"] == ["n_eff"]


# ------------------------------------------------------------------ pm.py wiring
def test_pm_run_writes_the_block_into_state_and_journal(pm, run_dir, quotes, scan):
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    e = state["house"]["exposure"]
    assert e["n_eff"] == pytest.approx(1.27, abs=0.01)
    assert state["pm_rules"]["house_exposure"]["enforce"] is False
    j = jrn["house"]["exposure"]
    assert j == house.compact(e)
    for k in ("n_eff", "n_eff_basis", "beta_w", "largest_sector_pct", "top_symbol_pct",
              "overlap_pct", "flags"):
        assert k in j
    # Reported, not enforced: the Financials candidate is still placed and no warning
    # is raised for a standing flag (a warning would publish a board every slot).
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]
    assert not any("HOUSE EXPOSURE" in w for w in jrn["warnings"])


def test_pm_run_measures_from_bars_when_bars_json_is_staged(pm, run_dir, quotes):
    held = ["MU", "SNDK", "NVDA", "HOOD", "MRVL", "DELL", "MDB"]
    bars = synthetic_bars(extra={s: 1.2 for s in held})
    (run_dir / "bars.json").write_text(json.dumps(bars), encoding="utf-8")
    pm.load_bars()
    assert pm.BARS["rows"] is not None
    _, jrn, state = run_pm(pm, run_dir, slot="sentinel")
    e = state["house"]["exposure"]
    assert e["n_eff_basis"] == "measured"
    assert e["corr_pairs_measured"] == 21
    # every name is β 1.2 on the same benchmark → perfectly correlated: one bet, and
    # β·w = 1.2 × 42% invested
    assert e["n_eff"] == pytest.approx(1.0, abs=0.01)
    assert e["beta_w"] == pytest.approx(1.2 * e["invested_pct"] / 100.0, abs=0.01)
    assert e["beta_benchmark"] == "SPY"
    # β·w ≈ 0.50 sits under the 0.8 cap: the beta flag is not raised, the N_eff one still is.
    assert e["beta_w"] < 0.8 and e["flags"] == ["n_eff"]
    assert e["momentum_crowd_pct"] is not None
    assert jrn["house"]["exposure"]["n_eff_basis"] == "measured"


def test_house_null_leaves_exposure_null(pm, run_dir, quotes):
    (run_dir / "paper_book_pullback.json").unlink()
    (run_dir / "paper_book_momentum.json").unlink()
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range")
    assert state["house"] is None and jrn["house"] is None
    assert pm.house_metrics(None) is None
    assert house.compact(None) is None
    assert "not measured" in house.console_line(None)


def _cli(run_dir, *args):
    env = dict(os.environ)
    env["SCAN_DIR"] = str(run_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args], cwd=run_dir,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_heartbeat_and_coverage_row_carry_the_compact_summary(run_dir, quotes):
    r = _cli(run_dir, "--slot", "sentinel")
    assert "SENTINEL QUIET" in r.stdout, r.stdout + r.stderr
    assert "house: n_eff 1.3 (proxy) · β·w n/a · sector 34% IT · overlap 57%" in r.stdout
    hb = json.loads((run_dir / "pm_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["quiet"] is True
    e = hb["exposure"]
    assert e["n_eff"] == pytest.approx(1.27, abs=0.01)
    assert e["flags"] == ["n_eff"] and e["block_new_entries"] is False
    assert set(e) >= {"n_eff", "beta_w", "largest_sector_pct", "top_symbol_pct", "overlap_pct", "flags"}
    # the runner lifts it onto the coverage row, additively
    sys.path.insert(0, str(ROOT / "runner"))
    import run as runner
    row = runner.coverage_row_from_results(
        [{"desk": "swing", "exit_code": 0, "quiet": True, "heartbeat": hb, "check": None},
         {"desk": "pullback", "exit_code": 0, "quiet": True, "heartbeat": None, "check": None}],
        "sentinel", "rid", "sha", hb["ts"])
    assert row["exposure"] == e
    assert set(row["desks"]) == {"swing", "pullback"}
    no_hb = runner.coverage_row_from_results([{"desk": "swing", "exit_code": 1}], "s", "r", "x", "t")
    assert "exposure" not in no_hb


def test_console_line_prints_on_a_decision_slot(run_dir, quotes, scan):
    r = _cli(run_dir, "--slot", "opening-range")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "house: n_eff 1.3 (proxy)" in r.stdout
    assert "FLAGS n_eff  [reported]" in r.stdout
    st = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    assert st["house"]["exposure"]["flags"] == ["n_eff"]
    jn = json.loads((run_dir / "pm_journal_next.json").read_text(encoding="utf-8"))
    assert jn["entries"][-1]["house"]["exposure"]["n_eff"] == pytest.approx(1.27, abs=0.01)


# ------------------------------------------------------------------ enforce
def test_enforce_blocks_new_entries_house_wide_but_never_an_exit(pm, run_dir, quotes, scan):
    # NVDA's stop is lifted above the quote so the exit pass MUST sell it this run.
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "NVDA":
            p["stop"] = 240.00
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    pm.PM_RULES["house_exposure"]["enforce"] = True
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    e = state["house"]["exposure"]
    assert e["enforce"] is True and e["block_new_entries"] is True
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "NVDA"]
    assert sells, "the stop must fire whatever the house looks like"
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "*"]
    assert any(r.startswith("house exposure (enforced)") and "N_eff" in r for r in reasons), reasons
    assert any(w.startswith("HOUSE EXPOSURE") for w in jrn["warnings"])
    assert jrn["house"]["exposure"]["block_new_entries"] is True


def test_enforce_with_no_breach_places_normally(pm, run_dir, quotes, scan):
    pm.PM_RULES["house_exposure"]["enforce"] = True
    pm.PM_RULES["house_exposure"]["min_n_eff"] = 1.0     # the proxy 1.27 clears this floor
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state["house"]["exposure"]["flags"] == []
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]


def test_sentinel_reports_but_takes_no_entry_decision_even_when_enforced(pm, run_dir, quotes):
    pm.PM_RULES["house_exposure"]["enforce"] = True
    _, jrn, state = run_pm(pm, run_dir, slot="sentinel")
    assert state["house"]["exposure"]["block_new_entries"] is True
    assert jrn["decisions"] == [] and jrn["warnings"] == [], "a quiet sentinel stays quiet"


# ------------------------------------------------------------------ runtime constraints
def test_house_module_is_stdlib_only():
    import ast
    src = (ENGINE / "house.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names) | {"technicals"}, names
    assert "house.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
