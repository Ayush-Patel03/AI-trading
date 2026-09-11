"""tests/test_gate_drills.py — the halt-and-refuse machinery, driven until it fires.

Eleven days of live journals put twelve of the taxonomy's rules at zero: they have never
been struck, so nothing in the record says whether they work. `report.py` can only classify
a refusal the engine actually wrote; a gate that has never refused anything is a gate
nobody has tested. This file is the fixture drill for them.

Each gate is asked three questions, and the third is the one that matters:

  1. Does it refuse — and is the book left in the state `docs/PM.md` says it should be?
  2. Is the string it writes into `skipped` the one `report.classify()` maps to that rule?
     (Classification is pinned per-string in `test_report.py`; what is pinned HERE is that
     the engine emits the string at all, from a real run, on a book shaped like the live
     ones.)
  3. **Do exits still run while it is on?** PM.md is explicit in three separate places —
     §7 for the kill switch, §7's ladder table, §2 for the macro gate, §14b for the house
     exposure gate — that a gate on entries is never a gate on a stop. That is the property
     a dormant control is most likely to have got wrong, so every gate here is driven with
     a position through its stop at the same time.

Written to the DOCTRINE, not to the code. Where the two disagree the test is `xfail` with
the divergence spelled out, and the engine is left alone:

  * `min_notional` against a rebalance shave — refused with no journal line at all
    (`test_a_rebalance_shave_under_the_minimum_is_journaled`).
  * `legacy_pdt` refusing a stop — the banner is right, the `skipped` reason says the sale
    was refused because "this is not a stop" when it is
    (`test_the_pdt_refusal_says_why_it_really_refused`).
  * `earnings_gate` — a reserved rule with no doctrine and no emitter
    (`test_a_candidate_reporting_earnings_is_gated`).

Two rules are drilled as facts about this branch rather than as behaviour:
`veto` does not exist here (it arrives with PR #3, `feat/phase5-2026-09-10`), and `halt`
has no producer in the engine at all — every halt the engine writes classifies as
`kill_switch` or `ladder`.

Fixtures: the three books `conftest.run_dir` already stages, which are the 2026-09-02
house (MU / SNDK / NVDA / HOOD on swing, seven distinct names across the desks, $14.9k
combined). Nothing identifying is added — no new fixture file is written at all; the
reshaping each drill needs is done in-test on the staged copies under tmp_path.
"""
import datetime as dt
import importlib
import json

import pytest

from conftest import ENGINE, run_pm, _fresh_scan_meta


# ------------------------------------------------------------------ scaffolding
@pytest.fixture
def rules(run_dir):
    """report.py, reloaded against this run directory."""
    import report
    return importlib.reload(report)


def _rule(rules, reason):
    return rules.classify(reason)["rule"]


def _skips(jrn, symbol=None):
    return [s["reason"] for s in jrn["skipped"]
            if symbol is None or s["symbol"] == symbol]


def _rules_fired(rules, jrn):
    return {_rule(rules, s["reason"]) for s in jrn["skipped"]}


def _sells(jrn, symbol=None, reason=None):
    return [d for d in jrn["decisions"]
            if d["action"] == "fill-sell"
            and (symbol is None or d["symbol"] == symbol)
            and (reason is None or d["reason"] == reason)]


def _placed(jrn, symbol=None):
    return [d for d in jrn["decisions"]
            if d["action"] == "place-buy" and (symbol is None or d["symbol"] == symbol)]


def _load(run_dir, name="paper_book.json"):
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def _save(run_dir, book, name="paper_book.json"):
    (run_dir / name).write_text(json.dumps(book), encoding="utf-8")


def _edit(run_dir, fn, name="paper_book.json"):
    b = _load(run_dir, name)
    fn(b)
    _save(run_dir, b, name)
    return b


def _today(pm):
    return pm._now().date()


def _marked(book):
    """The book's own equity at its last marks — enough to calibrate a day-P&L target
    without running the engine first."""
    return book["cash"] + sum(p["shares"] * (p.get("last_price") or p["avg_cost"])
                              for p in book["positions"])


def _arm_session(book, today, open_equity=None):
    """Keep `roll_day()` from wiping the day block.

    Every drill that needs a live day state — a halt, a session P&L, an exhausted
    day-trade budget — has to date the book to TODAY first: `roll_day` resets `day` and
    prunes `day_trades` the moment the book's date is not today's, which silently undoes
    the fixture.
    """
    book["day"] = {"date": today.isoformat(), "open_equity": open_equity,
                   "halted": False, "halt_reason": None}


def _quiet_ladder(book):
    """Park the drawdown ladder at rung 0 so it cannot mask the gate under test.

    `ladder_pass` runs before the exit pass and before every entry gate, so a book whose
    HWM is above its equity refuses entries for its own reason. Seeding the HWM from the
    current mark and clearing the curve the seeder falls back on isolates whatever else
    the test is driving.
    """
    book["equity_curve"] = []
    book["hwm"] = 1.0
    book["cool_until"] = None
    book["ladder_halt"] = None


def _break_stop(book, symbol="NVDA", stop=240.00):
    """Put a holding through its stop. The staged quote for NVDA prints 222.255."""
    for p in book["positions"]:
        if p["symbol"] == symbol:
            p["stop"] = stop


