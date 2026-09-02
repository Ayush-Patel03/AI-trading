"""portfolio.py is the audited risk layer. pm.py imports it and must never fork it."""
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
