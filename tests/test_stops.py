"""K-07 — stops by desk type.

Two promises, pinned in opposite directions:
  * the default `fixed_atr` policy — every existing desk — writes the SAME BYTES the engine
    wrote before stops.py existed (tests/fixtures/pm_golden_prechange_stops.json, generated
    from the engine at 3a72721 by tests/stops_sequence.py BEFORE pm.py was touched), plus
    only the new bookkeeping keys;
  * chandelier and time_catastrophe do what their docstrings say: ratchet up and never
    down, time-stop at max_sessions, ignore small drawdowns, exit at the catastrophe level
    and at the target.
"""
import json
import pathlib

import pytest

import stops_sequence as sq
from conftest import run_pm

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"

# Keys K-07 adds. Everything else must be byte-identical to the pre-change engine.
NEW_KEYS = {"stop_policy", "initial_risk", "initial_risk_usd", "sessions_held", "highest_close",
            "trail_level", "live_would_refuse", "live_guardrails", "stop_params",
            # K-04 (after this golden was frozen): the desk VaR / stress block and its rules.
            # Additive — reported, never gating with enforce off — so it is stripped here.
            "risk", "var"}


@pytest.fixture
def stops(run_dir):
    import importlib
    import stops as s
    importlib.reload(s)
    return s


def _strip(obj):
    if isinstance(obj, dict):
        return {k: _strip(v) for k, v in obj.items() if k not in NEW_KEYS}
    if isinstance(obj, list):
        return [_strip(v) for v in obj]
    return obj


def _canon(obj):
    return json.dumps(obj, sort_keys=True)


def _walk_pos(pos, policy, prices, sessions, stops):
    """Feed a price path through apply_update the way exit_pass does. Returns the trace."""
    trace = []
    for px, s in zip(prices, sessions):
        r = stops.apply_update(policy, pos, px, s)
        if r["meta"]["raised"]:
            pos["stop"] = r["stop"]
            pos["stop_basis_kind"] = r["meta"].get("stop_basis_kind") or pos.get("stop_basis_kind")
        pos["highest_close"] = r["meta"].get("highest_close", pos.get("highest_close"))
        pos["trail_level"] = r["meta"].get("trail_level")
        trace.append((px, pos["stop"], r["exit_reason"], r["meta"]["raised"]))
        if r["exit_reason"]:
            break
    return trace


# ------------------------------------------------------------------ fixed_atr identity
def test_fixed_atr_is_byte_identical_to_the_pre_change_engine(pm, run_dir):
    """The golden was written from the untouched tree. With every desk on fixed_atr the
    engine writes the same book, journal and state, plus only the K-07 keys."""
    out = sq.replay(pm, run_dir)
    g = json.loads(sq.GOLDEN.read_text(encoding="utf-8"))
    assert _canon(_strip(out["book"])) == _canon(_strip(g["book"]))
    assert _canon(_strip(out["journal"])) == _canon(_strip(g["journal"]))
    assert _canon(_strip(out["state"])) == _canon(_strip(g["state"]))
    # booked prices and P&L are the pre-change numbers exactly
    assert out["book"]["realized_pnl"] == g["book"]["realized_pnl"]
    assert [c["exit"] for c in out["book"]["closed_trades"]] == \
        [c["exit"] for c in g["book"]["closed_trades"]]
    assert [(p["symbol"], p["stop"], p["target"]) for p in out["book"]["positions"]] == \
        [(p["symbol"], p["stop"], p["target"]) for p in g["book"]["positions"]]


def test_every_position_records_the_policy_fields(pm, run_dir):
    out = sq.replay(pm, run_dir)
    for p in out["book"]["positions"]:
        assert p["stop_policy"] == "fixed_atr"
        for k in ("initial_risk", "initial_risk_usd", "sessions_held", "highest_close",
                  "trail_level"):
            assert k in p, (p["symbol"], k)
        assert p["trail_level"] is None
    schw = [p for p in out["book"]["positions"] if p["symbol"] == "SCHW"][0]
    assert schw["initial_risk"] == pytest.approx(92.4 - 86.46, abs=1e-6)
    assert schw["initial_risk_usd"] == pytest.approx(schw["initial_risk"] * schw["shares"], abs=0.01)
    assert schw["sessions_held"] == 1, "filled 09-10, marked 09-11: one session"
    nvda = [p for p in out["book"]["positions"] if p["symbol"] == "NVDA"][0]
    assert nvda["highest_close"] == 260.0
    assert out["journal"]["entries"][-1]["stop_policy"] == "fixed_atr"


