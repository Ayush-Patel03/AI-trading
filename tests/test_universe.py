"""The defined-universe screen — and the survivorship trap it exists to avoid.

Wired into nothing yet: the live scan still screens retail watchlists. These tests pin the
replacement so the backtest can judge it before it changes what the book trades.
"""
import json

import pytest


@pytest.fixture
def uni(run_dir):
    import importlib
    import universe as u
    importlib.reload(u)
    return u


def _bars(sym_prices, sessions=25, volume=1_000_000):
    """{SYMBOL: [bar]} with a flat price and a fixed volume, so dollar volume is exact."""
    out = {}
    for sym, px in sym_prices.items():
        out[sym] = [{"begins_at": f"2025-01-{d + 1:02d}T00:00:00Z",
                     "close_price": f"{px}", "volume": volume}
                    for d in range(sessions)]
    return out


# ------------------------------------------------------------------ point-in-time membership
def test_a_history_snapshot_is_chosen_by_date(uni):
    doc = {"index": "Test 100", "history": [
        {"as_of": "2024-06-28", "symbols": ["AAA", "BBB"]},
        {"as_of": "2025-06-27", "symbols": ["AAA", "CCC"]}]}
    assert uni.members_asof(doc, "2024-12-31")[0] == ["AAA", "BBB"]
    assert uni.members_asof(doc, "2025-12-31")[0] == ["AAA", "CCC"]


def test_the_latest_snapshot_is_used_when_no_date_is_given(uni):
    doc = {"index": "T", "history": [
        {"as_of": "2024-06-28", "symbols": ["AAA"]},
        {"as_of": "2025-06-27", "symbols": ["BBB"]}]}
    assert uni.members_asof(doc)[0] == ["BBB"]


def test_screening_before_the_first_snapshot_is_refused(uni):
    """Survivorship bias wearing a respectable name: a later membership list is, by
    construction, the companies that lasted."""
    doc = {"index": "T", "history": [{"as_of": "2025-06-27", "symbols": ["AAA"]}]}
    with pytest.raises(SystemExit) as e:
        uni.members_asof(doc, "2024-01-01")
    assert "survivorship" in str(e.value).lower()


def test_a_flat_list_cannot_be_used_to_screen_a_past_date(uni):
    doc = {"index": "T", "as_of": "2026-09-02", "symbols": ["AAA"]}
    with pytest.raises(SystemExit) as e:
        uni.members_asof(doc, "2024-01-01")
    assert "describes today only" in str(e.value)


def test_a_flat_list_is_fine_for_today(uni):
    doc = {"index": "T", "as_of": "2026-09-02", "symbols": ["aaa", "BBB"]}
    syms, stamp = uni.members_asof(doc)
    assert syms == ["AAA", "BBB"] and stamp == "2026-09-02"


def test_a_malformed_members_file_is_refused(uni):
    for bad in ([], {"index": "T"}, {"index": "T", "symbols": "AAA"}):
        with pytest.raises(SystemExit):
            uni.members_asof(bad)


# ------------------------------------------------------------------ liquidity
def test_dollar_volume_uses_the_median_not_the_mean(uni):
    """One earnings day at ten times normal volume must not qualify a name that is
    untradeable on the other nineteen."""
    rows = [{"begins_at": f"2025-01-{d + 1:02d}T00:00:00Z", "close_price": "10",
             "volume": 10_000} for d in range(19)]
    rows.append({"begins_at": "2025-01-20T00:00:00Z", "close_price": "10",
                 "volume": 100_000_000})
    liq = uni.liquidity({"SPIKE": rows})
    assert liq["SPIKE"]["dollar_volume"] == pytest.approx(100_000, rel=1e-6)


def test_liquidity_respects_the_as_of_boundary(uni):
    rows = [{"begins_at": "2025-01-01T00:00:00Z", "close_price": "10", "volume": 1},
            {"begins_at": "2026-01-01T00:00:00Z", "close_price": "999", "volume": 999}]
    liq = uni.liquidity({"AAA": rows}, as_of="2025-06-01")
    assert liq["AAA"]["price"] == 10.0, "a future bar set the price — look-ahead"


# ------------------------------------------------------------------ the screen
def test_a_cheap_name_is_excluded_with_its_reason(uni):
    doc = {"index": "T", "symbols": ["PENNY", "REAL"]}
    bars = _bars({"PENNY": 1.20, "REAL": 100.0}, volume=1_000_000)
    kept, excluded, meta = uni.screen(doc, bars)
    assert kept == ["REAL"]
    assert any(e["symbol"] == "PENNY" and "floor" in e["reason"] for e in excluded)


def test_an_illiquid_name_is_excluded(uni):
    doc = {"index": "T", "symbols": ["THIN", "DEEP"]}
    bars = {**_bars({"THIN": 50.0}, volume=1_000),
            **_bars({"DEEP": 50.0}, volume=1_000_000)}
    kept, excluded, _ = uni.screen(doc, bars)
    assert kept == ["DEEP"]
    assert any("liquidity floor" in e["reason"] for e in excluded)


def test_a_member_with_no_price_history_is_named_not_silently_dropped(uni):
    doc = {"index": "T", "symbols": ["AAA", "GHOST"]}
    kept, excluded, _ = uni.screen(doc, _bars({"AAA": 50.0}))
    assert kept == ["AAA"]
    assert any(e["symbol"] == "GHOST" and "no price history" in e["reason"] for e in excluded)


def test_without_bars_it_screens_membership_only_and_says_so(uni):
    doc = {"index": "T", "symbols": ["AAA", "BBB"]}
    kept, excluded, meta = uni.screen(doc)
    assert kept == ["AAA", "BBB"] and excluded == []
    assert meta["liquidity_measured"] is False


def test_the_cap_keeps_the_most_liquid_and_reports_the_rest(uni):
    doc = {"index": "T", "symbols": ["A", "B", "C"]}
    bars = {**_bars({"A": 50.0}, volume=9_000_000),
            **_bars({"B": 50.0}, volume=5_000_000),
            **_bars({"C": 50.0}, volume=1_000_000)}
    kept, excluded, meta = uni.screen(doc, bars, rules={"max_symbols": 2})
    assert kept == ["A", "B"]
    assert meta["capped_out"] == 1
    assert any(e["symbol"] == "C" and "max_symbols" in e["reason"] for e in excluded)


def test_every_excluded_name_carries_a_reason(uni):
    doc = {"index": "T", "symbols": ["PENNY", "THIN", "GHOST", "REAL"]}
    bars = {**_bars({"PENNY": 1.0}, volume=1_000_000),
            **_bars({"THIN": 50.0}, volume=100),
            **_bars({"REAL": 50.0}, volume=1_000_000)}
    kept, excluded, _ = uni.screen(doc, bars)
    assert kept == ["REAL"]
    assert len(excluded) == 3
    assert all(e.get("reason") for e in excluded), \
        "a screen that drops names silently is indistinguishable from a broken one"


def test_the_cli_writes_a_universe_file(uni, run_dir):
    (run_dir / "members.json").write_text(json.dumps({"index": "T", "symbols": ["AAA", "BBB"]}), encoding="utf-8")
    assert uni.main(["--members", str(run_dir / "members.json"),
                     "--out", "universe.json"]) == 0
    doc = json.loads((run_dir / "universe.json").read_text(encoding="utf-8"))
    assert doc["symbols"] == ["AAA", "BBB"]
    assert doc["index"] == "T"
