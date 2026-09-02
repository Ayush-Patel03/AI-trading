"""The live-account divergence check — a safety mechanism that used to pass silently.

PM.md section 1 promises the divergence check is "a mechanism, not a promise". Until
2026-09-02 the parser read `data.results`, the shape `get_equity_quotes` returns, while
`get_equity_positions` returns `data.positions`. A real live holding produced no warning
and the run reported "no divergence" — indistinguishable from a genuinely flat account.
These tests exist so the payload shape cannot drift back.
"""


def _holding(rows):
    return {"data": {"positions": rows}}


def test_a_live_holding_under_the_connectors_key_raises_a_divergence(pm):
    warns = pm.broker_divergence(_holding([{"symbol": "NVDA", "quantity": "2.5"}]), {})
    assert len(warns) == 1
    assert "LIVE ACCOUNT DIVERGENCE" in warns[0]
    assert "NVDA" in warns[0]


def test_the_legacy_results_key_still_parses(pm):
    """An older staged payload, or one written under both keys, must not regress."""
    warns = pm.broker_divergence({"data": {"results": [{"symbol": "MU", "quantity": "1"}]}}, {})
    assert len(warns) == 1 and "MU" in warns[0]


def test_a_top_level_positions_key_parses(pm):
    warns = pm.broker_divergence({"positions": [{"symbol": "HOOD", "quantity": "3"}]}, {})
    assert len(warns) == 1 and "HOOD" in warns[0]


def test_a_flat_account_raises_nothing(pm):
    assert pm.broker_divergence(_holding([]), {}) == []
    assert pm.broker_divergence({"data": {"positions": []}}, {}) == []


def test_a_zero_quantity_row_is_not_a_holding(pm):
    assert pm.broker_divergence(_holding([{"symbol": "NVDA", "quantity": "0"}]), {}) == []


def test_a_malformed_payload_contributes_nothing_rather_than_crashing(pm):
    for junk in (None, {}, {"data": None}, {"data": "nope"}, {"data": {"positions": "nope"}},
                 {"data": {"positions": [None, 7, "x"]}}):
        assert pm.broker_divergence(junk, {}) == []


def test_the_symbol_can_arrive_nested_under_instrument(pm):
    warns = pm.broker_divergence(
        _holding([{"instrument": {"symbol": "SNDK"}, "quantity": "0.5"}]), {})
    assert len(warns) == 1 and "SNDK" in warns[0]
