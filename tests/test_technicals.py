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