def test_fixed_atr_initial_delegates_to_derive_levels(stops):
    import portfolio
    row = {"ticker": "SCHW", "price": 92.4, "setup": "Momentum", "atr_14": 2.77,
           "ma_50": 87.78, "ma_200": 79.46}
    lv = stops.get_policy("fixed_atr").initial(92.4, 2.77, {"row": row})
    stop, target, basis, risk, kind = portfolio.derive_levels(row)
    assert (lv["stop"], lv["target"]) == (stop, target)
    assert lv["meta"]["stop_basis"] == basis and lv["meta"]["stop_basis_kind"] == kind


def test_fixed_atr_update_never_moves_anything(stops):
    pos = {"stop": 90.0, "target": 120.0, "avg_cost": 100.0}
    r = stops.apply_update(stops.get_policy("fixed_atr"), pos, 85.0, 50)
    assert r["stop"] == 90.0 and r["target"] == 120.0 and r["exit_reason"] is None
    assert r["meta"]["raised"] is False


# ------------------------------------------------------------------ chandelier
def test_chandelier_initial_levels_and_defaults(stops):
    c = stops.get_policy("chandelier")
    assert c.params == {"k_init": 2.5, "k_trail": 3.0, "max_sessions": 40, "target_r": None,
                        "time_stop_below_r": 1.0}
    lv = c.initial(100.0, 2.0, {})
    assert lv["stop"] == 95.0 and lv["target"] is None
    assert lv["meta"]["risk"] == 5.0 and lv["meta"]["stop_basis_kind"] == "chandelier"
    lv2 = stops.get_policy("chandelier", {"target_r": 3}).initial(100.0, 2.0, {})
    assert lv2["target"] == 115.0


def test_chandelier_ratchets_up_and_never_down(stops):
    c = stops.get_policy("chandelier")
    pos = {"avg_cost": 100.0, "shares": 10, "stop": 95.0, "target": None, "atr_14": 2.0,
           "initial_risk": 5.0, "highest_close": 100.0, "stop_basis_kind": "chandelier"}
    trace = _walk_pos(pos, c, [101, 108, 104, 103, 110, 105], [1, 2, 3, 4, 5, 6], stops)
    stops_seen = [t[1] for t in trace]
    assert stops_seen == [95.0, 102.0, 102.0, 102.0, 104.0, 104.0]
    assert all(b >= a for a, b in zip(stops_seen, stops_seen[1:])), "a stop never goes down"
    assert [t[3] for t in trace] == [False, True, False, False, True, False]
    assert all(t[2] is None for t in trace)
    assert pos["trail_level"] == 104.0 and pos["highest_close"] == 110


def test_chandelier_trail_exit_is_reported_as_trail_and_initial_as_stop(stops):
    c = stops.get_policy("chandelier")
    pos = {"avg_cost": 100.0, "stop": 95.0, "atr_14": 2.0, "initial_risk": 5.0,
           "highest_close": 100.0, "stop_basis_kind": "chandelier"}
    assert _walk_pos(pos, c, [94.0], [1], stops)[-1][2] == "stop"
    pos = {"avg_cost": 100.0, "stop": 95.0, "atr_14": 2.0, "initial_risk": 5.0,
           "highest_close": 100.0, "stop_basis_kind": "chandelier"}
    trace = _walk_pos(pos, c, [110.0, 103.9], [1, 2], stops)
    assert trace[-1][2] == "trail" and trace[-1][1] == 104.0


def test_chandelier_time_stop_only_below_1r(stops):
    c = stops.get_policy("chandelier")
    base = {"avg_cost": 100.0, "stop": 95.0, "atr_14": 2.0, "initial_risk": 5.0,
            "highest_close": 100.0, "stop_basis_kind": "chandelier"}
    assert stops.apply_update(c, dict(base), 103.0, 39)["exit_reason"] is None
    assert stops.apply_update(c, dict(base), 103.0, 40)["exit_reason"] == "time"
    # at or over 1R it is left to trail
    assert stops.apply_update(c, dict(base), 105.0, 40)["exit_reason"] is None
    assert stops.apply_update(c, dict(base), 103.0, 12)["exit_reason"] is None
    assert stops.get_policy("chandelier", {"max_sessions": 12}).update(
        dict(base), 103.0, 12)["exit_reason"] == "time"