def _resting_buy(symbol="AVGO", limit=400.00, shares=0.5, gics="Information Technology"):
    return {"id": f"drill-{symbol}", "symbol": symbol, "side": "buy", "kind": "entry",
            "type": "limit", "limit_price": limit, "shares": shares,
            "notional": round(limit * shares, 2), "placed": "2026-09-02T13:00:00Z",
            "placed_slot": "pre-market", "run_key": "2026-09-02#pre-market",
            "expires": "day", "status": "working", "reason": "Score 70 Momentum — Buy",
            "meta": {"gics": gics, "industry": "Semiconductors", "stop": limit * 0.9,
                     "target": limit * 1.3, "stop_basis": "1.5x ATR stop",
                     "stop_basis_kind": "atr", "score": 70.0}}


def _drop_quote(run_dir, symbol):
    q = json.loads((run_dir / "pm_quotes.json").read_text(encoding="utf-8"))
    q["data"]["results"] = [r for r in q["data"]["results"]
                            if r["quote"]["symbol"] != symbol]
    (run_dir / "pm_quotes.json").write_text(json.dumps(q), encoding="utf-8")


def _scan_rows(run_dir, rows, macro_events=()):
    (run_dir / "scan_results.json").write_text(json.dumps(_fresh_scan_meta(
        {"meta": {"slot": "opening range", "macro_events": list(macro_events)},
         "results": rows})), encoding="utf-8")


# Candidate rows in the shape build_proposals needs. SCHW is Financials (no desk holds a
# Financials name but HOOD, so it clears the per-desk sector cap); AVGO is Information
# Technology, which is where the house sits.
SCHW = {"ticker": "SCHW", "name": "Charles Schwab", "price": 92.40, "score": 78.0,
        "setup": "Momentum", "verdict": "Strong Buy", "gics": "Financials",
        "sector": "Financials", "industry": "Capital Markets", "rsi_14": 60.0,
        "atr_14": 2.77, "atr_pct": 3.0, "ma_50": 87.78, "ma_200": 79.46,
        "confidence": "ok", "upside_pct": 12.0, "analyst_target": 103.49,
        "setup_note": "Price above both MAs with 50 over 200 — trend intact"}

AVGO = {"ticker": "AVGO", "name": "Broadcom", "price": 402.10, "score": 78.0,
        "setup": "Momentum", "verdict": "Strong Buy", "gics": "Information Technology",
        "sector": "Information Technology", "industry": "Semiconductors", "rsi_14": 60.0,
        "atr_14": 12.00, "atr_pct": 3.0, "ma_50": 380.00, "ma_200": 350.00,
        "confidence": "ok", "upside_pct": 12.0, "analyst_target": 450.00,
        "setup_note": "Price above both MAs with 50 over 200 — trend intact"}


# ================================================================== kill_switch
# PM.md §7: "A session loss of 3% or worse halts the book: working buy orders are
# cancelled, no new entries for the rest of the session... Exits stay live — a halt that
# blocked stop-losses would be the opposite of a safety feature."

def _kill_switch_book(run_dir, pm, with_stop=False):
    b = _load(run_dir)
    target = round(_marked(b) / (1 - 0.035), 2)      # ≈ −3.5% on the session

    def edit(bk):
        _arm_session(bk, _today(pm), open_equity=target)
        _quiet_ladder(bk)
        bk["working_orders"] = [_resting_buy()]
        if with_stop:
            _break_stop(bk)
    return _edit(run_dir, edit)


def test_the_kill_switch_trips_cancels_resting_buys_and_freezes_entries(
        pm, run_dir, quotes, rules):
    _kill_switch_book(run_dir, pm)
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert jrn["daily_pnl_pct"] <= -3.0
    assert book["day"]["halted"] is True
    assert "kill switch" in book["day"]["halt_reason"]
    # the resting buy is cancelled, not left working — PM.md §7
    assert [d for d in jrn["decisions"]
            if d["action"] == "cancel" and d["reason"] == "kill switch"]
    assert book["working_orders"] == []
    # ...and no entry is placed for the rest of the session
    assert _placed(jrn) == []
    assert "kill_switch" in _rules_fired(rules, jrn)
    assert any("KILL SWITCH TRIPPED" in w for w in jrn["warnings"])


def test_a_stop_still_fires_through_the_kill_switch(pm, run_dir, quotes, rules):
    """The property the halt exists to preserve. A halt that blocked a stop-loss would be
    the opposite of a safety feature (PM.md §7)."""
    _kill_switch_book(run_dir, pm, with_stop=True)
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert book["day"]["halted"] is True
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]
    assert _placed(jrn) == []


# ================================================================== halt
# report.py: `halt` is "any other halt / HALT: the day carries as halt_reason" — the
# catch-all under kill_switch and ladder.

def _halted_book(run_dir, pm, reason, with_stop=True):
    def edit(bk):
        _arm_session(bk, _today(pm))
        _quiet_ladder(bk)
        bk["day"]["halted"] = True
        bk["day"]["halt_reason"] = reason
        bk["working_orders"] = [_resting_buy()]
        if with_stop:
            _break_stop(bk)
    return _edit(run_dir, edit)


def test_a_halt_the_book_already_carries_freezes_entries_and_names_itself(
        pm, run_dir, quotes, rules):
    reason = "HALT: dead-man's switch tripped — two sentinel runs missed"
    _halted_book(run_dir, pm, reason, with_stop=False)
    _scan_rows(run_dir, [SCHW])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert reason in _skips(jrn, "*")
    assert _rule(rules, reason) == "halt"
    assert _placed(jrn) == []


