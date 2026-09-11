"""P-06 — the earnings feed and the single-name event gate.

Fixtures only. Nothing here fetches: every payload is a hand-built copy of shapes actually
observed in `get_earnings_calendar` and `get_earnings_results` on 2026-09-11, including the
awkward ones (a null `report`, a null `timing`, duplicate rows, stale back-quarters parked
on a future placeholder date, and a row whose quarter has already been reported).

The engine tests replay the same frozen two-run sequence the K-06 identity tests use
(tests/shadow_sequence.py, pinned clock at 2026-09-10 10:45 and 13:15 ET) and compare
against the same byte-pinned golden, so "the gate changes nothing when no file is staged"
is proved against the pre-change engine's own bytes rather than against itself.
"""
import datetime as dt
import json

import pytest

import shadow_sequence as ss
from conftest import ENGINE  # noqa: F401  (path side effect)

# 2026-09-11 is a Friday. 09-12/09-13 are the weekend; 09-14 is the Monday.
FRI_1545 = "2026-09-11T19:45:00Z"      # 15:45 ET Friday — the power-hour slot
FRI_0700 = "2026-09-11T11:00:00Z"      # 07:00 ET Friday — ahead of an 08:00 am print


@pytest.fixture
def ea(run_dir):
    import importlib
    import earnings as m
    importlib.reload(m)
    return m


def _row(symbol, date, timing="pm", verified=True, actual=None, year=2026, quarter=3):
    return {"symbol": symbol, "year": year, "quarter": quarter,
            "eps": {"estimate": "1.000000", "actual": actual},
            "report": {"date": date, "timing": timing, "verified": verified}}


def _payload(rows, not_found=None):
    """The raw connector envelope, exactly as the session stages it."""
    return {"data": {"results": list(rows), "not_found": list(not_found or [])}}


def _stage(run_dir, rows, not_found=None, name="earnings.json"):
    (run_dir / name).write_text(json.dumps(_payload(rows, not_found)), encoding="utf-8")


# ------------------------------------------------------------------ the payload shapes
def test_an_unstaged_feed_is_unavailable_and_says_so(ea, run_dir):
    feed = ea.load()
    assert not feed.available and not feed
    assert "dormant" in feed.reason
    info = feed.lookup("MU", FRI_1545)
    assert info["status"] == "unavailable"
    # "unavailable" is a statement about the run, never about the symbol.
    assert "no earnings feed staged" in ea.describe(info)
    assert ea.within_gate(feed, "MU", FRI_1545) == (False, info)


def test_rows_without_a_usable_date_are_dropped_never_defaulted(ea, run_dir):
    """`report: null` is a real row (SNDK 2025 Q2), and so is a date that will not parse."""
    _stage(run_dir, [
        {"symbol": "SNDK", "year": 2025, "quarter": 2,
         "eps": {"estimate": None, "actual": None}, "report": None},
        {"symbol": "BAD", "year": 2026, "quarter": 1, "eps": {},
         "report": {"date": "not-a-date", "timing": "am", "verified": True}},
        "a string where a row should be",
        _row("MU", "2026-09-30"),
    ])
    feed = ea.load()
    assert feed.rows == 1 and feed.dropped == 3
    assert feed.lookup("SNDK", FRI_1545)["status"] == "absent"
    assert feed.lookup("MU", FRI_1545)["status"] == "event"


