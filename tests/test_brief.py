"""U-04 — the morning brief. Fixtures only: nothing here fetches, posts or writes state.

Every state directory a test builds is a tmp_path, every input is written by the test, and
the one network call brief.py can make (`post_ntfy`) is exercised only in its no-op form.
"""
import datetime as dt
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))

import brief                                            # noqa: E402
import pm as pm_mod                                     # noqa: E402

NOW = "2026-09-14T12:45:00Z"                            # Monday 08:45 ET
TODAY = "2026-09-14"


# ------------------------------------------------------------------ fixture builders
def position(symbol="AAA", price=100.0, stop=80.0, shares=1.0, **kw):
    p = {"symbol": symbol, "shares": shares, "avg_cost": price, "opened": "2026-09-01",
         "opened_slot": "midday", "stop": stop, "target": price * 1.5,
         "stop_basis_kind": "atr", "stop_basis_short": "1.5x ATR",
         "stop_policy": "fixed_atr", "last_price": price, "price_source": "pm-fetch",
         "sessions_held": 3, "gics": "Information Technology"}
    p.update(kw)
    return p


def order(symbol="BBB", limit=50.0, shares=2.0, **kw):
    o = {"id": f"{TODAY}-pre-market-{symbol}", "symbol": symbol, "side": "buy",
         "kind": "entry", "type": "limit", "limit_price": limit, "shares": shares,
         "notional": round(limit * shares, 2), "placed_slot": "pre-market",
         "expires": "day", "status": "working", "reason": "Score 70 Momentum",
         "meta": {"stop": limit * 0.9, "gics": "Information Technology"}}
    o.update(kw)
    return o


def book(desk="swing", positions=(), orders=(), cash=1000.0, hwm=None, closed=(), **kw):
    # starting_equity defaults to the book's own marked equity, so a fixture book is at
    # its high-water mark and sits at ladder rung 0 unless a test says otherwise.
    marked = round(cash + sum((p.get("last_price") or 0.0) * (p.get("shares") or 0.0)
                              for p in positions), 2)
    b = {"mode": "paper", "desk": desk, "revision": 1,
         "last_run": "2026-09-11T15:06:53Z", "starting_equity": marked,
         "cash": cash, "realized_pnl": 0.0,
         "positions": list(positions), "working_orders": list(orders),
         "closed_trades": list(closed), "day_trades": [],
         "day": {"date": "2026-09-11", "open_equity": 5000.0, "halted": False,
                 "halt_reason": None},
         "equity_curve": [], "hwm": hwm,
         "shadow": {"cum_gap_usd": 0.0, "n_fills": 0, "gap_share_of_realized_pct": 0.0,
                    "by_year": {}}}
    b.update(kw)
    return b


def journal(desk="swing", warnings=(), skipped=(), slot="opening-range"):
    return {"entries": [{
        "ts": "2026-09-11T15:06:53Z", "date": "2026-09-11", "slot": slot,
        "mode": "paper", "desk": desk, "decisions": [], "skipped": list(skipped),
        "warnings": list(warnings), "daily_pnl_pct": 0.0, "engine_sha": "89bf3f2",
    }], "updated": "2026-09-11T15:06:53Z"}


def coverage(day="2026-09-11", runs=None):
    rows = runs if runs is not None else [
        {"ts": f"{day}T13:07:33Z", "slot": "pre-market",
         "desks": {"swing": {"quiet": False}}},
        {"ts": f"{day}T15:06:53Z", "slot": "opening-range",
         "desks": {"swing": {"quiet": False}}},
    ]
    return {"days": {day: {"runs": rows}}, "keep_days": 10}


def scan(events=(), rows=None, date=TODAY, time="08:00"):
    return {
        "meta": {"scan_date": date, "time": time, "slot": "Pre-market",
                 "macro_events": list(events), "coverage_avg": 90.0,
                 "data_warnings": []},
        "regime": {"label": "Constructive", "vix": 16.0},
        "results": rows if rows is not None else [
            {"ticker": "AAA", "name": "Alpha Co", "score": 70.0, "setup": "Momentum",
             "verdict": "Buy", "coverage_pct": 100.0, "missing_pillars": [],
             "confidence": "single", "gics": "Information Technology"},
            {"ticker": "BBB", "name": "Beta Co", "score": 60.0, "setup": "Momentum",
             "verdict": "Watch", "coverage_pct": 80.0,
             "missing_pillars": ["intelligence"], "confidence": "single",
             "gics": "Information Technology"},
        ],
        "notable": [],
    }