def test_a_stop_still_fires_through_a_halt(pm, run_dir, quotes, rules):
    reason = "HALT: dead-man's switch tripped — two sentinel runs missed"
    _halted_book(run_dir, pm, reason)
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]
    assert _placed(jrn) == []


def test_no_halt_string_the_engine_writes_today_reaches_the_halt_rule(rules):
    """Why `halt` has never fired, and why it never will on this branch.

    Only two code paths in the engine set `day["halt_reason"]`, and both write a string
    that an earlier rule claims: the kill switch's own sentence goes to `kill_switch`, the
    ladder's rung-3 halt goes to `ladder`, and `portfolio.build_proposals`' two `HALT:`
    blocks go to `kill_switch` and `max_entries`. Nothing the engine can write lands in
    `halt`. It is reachable only from a halt some OTHER component stamped on the book —
    the dead-man's switch (K-05) is the specified case, and it stamps protective stops
    rather than halting, so today there is no such component.
    """
    assert _rule(rules, "Daily loss -3.15% breached the 3% kill switch") == "kill_switch"
    assert _rule(rules, "HALT: daily loss -3.15% breached the 3% kill-switch limit — "
                        "no new entries") == "kill_switch"
    assert _rule(rules, "Drawdown ladder halt — ladder rung 3: drawdown 8.10% from the "
                        "high-water mark ($5,200.00) reached the 8% halt") == "ladder"
    assert _rule(rules, "HALT: at max positions (10/10) — no new entries") == "max_entries"


# ================================================================== macro_gate
# PM.md §2: two conditions must both hold — high impact AND inside the 120-minute
# lookahead. "Exits, trims and rebalancing always stay live through a gate."

def _macro_event(minutes_ahead, name):
    from zoneinfo import ZoneInfo
    when = dt.datetime.now(ZoneInfo("America/New_York")) + dt.timedelta(minutes=minutes_ahead)
    return {"date": when.date().isoformat(), "name": name,
            "time_et": when.strftime("%H:%M")}


def test_a_high_impact_release_inside_the_window_freezes_entries(
        pm, run_dir, quotes, rules):
    _edit(run_dir, lambda b: (_arm_session(b, _today(pm)), _quiet_ladder(b)))
    _scan_rows(run_dir, [SCHW], macro_events=[_macro_event(60, "CPI")])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    gated = [r for r in _skips(jrn, "*") if r.startswith("macro gate:")]
    assert gated, _skips(jrn)
    assert "CPI" in gated[0]
    assert _rule(rules, gated[0]) == "macro_gate"
    assert _placed(jrn) == []
    assert any("MACRO GATE" in w for w in jrn["warnings"])


def test_the_macro_gate_does_not_fire_on_adp_or_on_a_release_outside_the_window(
        pm, run_dir, quotes, rules):
    """PM.md §2, retuned 2026-09-01: a gate that fires on every calendar item does not
    protect the book, it silently stops it. ADP is explicitly not the payrolls report,
    and a release outside the 120-minute lookahead is reported, never gated."""
    _edit(run_dir, lambda b: (_arm_session(b, _today(pm)), _quiet_ladder(b)))
    _scan_rows(run_dir, [SCHW], macro_events=[
        _macro_event(30, "ADP National Employment Report"),
        _macro_event(240, "FOMC rate decision"),
    ])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert not [r for r in _skips(jrn, "*") if r.startswith("macro gate:")]
    assert _placed(jrn, "SCHW"), "a calendar the gate does not own must not stop the book"
    noted = [w for w in jrn["warnings"] if w.startswith("Macro calendar today")]
    assert noted and "ADP" in noted[0] and "outside the 120-minute gate window" in noted[0]


def test_a_stop_still_fires_through_the_macro_gate(pm, run_dir, quotes, rules):
    """PM.md §2, the macro gate: exits, trims and rebalancing always stay live
    through a gate."""
    def edit(b):
        _arm_session(b, _today(pm))
        _quiet_ladder(b)
        _break_stop(b)
    _edit(run_dir, edit)
    _scan_rows(run_dir, [SCHW], macro_events=[_macro_event(60, "CPI")])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert [r for r in _skips(jrn, "*") if r.startswith("macro gate:")]
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


# ================================================================== ladder
# K-02 has a fixture drill for the PURE module (tests/test_ladder.py walks a book down
# every rung). Nothing drives it THROUGH pm.py, so the halt, the working-buy cancel, the
# flatten and the UNPROTECTED path are untested. That is what follows.

def _drawdown_book(run_dir, pm, hwm, with_stop=False, working=False):
    def edit(b):
        _arm_session(b, _today(pm))
        b["equity_curve"] = []
        b["hwm"] = hwm
        b["cool_until"] = None
        b["ladder_halt"] = None
        if working:
            b["working_orders"] = [_resting_buy()]
        if with_stop:
            _break_stop(b)
    return _edit(run_dir, edit)