def test_duplicates_and_stale_back_quarters_collapse_to_one_event(ea, run_dir):
    """GAUZ carried four rows on 2026-09-22 (2025 Q3/Q4, 2026 Q1/Q2) and YYAI two identical
    ones on 09-14. The earliest still-unreported event wins; a confirmed date beats an
    estimated one on the same day; the latest fiscal quarter breaks the remaining tie so a
    stale back-quarter never outranks the real forward one."""
    _stage(run_dir, [
        _row("YYAI", "2026-09-14", timing="pm", verified=False, quarter=1),
        _row("YYAI", "2026-09-14", timing="pm", verified=False, quarter=1),
        _row("GAUZ", "2026-09-22", verified=False, year=2025, quarter=3),
        _row("GAUZ", "2026-09-22", verified=False, year=2025, quarter=4),
        _row("GAUZ", "2026-09-22", verified=True, year=2026, quarter=1),
        _row("GAUZ", "2026-09-22", verified=False, year=2026, quarter=2),
    ])
    feed = ea.load()
    assert len(feed.by_symbol["YYAI"]) == 1, "the identical duplicate was not collapsed"
    g = feed.lookup("GAUZ", FRI_1545)
    assert g["confirmed"] is True and (g["year"], g["quarter"]) == (2026, 1)


def test_a_quarter_already_reported_is_not_the_next_one(ea, run_dir):
    """KR reported 2026-09-11 before the open. `eps.actual` is the ONLY field that says so,
    and the scan row does not have it."""
    _stage(run_dir, [_row("KR", "2026-09-11", timing="am", actual="1.090000")])
    feed = ea.load()
    info = feed.lookup("KR", FRI_1545)
    assert info["status"] == "cleared"
    assert ea.within_gate(feed, "KR", FRI_1545)[0] is False
    # and the misleading scan row does NOT resurrect it
    block = ea.attach(feed, "KR", FRI_1545, {"next_earnings": "2026-09-11",
                                             "earnings_timing": None})
    assert block["earnings_status"] == "cleared" and block["next_earnings"] is None


def test_a_pm_report_earlier_today_has_already_passed(ea, run_dir):
    _stage(run_dir, [_row("X", "2026-09-11", timing="am")])
    feed = ea.load()
    assert feed.lookup("X", FRI_0700)["status"] == "event", "08:00 am print, run at 07:00"
    # ...but the same row at 15:45 is behind us, and a run must not gate on a done event
    assert feed.lookup("X", FRI_1545)["status"] == "cleared"
    assert ea.within_gate(feed, "X", FRI_1545)[0] is False


# ------------------------------------------------------------------ absent is not "none"
def test_a_symbol_the_feed_has_nothing_for_is_absent_not_no_earnings(ea, run_dir):
    _stage(run_dir, [_row("MU", "2026-09-30")], not_found=["ZZZZQQ"])
    feed = ea.load()
    info = feed.lookup("ANET", FRI_1545)
    assert info["status"] == "absent" and info["not_found"] is False
    assert feed.lookup("ZZZZQQ", FRI_1545)["not_found"] is True
    block = ea.attach(feed, "ANET", FRI_1545, None)
    assert block["earnings_status"] == "absent"
    assert block["next_earnings"] is None and block["earnings_confirmed"] is None
    note = block["earnings_note"].lower()
    assert "absent" in note and "not the same as none" in note
    assert "no earnings" not in note, "absent must never render as 'this name has none'"


def test_the_scan_row_fills_in_for_display_only_and_never_gates(ea, run_dir):
    """The fallback the brief asks for — and the reason it is display-only. On the live
    2026-09-11 board every forward-dated `next_earnings` carried `earnings_timing: null`,
    and the field itself is written for a report already delivered as well as one ahead."""
    _stage(run_dir, [_row("MU", "2026-09-30")])
    feed = ea.load()
    block = ea.attach(feed, "DELL", FRI_1545, {"next_earnings": "2026-11-25",
                                               "earnings_timing": None})
    assert block["earnings_source"] == "scan" and block["earnings_status"] == "scan-only"
    assert block["next_earnings"] == "2026-11-25"
    assert block["earnings_confirmed"] is None, "the scan cannot confirm anything"
    assert "does not record" in block["earnings_note"]
    # a scan row is never a reason to refuse a trade
    _stage(run_dir, [], name="empty.json")
    assert ea.within_gate(feed, "DELL", FRI_1545)[0] is False
    # and with NO feed at all the scan row still reaches the page
    dormant = ea.Feed(staged=False)
    assert ea.attach(dormant, "DELL", FRI_1545,
                     {"next_earnings": "2026-11-25"})["earnings_source"] == "scan"
    assert ea.attach(dormant, "DELL", FRI_1545, None) is None, "nothing to say, no keys"