def test_chandelier_without_atr_falls_back_to_structure_and_does_not_trail(stops):
    c = stops.get_policy("chandelier")
    lv = c.initial(100.0, None, {"row": {"price": 100.0, "setup": "Momentum", "ma_50": 96.0}})
    assert lv["stop"] == pytest.approx(max(96.0 * 0.985, 92.0), abs=0.01)
    assert "no ATR" in lv["meta"]["stop_basis"]
    pos = {"avg_cost": 100.0, "stop": lv["stop"], "atr_14": None, "initial_risk": 5.0,
           "highest_close": 100.0}
    r = stops.apply_update(c, pos, 120.0, 3)
    assert r["stop"] == lv["stop"] and r["meta"]["raised"] is False and r["meta"]["trail_level"] is None


# ------------------------------------------------------------------ time_catastrophe
def test_time_catastrophe_defaults_and_levels(stops):
    t = stops.get_policy("time_catastrophe")
    assert t.params == {"k_cat": 3.5, "max_sessions": 6, "target_pct": 4.0}
    lv = t.initial(50.0, 1.0, {})
    assert lv["stop"] == 46.5 and lv["target"] == 52.0
    assert lv["meta"]["stop_basis_kind"] == "catastrophe"
    rot = stops.get_policy("time_catastrophe", {"max_sessions": 25, "target_pct": None})
    assert rot.initial(50.0, 1.0, {})["target"] is None


def test_time_catastrophe_ignores_small_drawdowns_and_exits_at_k_cat(stops):
    t = stops.get_policy("time_catastrophe")
    pos = {"avg_cost": 50.0, "stop": 46.5, "target": 52.0, "atr_14": 1.0}
    # a 1.5x-ATR stop would have sold every one of these; the catastrophe stop holds
    for px in (49.0, 48.0, 47.2, 46.6):
        r = stops.apply_update(t, pos, px, 2)
        assert r["exit_reason"] is None and r["stop"] == 46.5, px
    assert stops.apply_update(t, pos, 46.5, 2)["exit_reason"] == "stop"
    assert stops.apply_update(t, pos, 44.0, 2)["exit_reason"] == "stop"


def test_time_catastrophe_target_pct_and_time_stop(stops):
    t = stops.get_policy("time_catastrophe")
    pos = {"avg_cost": 50.0, "stop": 46.5, "target": 52.0, "atr_14": 1.0}
    assert stops.apply_update(t, pos, 51.99, 1)["exit_reason"] is None
    assert stops.apply_update(t, pos, 52.0, 1)["exit_reason"] == "target"
    # the time stop is unconditional for mean reversion: the holding period is the exit
    assert stops.apply_update(t, pos, 50.5, 5)["exit_reason"] is None
    assert stops.apply_update(t, pos, 50.5, 6)["exit_reason"] == "time"
    assert stops.apply_update(t, pos, 51.0, 6)["exit_reason"] == "time"
    rot = stops.get_policy("time_catastrophe", {"max_sessions": 25, "target_pct": None})
    pos = {"avg_cost": 50.0, "stop": 46.5, "target": None, "atr_14": 1.0}
    assert stops.apply_update(rot, pos, 60.0, 24)["exit_reason"] is None
    assert stops.apply_update(rot, pos, 60.0, 25)["exit_reason"] == "time"


# ------------------------------------------------------------------ helpers
def test_apply_update_enforces_the_ratchet_for_any_policy(stops):
    class Lowering:
        name = "x"
        params = {}

        def update(self, pos, price, s, ctx=None):
            return {"stop": 1.0, "target": None, "exit_reason": "bogus", "meta": {}}
    r = stops.apply_update(Lowering(), {"stop": 90.0}, 100.0, 1)
    assert r["stop"] == 90.0 and r["exit_reason"] is None and r["meta"]["raised"] is False


def test_sessions_between_counts_weekdays_only(stops):
    assert stops.sessions_between("2026-09-10", "2026-09-10") == 0
    assert stops.sessions_between("2026-09-10", "2026-09-11") == 1
    assert stops.sessions_between("2026-09-11", "2026-09-14") == 1       # Fri -> Mon
    assert stops.sessions_between("2026-09-04", "2026-09-10") == 4
    assert stops.sessions_between(None, "2026-09-10") == 0