def test_rung_2_refuses_new_entries_and_leaves_the_exit_pass_running(
        pm, run_dir, quotes, rules):
    """PM.md §7 ladder table, rung 2: no new entries, with the reason journaled in
    `skipped` — while exits, trims and rebalancing all still run."""
    _drawdown_book(run_dir, pm, hwm=5300.0, with_stop=True)   # ≈ 6.5% under the HWM
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    gated = [r for r in _skips(jrn, "*") if r.startswith("ladder rung 2")]
    assert gated, _skips(jrn)
    assert _rule(rules, gated[0]) == "ladder"
    assert _placed(jrn) == []
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


def test_rung_3_halts_cancels_the_resting_buys_and_flattens_the_book(
        pm, run_dir, quotes, rules):
    """PM.md §7 ladder table, rung 3: halt through the kill switch's own path, then
    flatten every position with a fresh price, and set cool_until five business days out."""
    _drawdown_book(run_dir, pm, hwm=5600.0, working=True)     # ≈ 11.5% under the HWM
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert book["day"]["halted"] is True
    assert book["day"]["halt_reason"].startswith("Drawdown ladder halt")
    assert [d for d in jrn["decisions"]
            if d["action"] == "cancel" and d["reason"] == "drawdown ladder halt"]
    assert book["working_orders"] == []
    assert {d["symbol"] for d in _sells(jrn, reason="flatten")} == {"MU", "SNDK", "NVDA",
                                                                   "HOOD"}
    assert book["positions"] == []
    assert _placed(jrn) == []
    halted = [r for r in _skips(jrn, "*") if r.startswith("Drawdown ladder halt")]
    assert halted and _rule(rules, halted[0]) == "ladder"
    # the cool-off record: five BUSINESS days out, and the re-entry base is the post-
    # flatten equity, not the old high-water mark
    rec = book["ladder_halt"]
    assert rec["cool_until"] == book["cool_until"]
    d0, d1 = dt.date.fromisoformat(rec["date"]), dt.date.fromisoformat(rec["cool_until"])
    assert sum(1 for n in range(1, (d1 - d0).days + 1)
               if (d0 + dt.timedelta(days=n)).weekday() < 5) == 5
    assert rec["equity_after"] == pytest.approx(
        book["cash"] + sum(p["shares"] * p["last_price"] for p in book["positions"]), abs=0.5)


def test_the_rung_3_flatten_will_not_sell_a_position_it_cannot_price(
        pm, run_dir, quotes, rules):
    """PM.md §7: a position with no fresh price is reported UNPROTECTED, never sold on a
    stale mark. The flatten is the one exit that could plausibly get this wrong, because
    it is a book-wide sweep rather than a per-position test."""
    _drop_quote(run_dir, "MU")
    _drawdown_book(run_dir, pm, hwm=5600.0)
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert [p["symbol"] for p in book["positions"]] == ["MU"]
    assert not _sells(jrn, "MU")
    stale = [r for r in _skips(jrn, "MU") if r.startswith("ladder flatten wanted to fire")]
    assert stale and _rule(rules, stale[0]) == "ladder"
    assert any("UNPROTECTED: MU" in w for w in jrn["warnings"])


def test_a_half_sized_entry_under_the_broker_minimum_is_refused_by_name(
        pm, run_dir, quotes, rules, monkeypatch):
    """Rung 1 halves the entry; PM.md §7: a halved order that falls under the broker
    minimum is skipped with the reason. The reason reads like min_notional and is
    classified `ladder`, because the ladder is what made it too small."""
    monkeypatch.setitem(pm.RULES, "min_notional", 400.0)
    _drawdown_book(run_dir, pm, hwm=5200.0)                   # ≈ 4.7% under the HWM
    _scan_rows(run_dir, [SCHW])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    small = [r for r in _skips(jrn, "SCHW") if r.startswith("ladder ×")]
    assert small, _skips(jrn)
    assert "under the $400.00 broker minimum" in small[0]
    assert _rule(rules, small[0]) == "ladder"
    assert _placed(jrn) == []


# ================================================================== house caps
# PM.md §14. The house gate runs LAST, after portfolio.py has approved the trade for this
# desk in isolation: "It can only ever refuse; it never resizes and never allows something
# the per-desk rules blocked."

def _reshape_for_a_real_sector_breach(run_dir):
    """Put the combined book where the 40% sector cap actually binds, at its real default.

    Two edits, both of which the live books could reach on their own:
      * swing closes SNDK (proceeds to cash) — which frees the third Information
        Technology slot its own per-desk `max_per_sector` of 3 was using;
      * pullback deploys $900 of its cash into MRVL, a name it already holds.
    Combined equity is unchanged; Information Technology goes from 34.0% to 36.6%, which
    leaves less headroom under the 40% cap than one desk's maximum position. No cap is
    lowered anywhere in this test.
    """
    def swing(b):
        sndk = next(p for p in b["positions"] if p["symbol"] == "SNDK")
        b["cash"] = round(b["cash"] + sndk["shares"] * 1526.54, 2)
        b["positions"] = [p for p in b["positions"] if p["symbol"] != "SNDK"]
    _edit(run_dir, swing)

    def pullback(b):
        b["cash"] = round(b["cash"] - 900.0, 2)
        for p in b["positions"]:
            if p["symbol"] == "MRVL":
                p["shares"] = round(p["shares"] + 900.0 / 206.135, 6)
    _edit(run_dir, pullback, name="paper_book_pullback.json")


