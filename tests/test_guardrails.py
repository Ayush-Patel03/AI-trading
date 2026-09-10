"""K-07 — the live guardrails: doctrine as validators. Nothing here trades; every test is
a pure function call plus, for the two-key logic, a signed file on disk."""
import datetime as dt
import json

import pytest


@pytest.fixture
def gr():
    import importlib
    import guardrails as g
    importlib.reload(g)
    return g


NOW = dt.datetime(2026, 9, 10, 15, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------ the table
def test_the_doctrine_table_is_what_pm_md_prints(gr):
    assert gr.LIVE_GUARDRAILS["two_key"] == {
        "env_flag": "AI_TRADING_LIVE", "signed_config": "live.signed.json",
        "max_age_hours": 24, "key_env": "AI_TRADING_LIVE_KEY"}
    assert gr.LIVE_GUARDRAILS["per_order"] == {
        "max_notional_usd": 750, "max_pct_from_last": 1.0, "max_quote_age_s": 60,
        "max_spread_pct": 1.0}
    assert gr.LIVE_GUARDRAILS["per_day"] == {
        "max_orders_per_desk": 12, "max_orders_per_symbol": 2,
        "max_notional_multiple_of_equity": 2.0}
    assert gr.LIVE_GUARDRAILS["deny"] == {
        "min_price": 5.0, "min_adv_usd": 10e6, "leveraged_etf": True, "ipo_days": 90,
        "volume_spike_x_adv": 10, "move_pct_no_earnings": 30}
    assert gr.LIVE_GUARDRAILS["circuit"] == {"vix_max": 35, "spy_intraday_drop_pct": -3.0}


def test_pm_rules_carry_the_same_table(run_dir):
    import importlib
    import pm
    importlib.reload(pm)
    import guardrails
    assert pm.PM_RULES["live_guardrails"] == guardrails.LIVE_GUARDRAILS
    assert pm.PM_RULES["live_guardrails"] is not guardrails.LIVE_GUARDRAILS, "a copy, not a view"


# ------------------------------------------------------------------ check_order
GOOD_CTX = {"last": 100.0, "quote_age_s": 5, "spread_pct": 0.05}


def test_check_order_passes_a_clean_ticket(gr):
    ok, why = gr.check_order({"symbol": "A", "shares": 5, "limit_price": 100.0}, GOOD_CTX)
    assert ok and why == []


def test_check_order_notional_ceiling(gr):
    ok, why = gr.check_order({"symbol": "A", "shares": 8, "limit_price": 100.0}, GOOD_CTX)
    assert not ok and any("$800.00" in r and "$750" in r for r in why)
    ok, _ = gr.check_order({"symbol": "A", "shares": 7.5, "limit_price": 100.0}, GOOD_CTX)
    assert ok, "at the ceiling is allowed; over it is not"
    ok, why = gr.check_order({"symbol": "A", "notional": 751.0}, GOOD_CTX)
    assert not ok


def test_check_order_distance_from_last(gr):
    ok, why = gr.check_order({"symbol": "A", "shares": 1, "limit_price": 101.5}, GOOD_CTX)
    assert not ok and any("1.50% from the last print" in r for r in why)
    ok, _ = gr.check_order({"symbol": "A", "shares": 1, "limit_price": 99.1}, GOOD_CTX)
    assert ok


def test_check_order_quote_age_and_spread(gr):
    ok, why = gr.check_order({"symbol": "A", "shares": 1, "limit_price": 100.0},
                             dict(GOOD_CTX, quote_age_s=61))
    assert not ok and any("61s old" in r for r in why)
    ok, why = gr.check_order({"symbol": "A", "shares": 1, "limit_price": 100.0},
                             dict(GOOD_CTX, spread_pct=1.2))
    assert not ok and any("spread 1.20%" in r for r in why)


def test_check_order_strict_refuses_what_it_cannot_verify(gr):
    order = {"symbol": "A", "shares": 1, "limit_price": 100.0}
    ok, why = gr.check_order(order, {})
    assert not ok and any("cannot verify last print, quote age, spread" in r for r in why)
    ok, why = gr.check_order(order, {"strict": False})
    assert ok and why == [], "the paper audit counts bites, not blind spots"


def test_check_order_refuses_a_ticket_with_no_price(gr):
    ok, why = gr.check_order({"symbol": "A", "shares": 1}, GOOD_CTX)
    assert not ok and "no usable price" in why[0]


def test_check_order_reads_overridden_rules(gr):
    rules = {"per_order": {"max_notional_usd": 100}}
    ok, why = gr.check_order({"symbol": "A", "shares": 2, "limit_price": 100.0},
                             dict(GOOD_CTX, rules=rules))
    assert not ok and "$100" in why[0]


# ------------------------------------------------------------------ check_day
def test_check_day_counts_orders_per_desk_and_per_symbol(gr):
    orders = [{"symbol": f"S{i}", "notional": 100.0} for i in range(12)]
    ok, why = gr.check_day({}, {"orders": orders, "equity": 5000.0, "desk": "swing"})
    assert ok and why == []
    ok, why = gr.check_day({}, {"orders": orders + [{"symbol": "X", "notional": 1.0}],
                                "equity": 5000.0, "desk": "swing"})
    assert not ok and any("13 orders today" in r for r in why)
    ok, why = gr.check_day({}, {"orders": [{"symbol": "A", "notional": 1.0}] * 3,
                                "equity": 5000.0, "desk": "swing"})
    assert not ok and any("3 orders in A" in r for r in why)


def test_check_day_notional_multiple_of_equity(gr):
    ok, why = gr.check_day({}, {"orders": [{"symbol": "A", "notional": 6000.0},
                                           {"symbol": "B", "notional": 5000.0}],
                                "equity": 5000.0})
    assert not ok and any("$11,000.00 sent today" in r and "2.0x" in r for r in why)


def test_check_day_reads_the_book_when_no_orders_are_given(gr):
    book = {"day": {"date": "2026-09-10"}, "cash": 1000.0,
            "positions": [{"symbol": "A", "shares": 10, "last_price": 100.0}],
            "working_orders": [{"symbol": "A", "placed": "2026-09-10T14:00:00Z",
                                "limit_price": 100.0, "shares": 1},
                               {"symbol": "A", "placed": "2026-09-10T15:00:00Z",
                                "limit_price": 100.0, "shares": 1},
                               {"symbol": "B", "placed": "2026-09-09T15:00:00Z",
                                "limit_price": 100.0, "shares": 1}],
            "closed_trades": [{"symbol": "A", "closed": "2026-09-10", "exit": 100.0, "shares": 1}]}
    ok, why = gr.check_day(book, {"desk": "swing"})
    assert not ok and any("3 orders in A" in r for r in why), why


# ------------------------------------------------------------------ check_symbol
CLEAN = {"ticker": "SCHW", "price": 92.4, "adv_usd": 500e6, "days_listed": 9000,
         "volume": 5e6, "avg_volume_20d": 5e6, "change_pct": 1.2, "name": "Charles Schwab"}


def test_check_symbol_passes_a_clean_name(gr):
    ok, why = gr.check_symbol(CLEAN)
    assert ok and why == []


def test_check_symbol_min_price_and_adv(gr):
    ok, why = gr.check_symbol(dict(CLEAN, price=4.99))
    assert not ok and any("minimum price" in r for r in why)
    ok, why = gr.check_symbol(dict(CLEAN, adv_usd=9e6))
    assert not ok and any("dollar ADV" in r for r in why)
    ok, why = gr.check_symbol({**{k: v for k, v in CLEAN.items() if k != "adv_usd"},
                               "avg_volume_20d": 50_000})       # 50k × 92.4 = $4.6m
    assert not ok and any("dollar ADV" in r for r in why)
    ok, why = gr.check_symbol({"ticker": "X"})
    assert not ok and "no price" in why[0]


def test_check_symbol_leveraged_etf_ipo_spike_and_unexplained_move(gr):
    ok, why = gr.check_symbol(dict(CLEAN, name="ProShares UltraPro QQQ"))
    assert not ok and any("leveraged ETF" in r for r in why)
    ok, why = gr.check_symbol(dict(CLEAN, leveraged=True))
    assert not ok
    ok, why = gr.check_symbol(dict(CLEAN, days_listed=45))
    assert not ok and any("45 days" in r and "90-day" in r for r in why)
    row = {k: v for k, v in CLEAN.items() if k != "days_listed"}
    ok, why = gr.check_symbol(dict(row, ipo_date="2026-08-01"), {"now": NOW})
    assert not ok and any("40 days" in r for r in why)
    ok, why = gr.check_symbol(dict(CLEAN, volume=55e6))
    assert not ok and any("11.0x the 20-day average" in r for r in why)
    ok, why = gr.check_symbol(dict(CLEAN, change_pct=-31.0))
    assert not ok and any("-31.0%" in r and "no earnings" in r for r in why)
    ok, why = gr.check_symbol(dict(CLEAN, change_pct=-31.0, earnings_today=True))
    assert ok, "a 30% move ON earnings is explained"


def test_check_symbol_strict_vs_audit_on_a_bare_scan_row(gr):
    row = {"ticker": "SCHW", "price": 92.4}
    ok, why = gr.check_symbol(row)
    assert not ok and any("cannot verify dollar ADV, listing age" in r for r in why)
    ok, why = gr.check_symbol(row, {"strict": False})
    assert ok and why == []


# ------------------------------------------------------------------ check_circuit
def test_check_circuit(gr):
    assert gr.check_circuit({"vix": 20.0, "spy_intraday_pct": -1.0}) == (True, [])
    ok, why = gr.check_circuit({"vix": 35.0, "spy_intraday_pct": -1.0})
    assert not ok and "VIX 35.0" in why[0]
    ok, why = gr.check_circuit({"vix": 20.0, "spy_intraday_pct": -3.0})
    assert not ok and "SPY -3.00%" in why[0]
    ok, why = gr.check_circuit({})
    assert not ok and "cannot read VIX, SPY intraday move" in why[0]
    assert gr.check_circuit({"strict": False}) == (True, [])


# ------------------------------------------------------------------ two-key
KEY = "test-key-do-not-use"


def _write(tmp_path, body, key=KEY, sig=None):
    doc = {"body": body}
    import guardrails
    doc["sig"] = sig if sig is not None else guardrails.sign(body, key)
    p = tmp_path / "live.signed.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def _body(expires_h=12, issued_h=1):
    return {"mode": "live", "desk": "swing",
            "issued_at": (NOW - dt.timedelta(hours=issued_h)).isoformat(),
            "expires_at": (NOW + dt.timedelta(hours=expires_h)).isoformat()}


def test_two_key_all_good(gr, tmp_path):
    path = _write(tmp_path, _body())
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert ok, why
    assert "both keys present" in why
    ok, _ = gr.live_mode_allowed({"AI_TRADING_LIVE": "true", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert ok


def test_two_key_missing_env_flag(gr, tmp_path):
    path = _write(tmp_path, _body())
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "AI_TRADING_LIVE is not set" in why
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "0", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "key 1 of 2" in why


def test_two_key_missing_key_or_config(gr, tmp_path):
    path = _write(tmp_path, _body())
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1"}, path, NOW)
    assert not ok and "AI_TRADING_LIVE_KEY is not set" in why
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY},
                                   str(tmp_path / "nope.json"), NOW)
    assert not ok and "key 2 of 2 missing" in why


def test_two_key_expired(gr, tmp_path):
    path = _write(tmp_path, _body(expires_h=-1))
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "expired" in why


def test_two_key_too_old(gr, tmp_path):
    path = _write(tmp_path, _body(expires_h=48, issued_h=25))
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "25.0h ago" in why and "24h maximum" in why


def test_two_key_bad_signature(gr, tmp_path):
    path = _write(tmp_path, _body(), key="some-other-key")
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "signature does not verify" in why
    # a signed body that is then edited
    body = _body()
    path = _write(tmp_path, body)
    doc = json.loads(open(path, encoding="utf-8").read())
    doc["body"]["expires_at"] = (NOW + dt.timedelta(days=365)).isoformat()
    (tmp_path / "live.signed.json").write_text(json.dumps(doc), encoding="utf-8")
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "signature does not verify" in why


def test_two_key_malformed_config(gr, tmp_path):
    p = tmp_path / "live.signed.json"
    p.write_text("not json", encoding="utf-8")
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, str(p), NOW)
    assert not ok and "unreadable" in why
    p.write_text(json.dumps({"body": "x"}), encoding="utf-8")
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, str(p), NOW)
    assert not ok and "{body, sig}" in why
    body = {"mode": "live", "expires_at": "whenever"}
    path = _write(tmp_path, body)
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "expires_at" in why
    path = _write(tmp_path, dict(_body(), mode="paper"))
    ok, why = gr.live_mode_allowed({"AI_TRADING_LIVE": "1", "AI_TRADING_LIVE_KEY": KEY}, path, NOW)
    assert not ok and "not 'live'" in why


def test_signature_is_canonical_and_deterministic(gr):
    a = gr.sign({"b": 1, "a": [1, 2]}, KEY)
    b = gr.sign({"a": [1, 2], "b": 1}, KEY)
    assert a == b and len(a) == 64
    assert gr.sign({"a": 1}, KEY) != gr.sign({"a": 1}, KEY + "x")
    doc = gr.signed_config({"a": 1}, KEY)
    assert doc == {"body": {"a": 1}, "sig": gr.sign({"a": 1}, KEY)}


def test_pm_never_calls_the_two_key_or_circuit_checks(gr):
    """Doctrine: paper mode runs the per-order and deny-list audit and nothing else."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "engine" / "pm.py").read_text(
        encoding="utf-8")
    assert "live_mode_allowed" not in src
    assert "check_circuit" not in src and "check_day(" not in src
    assert "guardrails_mod.check_order" in src and "guardrails_mod.check_symbol" in src