def test_unknown_policy_names_raise(stops):
    with pytest.raises(ValueError):
        stops.get_policy("stop_and_reverse")
    assert stops.resolve({"rules": {"stop_policy": "chandelier", "stop_params": {"k_init": 2}}}) == \
        ("chandelier", {"k_init": 2})
    assert stops.resolve({}) == ("fixed_atr", {})


# ------------------------------------------------------------------ desks.json
def _desks(run_dir):
    return json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))["desks"]


def test_the_three_live_desks_stay_on_fixed_atr(run_dir):
    d = _desks(run_dir)
    for name in ("swing", "pullback", "momentum"):
        assert d[name]["rules"].get("stop_policy") == "fixed_atr"
        assert not d[name].get("inactive")
    assert "chandelier" in d["momentum"]["_note"]


def test_inactive_templates_are_defined_as_specified(run_dir):
    d = _desks(run_dir)
    rot, orb = d["rotation"], d["orb"]
    assert rot["inactive"] is True and orb["inactive"] is True
    assert rot["rules"]["stop_policy"] == "time_catastrophe"
    assert rot["rules"]["stop_params"]["max_sessions"] == 25
    assert rot["rules"]["stop_params"]["k_cat"] == 3.5
    assert rot["filter"] == {"universe": "sector-etfs"}
    assert orb["rules"]["stop_policy"] == "chandelier"
    assert orb["rules"]["stop_params"]["k_init"] == 0.1
    assert orb["rules"]["flatten_at_close"] is True
    assert orb["rules"]["broker_policy"] == "intraday_margin"


def test_an_inactive_desk_refuses_to_run(pm, run_dir):
    with pytest.raises(pm.InactiveDesk):
        pm.use_desk("rotation")
    with pytest.raises(pm.InactiveDesk):
        pm.use_desk("orb")
    cfg = pm.use_desk("orb", allow_inactive=True)
    assert pm.DESK["name"] == "orb" and pm.DESK["rules"]["stop_policy"] == "chandelier"
    assert cfg["book"] == "paper_book_orb.json"
    assert not (run_dir / "paper_book_orb.json").exists(), "no book is created for a template"


