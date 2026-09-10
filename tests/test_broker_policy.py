"""K-01 — the broker policy. Three regimes behind one interface; legacy_pdt must be the
old PDT guard byte-for-byte, intraday_margin (the default) must never count day trades,
cash_settled must hold proceeds until T+1. The resolution order is pinned because a
policy picked from the wrong place silently trades the book under the wrong rulebook."""
import datetime as dt
import json

import pytest

import broker_policy as bp
from conftest import run_pm

TODAY = dt.date(2026, 9, 10)          # a Thursday


def _pos(shares=2.0, intraday=0.0, price=100.0):
    return {"symbol": "XYZ", "shares": shares, "intraday_shares": intraday, "last_price": price,
            "avg_cost": price}


def _book(cash=1000.0, day_trades=None, **extra):
    b = {"cash": cash, "positions": [], "working_orders": [],
         "day_trades": day_trades or []}
    b.update(extra)
    return b


def _dts(n, today=TODAY):
    return [{"date": d, "symbol": "Q", "shares": 1.0, "reason": "stop"}
            for d in bp.business_days_back(today, n)]


# ---------------------------------------------------------------- legacy_pdt, unit
def test_legacy_stop_on_intraday_shares_spends_a_day_trade():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    shares, note = pol.sellable(_pos(2.0, intraday=2.0), 2.0, "stop", _book(), TODAY, 5000.0)
    assert shares == 2.0
    assert note == "legacy_pdt: Day trade used to honour a stop — 1/3 in the rolling window"


def test_legacy_target_on_fully_intraday_position_is_held():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    shares, note = pol.sellable(_pos(2.0, intraday=2.0), 1.0, "target", _book(), TODAY, 5000.0)
    assert shares == 0.0
    assert note == "legacy_pdt: PDT guard: selling would be day trade 1/3 and this is not a stop — held"


def test_legacy_sells_only_the_settled_part_when_it_clears_the_minimum():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    shares, note = pol.sellable(_pos(3.0, intraday=2.0), 3.0, "trim", _book(), TODAY, 5000.0)
    assert shares == 1.0
    assert note.startswith("legacy_pdt: PDT guard: only the 1.000000 settled shares are sellable")


def test_legacy_settled_shares_cover_the_want_with_no_note():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    assert pol.sellable(_pos(3.0, intraday=1.0), 2.0, "trim", _book(), TODAY, 5000.0) == (2.0, None)


def test_legacy_stop_with_budget_exhausted_is_refused():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    shares, note = pol.sellable(_pos(2.0, intraday=2.0), 2.0, "stop", _book(day_trades=_dts(3)),
                                TODAY, 5000.0)
    assert shares == 0.0 and "day trade 4/3" in note


def test_legacy_entries_stop_at_two_of_three_and_name_the_hatch():
    pol = bp.LegacyPDT()
    assert pol.entries_allowed(_book(day_trades=_dts(1)), TODAY, 5000.0) == (True, None)
    ok, note = pol.entries_allowed(_book(day_trades=_dts(2)), TODAY, 5000.0)
    assert ok is False
    assert note == ("legacy_pdt: PDT guard: 2/3 day trades used — no new entries, one is held "
                    "back as an exit hatch")


def test_legacy_guard_disables_itself_above_the_threshold():
    pol = bp.LegacyPDT(risk_rules={"min_notional": 1.0})
    assert pol.sellable(_pos(2.0, intraday=2.0), 2.0, "trim", _book(day_trades=_dts(3)),
                        TODAY, 25000.0) == (2.0, None)
    assert pol.entries_allowed(_book(day_trades=_dts(3)), TODAY, 25000.0) == (True, None)
    st = pol.state(_book(day_trades=_dts(2)), TODAY, 25000.0)
    assert st == {"broker_policy": "legacy_pdt", "day_trades_used": 2, "day_trade_limit": 3,
                  "pdt_applies": False}


def test_legacy_reads_pm_rules_by_reference():
    """A desk's pm_rules override of a pdt_* key must reach the policy, as it used to."""
    import pm
    for k, v in bp.LEGACY_PDT_RULES.items():
        assert pm.PM_RULES[k] == v
    rules = dict(bp.LEGACY_PDT_RULES)
    pol = bp.LegacyPDT(pm_rules=rules)
    rules["pdt_max_day_trades"] = 5
    assert pol.entries_allowed(_book(day_trades=_dts(3)), TODAY, 5000.0) == (True, None)