# ------------------------------------------------------------------ confirmed vs estimated
def test_confirmed_and_estimated_are_never_conflated(ea, run_dir):
    """Every large cap this book held on 2026-09-11 carried verified:false on its next
    report; only MU, nineteen days out, was confirmed. The flag tracks how far out the date
    is as much as how good it is, so it has to survive to the page."""
    _stage(run_dir, [_row("MU", "2026-09-14", verified=True),
                     _row("ANET", "2026-09-14", verified=False)])
    feed = ea.load()
    sure = feed.lookup("MU", FRI_1545)
    guess = feed.lookup("ANET", FRI_1545)
    assert sure["confirmed"] is True and guess["confirmed"] is False
    assert "confirmed by the company" in ea.describe(sure)
    assert "ESTIMATED" in ea.describe(guess)
    assert "confirmed by the company" not in ea.describe(guess).replace("not confirmed", "")
    assert ea.attach(feed, "MU", FRI_1545)["earnings_confirmed"] is True
    assert ea.attach(feed, "ANET", FRI_1545)["earnings_confirmed"] is False
    # the refusal a trader reads carries it too — this is the line that costs a trade
    assert "ESTIMATED" in ea.refusal("ANET", guess)
    assert "ESTIMATED" not in ea.refusal("MU", sure)


# ------------------------------------------------------------------ the session boundary
def test_before_open_and_after_close_at_the_session_boundary(ea, run_dir):
    """Friday 15:45 ET. The next open is Monday 09:30; the second is Tuesday 09:30."""
    _stage(run_dir, [
        _row("FRIPM", "2026-09-11", timing="pm"),     # tonight, after this close
        _row("MONAM", "2026-09-14", timing="am"),     # Monday, before the open
        _row("MONPM", "2026-09-14", timing="pm"),     # Monday, after the close
        _row("TUEAM", "2026-09-15", timing="am"),
        _row("TUEPM", "2026-09-15", timing="pm"),
    ])
    feed = ea.load()
    nxt = ea.next_session_open(FRI_1545, feed.tz)
    assert (nxt.date(), nxt.hour, nxt.minute) == (dt.date(2026, 9, 14), 9, 30)

    before = {s for s in ("FRIPM", "MONAM", "MONPM", "TUEAM", "TUEPM")
              if ea.before_next_open(feed, s, FRI_1545)[0]}
    assert before == {"FRIPM", "MONAM"}, "exactly the two that print before Monday's open"

    within2 = {s for s in ("FRIPM", "MONAM", "MONPM", "TUEAM", "TUEPM")
               if ea.within_gate(feed, s, FRI_1545, 2)[0]}
    assert within2 == {"FRIPM", "MONAM", "MONPM", "TUEAM"}
    assert ea.within_gate(feed, "TUEPM", FRI_1545, 3)[0] is True


def test_the_window_counts_sessions_not_calendar_days(ea, run_dir):
    """Two calendar days from Friday 09-11 ends on Sunday and sees nothing. Two sessions
    reaches through Monday's close — which is the whole point on a Friday run."""
    _stage(run_dir, [_row("MONPM", "2026-09-14", timing="pm")])
    feed = ea.load()
    ev = feed.lookup("MONPM", FRI_1545)
    assert (dt.date.fromisoformat(ev["date"]) - dt.date(2026, 9, 11)).days == 3
    assert ea.within_gate(feed, "MONPM", FRI_1545, 2)[0] is True


def test_unknown_timing_is_gated_conservatively_but_still_reported_as_unknown(ea, run_dir):
    """`timing: null` is rare but real (IPHA, ABVX, PRTCY). Treated as the earliest moment
    it could be, and never described as if the time were known."""
    _stage(run_dir, [_row("IPHA", "2026-09-14", timing=None)])
    feed = ea.load()
    info = feed.lookup("IPHA", FRI_1545)
    assert info["timing"] is None
    assert ea.before_next_open(feed, "IPHA", FRI_1545)[0] is True, "earliest it could be"
    d = ea.describe(info)
    assert "time of day not given" in d
    assert "before the open" not in d and "after the close" not in d