def write_state(root, books=None, journals=None, cov=coverage(), scn=None, watch=None):
    root = pathlib.Path(root)
    (root / "books").mkdir(parents=True, exist_ok=True)
    (root / "journals").mkdir(parents=True, exist_ok=True)
    (root / "coverage").mkdir(parents=True, exist_ok=True)
    (root / "scans").mkdir(parents=True, exist_ok=True)
    for desk, b in (books or {}).items():
        (root / "books" / f"{desk}.json").write_text(json.dumps(b), encoding="utf-8")
    for desk, j in (journals or {}).items():
        (root / "journals" / f"{desk}.json").write_text(json.dumps(j), encoding="utf-8")
    if cov is not None:
        (root / "coverage" / "pm-coverage.json").write_text(json.dumps(cov),
                                                            encoding="utf-8")
    if scn is not None:
        (root / "scans" / "latest.json").write_text(json.dumps(scn), encoding="utf-8")
    if watch is not None:
        (root / "journals" / "watch.json").write_text(json.dumps(watch), encoding="utf-8")
    return str(root)


def quiet_state(tmp_path, **kw):
    """Two desks with nothing wrong at desk level: clean journals, coverage, scan, watch.

    Quiet, not clear. The house it adds up to is flagged — two holdings and a working buy
    put N_eff at 1.0, under the 3.0 floor — and that is not an artefact of the fixture:
    on the `proxy` basis a book cannot clear that floor at all. Sector ρ is 1.0 inside a
    GICS sector and `rho_default` 0.3 across, so eleven equally weighted names, one per
    sector, still measure 2.75. `flat_state` below is the only shape that comes out clear.
    """
    books = {"swing": book("swing", [position("AAA")], [order("BBB")]),
             "momentum": book("momentum", [position("CCC", price=200.0, stop=150.0)])}
    journals = {"swing": journal("swing"), "momentum": journal("momentum")}
    return write_state(tmp_path, books, journals, coverage(), scan(),
                       {"entries": [], "updated": "2026-09-13T21:00:00Z"}, **kw)


def flat_state(tmp_path):
    """Two desks holding nothing at all — the one house that carries no exposure flag.

    No holdings means no weights, so N_eff is None, and a metric that is None never flags
    (house.assess: an unmeasured number is not a low one). Every other check is quiet too,
    which is what makes this the fixture for the no-alert brief.
    """
    books = {"swing": book("swing"), "momentum": book("momentum")}
    return write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": [], "updated": "2026-09-13T21:00:00Z"})


def build(root, **kw):
    kw.setdefault("desks", ("swing", "momentum"))
    kw.setdefault("now", NOW)
    return brief.build(root, **kw)


# ------------------------------------------------------------------ the quiet brief
def test_a_brief_nobody_needs_is_short_and_names_its_checks(tmp_path):
    # flat_state, not quiet_state: a house holding anything at all trips the N_eff floor
    # on the proxy basis, and a brief with an alert on it is not the no-alert brief.
    b = build(flat_state(tmp_path))
    assert b["alerts"] == []
    body = brief.text_body(b)
    assert "nothing needs you" in body.splitlines()[0]
    # "short enough to prove it": the eight checks, on one line, well inside the cap.
    checks = [ln for ln in body.splitlines() if ln.startswith("Checked:")]
    assert len(checks) == 1
    for c in brief.QUIET_CHECKS:
        assert c in checks[0]
    assert len(body) < brief.MAX_CHARS


def test_the_page_renders_and_is_a_paper_page(tmp_path):
    b = build(quiet_state(tmp_path))
    page = brief.page_document(b)
    assert page.startswith("<!doctype html>")
    assert "</html>" in page
    assert brief.render_pm.CSS.split("\n", 2)[1] in page      # the Trade Desk palette
    assert "Paper &mdash; no orders sent" in page
    assert "Paper books. Not investment advice." in page
    assert "@@" not in page