def test_the_house_sector_cap_refuses_at_its_real_forty_percent_default(
        pm, run_dir, quotes, rules):
    _reshape_for_a_real_sector_breach(run_dir)
    _scan_rows(run_dir, [AVGO])
    book, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert state["house"]["sector_pct"]["Information Technology"] < 40.0, \
        "the book must be UNDER the cap before the entry, or this is not a gate test"
    refused = [r for r in _skips(jrn, "AVGO") if r.startswith("House cap:")]
    assert refused, _skips(jrn)
    assert "sector house limit" in refused[0]
    assert _rule(rules, refused[0]) == "house_sector_cap"
    assert _placed(jrn) == []
    assert book["working_orders"] == [], "the house gate refuses; it never resizes"


def test_the_house_sector_cap_refuses_a_trade_portfolio_py_had_already_approved(
        pm, run_dir, quotes, rules):
    """The point of HOUSE-01: the per-desk rules see one book and say yes. Run the same
    slot with `house_caps_enabled` off — PM.md's `--no-house-caps`, which measures and
    reports without refusing — and the identical proposal is placed."""
    _reshape_for_a_real_sector_breach(run_dir)
    _scan_rows(run_dir, [AVGO])

    pm.PM_RULES["house_caps_enabled"] = False
    book_a, jrn_a, state_a = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert state_a["house"] is not None, "advisory mode still measures"
    approved = _placed(jrn_a, "AVGO")
    assert approved, "portfolio.py approves this trade for the swing desk in isolation"

    pm.PM_RULES["house_caps_enabled"] = True
    book_b, jrn_b, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    # the refusal is total: nothing is placed, and no smaller order appears in its place
    assert approved[0]["shares"] > 0
    assert _placed(jrn_b) == []
    assert book_b["working_orders"] == []


def test_the_house_single_name_cap_refuses_a_name_two_other_desks_already_hold(
        pm, run_dir, quotes, rules):
    """HOOD sits on swing and momentum at 7.96% of the combined book; pullback holds none
    and its own sector count is zero, so portfolio.py would take it."""
    hood = dict(SCHW, ticker="HOOD", name="Robinhood Markets", price=106.565,
                setup="Pullback in Uptrend", rsi_14=45.0, atr_14=3.2,
                ma_50=101.2, ma_200=91.6, analyst_target=119.3)
    _scan_rows(run_dir, [hood])
    pm.PM_RULES["house_max_symbol_pct"] = 8.0      # a risk-policy number, PM.md §14
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", desk="pullback",
                          with_scan=True)

    refused = [r for r in _skips(jrn, "HOOD") if r.startswith("House cap:")]
    assert refused, _skips(jrn)
    assert "single-name house limit" in refused[0] and "held elsewhere" in refused[0]
    assert _rule(rules, refused[0]) == "house_symbol_cap"
    assert _placed(jrn) == []
    assert book["working_orders"] == []


def test_a_stop_still_fires_while_a_house_cap_is_refusing_entries(
        pm, run_dir, quotes, rules):
    """PM.md §14: the house gate sits in `entry_pass`, after the exit pass has run."""
    _edit(run_dir, lambda b: (_arm_session(b, _today(pm)), _quiet_ladder(b),
                              _break_stop(b)))
    _scan_rows(run_dir, [SCHW])
    pm.PM_RULES["house_max_sector_pct"] = 8.0
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert [r for r in _skips(jrn, "SCHW") if r.startswith("House cap:")]
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


def test_the_house_single_name_cap_cannot_bind_while_the_desks_carry_equal_equity(pm):
    """Why `house_symbol_cap` has fired zero times in eleven days — arithmetic, not luck.

    The house cap is 15% of COMBINED equity. With three desks at the same equity E that is
    0.45E. Each desk's own `max_position_pct` already holds one name to 0.15E, and a name
    a desk already holds is never re-proposed — so three desks simultaneously at their
    per-desk maximum in one ticker total exactly 0.45E, and `house_block` refuses only
    STRICTLY above the cap. The house single-name cap therefore equals the sum of the three
    per-desk caps it sits above and, at equal desk equity, cannot refuse anything.

    It becomes reachable only when the desks' equities diverge, or if a fourth desk is
    added. The sector cap is different and IS reachable: 40% of 3E is 1.2E against three
    desks' 3 × (3 × 0.15E) = 1.35E of per-desk room — which the test above drives.
    """
    desk_equity = 5000.0
    house = {"equity": 3 * desk_equity, "desk_count": 3, "by_symbol": {}, "by_sector": {},
             "symbol_pct": {}, "sector_pct": {},
             "caps": {"symbol_pct": pm.PM_RULES["house_max_symbol_pct"],
                      "sector_pct": pm.PM_RULES["house_max_sector_pct"]}}
    per_desk_max = desk_equity * pm.RULES["max_position_pct"] / 100.0
    for _ in range(3):
        assert pm.house_block(house, "AAA", "Information Technology", per_desk_max) is None
        pm.house_apply(house, "AAA", "Information Technology", per_desk_max)
    assert house["symbol_pct"]["AAA"] == pm.PM_RULES["house_max_symbol_pct"]
    # one cent more is a breach — the cap works, there is simply no route to it
    assert pm.house_block(house, "AAA", "Information Technology", 0.01) is not None


# ================================================================== house_exposure
# PM.md §14b (K-03). Reported by default; with `enforce` on a flagged breach refuses new
# entries house-wide, "and exits are never touched by anything in house.py or by this
# gate, in either mode: a crowded house is a reason not to add, never a reason not to sell."