def test_the_window_comes_from_the_desk_then_pm_rules_then_the_default(ea):
    assert ea.gate_sessions(None, None) == ea.GATE_SESSIONS == 2
    assert ea.gate_sessions({}, {"earnings_gate_days": 5}) == 5
    assert ea.gate_sessions({"earnings_gate_days": 1}, {"earnings_gate_days": 5}) == 1
    assert ea.gate_sessions({"earnings_gate_days": "nonsense"}, None) == 2


def test_hold_policy_defaults_to_holding_and_never_guesses_into_a_sale(ea):
    assert ea.hold_policy(None) == ea.HOLD
    assert ea.hold_policy({}) == ea.HOLD
    assert ea.hold_policy({"hold_through_earnings": "close"}) == ea.CLOSE
    assert ea.hold_policy({"hold_through_earnings": "hold"}) == ea.HOLD
    assert ea.hold_policy({"hold_through_earnings": "tuesday"}) == ea.HOLD


# ------------------------------------------------------------------ the engine, off and on
def _golden():
    return json.loads(ss.GOLDEN.read_text(encoding="utf-8"))


def _canon(obj):
    return json.dumps(obj, sort_keys=True)


def _has_earnings_key(obj):
    if isinstance(obj, dict):
        return any(str(k).startswith("earnings") or k == "next_earnings" or
                   _has_earnings_key(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_has_earnings_key(v) for v in obj)
    return False


def test_with_no_file_staged_the_engine_is_byte_identical_to_the_pre_change_output(pm, run_dir):
    """The behaviour change is gated on staging a file, and this is the proof. No stripping
    and no allowance: the book and the journal must equal the bytes the engine wrote before
    P-06 existed, and nothing anywhere may carry an earnings key."""
    assert not (run_dir / "earnings.json").exists()
    pm.load_earnings()
    assert not pm.EARNINGS.available
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    g = _golden()
    assert _canon(out["book"]) == _canon(g["book"])
    assert _canon(out["journal"]) == _canon(g["journal"])
    assert not _has_earnings_key(out["book"])
    assert not _has_earnings_key(out["state"]["positions"])
    assert not _has_earnings_key(out["journal"])
    assert "earnings_gate_days" not in out["state"]["pm_rules"], "no new PM_RULES key"


def test_a_staged_feed_with_nothing_relevant_changes_no_decision(pm, run_dir):
    """Arming the gate is not the same as firing it. The feed is staged and covers the
    window, but none of the book's names report inside it — so the decisions, the fills and
    the book are still the pre-change bytes, and only the STATE gains the absent labels."""
    _stage(run_dir, [_row("KR", "2026-09-11", timing="am", actual="1.09")])
    pm.load_earnings()
    assert pm.EARNINGS.available
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    g = _golden()
    assert _canon(out["book"]) == _canon(g["book"]), "the book is state; a feed must not touch it"
    assert _canon(out["journal"]) == _canon(g["journal"])
    assert not _has_earnings_key(out["book"]), "a feed value must never enter durable state"
    held = {p["symbol"]: p for p in out["state"]["positions"]}
    assert held, "the sequence ends holding something"
    for sym, p in held.items():
        assert p["earnings_status"] == "absent", sym
        assert p["next_earnings"] is None and p["earnings_confirmed"] is None


def test_an_entry_inside_the_window_is_refused_and_classifies_as_earnings_gate(pm, run_dir):
    """SCHW is the only candidate the frozen scan carries, and the sequence opens it at
    10:45 on 2026-09-10. Give it a print after that afternoon's close and it is refused —
    no order placed, nothing to fill at 13:15."""
    _stage(run_dir, [_row("SCHW", "2026-09-10", timing="pm", verified=True)])
    pm.load_earnings()
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    first = out["journal"]["entries"][0]
    reasons = [s["reason"] for s in first["skipped"] if s.get("symbol") == "SCHW"]
    assert reasons, "SCHW was not refused"
    assert "earnings gate" in reasons[0] and "2026-09-10" in reasons[0]
    assert "after the close" in reasons[0] and "confirmed by the company" in reasons[0]
    assert not [d for e in out["journal"]["entries"] for d in e["decisions"]
                if d["symbol"] == "SCHW"], "no order should have been placed or filled"
    assert "SCHW" not in {p["symbol"] for p in out["book"]["positions"]}
    assert any("EARNINGS GATE" in w for w in first["warnings"])

    import importlib
    import report
    importlib.reload(report)
    assert report.classify(reasons[0])["rule"] == "earnings_gate"


def test_an_entry_just_outside_the_window_is_not_refused(pm, run_dir):
    """2026-09-10 is a Thursday; the run is at 10:45. Two sessions reach Monday's open, so
    a Monday-after-the-close print is outside and the trade goes on as it does today."""
    _stage(run_dir, [_row("SCHW", "2026-09-14", timing="pm")])
    pm.load_earnings()
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    g = _golden()
    assert _canon(out["book"]) == _canon(g["book"])
    assert not [s for e in out["journal"]["entries"] for s in e["skipped"]
                if "earnings gate" in s["reason"]]


def test_exits_trims_and_rebalancing_stay_live_through_the_gate(pm, run_dir):
    """The same rule every other gate here follows: a reason not to add is never a reason
    not to sell. HOOD gaps through its 93.85 stop at 13:15 and reports the same night."""
    _stage(run_dir, [_row("HOOD", "2026-09-10", timing="pm"),
                     _row("NVDA", "2026-09-10", timing="pm")])
    pm.load_earnings()
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    g = _golden()
    stops = [t for t in out["book"]["closed_trades"] if t["reason"] == "stop"]
    assert "HOOD" in {t["symbol"] for t in stops}, "the gate suppressed a stop-out"
    assert _canon(stops) == _canon([t for t in g["book"]["closed_trades"]
                                    if t["reason"] == "stop"]), "the stop-out is unchanged"
    # NVDA's 3R scale-out fires through the gate too
    assert [t["symbol"] for t in out["book"]["closed_trades"] if t["reason"] == "target"] == \
        [t["symbol"] for t in g["book"]["closed_trades"] if t["reason"] == "target"]


def test_hold_through_earnings_hold_carries_the_name_and_says_so(pm, run_dir):
    _stage(run_dir, [_row("SNDK", "2026-09-10", timing="pm", verified=False)])
    pm.load_earnings()
    pm.DESK["rules"] = {}                       # the default
    out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    last = out["journal"]["entries"][-1]
    warn = [w for w in last["warnings"] if w.startswith("EARNINGS: SNDK")]
    assert warn, last["warnings"]
    assert "hold_through_earnings policy is 'hold'" in warn[0]
    assert "ESTIMATED" in warn[0], "an estimated date must not read as a fact in the warning"
    assert "SNDK" in {p["symbol"] for p in out["book"]["positions"]}, "held, not sold"


def test_hold_through_earnings_close_goes_flat_before_the_print(pm, run_dir):
    _stage(run_dir, [_row("SNDK", "2026-09-10", timing="pm")])
    pm.load_earnings()
    pm.DESK["rules"] = {"hold_through_earnings": "close"}
    try:
        out = ss.replay(pm, run_dir, {"shadow": {"enabled": False}})
    finally:
        pm.DESK["rules"] = {}
    closed = [t for t in out["book"]["closed_trades"] if t["reason"] == "earnings"]
    assert closed and closed[0]["symbol"] == "SNDK"
    assert "hold_through_earnings policy is 'close'" in closed[0]["detail"]
    assert "SNDK" not in {p["symbol"] for p in out["book"]["positions"]}