# ------------------------------------------------------------------ alert-first ordering
def loud_state(tmp_path):
    """One desk carrying every alert class at once."""
    held = [
        position("UNPR", price=100.0, stop=80.0),          # UNPROTECTED, from the journal
        position("UNJU", price=100.0, stop=80.0),          # UNJUDGED, from the journal
        position("NEAR", price=100.0, stop=99.0),          # inside 1.02x its stop
        position("EARN", price=100.0, stop=80.0,
                 next_earnings=TODAY, earnings_timing="am"),
    ]
    b = book("swing", held, hwm=100000.0)                  # a huge HWM -> ladder rung 3
    b["day"] = {"date": "2026-09-11", "open_equity": 5000.0, "halted": True,
                "halt_reason": "daily loss -3.1%"}
    j = journal("swing",
                warnings=["UNPROTECTED: UNPR has no fresh price (none) and cannot be sold",
                          "UNJU: not in this scan's universe — priced but unjudged"],
                skipped=[{"symbol": "ZZZ",
                          "reason": "house cap: ZZZ would be 16.0% of combined equity, "
                                    "over the 15% single-name house limit"}])
    books = {"swing": b, "momentum": book("momentum", [position("CCC")])}
    return write_state(tmp_path, books,
                       {"swing": j, "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})


def test_alerts_are_ordered_most_urgent_first(tmp_path):
    b = build(loud_state(tmp_path))
    kinds = [a["kind"] for a in b["alerts"]]
    for want in ("UNPROTECTED", "KILL SWITCH", "LADDER", "HOUSE CAP", "UNJUDGED",
                 "NEAR STOP", "EARNINGS"):
        assert want in kinds, f"{want} did not fire"
    ranks = [a["rank"] for a in b["alerts"]]
    assert ranks == sorted(ranks)
    assert kinds[0] == "UNPROTECTED"


def test_the_body_leads_with_the_alert_not_with_the_book(tmp_path):
    b = build(loud_state(tmp_path))
    lines = brief.text_body(b).splitlines()
    assert "need you" in lines[0]
    assert lines[1].startswith("UNPROTECTED:")
    # The per-desk digest is the tail, never ahead of an alert.
    first_digest = next(i for i, ln in enumerate(lines) if ln.startswith("swing paper"))
    last_alert = max(i for i, ln in enumerate(lines)
                     if any(ln.startswith(k + ":") for k in brief.ALERT_ORDER))
    assert last_alert < first_digest


def test_near_stop_uses_the_render_pm_threshold(tmp_path):
    b = build(loud_state(tmp_path))
    near = [a for a in b["alerts"] if a["kind"] == "NEAR STOP"]
    assert [a["symbol"] for a in near] == ["NEAR"]
    assert brief.NEAR_STOP_MULT == 1.02
    row = next(p for dv in b["desks"] if dv["present"]
               for p in dv["positions"] if p["symbol"] == "NEAR")
    assert row["stop_distance_pct"] == pytest.approx(1.01, abs=0.01)
    assert row["stop_basis_kind"] == "atr"


def test_earnings_before_the_next_open_fires_and_after_it_does_not(tmp_path):
    tomorrow = (dt.date.fromisoformat(TODAY) + dt.timedelta(days=1)).isoformat()
    held = [position("AM", next_earnings=TODAY, earnings_timing="am"),
            position("PM", next_earnings=TODAY, earnings_timing="pm"),
            position("TOM", next_earnings=tomorrow, earnings_timing="am"),
            position("UNK", next_earnings=TODAY, earnings_timing=None)]
    books = {"swing": book("swing", held), "momentum": book("momentum", [position("C")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})
    b = build(root)
    fired = {e["symbol"] for e in b["earnings"]["before_next_open"]}
    assert fired == {"AM", "UNK"}            # unknown time cannot be cleared
    noted = {e["symbol"] for e in b["earnings"]["noted"]}
    assert {"PM", "TOM"} <= noted


def test_ladder_rung_comes_from_ladder_state_for(tmp_path):
    b = build(loud_state(tmp_path))
    swing = next(dv for dv in b["desks"] if dv["desk"] == "swing")
    assert swing["ladder"]["rung"] == 3 and swing["ladder"]["halt"] is True
    assert swing["ladder"]["rules"] == dict(pm_mod.PM_RULES["ladder"])
    assert any(a["kind"] == "LADDER" for a in b["alerts"])


# ------------------------------------------------------------------ the length cap
def test_the_body_is_hard_capped_and_says_what_it_dropped(tmp_path):
    held = [position(f"N{i:02d}", price=100.0, stop=99.0) for i in range(40)]
    books = {"swing": book("swing", held),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})
    b = build(root)
    assert len(b["alerts"]) >= 40
    for cap in (200, 400, 1200):
        body = brief.text_body(b, cap)
        assert len(body) <= cap, f"cap {cap} breached: {len(body)}"
        if len(b["alerts"]) * 20 > cap:
            assert "more on the page" in body, "alerts were dropped without saying so"


def test_the_cap_never_drops_an_alert_in_favour_of_the_digest(tmp_path):
    held = [position(f"N{i:02d}", price=100.0, stop=99.0) for i in range(40)]
    books = {"swing": book("swing", held),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})
    b = build(root)
    body = brief.text_body(b, 400)
    assert len(body) <= 400
    assert body.splitlines()[1].startswith("NEAR STOP:")
    assert "more on the page" in body         # the cap bound, and the body says so
    assert "swing paper" not in body          # the digest is the first thing to go


# ------------------------------------------------------------------ absent inputs
def test_a_missing_book_is_named_not_zeroed(tmp_path):
    books = {"momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books, {"momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})
    b = build(root)
    swing = next(dv for dv in b["desks"] if dv["desk"] == "swing")
    assert swing["present"] is False
    assert swing["equity"] is None and swing["n_positions"] is None
    assert "swing" in swing["absent_reason"] or "swing.json" in swing["absent_reason"]
    body = brief.text_body(b)
    assert "swing: NO BOOK" in body
    assert "swing paper $0" not in body
    page = brief.page_document(b)
    assert "No book for the swing desk" in page
    # One book left, so the house cannot be measured — and says so rather than reporting
    # the measurable part as the whole house.
    assert b["house"]["measured"] is False
    assert "unmeasured house is not a safe one" in b["house"]["reason"]


