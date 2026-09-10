"""portfolio.py is the audited risk layer. pm.py imports it and must never fork it."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import portfolio as pf


def _row(**kw):
    base = {"ticker": "TST", "name": "Test", "price": 100.0, "score": 78.0,
            "setup": "Momentum", "verdict": "Strong Buy", "gics": "Information Technology",
            "industry": "Semiconductors", "ma_50": None, "ma_200": None, "atr_14": None}
    base.update(kw)
    return base


def test_atr_stop_clamps_to_the_band():
    wide = pf.derive_levels(_row(atr_14=30.0))          # 1.5x ATR would be 45% of price
    assert round((100 - wide[0]) / 100 * 100, 2) == 12.0
    assert "capped at the 12% limit" in wide[2]
    tight = pf.derive_levels(_row(atr_14=0.5))          # 1.5x ATR would be 0.75%
    assert round((100 - tight[0]) / 100 * 100, 2) == 3.0
    assert "floored at 3%" in tight[2]


def test_nearby_ma_takes_precedence_over_atr():
    # 1.5 x 4.0 = 6.0 -> ATR stop 94.00; ma_200 * 0.985 = 93.58, within 2% of price.
    stop, _, basis, _, kind = pf.derive_levels(_row(atr_14=4.0, ma_200=95.0))
    assert stop == pytest.approx(round(95.0 * 0.985, 2))
    assert "200-day MA" in basis
    assert kind == "atr", "the basis text names the structure; the KIND stays atr"


def test_target_is_three_r():
    stop, target, _, risk, _ = pf.derive_levels(_row(atr_14=4.0))
    assert target == pytest.approx(round(100.0 + 3 * risk, 2))


def test_conviction_scales_risk_with_score():
    got = [round(pf.conviction_risk_pct(pf.conviction_from_score(s), 2.0, 0.5), 3)
           for s in (45, 65, 85)]
    assert got == [0.5, 1.25, 2.0]


def test_structural_fallback_is_labelled_when_no_atr():
    stop, _, basis, _, kind = pf.derive_levels(_row(setup="Pullback in Uptrend", ma_200=90.0))
    assert kind == "structure"
    assert "no ATR available" in basis


def _marked(positions, cash=5000.0, equity=5000.0):
    return {"positions": positions, "invested": equity - cash, "cash": cash,
            "equity": equity, "deployed_pct": 0.0, "cash_pct": 0.0, "unrealized": 0.0}


def test_sector_cap_blocks_a_fourth_name():
    held = [{"symbol": s, "gics": "Information Technology", "industry": "Semiconductors"}
            for s in ("AAA", "BBB", "CCC")]
    props, _ = pf.build_proposals([_row(atr_14=4.0)], _marked(held))
    assert props[0]["blocked"] is True
    assert any("Sector limit" in w for w in props[0]["warnings"])


def test_a_clean_book_proposes_the_candidate():
    props, blocks = pf.build_proposals([_row(atr_14=4.0)], _marked([]))
    assert props[0]["blocked"] is False
    assert props[0]["shares"] > 0
    assert props[0]["reward_risk"] == 3.0


# ---------------------------------------------------------------- S-05 / E13: vol-targeted sizing
def test_size_by_vol_is_equity_times_target_over_realised_vol():
    # $10,000 at a 12% target against a 24%-vol name: $5,000 of notional, 50 shares at $100
    assert pf.size_by_vol(10_000.0, 100.0, 0.24, 12.0) == pytest.approx(50.0)
    # a calmer 12%-vol name earns the full 100 shares; a 48%-vol one a quarter of that
    assert pf.size_by_vol(10_000.0, 100.0, 0.12, 12.0) == pytest.approx(100.0)
    assert pf.size_by_vol(10_000.0, 100.0, 0.48, 12.0) == pytest.approx(25.0)


def test_size_by_vol_is_capped_and_refuses_bad_inputs():
    assert pf.size_by_vol(10_000.0, 100.0, 0.24, 12.0, cap_notional=3_000.0) == pytest.approx(30.0)
    assert pf.size_by_vol(10_000.0, 100.0, None, 12.0) == 0.0
    assert pf.size_by_vol(10_000.0, 100.0, 0.0, 12.0) == 0.0, "zero vol is a data gap, not infinity"
    assert pf.size_by_vol(10_000.0, 0.0, 0.24, 12.0) == 0.0
    assert pf.size_by_vol(10_000.0, 100.0, 0.24, 12.0, fractional=False) == 50.0
    assert pf.size_by_vol(10_000.0, 33.0, 0.24, 12.0, fractional=False) == float(int(5000 / 33))


def test_desk_vol_scalar_is_target_over_spy_vol_clamped():
    assert pf.desk_vol_scalar(0.12, 12.0) == pytest.approx(1.0)
    assert pf.desk_vol_scalar(0.24, 12.0) == pytest.approx(0.5)
    assert pf.desk_vol_scalar(0.48, 12.0) == pytest.approx(0.5), "floored at lo"
    assert pf.desk_vol_scalar(0.06, 12.0) == pytest.approx(1.5), "capped at hi"
    assert pf.desk_vol_scalar(0.06, 12.0, lo=0.25, hi=3.0) == pytest.approx(2.0)
    assert pf.desk_vol_scalar(None, 12.0) == 1.0, "unknown vol must not scale the book"
    assert pf.desk_vol_scalar(0.0, 12.0) == 1.0


def test_vol_target_ships_off():
    assert pf.RULES["vol_target"]["enabled"] is False


def _vol_rules(enabled=True, **kw):
    import copy
    rules = copy.deepcopy(pf.RULES)
    rules["vol_target"].update({"enabled": enabled, **kw})
    return rules


def test_the_off_path_is_byte_identical_to_an_engine_without_the_rule():
    """The flag defaults off, and off must mean OFF: same proposals whether the rule is
    present-and-disabled, absent entirely, or a SPY vol is offered to it."""
    import copy
    rows = [_row(atr_14=4.0, features={"rv_20d": 1.20}),
            _row(ticker="ZZZ", atr_14=2.0, gics="Energy", industry="Oil", features={"rv_20d": 0.05}),
            _row(ticker="NOF", atr_14=3.0, gics="Health Care", industry="Pharma")]
    held = [{"symbol": "AAA", "gics": "Energy", "industry": "Oil"}]
    marked = _marked(held, cash=4000.0, equity=5000.0)
    without = copy.deepcopy(pf.RULES)
    del without["vol_target"]
    a = json.dumps(pf.build_proposals(rows, marked), sort_keys=True)
    b = json.dumps(pf.build_proposals(rows, marked, rules=without), sort_keys=True)
    c = json.dumps(pf.build_proposals(rows, marked, spy_rv_20d=0.40), sort_keys=True)
    d = json.dumps(pf.build_proposals(rows, marked, rules=_vol_rules(enabled=False),
                                      spy_rv_20d=0.40), sort_keys=True)
    assert a == b == c == d
    assert "vol_target" not in a and "Vol target" not in a and "Desk vol scalar" not in a


def test_when_enabled_the_smaller_of_atr_and_vol_size_wins():
    # ATR-risk size at $100 with a $6 stop and 1.74% risk: ~14.5 shares, then the 15%
    # position cap (7.5 shares). A 120%-vol name at a 12% target is $500 -> 5 shares.
    row = _row(atr_14=4.0, features={"rv_20d": 1.20})
    off = pf.build_proposals([row], _marked([]))[0][0]
    on = pf.build_proposals([row], _marked([]), rules=_vol_rules())[0][0]
    assert off["shares"] == pytest.approx(7.5)
    assert on["shares"] == pytest.approx(5.0)
    assert on["vol_target"]["rv_20d"] == 1.20
    assert on["vol_target"]["vol_shares"] == pytest.approx(5.0)
    assert on["vol_target"]["atr_shares"] > 7.5, "the ATR-risk size before any cap"
    assert any("Vol target" in w for w in on["warnings"])
    assert on["blocked"] is False


def test_when_enabled_a_calm_name_keeps_its_atr_size():
    # 10%-vol name: vol size $6,000 > ATR size, so the ATR-risk size (and its cap) stands
    row = _row(atr_14=4.0, features={"rv_20d": 0.10})
    off = pf.build_proposals([row], _marked([]))[0][0]
    on = pf.build_proposals([row], _marked([]), rules=_vol_rules())[0][0]
    assert on["shares"] == pytest.approx(off["shares"])
    assert not any("Vol target" in w for w in on["warnings"])


def test_when_enabled_a_row_without_rv_keeps_its_atr_size_and_says_so():
    row = _row(atr_14=4.0)
    off = pf.build_proposals([row], _marked([]))[0][0]
    on = pf.build_proposals([row], _marked([]), rules=_vol_rules())[0][0]
    assert on["shares"] == pytest.approx(off["shares"])
    assert any("no rv_20d" in w for w in on["warnings"])
    assert on["vol_target"]["rv_20d"] is None


def test_the_desk_scalar_multiplies_new_entry_notional_when_enabled():
    row = _row(atr_14=4.0, features={"rv_20d": 1.20})       # vol size 5 shares
    calm = pf.build_proposals([row], _marked([]), rules=_vol_rules(), spy_rv_20d=0.12)[0][0]
    panic = pf.build_proposals([row], _marked([]), rules=_vol_rules(), spy_rv_20d=0.48)[0][0]
    assert calm["shares"] == pytest.approx(5.0)
    assert panic["shares"] == pytest.approx(2.5), "SPY at 48% vol -> x0.5 floor"
    assert panic["vol_target"]["desk_scalar"] == pytest.approx(0.5)
    assert any("Desk vol scalar x0.50" in w for w in panic["warnings"])
    # a dead-calm tape scales UP, to the hi clamp, and the position cap still binds
    quiet = pf.build_proposals([row], _marked([]), rules=_vol_rules(), spy_rv_20d=0.04)[0][0]
    assert quiet["vol_target"]["desk_scalar"] == pytest.approx(1.5)
    assert quiet["shares"] == pytest.approx(7.5)