# ---------------------------------------------------------------- legacy_pdt, engine
def _set_position(run_dir, symbol="NVDA", **fields):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == symbol:
            p.update(fields)
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    return b


def _set_book(run_dir, **fields):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    b.update(fields)
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    return b


def _bought_today(run_dir, symbol="NVDA"):
    """Tag the position as opened this session. The book's day record is stamped today
    too, or roll_day() would clear the intraday tag before any policy saw it."""
    b = _set_position(run_dir, symbol, stop=240.0)
    shares = [p for p in b["positions"] if p["symbol"] == symbol][0]["shares"]
    _set_position(run_dir, symbol, intraday_shares=shares)
    _set_book(run_dir, day={"date": dt.date.today().isoformat(), "open_equity": None,
                            "halted": False, "halt_reason": None})
    return shares


def test_engine_under_legacy_records_the_day_trade_exactly_as_before(pm, run_dir, quotes):
    shares = _bought_today(run_dir)
    bk = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    book, jrn, state = pm.run(bk, None, pm.quotes_to_prices(quotes, pm._now())[0],
                              "sentinel", None, "paper", policy_name="legacy_pdt")
    assert jrn["broker_policy"] == "legacy_pdt"
    assert state["book"]["day_trade_limit"] == 3 and state["book"]["pdt_applies"] is True
    assert [d["action"] for d in jrn["decisions"]] == ["fill-sell"]
    assert book["day_trades"] == [{"date": dt.date.today().isoformat(), "symbol": "NVDA",
                                   "shares": round(shares, 6), "reason": "stop"}]
    assert any(w == "NVDA: legacy_pdt: Day trade used to honour a stop — 1/3 in the rolling window"
               for w in jrn["warnings"])


def test_engine_under_legacy_refuses_entries_at_two_day_trades(pm, run_dir, quotes, scan):
    _set_book(run_dir, day_trades=_dts(2, dt.date.today()))
    bk = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    book, jrn, state = pm.run(bk, scan, pm.quotes_to_prices(quotes, pm._now())[0],
                              "opening-range", None, "paper", policy_name="legacy_pdt")
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert any(s["reason"].startswith("legacy_pdt: PDT guard: 2/3") for s in jrn["skipped"])
    assert state["book"]["day_trades_used"] == 2


def test_engine_under_legacy_leaves_a_broken_stop_unprotected_when_exhausted(pm, run_dir, quotes):
    _bought_today(run_dir)
    _set_book(run_dir, day_trades=_dts(3, dt.date.today()))
    bk = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    book, jrn, _ = pm.run(bk, None, pm.quotes_to_prices(quotes, pm._now())[0],
                          "sentinel", None, "paper", policy_name="legacy_pdt")
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert any(w.startswith("UNPROTECTED: NVDA broke its stop") for w in jrn["warnings"])


# ---------------------------------------------------------------- intraday_margin
def test_intraday_margin_sells_same_day_shares_with_no_cap():
    pol = bp.IntradayMargin()
    book = _book(day_trades=_dts(5))
    assert pol.sellable(_pos(2.0, intraday=2.0), 2.0, "target", book, TODAY, 500.0) == (2.0, None)
    assert pol.sellable(_pos(2.0, intraday=2.0), 2.0, "trim", book, TODAY, 500.0) == (2.0, None)
    st = pol.state(book, TODAY, 500.0)
    assert st["day_trades_used"] == 5 and st["day_trade_limit"] is None
    assert st["pdt_applies"] is False and st["broker_policy"] == "intraday_margin"


def test_intraday_margin_still_records_day_trades_for_reporting():
    pol = bp.IntradayMargin()
    book = _book()
    pol.record_sale(book, _pos(2.0, intraday=1.5), 2.0, TODAY, 100.0, "target")
    assert book["day_trades"] == [{"date": "2026-09-10", "symbol": "XYZ", "shares": 1.5,
                                   "reason": "target"}]


def test_intraday_margin_refuses_entries_on_a_projected_deficit():
    pol = bp.IntradayMargin()
    # cash -4,000 against $5,000 of stock: equity 1,000, 25% maintenance is 1,250 -> $250 short
    book = _book(cash=-4000.0)
    assert pol.projected_deficit(book, 1000.0) == 250.0
    ok, note = pol.entries_allowed(book, TODAY, 1000.0)
    assert ok is False
    assert note.startswith("intraday_margin: projected intraday margin deficit $250.00")
    assert book["imd_events"] == [{"date": "2026-09-10", "amount": 250.0, "counted": True,
                                   "met": None}]