def test_a_missing_scan_leaves_no_watchlist_and_no_empty_calendar(tmp_path):
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scn=None, watch={"entries": []})
    b = build(root)
    assert b["watchlist"]["measured"] is False
    assert b["macro"]["measured"] is False
    assert "unknown, not empty" in b["macro"]["reason"]
    page = brief.page_document(b)
    assert "no watchlist" in page
    assert any(a["kind"] == "INPUT ABSENT" and "scan" in a["text"] for a in b["alerts"])


def test_a_missing_coverage_file_reports_unknown_gaps_not_zero_gaps(tmp_path):
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       cov=None, scn=scan(), watch={"entries": []})
    b = build(root)
    assert b["coverage"]["measured"] is False
    assert "gaps unknown" in b["coverage"]["reason"]
    body = brief.text_body(b)
    assert "0 gap" not in body
    assert "coverage:" in body


def test_a_missing_watch_journal_is_unverified_not_quiet(tmp_path):
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), watch=None)
    b = build(root)
    assert b["watch"]["measured"] is False
    assert "unverified, not quiet" in b["watch"]["reason"]


def test_coverage_gaps_are_drawn_as_gaps(tmp_path):
    b = build(quiet_state(tmp_path))
    c = b["coverage"]
    assert c["measured"] is True and c["day"] == "2026-09-11"
    assert c["expected"] > c["present"] and c["missing"]
    page = brief.page_document(b)
    assert "no coverage row" in page
    assert "drawn as gaps, not skipped" in page
    # A step no run has ever recorded is separated from a run that actually failed.
    assert c["steps_recorded"] == ["pm"]
    assert any(m["never_recorded"] for m in c["missing"])


# ------------------------------------------------------------------ the macro gate
MACRO_NAMES = [
    "CPI / Core CPI (Inflation Rate MoM & YoY)", "FOMC Rate Decision",
    "Core PCE Price Index", "Non Farm Payrolls", "Unemployment Rate", "GDP Growth Rate",
    "Powell Speech", "ADP National Employment", "ADP Employment Change",
    "Michigan Consumer Sentiment Prel", "Baker Hughes Oil Rig Count",
    "Initial Jobless Claims", "ISM Services PMI", "Fed Beige Book",
]


def test_high_impact_classification_is_pm_s_own(tmp_path):
    evs = [{"date": TODAY, "name": n, "time_et": "10:00"} for n in MACRO_NAMES]
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(events=evs), {"entries": []})
    b = build(root)
    got = {e["name"]: e["high_impact"] for e in b["macro"]["events"]}
    assert got == {n: pm_mod._is_high_impact(n) for n in MACRO_NAMES}
    # The ADP carve-out pm.py makes is inherited, not re-implemented.
    assert got["ADP National Employment"] is False
    assert got["Non Farm Payrolls"] is True


def test_gating_now_agrees_with_pm_macro_events_pending(tmp_path):
    evs = [{"date": TODAY, "name": "CPI / Core CPI", "time_et": "10:00"},      # ahead
           {"date": TODAY, "name": "FOMC Rate Decision", "time_et": "08:00"},  # printed
           {"date": TODAY, "name": "ISM Services PMI", "time_et": "10:00"}]    # not on list
    s = scan(events=evs)
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), s, {"entries": []})
    b = build(root)
    jrn = {"ts": NOW, "warnings": []}
    pending = pm_mod.macro_events_pending(s, dt.date.fromisoformat(TODAY), jrn)
    gating = [e["name"] for e in b["macro"]["events"] if e["gating_now"]]
    assert gating == [p.split(" at ")[0] for p in pending]
    assert gating == ["CPI / Core CPI"]       # 08:00 already printed, ISM never gates
    page = brief.page_document(b)
    assert "GATES NOW" in page
    assert "pm._is_high_impact" in page