def test_an_enforced_exposure_flag_blocks_new_entries_house_wide(
        pm, run_dir, quotes, rules):
    pm.PM_RULES["house_exposure"]["enforce"] = True
    _scan_rows(run_dir, [SCHW])
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert state["house"]["exposure"]["flags"] == ["n_eff"]
    assert state["house"]["exposure"]["block_new_entries"] is True
    blocked = [r for r in _skips(jrn, "*") if r.startswith("house exposure (enforced):")]
    assert blocked, _skips(jrn)
    assert _rule(rules, blocked[0]) == "house_exposure"
    assert "exits unaffected" in blocked[0]
    assert _placed(jrn) == []
    assert any("HOUSE EXPOSURE" in w for w in jrn["warnings"])


def test_exits_run_through_an_enforced_house_exposure_block(pm, run_dir, quotes, rules):
    pm.PM_RULES["house_exposure"]["enforce"] = True
    _edit(run_dir, lambda b: (_arm_session(b, _today(pm)), _quiet_ladder(b),
                              _break_stop(b)))
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert [r for r in _skips(jrn, "*") if r.startswith("house exposure (enforced):")]
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


def test_the_default_reported_only_exposure_block_refuses_nothing_and_warns_nothing(
        pm, run_dir, quotes, rules):
    """PM.md §14b: with `enforce` off — the default — the block is reported and gates
    nothing, and a reported-only flag raises NO journal warning, on purpose — because
    `archive.should_publish` treats any warning as a reason to publish a board."""
    _scan_rows(run_dir, [SCHW])
    _, jrn, state = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert state["house"]["exposure"]["flags"] == ["n_eff"]
    assert state["house"]["exposure"]["block_new_entries"] is False
    assert "house_exposure" not in _rules_fired(rules, jrn)
    assert not [w for w in jrn["warnings"] if "HOUSE EXPOSURE" in w]
    assert _placed(jrn, "SCHW")


# ================================================================== min_notional
# portfolio.RULES["min_notional"] is $1.00 — Robinhood's fractional minimum. At $5,000 a
# desk it is load-bearing on the SELL side: the four rebalance shaves the live books have
# taken were $1.68, $8.46, $15.56 and $18.40, so the smallest was 68 cents clear of it.

def test_an_entry_under_the_broker_minimum_is_refused_and_named(
        pm, run_dir, quotes, rules, monkeypatch):
    monkeypatch.setitem(pm.RULES, "min_notional", 900.0)
    _scan_rows(run_dir, [SCHW])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    small = [r for r in _skips(jrn, "SCHW") if "below the broker's" in r]
    assert small, _skips(jrn)
    assert _rule(rules, small[0]) == "min_notional"
    assert _placed(jrn) == []


def test_a_trim_under_the_broker_minimum_is_refused_and_named(
        pm, run_dir, quotes, rules, monkeypatch):
    """A weakening score asks for a third of the position; when that third is under the
    minimum the exit pass refuses it and says so."""
    monkeypatch.setitem(pm.RULES, "min_notional", 500.0)
    _scan_rows(run_dir, [dict(SCHW, ticker="HOOD", name="Robinhood Markets",
                              price=106.565, score=47.0)])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    refused = [r for r in _skips(jrn, "HOOD") if "broker minimum" in r]
    assert refused, _skips(jrn)
    assert refused[0].startswith("trim sized to $")
    assert _rule(rules, refused[0]) == "min_notional"
    assert not _sells(jrn, "HOOD")
    assert [p for p in book["positions"] if p["symbol"] == "HOOD"]


def test_a_stop_under_the_broker_minimum_still_fires(
        pm, run_dir, quotes, rules, monkeypatch):
    """The exit-survival case for min_notional, and the engine gets it right: the
    sub-minimum guard in the exit pass only bites a PARTIAL sale (`sellable <
    pos["shares"]`), so a whole position worth less than the minimum is still sold when
    its stop breaks. Protection that can go unfilled is not protection (PM.md §3)."""
    monkeypatch.setitem(pm.RULES, "min_notional", 5000.0)     # bigger than the book
    _edit(run_dir, lambda b: _break_stop(b))
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")

    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


def _rebalance_shave_under_the_minimum(pm, run_dir, monkeypatch):
    """NVDA at 4.0 shares is ~17.5% of equity: over the 15% cap and clear of the 1.5pt
    deadband, so `rebalance_pass` wants to shave it back. The shave is ~$123, so a $200
    minimum puts it under the broker's floor. This is the live shape: the four real shaves
    were $1.68 to $18.40 against a $1.00 floor."""
    monkeypatch.setitem(pm.RULES, "min_notional", 200.0)
    _edit(run_dir, lambda b: [p.__setitem__("shares", 4.0)
                              for p in b["positions"] if p["symbol"] == "NVDA"])
    return run_pm(pm, run_dir, slot="opening-range")


def test_a_rebalance_shave_under_the_minimum_leaves_the_position_over_the_cap(
        pm, run_dir, quotes, monkeypatch):
    """What actually happens, on the record. The consequence is real: the position stays
    over the 15% cap, and the next slot will try and fail in exactly the same way."""
    book, jrn, _ = _rebalance_shave_under_the_minimum(pm, run_dir, monkeypatch)

    assert not _sells(jrn, "NVDA", "rebalance")
    nvda = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    equity = book["cash"] + sum(p["shares"] * p["last_price"] for p in book["positions"])
    assert nvda["shares"] * nvda["last_price"] / equity * 100 > pm.RULES["max_position_pct"]