def test_intraday_margin_counts_working_buys_toward_the_requirement():
    pol = bp.IntradayMargin()
    book = _book(cash=0.0)
    book["working_orders"] = [{"side": "buy", "status": "working", "notional": 4000.0,
                               "limit_price": 40.0, "shares": 100}]
    # equity 900 (all stock), plus a $4,000 resting buy: 25% of 4,900 = 1,225 > 900
    assert pol.projected_deficit(book, 900.0) == 325.0
    assert pol.entries_allowed(book, TODAY, 900.0)[0] is False


def test_intraday_margin_never_binds_on_a_cash_only_book():
    pol = bp.IntradayMargin()
    book = _book(cash=2764.25)
    assert pol.projected_deficit(book, 5000.0) < 0
    assert pol.entries_allowed(book, TODAY, 5000.0) == (True, None)
    assert "imd_events" not in book, "a book that never had a deficit carries no ledger of them"


def test_intraday_margin_de_minimis_deficit_is_not_counted():
    pol = bp.IntradayMargin()
    # cash -15,300 against $20,300 of stock: equity 5,000, 25% maintenance 5,075 -> a $75
    # deficit, under the min(5% of 5,000 = $250, $1,000) de minimis line.
    book = _book(cash=-15300.0)
    ok, note = pol.entries_allowed(book, TODAY, 5000.0)
    assert ok is False and "de minimis, not counted" in note
    assert book["imd_events"][0]["counted"] is False
    assert pol.counted_recent(book, TODAY) == []


def test_intraday_margin_cured_deficit_is_marked_met():
    pol = bp.IntradayMargin()
    book = _book(cash=-4000.0)
    pol.entries_allowed(book, TODAY, 1000.0)
    book["cash"] = 1000.0                     # funded: equity 6,000 against 5,000 of stock
    assert pol.entries_allowed(book, TODAY + dt.timedelta(days=1), 6000.0) == (True, None)
    assert book["imd_events"][0]["met"] == "2026-09-11"
    assert book.get("freeze_until") is None


def test_intraday_margin_freezes_after_a_practice_of_unmet_deficits():
    pol = bp.IntradayMargin()
    book = _book(cash=-4000.0, imd_events=[
        {"date": "2026-08-20", "amount": 500.0, "counted": True, "met": "2026-08-21"},
        {"date": "2026-09-01", "amount": 600.0, "counted": True, "met": None},   # 7 business days open
    ])
    ok, note = pol.entries_allowed(book, TODAY, 1000.0)
    assert ok is False and "that is a practice" in note
    assert book["freeze_until"] == "2026-12-09"
    # Frozen stays frozen even once the deficit is cured
    book["cash"] = 5000.0
    ok, note = pol.entries_allowed(book, TODAY + dt.timedelta(days=3), 10000.0)
    assert ok is False and note.startswith("intraday_margin: account restricted until 2026-12-09")
    assert pol.state(book, TODAY, 10000.0)["frozen"] is True
    # and thaws the day it expires
    assert pol.entries_allowed(book, dt.date(2026, 12, 9), 10000.0) == (True, None)


def test_intraday_margin_one_unmet_deficit_is_a_call_not_a_practice():
    pol = bp.IntradayMargin()
    book = _book(cash=-4000.0, imd_events=[
        {"date": "2026-09-01", "amount": 600.0, "counted": True, "met": None}])
    ok, note = pol.entries_allowed(book, TODAY, 1000.0)
    assert ok is False and "practice" not in note
    assert book.get("freeze_until") is None


def test_intraday_margin_notes_the_two_thousand_minimum_without_refusing():
    pol = bp.IntradayMargin()
    ok, note = pol.entries_allowed(_book(cash=1500.0), TODAY, 1500.0)
    assert ok is True and "$2,000 margin minimum" in note


def test_engine_default_is_intraday_margin_and_the_book_trades_as_before(pm, run_dir, quotes, scan):
    book, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert jrn["broker_policy"] == "intraday_margin"
    assert state["broker_policy"]["name"] == "intraday_margin"
    assert state["book"]["pdt_applies"] is False and state["book"]["day_trade_limit"] is None
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy"], \
        "a cash-only book under intraday margin places entries exactly as before"