def test_a_scan_from_another_day_reports_an_uncollected_calendar(tmp_path):
    evs = [{"date": "2026-09-11", "name": "CPI / Core CPI", "time_et": "08:30"}]
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(events=evs, date="2026-09-11", time="12:30"),
                       {"entries": []})
    b = build(root)
    assert b["macro"]["n_today"] == 0
    assert "not been collected yet" in b["macro"]["stale_note"]
    assert "uncollected, not empty" in brief.text_body(b)


# ------------------------------------------------------------------ the honesty budget
FORBIDDEN_SAMPLES = ("Sharpe 2.1", "win rate 62%", "win_rate=0.62", "Sortino 1.4",
                     "annualised return of 34%", "annualized 34%", "CAGR 22%",
                     "information ratio 0.9", "Calmar 3.0")


@pytest.mark.parametrize("bad", FORBIDDEN_SAMPLES)
def test_assert_clean_refuses_every_forbidden_name(bad):
    with pytest.raises(brief.BriefHonestyError):
        brief.assert_clean(f"the desk posted a {bad} over the window", "a test string")


def test_forbidden_metrics_cannot_reach_either_output(tmp_path):
    """The guard: state full of forbidden metric names, and neither output carries one."""
    poisoned_rows = [
        {"ticker": "SHRP", "name": "Sharpe Industries", "score": 70.0,
         "setup": "Momentum with a win rate of 62%", "verdict": "Buy",
         "coverage_pct": 100.0, "missing_pillars": [], "confidence": "single"},
    ]
    s = scan(events=[{"date": TODAY, "name": "CPI, annualised basis", "time_et": "10:00"}],
             rows=poisoned_rows)
    s["meta"]["data_warnings"] = ["Sharpe was 2.1 on the window and the win rate 62%"]
    j = journal("swing", warnings=["Desk Sharpe 2.1 this week, annualised 34%"],
                skipped=[{"symbol": "ZZZ",
                          "reason": "house cap: ZZZ over the 15% single-name house limit; "
                                    "CAGR 22%"}])
    books = {"swing": book("swing", [position("AAA")]),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books, {"swing": j, "momentum": journal("momentum")},
                       coverage(), s,
                       {"entries": [{"ts": "2026-09-14T11:00:00Z", "date": TODAY,
                                     "session": "pre-open", "desk": "swing",
                                     "alerts": [{"severity": "warn", "symbol": "AAA",
                                                 "message": "AAA Sortino 1.4 overnight"}]}]})
    b = build(root)

    body = brief.text_body(b)
    page = brief.page_document(b)
    for out, what in ((body, "the text body"), (page, "the HTML page")):
        # The one sanctioned sentence names them only to refuse them; take it out and
        # nothing in either output may carry a forbidden metric name at all.
        rest = out.replace(brief.DENIAL, "")
        assert brief.forbidden_hits(rest) == [], (
            f"forbidden metric name(s) reached {what}: {brief.forbidden_hits(rest)}")
    assert brief.REDACTED in page                 # the state's own prose was redacted
    assert "Sortino" not in page and "CAGR" not in page


def test_the_denial_sentence_is_the_only_route_and_it_is_a_denial(tmp_path):
    b = build(quiet_state(tmp_path))
    page = brief.page_document(b)
    assert brief.DENIAL in page
    assert page.count(brief.DENIAL) == 1
    # Remove the one sanctioned sentence and the page must be clean of every term.
    rest = page.replace(brief.DENIAL, "")
    assert brief.forbidden_hits(rest) == []


def test_every_aggregate_carries_its_n_and_under_thirty_says_not_a_sample(tmp_path):
    closed = [{"symbol": "OLD", "opened": "2026-09-01", "closed": "2026-09-02",
               "shares": 1.0, "entry": 10.0, "exit": 11.0, "pnl": 1.0, "reason": "trim"}]
    books = {"swing": book("swing", [position("AAA")], closed=closed),
             "momentum": book("momentum", [position("CCC")])}
    root = write_state(tmp_path, books,
                       {"swing": journal("swing"), "momentum": journal("momentum")},
                       coverage(), scan(), {"entries": []})
    b = build(root)
    assert b["combined"]["n_closed"] == 1
    assert b["combined"]["enough_closed"] is False
    page = brief.page_document(b)
    assert brief.NOT_A_SAMPLE in page
    assert f"under {brief.MIN_N}" in page
    assert "n = 1" in page