@pytest.mark.xfail(strict=True, reason=(
    "DIVERGENCE. rebalance_pass drops a sub-minimum shave with a bare `continue` and "
    "writes nothing: no `skipped` row, no warning, no decision. Every other refusal in "
    "the engine is named in the journal — the spread gate, the drift block, the deadband, "
    "the once-per-session guard and the exit pass's own identical min_notional test all "
    "journal their reason — and PM.md §9 states the rule for the whole run ('Refused "
    "symbols are named in the journal'). Because there is no string, report.classify() "
    "never sees it: the refusal cannot land in min_notional, cannot land in `other`, and "
    "is absent from the weekly review's refusals table and from the E5 counterfactual. "
    "At $5,000 a desk this is not hypothetical — the live shaves run $1.68 to $18.40 "
    "against a $1.00 floor. engine/pm.py rebalance_pass, the `excess * px < "
    "min_notional` branch."))
def test_a_rebalance_shave_under_the_minimum_is_journaled(
        pm, run_dir, quotes, rules, monkeypatch):
    _, jrn, _ = _rebalance_shave_under_the_minimum(pm, run_dir, monkeypatch)

    refused = [r for r in _skips(jrn, "NVDA") if "minimum" in r]
    assert refused, ("a refusal with no reason is invisible to the taxonomy: "
                     + str(_skips(jrn)))
    assert _rule(rules, refused[0]) == "min_notional"


# ================================================================== broker_policy
# PM.md §4, legacy_pdt, "The state to fear": "If the budget is exhausted and a position
# opened today breaks its stop, the manager will not sell it. It emits `UNPROTECTED:
# <SYM> broke its stop and the legacy_pdt policy refused the sale`... it is the loudest
# thing the system can say."

def _pdt_exhausted(run_dir, pm):
    today = _today(pm)

    def edit(b):
        _arm_session(b, today)
        _quiet_ladder(b)
        b["broker_policy"] = "legacy_pdt"
        b["day_trades"] = [{"date": today.isoformat(), "symbol": s, "shares": 1.0}
                           for s in ("AAA", "BBB", "CCC")]
        for p in b["positions"]:
            if p["symbol"] == "NVDA":
                p["stop"] = 240.00
                p["opened"] = today.isoformat()
                p["intraday_shares"] = p["shares"]      # bought this session
    return _edit(run_dir, edit)


def test_an_exhausted_day_trade_budget_refuses_the_stop_and_shouts_unprotected(
        pm, run_dir, quotes, rules):
    _pdt_exhausted(run_dir, pm)
    book, jrn, state = run_pm(pm, run_dir, slot="sentinel")

    assert state["broker_policy"]["name"] == "legacy_pdt"
    assert not _sells(jrn, "NVDA"), "the sale must not go through"
    assert [p for p in book["positions"] if p["symbol"] == "NVDA"], \
        "and the position is carried, not quietly closed"
    banner = [w for w in jrn["warnings"] if w.startswith("UNPROTECTED: NVDA")]
    assert banner, jrn["warnings"]
    assert "broke its stop and the legacy_pdt policy refused the sale" in banner[0]
    refused = [r for r in _skips(jrn, "NVDA") if r.startswith("stop wanted to fire")]
    assert refused and _rule(rules, refused[0]) == "broker_policy"


def test_the_default_intraday_margin_policy_never_refuses_a_stop(
        pm, run_dir, quotes, rules):
    """PM.md §4: under `intraday_margin` sales are always allowed, and the UNPROTECTED
    'policy refused the sale' state of the old guard cannot arise under this policy. The
    same book that produces the banner above must produce a fill here."""
    _pdt_exhausted(run_dir, pm)
    _edit(run_dir, lambda b: b.pop("broker_policy", None))     # back to the default
    book, jrn, state = run_pm(pm, run_dir, slot="sentinel")

    assert state["broker_policy"]["name"] == "intraday_margin"
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]
    assert not [w for w in jrn["warnings"] if w.startswith("UNPROTECTED: NVDA")]


@pytest.mark.xfail(strict=True, reason=(
    "DIVERGENCE, wording. The UNPROTECTED banner is exactly what PM.md §4 promises, but "
    "the `skipped` reason beside it reads 'PDT guard: selling would be day trade 4/3 and "
    "this is not a stop — held' — and it IS a stop. LegacyPDT.sellable tests `used < max "
    "AND reason == 'stop'` in one branch and falls through to a single message written "
    "for the not-a-stop case, so an exhausted budget is reported as a wrong exit reason. "
    "The journal line contradicts the warning printed next to it, and the weekly review "
    "reads the journal line. PM.md is explicit that the refusal here is the exhausted "
    "budget: 'a ninety-day restriction is worse than one bad hold'. "
    "engine/broker_policy.py LegacyPDT.sellable, final return."))
def test_the_pdt_refusal_says_why_it_really_refused(pm, run_dir, quotes, rules):
    _pdt_exhausted(run_dir, pm)
    _, jrn, _ = run_pm(pm, run_dir, slot="sentinel")

    refused = [r for r in _skips(jrn, "NVDA") if r.startswith("stop wanted to fire")][0]
    assert "this is not a stop" not in refused, refused