def test_engine_under_intraday_margin_ignores_the_day_trade_budget(pm, run_dir, quotes):
    _bought_today(run_dir)
    _set_book(run_dir, day_trades=_dts(3, dt.date.today()))
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell" and d["symbol"] == "NVDA"]
    assert len(sells) == 1
    assert not any("UNPROTECTED" in w for w in jrn["warnings"])
    assert len(book["day_trades"]) == 4, "recorded for reporting, never gated on"


# ---------------------------------------------------------------- cash_settled
def test_cash_settled_proceeds_settle_next_business_day():
    pol = bp.CashSettled()
    book = _book(cash=1000.0)
    pol.record_sale(book, _pos(2.0), 2.0, TODAY, 100.0, "target")
    book["cash"] += 200.0                                  # what pm.py does after the sale
    assert book["unsettled"][0]["settles"] == "2026-09-11"
    assert pol.buying_power(book, TODAY, book["cash"]) == 1000.0
    st = pol.state(book, TODAY, 1200.0)
    assert st["settled_cash"] == 1000.0 and st["unsettled_cash"] == 200.0
    assert pol.buying_power(book, dt.date(2026, 9, 11), book["cash"]) == 1200.0


def test_cash_settled_friday_proceeds_settle_monday():
    assert bp.next_business_day(dt.date(2026, 9, 11)) == dt.date(2026, 9, 14)


def test_cash_settled_counts_a_good_faith_violation():
    pol = bp.CashSettled()
    book = _book(cash=100.0)
    pol.record_sale(book, _pos(2.0), 2.0, TODAY, 100.0, "target")     # +200 unsettled
    book["cash"] += 200.0
    new = _pos(2.0, intraday=2.0, price=125.0)
    book["cash"] -= 250.0                                             # pm.py debits first
    pol.record_buy(book, new, 2.0, TODAY, 125.0)
    assert new["funds_settle"] == "2026-09-11", "150 of the 250 came from unsettled proceeds"
    pol.record_sale(book, new, 2.0, TODAY, 130.0, "stop")
    assert len(book["gfv"]) == 1 and book["gfv"][0]["symbol"] == "XYZ"
    assert book.get("restricted_until") is None
    # the same sale once the funds have settled is clean
    other = _pos(1.0, price=100.0); other["funds_settle"] = "2026-09-11"
    pol.record_sale(book, other, 1.0, dt.date(2026, 9, 11), 100.0, "stop")
    assert len(book["gfv"]) == 1


def test_cash_settled_buy_from_settled_cash_carries_no_settle_date():
    pol = bp.CashSettled()
    book = _book(cash=1000.0)
    new = _pos(2.0, intraday=2.0)
    book["cash"] -= 200.0
    pol.record_buy(book, new, 2.0, TODAY, 100.0)
    assert "funds_settle" not in new


def test_cash_settled_three_gfvs_restrict_and_are_flagged():
    pol = bp.CashSettled()
    book = _book(cash=0.0, gfv=[{"date": "2026-06-01", "symbol": "A", "detail": "x"},
                                {"date": "2026-08-01", "symbol": "B", "detail": "x"}])
    p = _pos(1.0); p["funds_settle"] = "2026-09-11"
    pol.record_sale(book, p, 1.0, TODAY, 100.0, "trim")
    assert len(book["gfv"]) == 3
    assert book["restricted_until"] == "2026-12-09"
    st = pol.state(book, TODAY, 100.0)
    assert st["restricted"] is True and st["gfv_12m"] == 3
    ok, note = pol.entries_allowed(book, TODAY, 100.0)
    assert ok is True and note.startswith("cash_settled: account restricted until 2026-12-09")


def test_cash_settled_old_gfvs_roll_off_after_twelve_months():
    pol = bp.CashSettled()
    book = _book(gfv=[{"date": "2025-09-01", "symbol": "A", "detail": "x"},
                      {"date": "2025-09-02", "symbol": "B", "detail": "x"}])
    p = _pos(1.0); p["funds_settle"] = "2026-09-11"
    pol.record_sale(book, p, 1.0, TODAY, 100.0, "trim")
    assert book.get("restricted_until") is None