def test_a_trimmed_but_still_open_lot_is_not_a_round_trip():
    b = book("swing",
             [position("HELD", price=100.0)],
             closed=[{"symbol": "HELD", "opened": "2026-09-01", "reason": "trim"},
                     {"symbol": "HELD", "opened": "2026-09-01", "reason": "trim"},
                     {"symbol": "GONE", "opened": "2026-09-01", "reason": "stop"}])
    # Three sale legs, one completed round trip: the still-held lot has not round-tripped.
    assert brief.round_trips(b) == 1
    assert len(b["closed_trades"]) == 3


def test_paper_is_said_wherever_a_number_could_be_mistaken_for_a_real_trade(tmp_path):
    b = build(quiet_state(tmp_path))
    body, page = brief.text_body(b), brief.page_document(b)
    assert body.startswith("PAPER brief")
    assert "paper" in body
    assert "Paper books." in body
    assert "is not a live return" in page
    assert "Combined paper equity" in page
    assert "no real money moved" in page


def test_the_shadow_gap_is_an_em_dash_when_nothing_has_been_priced(tmp_path):
    b = build(quiet_state(tmp_path))
    page = brief.page_document(b)
    assert "no shadow-priced fill yet" in page
    assert "not because execution was free" in page


# ------------------------------------------------------------------ house + ntfy
def test_the_house_block_comes_from_pm_and_carries_its_caps(tmp_path):
    b = build(quiet_state(tmp_path))
    h = b["house"]
    assert h["measured"] is True
    assert h["caps"]["symbol_pct"] == pm_mod.PM_RULES["house_max_symbol_pct"]
    assert h["caps"]["sector_pct"] == pm_mod.PM_RULES["house_max_sector_pct"]
    ex = h["exposure"]
    assert ex["n_eff_basis"] == "proxy"           # no bars staged, and it says so
    assert ex["beta_w"] is None
    page = brief.page_document(b)
    assert "no bars staged, so beta is unmeasured" in page
    assert "N_eff" in page and "Desk overlap" in page


# ------------------------------------------------------------------ the house-exposure alert
# 2026-09-11 midday, copied out of the swing journal's `house.exposure` block. This is the
# state the alert was written for: flagged on N_eff, on the proxy basis, enforce off.
REAL_EXPOSURE = {
    "n_eff": 1.78, "n_eff_basis": "proxy", "beta_w": None, "momentum_crowd_pct": None,
    "largest_sector": "Information Technology", "largest_sector_pct": 23.28,
    "top_symbol": "ANET", "top_symbol_pct": 10.25,
    "overlap_pct": 75.0, "overlap_equity_pct": 38.36,
    "flags": ["n_eff"], "reasons": ["house N_eff 1.78 (proxy) under the 3.0 floor"],
    "enforce": False, "block_new_entries": False,
    "rules": {"min_n_eff": 3.0, "max_beta_w": 0.8, "max_momentum_crowd_pct": 60.0,
              "rho_default": 0.3, "enforce": False},
}


def with_exposure(monkeypatch, block):
    """Serve `block` as the house exposure, leaving every other computation alone."""
    monkeypatch.setattr(brief.pm_mod, "house_metrics", lambda house, bars=None: block)


def test_a_flagged_house_exposure_joins_the_alert_list_and_the_text_body(tmp_path):
    b = build(quiet_state(tmp_path))
    ex = b["house"]["exposure"]
    assert ex["flags"] == ["n_eff"] and ex["enforce"] is False
    fired = [a for a in b["alerts"] if a["kind"] == "HOUSE EXPOSURE"]
    assert len(fired) == 1, "a flagged house did not reach the alert list"
    assert fired[0]["desk"] is None            # it is the house's, not any one desk's
    body = brief.text_body(b)
    assert f"HOUSE EXPOSURE: {fired[0]['text']}" in body
    assert fired[0]["text"] in brief.page_document(b)


REFUSAL_WORDS = ("refus", "block", "reject", "turned away", "held back", "cap", "limit",
                 "gated", "denied", "cut")


@pytest.mark.parametrize("flags", (["n_eff"], ["beta_w"], ["momentum_crowd"],
                                   ["n_eff", "beta_w", "momentum_crowd"]))
def test_the_unenforced_line_makes_no_claim_that_anything_was_refused(
        tmp_path, monkeypatch, flags):
    """The DENIAL device, applied to refusal language: strip the one negating clause and
    nothing left in the line may suggest a cap bit — whichever rule raised the flag."""
    with_exposure(monkeypatch, dict(REAL_EXPOSURE, beta_w=1.05, momentum_crowd_pct=71.0,
                                    flags=list(flags)))
    b = build(quiet_state(tmp_path))
    text = next(a["text"] for a in b["alerts"] if a["kind"] == "HOUSE EXPOSURE")
    assert "not enforced" in text
    assert brief.NOT_ENFORCED_NOTE in text
    rest = text.replace(brief.NOT_ENFORCED_NOTE, "").lower()
    for claim in REFUSAL_WORDS:
        assert claim not in rest, f"the unenforced line implies a refusal: {claim!r}"