# ================================================================== stop_policy
# PM.md §16 (K-07): under a policy other than fixed_atr the stop is re-derived and the
# position re-sized to the same dollar risk. A policy whose parameters strike no usable
# stop must refuse the entry rather than size one anyway.

def _chandelier_desk(pm, k_init):
    pm.DESK["rules"] = {"stop_policy": "chandelier", "stop_params": {"k_init": k_init}}


def test_a_stop_policy_that_strikes_no_usable_stop_refuses_the_entry(
        pm, run_dir, quotes, rules):
    """`k_init` is a desks.json number (the ORB template ships 0.1). At zero or below,
    `chandelier.initial()` returns a stop at or above the entry, there is no risk per
    share to size against, and the entry is refused by name."""
    _chandelier_desk(pm, -0.5)
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    refused = [r for r in _skips(jrn, "SCHW") if "cannot size a position" in r]
    assert refused, _skips(jrn)
    assert refused[0].startswith("chandelier: stop ")
    assert _rule(rules, refused[0]) == "stop_policy"
    assert _placed(jrn) == []
    assert book["working_orders"] == []


def test_exits_run_on_a_desk_whose_stop_policy_cannot_size_an_entry(
        pm, run_dir, quotes, rules):
    _chandelier_desk(pm, -0.5)
    _edit(run_dir, lambda b: (_arm_session(b, _today(pm)), _quiet_ladder(b),
                              _break_stop(b)))
    _scan_rows(run_dir, [SCHW])
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert [r for r in _skips(jrn, "SCHW") if "cannot size a position" in r]
    assert len(_sells(jrn, "NVDA", "stop")) == 1
    assert not [p for p in book["positions"] if p["symbol"] == "NVDA"]


# ================================================================== earnings_gate
@pytest.mark.xfail(strict=True, reason=(
    "NOT IMPLEMENTED, AND NOT SPECIFIED. report.py reserves the `earnings_gate` rule and "
    "says so in its own table ('reserved: any reason naming earnings; nothing emits one "
    "today'). No path in pm.py, portfolio.py, broker_policy.py or ladder.py writes a "
    "refusal naming earnings, and docs/PM.md never asks for one: earnings appear in §12 "
    "and §12c as an ALERT ('a held position carries earnings before the next open' pushes "
    "a notification) and in §2 only as the analogy the MACRO gate is built from ('binary "
    "events for every name at once, exactly as earnings are for one'). So the taxonomy "
    "carries a slot for a control that has no doctrine behind it. Either PM.md gains a "
    "single-name event gate — the §2 argument applies unchanged — or the rule should say "
    "it is a placeholder for the scanner's earnings scoring, which is not a refusal."))
def test_a_candidate_reporting_earnings_is_gated(pm, run_dir, quotes, rules):
    today = _today(pm)
    _scan_rows(run_dir, [dict(SCHW, next_earnings=today.isoformat(),
                              earnings_timing="after market close",
                              implied_move_pct=8.0)])
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)

    assert "earnings_gate" in _rules_fired(rules, jrn), _skips(jrn)


# ================================================================== veto
def test_veto_has_no_rule_on_this_branch_so_its_drill_is_deferred(rules):
    """DEFERRED, not skipped for convenience.

    `veto` is not in `report.RULES` on `main` and no module here emits a veto string: the
    rule, `engine/veto.py`, `veto.json` and its own tests arrive with PR #3
    (`feat/phase5-2026-09-10`). Inventing a string for it would pin a format that branch
    has not settled. When PR #3 lands this test skips with the instruction instead of
    passing silently, so the drill gets written rather than forgotten.
    """
    if "veto" in rules.RULES:
        pytest.skip("PR #3 has landed and report.RULES now carries `veto` — write the "
                    "drill against engine/veto.py's own refusal strings")
    assert "veto" not in rules.RULES
    assert not any("veto" in (ENGINE / name).read_text(encoding="utf-8").lower()
                   for name in ("pm.py", "portfolio.py", "broker_policy.py", "ladder.py"))


# ================================================================== other
def test_an_unrecognised_reason_lands_in_other_with_its_text_intact(rules):
    """`other` is the taxonomy's safety net: anything else, with the raw text kept in
    `detail`, so a new string is seen and never silently absorbed. It works."""
    unknown = "refused because the desk did not like the look of it"
    got = rules.classify(unknown)
    assert got["rule"] == "other"
    assert got["detail"] == unknown
    assert "other" in rules.RULES


def test_the_other_bucket_cannot_catch_a_refusal_that_was_never_written_down(
        pm, run_dir, quotes, rules, monkeypatch):
    """The limit of the safety net, and the reason `other` has never fired either.

    `test_report.py` harvests every refusal literal the engine can emit and proves none of
    them land in `other` — so on this branch `other` is unreachable from the engine by
    construction, which is the healthy reading. The unhealthy one is that `other` can only
    catch a refusal that produced a STRING. The sub-minimum rebalance shave above produces
    none, so it is invisible to every bucket, `other` included: the run refuses to trim a
    position that is over its cap and the journal's `skipped` list does not mention the
    symbol at all.
    """
    _, jrn, _ = _rebalance_shave_under_the_minimum(pm, run_dir, monkeypatch)

    assert "NVDA" not in [s["symbol"] for s in jrn["skipped"]]
    assert "other" not in _rules_fired(rules, jrn)