def test_engine_under_cash_settled_holds_proceeds_from_the_next_entry(pm, run_dir, quotes, scan):
    _set_position(run_dir, stop=240.0)
    bk = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    book, jrn, state = pm.run(bk, scan, pm.quotes_to_prices(quotes, pm._now())[0],
                              "opening-range", None, "paper", policy_name="cash_settled")
    sells = [d for d in jrn["decisions"] if d["action"] == "fill-sell"]
    assert sells and book["unsettled"][0]["symbol"] == "NVDA"
    assert state["book"]["broker_policy"] == "cash_settled"
    assert state["book"]["settled_cash"] == pytest.approx(book["cash"] - book["unsettled"][0]["amount"], abs=0.01)


# ---------------------------------------------------------------- selection
def test_resolution_order_explicit_book_desk_config_default():
    book = {"broker_policy": "cash_settled"}
    desk = {"rules": {"broker_policy": "legacy_pdt"}}
    cfg = {"broker_policy": "cash_settled"}
    assert bp.resolve_name("legacy_pdt", book, desk, cfg) == "legacy_pdt"
    assert bp.resolve_name(None, book, desk, cfg) == "cash_settled"
    assert bp.resolve_name(None, {}, desk, cfg) == "legacy_pdt"
    assert bp.resolve_name(None, {}, {"rules": {}}, cfg) == "cash_settled"
    assert bp.resolve_name(None, {}, {}, {}) == "intraday_margin"
    assert bp.DEFAULT == "intraday_margin"


def test_get_policy_reads_engine_config_when_staged(run_dir, monkeypatch):
    import config
    (run_dir / "engine-config.json").write_text(json.dumps({"broker_policy": "cash_settled"}),
                                                encoding="utf-8")
    config._CACHE.clear()
    assert bp.get_policy(None, {}, {}).name == "cash_settled"
    config._CACHE.clear()


def test_get_policy_returns_the_named_class():
    assert isinstance(bp.get_policy("legacy_pdt"), bp.LegacyPDT)
    assert isinstance(bp.get_policy("intraday_margin"), bp.IntradayMargin)
    assert isinstance(bp.get_policy("cash_settled"), bp.CashSettled)
    for name in bp.VALID:
        assert bp.get_policy(name).describe().startswith(name + ":")


def test_unknown_policy_name_names_the_valid_ones():
    with pytest.raises(ValueError) as e:
        bp.get_policy("robinhood_gold")
    assert "legacy_pdt, intraday_margin, cash_settled" in str(e.value)
    with pytest.raises(ValueError):
        bp.get_policy(None, {"broker_policy": "nope"})


def test_engine_reads_the_policy_from_the_book(pm, run_dir, quotes):
    _set_book(run_dir, broker_policy="legacy_pdt")
    _, jrn, state = run_pm(pm, run_dir, slot="sentinel")
    assert jrn["broker_policy"] == "legacy_pdt" and state["book"]["day_trade_limit"] == 3


def test_engine_reads_the_policy_from_the_desk(pm, run_dir, quotes):
    pm.DESK["rules"] = {"broker_policy": "cash_settled"}
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert jrn["broker_policy"] == "cash_settled"


def _cli(run_dir, *args):
    import os
    import subprocess
    import sys
    from conftest import ENGINE
    env = dict(os.environ, SCAN_DIR=str(run_dir), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args], cwd=run_dir,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_cli_flag_selects_the_policy_and_the_console_names_it(run_dir, quotes):
    _bought_today(run_dir)                             # so the sentinel acts and writes state
    r = _cli(run_dir, "--slot", "sentinel", "--broker-policy", "legacy_pdt")
    assert r.returncode == 0, r.stderr
    assert "day trades 1/3" in r.stdout, r.stdout
    st = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    assert st["book"]["broker_policy"] == "legacy_pdt"
    _set_position(run_dir, stop=240.0)
    r2 = _cli(run_dir, "--slot", "sentinel")
    assert r2.returncode == 0, r2.stderr
    assert "policy intraday_margin" in r2.stdout and "day trades" not in r2.stdout
    st2 = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    assert st2["book"]["broker_policy"] == "intraday_margin"


def test_cli_rejects_an_unknown_policy_in_the_book(run_dir, quotes):
    _set_book(run_dir, broker_policy="nope")
    r = _cli(run_dir, "--slot", "opening-range")
    assert r.returncode == 2
    assert "unknown broker policy 'nope'" in r.stderr