def test_enforcing_is_a_different_class_and_a_different_sentence(tmp_path, monkeypatch):
    lax = build(quiet_state(tmp_path))
    soft = next(a for a in lax["alerts"] if a["kind"] == "HOUSE EXPOSURE")

    monkeypatch.setitem(pm_mod.PM_RULES["house_exposure"], "enforce", True)
    hard_b = build(quiet_state(tmp_path / "enforced"))
    hard = [a for a in hard_b["alerts"] if "house exposure is" in a["text"]]
    assert len(hard) == 1
    hard = hard[0]

    # Different class, different rank, different words — and the serious one is serious.
    assert hard["kind"] == "HOUSE CAP" and soft["kind"] == "HOUSE EXPOSURE"
    assert hard["rank"] < soft["rank"]
    assert "ENFORCING" in hard["text"] and "refused house-wide" in hard["text"]
    assert "ENFORCING" not in soft["text"]
    assert brief.NOT_ENFORCED_NOTE not in hard["text"]
    assert not any(a["kind"] == "HOUSE EXPOSURE" for a in hard_b["alerts"]), \
        "an enforcing house must not also raise the awareness class"
    assert hard["text"] != soft["text"]


def test_n_eff_never_travels_without_the_basis_that_qualifies_it(tmp_path, monkeypatch):
    with_exposure(monkeypatch, REAL_EXPOSURE)
    b = build(quiet_state(tmp_path))
    text = next(a["text"] for a in b["alerts"] if a["kind"] == "HOUSE EXPOSURE")
    assert "proxy basis" in text
    assert "correlation assumed from sector, not measured" in text
    # One decimal. On an assumed correlation the second one is precision the number has
    # no claim to, and 1.78 read aloud sounds measured.
    assert "1.8 vs the 3.0 floor" in text and "1.78" not in text
    for line in brief.text_body(b).splitlines():
        if "n_eff" in line.lower():
            assert "proxy" in line, f"n_eff without its basis: {line}"
    page = brief.page_document(b)
    assert "proxy basis" in page            # the house-card row
    assert "floor 3.0" in page and "proxy" in page       # the rail tile


def test_n_eff_is_shown_to_one_decimal_in_every_output(tmp_path, monkeypatch):
    """1.78 and 1.8 are the same claim on an assumed correlation; only one of them is
    honest about how much the house knows."""
    with_exposure(monkeypatch, REAL_EXPOSURE)
    b = build(quiet_state(tmp_path))
    body, page = brief.text_body(b), brief.page_document(b)
    assert b["house"]["exposure"]["n_eff"] == 1.78      # the stored value is untouched
    for out, what in ((body, "the text body"), (page, "the HTML page")):
        assert "1.78" not in out, f"two-decimal N_eff reached {what}"
        assert "1.8" in out
    assert brief.n_eff_str(None) == brief.DASH          # absent is an em-dash, not 0.0


def test_the_line_as_it_reads_against_the_real_house(tmp_path, monkeypatch):
    with_exposure(monkeypatch, REAL_EXPOSURE)
    b = build(quiet_state(tmp_path))
    text = next(a["text"] for a in b["alerts"] if a["kind"] == "HOUSE EXPOSURE")
    assert text == (
        "house exposure flagged, not enforced: n_eff 1.8 vs the 3.0 floor (proxy basis "
        "— correlation assumed from sector, not measured) — no entry was refused and no "
        "size was cut; this is a reading of how crowded the three paper books already are")
    assert len(brief.text_body(b)) <= brief.MAX_CHARS


def test_any_flag_joins_the_list_and_an_unknown_one_is_named_not_swallowed(
        tmp_path, monkeypatch):
    """Any non-empty flags, not just n_eff — including a rule house.py has not grown yet."""
    with_exposure(monkeypatch, dict(REAL_EXPOSURE, n_eff=4.0, n_eff_basis="measured",
                                    beta_w=1.05, flags=["beta_w"]))
    b = build(quiet_state(tmp_path))
    text = next(a["text"] for a in b["alerts"] if a["kind"] == "HOUSE EXPOSURE")
    assert "beta-weighted 1.05 vs the 0.80 ceiling" in text
    assert "n_eff" not in text               # an unflagged metric is not narrated

    with_exposure(monkeypatch, dict(REAL_EXPOSURE, flags=["some_rule_added_later"]))
    b2 = build(quiet_state(tmp_path / "later"))
    text2 = next(a["text"] for a in b2["alerts"] if a["kind"] == "HOUSE EXPOSURE")
    assert "some_rule_added_later" in text2


