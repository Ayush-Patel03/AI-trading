"""Wilder ATR and RSI. Every stop in the book is struck off the ATR, so a silent change
here moves every stop in every desk."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import technicals as t


def test_wilder_atr_on_a_constant_range_series():
    # 30 bars, every bar a $2 range with no gaps: the true range is 2.0 every bar, so
    # Wilder's smoothing must converge on exactly 2.0.
    highs = [101.0] * 30
    lows = [99.0] * 30
    closes = [100.0] * 30
    assert t.wilder_atr(highs, lows, closes, 14) == pytest.approx(2.0, abs=1e-9)


def test_atr_widens_when_the_range_widens():
    highs = [101.0] * 20 + [110.0] * 10
    lows = [99.0] * 20 + [90.0] * 10
    closes = [100.0] * 30
    assert t.wilder_atr(highs, lows, closes, 14) > 2.0


def test_rsi_is_high_on_a_rising_series():
    closes = [100.0 + i for i in range(30)]
    assert t.wilder_rsi(closes, 14) > 70


def test_rsi_is_low_on_a_falling_series():
    closes = [130.0 - i for i in range(30)]
    assert t.wilder_rsi(closes, 14) < 30


def test_rsi_is_mid_on_an_alternating_series():
    closes = [100.0 + (1.0 if i % 2 else 0.0) for i in range(40)]
    assert 40 < t.wilder_rsi(closes, 14) < 60


def test_sma_matches_the_arithmetic_mean():
    assert t.sma([1.0, 2.0, 3.0, 4.0], 4) == pytest.approx(2.5)
    assert t.sma([1.0, 2.0], 4) is None, "too few bars must be None, never a partial average"


def test_insufficient_bars_return_none_rather_than_a_guess():
    assert t.wilder_atr([1.0] * 3, [1.0] * 3, [1.0] * 3, 14) is None
    assert t.wilder_rsi([1.0] * 3, 14) is None


# --- get_equity_quotes row shapes (live defect, 2026-09-09) -------------------------
#
# The connector moved the live fields under `quote` and renamed the extended-hours print.
# The flat reader looked for a top-level `symbol`, found none, and skipped every row —
# leaving the session overrides inert and gap_pct on the `prior_bar` basis during an open
# market. It never raised, so nothing caught it for nine days.

NESTED = {"data": {"results": [
    {"quote": {"symbol": "NVDA", "last_trade_price": "224.140000",
               "last_non_reg_trade_price": None,
               "adjusted_previous_close": "225.730000",
               "previous_close": "225.730000", "has_traded": True, "state": "active"},
     "close": {"symbol": "NVDA", "date": "2026-09-08", "price": "225.73"}},
    {"quote": {"symbol": "MU", "last_trade_price": "1021.250000",
               "last_non_reg_trade_price": "1030.000000",
               "adjusted_previous_close": "1000.260000", "has_traded": True,
               "state": "active"},
     "close": {"symbol": "MU", "date": "2026-09-08", "price": "1000.26"}},
]}}

FLAT_LEGACY = {"data": {"results": [
    {"symbol": "NVDA", "last_trade_price": "224.14",
     "adjusted_previous_close": "225.73",
     "last_extended_hours_trade_price": "226.00"},
]}}


def test_nested_quote_rows_are_read():
    got = t.quote_extras(NESTED)
    assert set(got) == {"NVDA", "MU"}
    assert got["NVDA"]["price"] == pytest.approx(224.14)
    assert got["NVDA"]["prev_close"] == pytest.approx(225.73)


def test_the_extended_print_is_read_from_its_current_field_name():
    # last_non_reg_trade_price is what the connector now calls it. Reading only the old
    # name left --premarket with no basis and silently no gap override.
    assert t.quote_extras(NESTED)["MU"]["ext_price"] == pytest.approx(1030.0)


def test_the_legacy_flat_shape_still_reads():
    # Archived payloads and older fixtures carry the flat form; a reader that only knew
    # the new shape would break replaying them.
    got = t.quote_extras(FLAT_LEGACY)
    assert got["NVDA"]["price"] == pytest.approx(224.14)
    assert got["NVDA"]["ext_price"] == pytest.approx(226.00)


def test_prev_close_falls_back_to_the_official_close_block():
    payload = {"data": {"results": [
        {"quote": {"symbol": "AAA", "last_trade_price": "10.00"},
         "close": {"symbol": "AAA", "price": "9.00"}}]}}
    assert t.quote_extras(payload)["AAA"]["prev_close"] == pytest.approx(9.0)


def test_a_symbol_that_has_not_traded_contributes_nothing():
    # Better the honest bar-derived value than a session gap computed off a price no
    # venue printed.
    payload = {"data": {"results": [
        {"quote": {"symbol": "ZZZ", "last_trade_price": "1.00", "has_traded": False}}]}}
    assert t.quote_extras(payload) == {}


def test_a_halted_symbol_contributes_nothing():
    payload = {"data": {"results": [
        {"quote": {"symbol": "ZZZ", "last_trade_price": "1.00", "state": "halted"}}]}}
    assert t.quote_extras(payload) == {}


def test_the_shipped_quote_fixture_parses():
    # The fixture the PM tests drive the engine with is a real captured payload; if
    # technicals cannot read it, technicals cannot read production.
    import json
    import pathlib
    fix = pathlib.Path(__file__).parent / "fixtures" / "quotes.json"
    got = t.quote_extras(json.loads(fix.read_text(encoding="utf-8")))
    assert got, "the captured connector payload produced no rows"
    assert all(v["price"] for v in got.values())
