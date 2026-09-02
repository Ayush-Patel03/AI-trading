"""The honest fill model. Every test here describes a way the current flat 0.25% flatters
the book, and pins the correction.

This module is wired into nothing yet by design — switching pm.py over is a deliberate act,
because it makes yesterday's equity curve and tomorrow's incomparable.
"""
import pytest


@pytest.fixture
def fills(run_dir):
    import importlib
    import fills as f
    importlib.reload(f)
    return f


# ------------------------------------------------------------------ the spread
def test_a_sale_fills_at_the_bid_not_at_the_last_print(fills):
    px, detail = fills.exit_fill(price=100.00, bid=99.50, ask=100.50)
    assert px == pytest.approx(99.50 * (1 - 0.3 / 10_000), abs=1e-6)
    assert "bid" in detail


def test_a_wide_spread_costs_more_than_the_old_flat_assumption(fills):
    """The whole point. On a thin name the spread dwarfs a 0.25% constant."""
    tight, _ = fills.exit_fill(price=100.00, bid=99.95, ask=100.05)
    wide, _ = fills.exit_fill(price=100.00, bid=98.00, ask=102.00)
    old_flat = 100.00 * (1 - 0.0025)
    assert tight > old_flat > wide


def test_with_no_quote_it_falls_back_and_says_so(fills):
    px, detail = fills.exit_fill(price=100.00)
    assert px == pytest.approx(100.00 * 0.9975 * (1 - 0.3 / 10_000), abs=1e-6)
    assert "assumed" in detail, "an assumption must never be reported as a quote"


def test_an_absurd_spread_is_not_modelled_as_a_fill(fills):
    frac, basis = fills.half_spread(100.0, bid=10.0, ask=190.0)
    assert basis == "assumed", "a 180% spread is not a market; do not invent a fill inside it"


def test_a_crossed_quote_is_rejected(fills):
    frac, basis = fills.half_spread(100.0, bid=101.0, ask=99.0)
    assert basis == "assumed"


# ------------------------------------------------------------------ the gap through a stop
def test_a_gap_through_the_stop_fills_at_the_open(fills):
    """The single largest overstatement in the paper record, and it is worst on exactly the
    days that matter."""
    px, detail = fills.stop_exit_fill(stop=205.49, session_open=190.00)
    assert px < 190.01
    assert "gapped through" in detail
    old_model = 205.49 * (1 - 0.0025)
    assert px < old_model - 10, "the old model would have booked a loss $15 too small"


def test_a_stop_broken_intraday_still_fills_near_the_stop(fills):
    px, _ = fills.stop_exit_fill(stop=205.49, session_open=210.00, price=205.00,
                                 bid=204.90, ask=205.10)
    assert px == pytest.approx(204.90, rel=1e-4)


def test_an_open_above_the_stop_is_not_a_gap(fills):
    px, detail = fills.stop_exit_fill(stop=100.0, session_open=101.0, price=99.5,
                                      bid=99.4, ask=99.6)
    assert "gapped" not in detail


# ------------------------------------------------------------------ the gap through a limit
def test_a_buy_limit_fills_at_the_limit_when_price_comes_to_it(fills):
    px, detail = fills.entry_fill(limit=218.38, session_open=220.00)
    assert px == pytest.approx(218.38)
    assert "resting limit" in detail


def test_a_buy_limit_fills_BETTER_when_the_session_gaps_below_it(fills):
    """Included for symmetry. A cost model that only ever corrects against the book is not a
    cost model, it is a haircut."""
    px, detail = fills.entry_fill(limit=218.38, session_open=210.00)
    assert px == pytest.approx(210.00)
    assert "gapped through" in detail


def test_no_limit_is_no_fill(fills):
    px, detail = fills.entry_fill(limit=None)
    assert px is None


# ------------------------------------------------------------------ the number to beat
def test_round_trip_cost_is_reported_with_its_basis(fills):
    cost, basis = fills.round_trip_cost_pct(100.0, bid=99.9, ask=100.1)
    assert basis == "quoted"
    assert cost == pytest.approx(0.2 + 0.003, abs=0.01), "0.1% each way plus the sale fee"


def test_the_fallback_round_trip_is_the_old_flat_assumption_doubled(fills):
    cost, basis = fills.round_trip_cost_pct(100.0)
    assert basis == "assumed"
    assert cost == pytest.approx(0.5 + 0.003, abs=0.01)


# ------------------------------------------------------------------ the entry gate
@pytest.mark.parametrize("bid,ask,ok", [
    (99.9, 100.1, True),      # 0.2% — inside the 1% gate
    (99.0, 101.0, False),     # 2.0% — refused
    (None, 100.1, False),     # unquotable — refused, never assumed tradeable
])
def test_the_spread_gate_matches_the_engines_rule(fills, bid, ask, ok):
    got, pct = fills.tradeable_spread(100.0, bid, ask, max_spread_pct=1.0)
    assert got is ok


def test_fees_are_charged_on_the_sale_side_only(fills):
    sell, _ = fills.exit_fill(price=100.0, bid=100.0, ask=100.0)
    buy, _ = fills.entry_fill(limit=100.0, session_open=100.0)
    assert sell < 100.0, "a sale pays the SEC/FINRA pass-through"
    assert buy == pytest.approx(100.0), "a purchase does not"
