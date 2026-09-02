"""HOUSE-01 — the seven checks run by hand on 2026-09-02, made permanent.

Every desk sits at three Information Technology names in these books, so the per-desk
sector cap would refuse any IT candidate on its own. The scan fixture is deliberately
Financials (SCHW, held by no desk) so these tests isolate the HOUSE gate.
"""
import json

import pytest

from conftest import run_pm


def test_house_exposure_matches_the_hand_calculation(pm, run_dir, quotes):
    _, _, state = run_pm(pm, run_dir, slot="sentinel")
    h = state["house"]
    assert h["desk_count"] == 3
    assert sorted(h["peers_loaded"]) == ["momentum", "pullback"]
    assert h["equity"] == pytest.approx(14926.57, abs=0.05)
    assert h["sector_pct"]["Information Technology"] == pytest.approx(34.0, abs=0.1)
    # NVDA is held in two books; neither desk alone can see that.
    assert h["by_symbol"]["NVDA"] == pytest.approx(1517.97, abs=0.05)


def test_an_entry_under_both_caps_is_allowed(pm, run_dir, quotes, scan):
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]
    assert placed, "Financials is 8% of the house; the 40% cap must not refuse this"


def test_a_sector_breach_is_refused_and_names_the_sector(pm, run_dir, quotes, scan):
    pm.PM_RULES["house_max_sector_pct"] = 8.0          # Financials already sits at 7.96%
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "SCHW"]
    assert any("House cap" in r and "Financials" in r for r in reasons), reasons
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]


def test_a_single_name_breach_names_what_is_held_elsewhere(pm, run_dir, quotes):
    # HOOD is held on swing and momentum; enter it on pullback, which holds none.
    s = json.loads((run_dir / "scan_results.json").read_text()) if (run_dir / "scan_results.json").exists() else None
    hood = {"ticker": "HOOD", "name": "Robinhood Markets", "price": 106.565, "score": 78.0,
            "setup": "Pullback in Uptrend", "verdict": "Strong Buy", "gics": "Financials",
            "sector": "Financials", "industry": "Capital Markets", "rsi_14": 45.0,
            "atr_14": 3.2, "atr_pct": 3.0, "ma_50": 101.2, "ma_200": 91.6,
            "confidence": "ok", "upside_pct": 12.0, "analyst_target": 119.3,
            "setup_note": "dip inside an uptrend"}
    from conftest import _fresh_scan_meta
    (run_dir / "scan_results.json").write_text(json.dumps(_fresh_scan_meta(
        {"meta": {"slot": "opening range", "macro_events": []}, "results": [hood]})))
    pm.PM_RULES["house_max_symbol_pct"] = 8.0          # HOOD already sits at 7.96%
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", desk="pullback", with_scan=True)
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "HOOD"]
    assert any("House cap" in r and "held elsewhere" in r for r in reasons), reasons


def test_house_tally_updates_within_a_single_run(pm, run_dir, quotes):
    house = {"equity": 10000.0, "desk_count": 3,
             "by_symbol": {}, "by_sector": {}, "symbol_pct": {}, "sector_pct": {},
             "caps": {"symbol_pct": 15.0, "sector_pct": 40.0}}
    assert pm.house_block(house, "AAA", "Tech", 1000.0) is None
    pm.house_apply(house, "AAA", "Tech", 1000.0)
    assert house["by_symbol"]["AAA"] == 1000.0
    # A second entry of the same size now breaches the 15% single-name cap.
    assert pm.house_block(house, "AAA", "Tech", 1000.0) is not None


def test_a_missing_peer_book_warns_and_journals_house_null(pm, run_dir, quotes):
    (run_dir / "paper_book_pullback.json").unlink()
    (run_dir / "paper_book_momentum.json").unlink()
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range")
    assert state["house"] is None
    assert jrn["house"] is None
    assert any("House caps NOT evaluated" in w for w in jrn["warnings"])


def test_no_house_caps_measures_without_refusing(pm, run_dir, quotes, scan):
    pm.PM_RULES["house_max_sector_pct"] = 8.0
    pm.PM_RULES["house_caps_enabled"] = False
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state["house"] is not None, "exposure is still measured"
    assert [d for d in jrn["decisions"] if d["symbol"] == "SCHW"], "advisory mode must not refuse"