def test_an_absent_house_exposure_block_is_absent_not_clear(tmp_path, monkeypatch):
    """A missing block must never render as "no flags": unread is its own answer."""
    with_exposure(monkeypatch, {})
    b = build(flat_state(tmp_path))          # everything else is quiet, so only this speaks
    assert [a["kind"] for a in b["alerts"]] == ["HOUSE EXPOSURE"]
    text = b["alerts"][0]["text"]
    assert "unread" in text and "not unflagged" in text
    body = brief.text_body(b)
    assert "nothing needs you" not in body
    assert "house exposure measured and carrying no flag" not in body

    # The same answer when the house itself could not be tallied at all.
    assert brief.exposure_verdict({"measured": False, "reason": "one book"})[0] == "unread"
    # ...and a flag list that is present and empty is the one thing that reads as clear.
    assert brief.exposure_verdict(
        {"measured": True, "exposure": {"flags": []}}) == ("clear", "")


def test_a_house_flag_never_outranks_a_holding_that_can_lose_money_now(tmp_path):
    b = build(loud_state(tmp_path))
    kinds = [a["kind"] for a in b["alerts"]]
    assert "HOUSE EXPOSURE" in kinds
    house_at = kinds.index("HOUSE EXPOSURE")
    for urgent in ("UNPROTECTED", "KILL SWITCH", "LADDER", "HOUSE CAP", "UNJUDGED",
                   "NEAR STOP", "EARNINGS"):
        assert kinds.index(urgent) < house_at, f"{urgent} ranked below the house flag"
    assert brief.ALERT_ORDER.index("HOUSE EXPOSURE") > brief.ALERT_ORDER.index("NEAR STOP")
    lines = brief.text_body(b).splitlines()
    assert lines[1].startswith("UNPROTECTED:")
    assert (next(i for i, ln in enumerate(lines) if ln.startswith("HOUSE EXPOSURE:"))
            > next(i for i, ln in enumerate(lines) if ln.startswith("NEAR STOP:")))


def test_the_quiet_line_names_the_house_check_it_made(tmp_path):
    b = build(flat_state(tmp_path))
    assert b["alerts"] == []
    checked = next(ln for ln in brief.text_body(b).splitlines()
                   if ln.startswith("Checked:"))
    assert "house exposure measured and carrying no flag" in checked
    assert len(brief.QUIET_CHECKS) == 8
    assert "eight checks" in brief.page_document(b)


def test_building_the_house_does_not_leave_pm_s_active_desk_changed(tmp_path):
    before = dict(pm_mod.DESK)
    build(quiet_state(tmp_path))
    assert dict(pm_mod.DESK) == before


def test_the_watchlist_says_it_is_not_a_buy_list(tmp_path):
    b = build(quiet_state(tmp_path))
    page = brief.page_document(b)
    assert "Watchlist, not a buy list" in page
    assert "a high score is a ranking, not a recommendation" in page
    assert b["watchlist"]["rows"][0]["ticker"] == "AAA"


def test_ntfy_without_a_topic_is_a_no_op(tmp_path):
    res = brief.post_ntfy(None, "body")
    assert res == {"sent": False, "reason": "no --ntfy topic given"}
    res = brief.post_ntfy("", "body")
    assert res["sent"] is False


def test_no_ntfy_topic_or_url_is_committed_to_the_repo():
    """The topic name is the password for a public topic, so it may not be in the tree."""
    src = (ROOT / "engine" / "brief.py").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "runner" / "prompts" / "morning-brief.md").read_text(
        encoding="utf-8")
    for text in (src, doc):
        assert "ntfy.sh/" not in text, "an ntfy topic URL is committed"
    assert "alerts.ntfy_topic" in src and "P-08" in src


def test_cli_writes_both_outputs_and_exits_zero(tmp_path):
    root = quiet_state(tmp_path / "state")
    txt, page, js = (tmp_path / "b.txt"), (tmp_path / "b.html"), (tmp_path / "b.json")
    rc = brief.main(["--state", root, "--desks", "swing,momentum", "--now", NOW,
                     "--text", str(txt), "--html", str(page), "--json", str(js)])
    assert rc == 0
    assert txt.read_text(encoding="utf-8").startswith("PAPER brief")
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")
    assert json.loads(js.read_text(encoding="utf-8"))["mode"] == "paper"


def test_brief_is_in_the_engine_manifest():
    manifest = (ROOT / "engine" / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert "brief.py" in manifest
    assert manifest == sorted(manifest)