def test_inactive_desk_refused_on_the_cli(run_dir):
    import subprocess, sys, os
    env = dict(os.environ, SCAN_DIR=str(run_dir))
    r = subprocess.run([sys.executable, str(pathlib.Path(__file__).resolve().parents[1] /
                                             "engine" / "pm.py"), "--desk", "rotation"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 2 and "INACTIVE" in r.stderr
    assert not (run_dir / "paper_book_rotation.json").exists()
    assert not (run_dir / "pm_book_next-rotation.json").exists()


def test_inactive_desks_are_not_peers_and_not_missing(pm, run_dir):
    peers = pm.load_peers("desks.json", "swing", "paper_book.json")
    assert set(peers["loaded"]) == {"pullback", "momentum"}
    assert peers["missing"] == []
    import paper_mirror
    found, missing = paper_mirror.desk_books("desks.json")
    assert missing == [] and {n for n, _ in found} == {"swing", "pullback", "momentum"}


# ------------------------------------------------------------------ pm.py wiring
def _desk(run_dir, name, book, rules, filt=None):
    d = json.loads((run_dir / "desks.json").read_text(encoding="utf-8"))
    d["desks"][name] = {"book": book, "journal": f"pm_journal_current_{name}.json",
                        "rules": rules, "filter": filt or {}}
    (run_dir / "desks.json").write_text(json.dumps(d), encoding="utf-8")


def test_chandelier_desk_sets_policy_levels_and_resizes_at_entry(pm, run_dir, quotes, scan):
    _desk(run_dir, "chand", "paper_book_momentum.json", {"stop_policy": "chandelier"})
    pm.use_desk("chand")
    book, jrn, state = run_pm(pm, run_dir, slot="opening-range", desk="chand", with_scan=True)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]
    assert placed, jrn["skipped"]
    o = [w for w in book["working_orders"] if w["symbol"] == "SCHW"][0]
    assert o["meta"]["stop_policy"] == "chandelier"
    assert o["meta"]["stop"] == pytest.approx(92.4 - 2.5 * 2.77, abs=0.01)
    assert o["meta"]["target"] is None
    assert o["meta"]["initial_risk"] == pytest.approx(2.5 * 2.77, abs=0.01)
    assert "no fixed target (chandelier)" in placed[0]["detail"]
    # wider stop, same dollar risk: fewer shares than the fixed_atr sizing would give
    fixed_risk = 92.4 - 86.46
    assert o["shares"] < o["meta"]["unscaled_shares"]
    assert o["shares"] == pytest.approx(o["meta"]["dollar_risk"] / o["meta"]["initial_risk"], rel=1e-3)
    assert o["meta"]["dollar_risk"] <= o["meta"]["unscaled_shares"] * fixed_risk + 0.01
    assert jrn["stop_policy"] == "chandelier"


def test_chandelier_desk_raises_the_stop_and_journals_it(pm, run_dir, quotes):
    _desk(run_dir, "chand", "paper_book_momentum.json", {"stop_policy": "chandelier"})
    b = json.loads((run_dir / "paper_book_momentum.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "DELL":               # live print 436.11, ATR 26.9
            p.update({"stop_policy": "chandelier", "stop": 380.0, "target": None,
                      "initial_risk": 61.01, "highest_close": 500.0, "trail_level": None})
    (run_dir / "paper_book_momentum.json").write_text(json.dumps(b), encoding="utf-8")
    pm.use_desk("chand")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel", desk="chand")
    raises = [d for d in jrn["decisions"] if d["action"] == "raise-stop"]
    assert [d["symbol"] for d in raises] == ["DELL"]
    dell = [p for p in book["positions"] if p["symbol"] == "DELL"][0]
    assert dell["stop"] == pytest.approx(500.0 - 3.0 * 26.9, abs=0.01)
    assert raises[0]["price"] == dell["stop"] and raises[0]["reason"] == "stop raised"
    assert dell["stop_basis_kind"] == "trail" and dell["stop_basis_short"] == "trail"
    assert dell["trail_level"] == dell["stop"]
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    # a second visit at the same prices raises nothing and lowers nothing
    (run_dir / "paper_book_momentum.json").write_text(json.dumps(book), encoding="utf-8")
    book2, jrn2, _ = run_pm(pm, run_dir, slot="sentinel", desk="chand")
    assert not [d for d in jrn2["decisions"] if d["action"] == "raise-stop"]
    assert [p for p in book2["positions"] if p["symbol"] == "DELL"][0]["stop"] == dell["stop"]


def test_chandelier_trail_exit_closes_the_whole_position_as_a_stop(pm, run_dir, quotes):
    _desk(run_dir, "chand", "paper_book_momentum.json", {"stop_policy": "chandelier"})
    b = json.loads((run_dir / "paper_book_momentum.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "DELL":               # trail from a 520 high: 520 - 80.7 = 439.3 > 436.11
            p.update({"stop_policy": "chandelier", "stop": 380.0, "target": None,
                      "initial_risk": 61.01, "highest_close": 520.0, "trail_level": None})
    (run_dir / "paper_book_momentum.json").write_text(json.dumps(b), encoding="utf-8")
    pm.use_desk("chand")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel", desk="chand")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "DELL"]
    assert len(sells) == 1 and sells[0]["reason"] == "stop"
    assert "trailing stop" in sells[0]["detail"]
    assert not [p for p in book["positions"] if p["symbol"] == "DELL"]
    assert not [d for d in jrn["decisions"] if d["action"] == "raise-stop"], \
        "a raise that coincides with the exit is not journaled as a raise"


def test_chandelier_time_stop_closes_a_position_below_1r(pm, run_dir, quotes):
    _desk(run_dir, "chand", "paper_book_momentum.json",
          {"stop_policy": "chandelier", "stop_params": {"max_sessions": 3}})
    b = json.loads((run_dir / "paper_book_momentum.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "HOOD":               # print 106.565, cost 105.91: well under 1R
            p.update({"stop_policy": "chandelier", "stop": 90.0, "target": None,
                      "initial_risk": 15.0, "highest_close": 106.0, "trail_level": None})
    (run_dir / "paper_book_momentum.json").write_text(json.dumps(b), encoding="utf-8")
    pm.use_desk("chand")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel", desk="chand")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "HOOD"]
    assert len(sells) == 1 and sells[0]["reason"] == "time"
    assert "Time stop" in sells[0]["detail"]
    closed = [c for c in book["closed_trades"] if c["symbol"] == "HOOD"][-1]
    assert closed["reason"] == "time"


def test_time_catastrophe_desk_holds_through_a_fixed_atr_stop_and_closes_whole_at_target(
        pm, run_dir, quotes):
    _desk(run_dir, "mr", "paper_book_pullback.json",
          {"stop_policy": "time_catastrophe", "stop_params": {"max_sessions": 60}})
    b = json.loads((run_dir / "paper_book_pullback.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "MU":                 # print 941.90, cost 959.46, ATR 58.21
            p.update({"stop_policy": "time_catastrophe", "stop": 959.46 - 3.5 * 58.21,
                      "target": 959.46 * 1.04, "initial_risk": 3.5 * 58.21})
        if p["symbol"] == "MRVL":               # print 206.135 — through a 4% target
            p.update({"stop_policy": "time_catastrophe", "stop": 150.0, "target": 200.0,
                      "initial_risk": 58.72, "scaled_out": False})
    (run_dir / "paper_book_pullback.json").write_text(json.dumps(b), encoding="utf-8")
    pm.use_desk("mr")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel", desk="mr")
    sold = {d["symbol"]: d for d in jrn["decisions"] if d["action"] == "fill-sell"}
    assert "MU" not in sold, "a 1.8% drawdown is noise to a mean-reversion desk"
    assert sold["MRVL"]["reason"] == "target" and "closing whole" in sold["MRVL"]["detail"]
    assert not [p for p in book["positions"] if p["symbol"] == "MRVL"], "no runner, no breakeven"


def test_legacy_positions_are_recorded_as_fixed_atr_on_their_next_visit(pm, run_dir, quotes):
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    for p in book["positions"]:
        assert p["stop_policy"] == "fixed_atr"
        assert p["initial_risk"] == pytest.approx(p["avg_cost"] - p["stop"], abs=1e-6)
        assert p["sessions_held"] > 0 and p["trail_level"] is None
    assert jrn["decisions"] == [], "the bookkeeping takes no decision"


def test_flatten_at_close_closes_everything_at_power_hour(pm, run_dir, quotes):
    _desk(run_dir, "intraday", "paper_book_momentum.json",
          {"stop_policy": "chandelier", "flatten_at_close": True})
    pm.use_desk("intraday")
    book, jrn, _ = run_pm(pm, run_dir, slot="midday", desk="intraday")
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    (run_dir / "paper_book_momentum.json").write_text(json.dumps(book), encoding="utf-8")
    book, jrn, _ = run_pm(pm, run_dir, slot="power-hour", desk="intraday")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert len(sells) == 4 and all(d["reason"] == "time" for d in sells)
    assert book["positions"] == []


# ------------------------------------------------------------------ live audit
def test_pm_run_journals_live_would_refuse(pm, run_dir, quotes, scan):
    """The fixture's one proposal (SCHW, $557.94) clears every default ceiling, so the
    audit is empty under the shipped rules — and that is the honest count. Lower the
    per-order ceiling under it and the same run journals the refusal without changing
    a single decision."""
    book, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert [d["symbol"] for d in placed] == ["SCHW"]
    assert jrn["live_would_refuse"] == []
    assert state["pm_rules"]["live_guardrails"]["per_order"]["max_notional_usd"] == 750
    notional = round(placed[0]["shares"] * placed[0]["price"], 2)
    assert notional < 750

    pm.PM_RULES["live_guardrails"] = json.loads(json.dumps(pm.PM_RULES["live_guardrails"]))
    pm.PM_RULES["live_guardrails"]["per_order"]["max_notional_usd"] = 500
    pm.PM_RULES["live_guardrails"]["deny"]["min_price"] = 100.0
    book2, jrn2, state2 = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    placed2 = [d for d in jrn2["decisions"] if d["action"] == "place-buy"]
    assert [(d["symbol"], d["shares"], d["price"]) for d in placed2] == \
        [(d["symbol"], d["shares"], d["price"]) for d in placed], "paper behaviour is unchanged"
    audit = jrn2["live_would_refuse"]
    assert [a["symbol"] for a in audit] == ["SCHW"]
    assert audit[0]["notional"] == notional
    assert any("$500" in r and "per-order ceiling" in r for r in audit[0]["reasons"])
    assert any("minimum price" in r for r in audit[0]["reasons"])
    assert state2["journal"]["live_would_refuse"] == audit


def test_a_sentinel_carries_an_empty_audit(pm, run_dir, quotes):
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert jrn["live_would_refuse"] == []
