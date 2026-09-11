"""brief.py — the 08:45 ET morning brief (U-04).

    python3 brief.py --state <dir> [--desks swing,pullback,momentum]
                     [--now ISO] [--max-chars 1200]
                     [--text brief.txt] [--html morning-brief.html] [--json brief.json]
                     [--ntfy <topic-url>]

Reads the state a run already has on disk — the three paper books, the three PM
journals, `coverage/pm-coverage.json`, `scans/latest.json` and the watch journal — and
emits two things:

  1. A short plain-text body for a push notification or an ntfy POST, hard-capped at
     `--max-chars` (1,200 by default). It LEADS with anything that needs a human, in this
     order: UNPROTECTED, a kill switch, a ladder rung above 0, a house-cap refusal, an
     UNJUDGED holding, a position within NEAR_STOP_MULT of its stop, a held name with
     earnings before the next open. When none of that is true it says so in one line and
     names each of the seven checks it made, because a brief nobody needs to read should
     be short enough to prove it.

  2. A self-contained HTML page in the Trade Desk palette. It reuses `render_pm.CSS`
     rather than inventing a second palette, and it is structured the way `render_pm.py`
     and `render.py` structure a page: a masthead with a paper badge, banners for what
     needs a human, a stat rail, then cards.

WHAT IT NEVER DOES. It places no order, opens no socket except the optional ntfy POST,
and writes only the files the caller names. It never writes a book, a journal or a
coverage row: the brief is a reader.

NO DRIFT BY CONSTRUCTION. Every number that has an owner elsewhere in the engine is
computed by that owner, never re-derived here:

  * the macro gate      — `pm._is_high_impact` and `pm.macro_events_pending`. Re-deriving
                          the high-impact list is exactly the defect fixed in `423ed33`,
                          where a second copy of the keyword list drifted from pm.py's.
  * the ladder          — `ladder.state_for` with `pm.PM_RULES["ladder"]`.
  * house exposure      — `pm.house_exposure` + `pm.house_metrics` (K-03), with the caps
                          from `pm.PM_RULES`.
  * the shadow ledger   — `fills.shadow_summary` and `fills.cost_budget_status`.
  * coverage gaps       — `health.load_schedule`, `health.day_events` and
                          `health.coverage_rows`, the same expected-event set the health
                          sheet grades.
  * unjudged holdings   — `health.unjudged_names`.

THE HONESTY BUDGET, enforced in code the way `render_pm.py` and the weekly review
(`report.py`) do it:

  * every aggregate carries its `n`;
  * under MIN_N (30) closed round trips the page says in words that it is not a sample;
  * absent is never zero — a missing file is named with the reason it is missing, and the
    fields it would have filled render as an em-dash, never as 0;
  * no win rate, no Sharpe, no annualised anything. FORBIDDEN_METRICS is scrubbed out of
    every piece of free text copied from state, and `assert_clean()` refuses to emit
    either output if one survives;
  * paper P&L is never presented as a live return: the word "paper" sits on every number
    that could be mistaken for a real trade.

NTFY DELIVERY. `--ntfy <topic-url>` POSTs the text body with `urllib.request`. Absent, it
is a no-op and the exit code is 0. No topic name, key or URL is committed to this repo:
for a public ntfy topic the topic name IS the password, so it lives in
`engine-config.json` on the box (`alerts.ntfy_topic`) and is passed in on the command
line. The host needs an egress-allowlist entry (`ntfy.sh`, or whatever host the topic is
on) before a scheduled run can reach it — P-08 covers that request.

Exit codes: 0 brief emitted (with or without alerts), 2 nothing could be read at all.
Stdlib only. Paper mode is inherited whole from the state it reads.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fills as fills_mod
import health as health_mod
import ladder as ladder_mod
import pm as pm_mod
import render_pm

DESKS = ("swing", "pullback", "momentum")
DASH = "—"

# The same threshold render_pm.positions_table draws its "Near stop" chip at: a position
# is flagged when the last mark is at or under stop × 1.02. Two percent of a 6%-wide ATR
# stop is a third of the remaining room, which is where a human wants to be told.
NEAR_STOP_MULT = 1.02

# report.py's number, deliberately: one honesty budget across the whole project.
MIN_N = 30
NOT_A_SAMPLE = "not a sample"

MAX_CHARS = 1200          # the ntfy / push body. The prompt file promises the same number.

# Metrics this project does not report at this sample size, in any output. Matched
# case-insensitively against generated text AND against free text copied out of state.
FORBIDDEN_METRICS = (
    "win rate", "win-rate", "win_rate", "winrate",
    "sharpe", "sortino", "calmar", "information ratio",
    "annualised", "annualized", "annualised return", "annualized return",
    "cagr", "compound annual",
)
REDACTED = "[metric withheld: not reported under the honesty budget]"
_FORBIDDEN_RE = re.compile("|".join(re.escape(t) for t in FORBIDDEN_METRICS), re.I)

# The ONE sentence allowed to name them, and it names them only to refuse them. It is
# substituted in AFTER assert_clean() has run, so the guard stays absolute: no forbidden
# metric name can reach an output through any path except this fixed, audited string.
DENIAL_TOKEN = "@@DENIAL@@"
DENIAL = ("There is no win rate, no Sharpe and no annualised figure anywhere on this "
          "page, because at this sample size none of them is evidence.")


def emit(text, what):
    """assert_clean, then substitute the one sanctioned denial sentence."""
    return assert_clean(text, what).replace(DENIAL_TOKEN, DENIAL)


class BriefHonestyError(AssertionError):
    """A forbidden metric name reached an output. The template, not the data, is wrong."""


def scrub(text):
    """Redact forbidden metric names out of free text copied from state.

    Journal warnings, scan `notable` lines and coverage reasons are prose written by
    other runs. They are data, not template, so they are cleaned rather than refused."""
    if text is None:
        return None
    return _FORBIDDEN_RE.sub(REDACTED, str(text))


def forbidden_hits(text):
    """Every forbidden metric name present in `text`, lowercased, in order."""
    return [m.group(0).lower() for m in _FORBIDDEN_RE.finditer(str(text or ""))]


def assert_clean(text, what):
    hits = forbidden_hits(text)
    if hits:
        raise BriefHonestyError(
            f"REFUSING TO EMIT {what}: forbidden metric name(s) {sorted(set(hits))} "
            "survived into the output. No win rate, no Sharpe, no annualised anything.")
    return text


# ---------------------------------------------------------------- formatting
def esc(v):
    return html.escape(str(v), quote=True)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def money(v, dp=2):
    return f"${float(v):,.{dp}f}" if _num(v) else DASH


def signed_money(v, dp=2):
    if not _num(v):
        return DASH
    return f"{'+' if float(v) >= 0 else '-'}${abs(float(v)):,.{dp}f}"


def pct(v, dp=1, sign=False):
    if not _num(v):
        return DASH
    return f"{float(v):+.{dp}f}%" if sign else f"{float(v):.{dp}f}%"


def price(v):
    return f"{float(v):,.2f}" if _num(v) else DASH


def n_of(n, unit, plural=None):
    """`n` with its unit, always — every aggregate carries its n."""
    return f"{n} {unit if n == 1 else (plural or unit + 's')}"


def parse_iso(s):
    return health_mod.parse_iso(s)


def _tz(name="America/New_York"):
    return health_mod._tz(name)


# ---------------------------------------------------------------- inputs
def _reason(path, root, label):
    if path is None:
        return f"no file for {label} under {root}"
    if not os.path.exists(path):
        return f"{path} does not exist"
    return f"{path} is present but unreadable or not JSON"


class Inputs:
    """Every file the brief reads, with the reason for each one that is not there.

    Path resolution follows `health.State` exactly, so the brief and the health sheet
    never disagree about where the state repo keeps a book: `books/<desk>.json` first,
    then the staged run-directory names (`paper_book<suffix>.json`), then nothing.
    """

    def __init__(self, root, desks=DESKS, desk_cfg=None):
        self.root = root
        self.desks = list(desks)
        self.state = health_mod.State(root, self.desks, desk_cfg or {})
        self.absent = []            # [{"what", "reason"}] — rendered, never zeroed

        self.books, self.journals = {}, {}
        for d in self.desks:
            p = self.state.book_path(d)
            b = health_mod.load_json(p) if p else None
            if isinstance(b, dict):
                self.books[d] = b
            else:
                self.books[d] = None
                self.absent.append({"what": f"{d} book",
                                    "reason": _reason(p, root, f"the {d} book")})
            jp = self.state.journal_path(d)
            j = health_mod.load_json(jp) if jp else None
            if isinstance(j, dict):
                self.journals[d] = j
            else:
                self.journals[d] = None
                self.absent.append({"what": f"{d} journal",
                                    "reason": _reason(jp, root, f"the {d} journal")})

        cp = self.state._first(os.path.join("coverage", "pm-coverage.json"),
                               "pm-coverage.json")
        cov = health_mod.load_json(cp) if cp else None
        self.coverage = cov if isinstance(cov, dict) else None
        self.coverage_path = cp
        if self.coverage is None:
            self.absent.append({"what": "coverage",
                                "reason": _reason(cp, root, "pm-coverage.json")})

        sp = self.state._first(os.path.join("scans", "latest.json"), "latest-scan.json",
                               "scan_results.json")
        sc = health_mod.load_json(sp) if sp else None
        self.scan = sc if isinstance(sc, dict) else None
        self.scan_path = sp
        if self.scan is None:
            self.absent.append({"what": "latest scan",
                                "reason": _reason(sp, root, "the latest scan")})

        wp = self.state._first(os.path.join("journals", "watch.json"),
                               "pm-watch-journal.json", "pm_watch_journal.json")
        w = health_mod.load_json(wp) if wp else None
        self.watch = w if isinstance(w, dict) else None
        self.watch_path = wp
        if self.watch is None:
            self.absent.append({"what": "watch journal",
                                "reason": _reason(wp, root, "the watch journal")})

    def present_desks(self):
        return [d for d in self.desks if isinstance(self.books.get(d), dict)]


# ---------------------------------------------------------------- per-desk view
def latest_decision_entry(journal):
    return health_mod.latest_entry(journal or {}, decision_only=True)


def marked_equity(book):
    """(equity, cash, invested, unpriced) from the book's own marks.

    A position the book cannot price contributes nothing and is COUNTED, so the page can
    say the equity excludes it rather than quietly valuing it at zero."""
    cash = float(book.get("cash") or 0.0)
    inv, unpriced = 0.0, []
    for p in book.get("positions") or []:
        px = p.get("last_price")
        sh = p.get("shares")
        if _num(px) and _num(sh):
            inv += float(px) * float(sh)
        else:
            unpriced.append(p.get("symbol") or "?")
    return round(cash + inv, 2), round(cash, 2), round(inv, 2), unpriced


def stamped_equity(book):
    """The equity the last run stamped on the curve, when the curve matches `last_run`."""
    curve = [c for c in (book.get("equity_curve") or []) if isinstance(c, dict)]
    if not curve:
        return None, None
    last = curve[-1]
    if book.get("last_run") and last.get("ts") == book.get("last_run"):
        return last.get("equity"), last.get("ts")
    return None, last.get("ts")


def round_trips(book):
    """Completed round trips, which is NOT len(closed_trades).

    `closed_trades` carries one row per SALE, so a name trimmed twice and then exited
    contributes three rows, and a name trimmed once and still held contributes one row for
    a position that has not round-tripped at all. Counting rows would have put this book
    over the 30-observation honesty threshold on partial exits alone. A round trip is a
    (symbol, opened) lot that appears in `closed_trades` and no longer appears in
    `positions`."""
    open_lots = {(p.get("symbol"), p.get("opened"))
                 for p in (book.get("positions") or []) if isinstance(p, dict)}
    closed_lots = {(t.get("symbol"), t.get("opened"))
                   for t in (book.get("closed_trades") or []) if isinstance(t, dict)}
    return len(closed_lots - open_lots)


def position_view(p):
    px, stop = p.get("last_price"), p.get("stop")
    dist = None
    if _num(px) and _num(stop) and float(stop) > 0:
        dist = (float(px) / float(stop) - 1.0) * 100.0
    near = _num(px) and _num(stop) and float(px) <= float(stop) * NEAR_STOP_MULT
    return {
        "symbol": p.get("symbol"),
        "shares": p.get("shares"),
        "avg_cost": p.get("avg_cost"),
        "price": px,
        "price_source": p.get("price_source"),
        "last_priced": p.get("last_priced"),
        "market_value": (round(float(px) * float(p.get("shares") or 0.0), 2)
                         if _num(px) else None),
        "stop": stop,
        "stop_distance_pct": round(dist, 2) if dist is not None else None,
        "stop_basis_kind": p.get("stop_basis_kind"),
        "stop_basis_short": p.get("stop_basis_short"),
        "stop_policy": p.get("stop_policy"),
        "near_stop": bool(near),
        "unpriced": not _num(px),
        "gics": p.get("gics") or p.get("industry"),
        "opened": p.get("opened"),
        "sessions_held": p.get("sessions_held"),
        "next_earnings": p.get("next_earnings"),
        "earnings_timing": p.get("earnings_timing"),
    }


def order_view(o):
    return {
        "symbol": o.get("symbol"),
        "side": o.get("side"),
        "kind": o.get("kind"),
        "limit_price": o.get("limit_price"),
        "shares": o.get("shares"),
        "notional": o.get("notional"),
        "placed_slot": o.get("placed_slot"),
        "status": o.get("status"),
        # A day order dies at the session roll. That is the whole reason the brief lists
        # working orders at 08:45: the ones placed yesterday are already gone.
        "expires_at_roll": (o.get("expires") or "day") == "day",
        "stop": (o.get("meta") or {}).get("stop"),
        "reason": scrub(o.get("reason")),
    }


def day_pnl_pct_for(book, today):
    """The session P&L the kill switch would compute, or None before the session opens."""
    day = book.get("day") or {}
    if str(day.get("date") or "")[:10] != today.isoformat():
        return None
    open_eq = day.get("open_equity")
    eq, _, _, _ = marked_equity(book)
    if not _num(open_eq) or float(open_eq) <= 0:
        return None
    return round((eq - float(open_eq)) / float(open_eq) * 100.0, 2)


def desk_view(desk, book, journal, today):
    eq, cash, inv, unpriced = marked_equity(book)
    stamped, stamped_ts = stamped_equity(book)
    entry = latest_decision_entry(journal)
    positions = [position_view(p) for p in (book.get("positions") or [])]
    orders = [order_view(o) for o in (book.get("working_orders") or [])
              if (o.get("status") or "working") == "working"]

    day_pnl = day_pnl_pct_for(book, today)
    lad = ladder_mod.state_for(book, eq, today, pm_mod.PM_RULES.get("ladder"), day_pnl)

    closed = book.get("closed_trades") or []
    shadow = fills_mod.shadow_summary(book)
    budget = fills_mod.cost_budget_status(book, today, pm_mod.PM_RULES)

    halted = bool((book.get("day") or {}).get("halted")) or bool(book.get("halted"))
    halt_reason = scrub((book.get("day") or {}).get("halt_reason") or book.get("halt_reason"))

    unjudged = health_mod.unjudged_names(book, entry) if entry else {}

    # House-cap refusals are journalled as skipped items by pm.house_cap_check; the same
    # prose report.py's `house_symbol_cap` / `house_sector_cap` rules key on.
    refusals = []
    for s in ((entry or {}).get("skipped") or []):
        why = str(s.get("reason") or "")
        if "house cap:" in why or "house exposure" in why:
            refusals.append({"symbol": s.get("symbol"), "reason": scrub(why)})

    return {
        "desk": desk,
        "present": True,
        "mode": book.get("mode") or "paper",
        "last_run": book.get("last_run"),
        "revision": book.get("revision"),
        "equity": eq,
        "equity_basis": "marked from the book's own last prices",
        "stamped_equity": stamped,
        "stamped_equity_ts": stamped_ts,
        "starting_equity": book.get("starting_equity"),
        "cash": cash,
        "invested": inv,
        # Nothing on these desks flattens at the close, so every open position is carried
        # overnight. Working buy orders are NOT overnight exposure: a day order expires at
        # the session roll before it can ever fill.
        "overnight_usd": inv,
        "overnight_pct": round(inv / eq * 100.0, 2) if eq > 0 else None,
        "unpriced": unpriced,
        "positions": positions,
        "orders": orders,
        "n_positions": len(positions),
        "n_orders": len(orders),
        "ladder": lad,
        "halted": halted,
        "halt_reason": halt_reason,
        "day_pnl_pct": day_pnl,
        "shadow": shadow,
        "cost_budget": budget,
        "n_closed_legs": len(closed),
        "n_round_trips": round_trips(book),
        "realized_pnl": book.get("realized_pnl"),
        "unjudged": unjudged,
        "house_refusals": refusals,
        "broker_policy": (entry or {}).get("broker_policy") or book.get("broker_policy"),
        "entry_slot": (entry or {}).get("slot"),
        "entry_ts": (entry or {}).get("ts"),
        "entry_warnings": [scrub(w) for w in ((entry or {}).get("warnings") or [])],
        "engine_sha": (entry or {}).get("engine_sha"),
        "journal_present": journal is not None,
    }


def absent_desk_view(desk, reason):
    return {"desk": desk, "present": False, "absent_reason": reason,
            "positions": [], "orders": [], "n_positions": None, "n_orders": None,
            "equity": None, "cash": None, "invested": None, "overnight_usd": None,
            "overnight_pct": None, "ladder": None, "halted": None,
            "n_closed_legs": None, "n_round_trips": None,
            "shadow": None, "cost_budget": None, "unjudged": {}, "house_refusals": [],
            "unpriced": [], "journal_present": False}


# ---------------------------------------------------------------- the house
def house_view(inputs):
    """Combined exposure, computed by pm.py's own HOUSE-01 + K-03 code paths.

    `pm.house_exposure` wants one book plus peers and labels the first one from the module
    global `pm.DESK`. The brief has no active desk, so it names the first present desk and
    restores the global afterwards — reusing the engine's tally is worth the two lines."""
    present = inputs.present_desks()
    if len(present) < 2:
        return {"measured": False,
                "reason": ("house exposure needs at least two desk books; "
                           f"{n_of(len(present), 'book')} could be read "
                           "— an unmeasured house is not a safe one")}
    anchor, peers = present[0], present[1:]
    saved = dict(pm_mod.DESK)
    try:
        pm_mod.DESK["name"] = anchor
        house = pm_mod.house_exposure(
            inputs.books[anchor], {},
            peers={"books": {d: inputs.books[d] for d in peers},
                   "loaded": list(peers),
                   "missing": [d for d in inputs.desks if d not in present]})
    finally:
        pm_mod.DESK.clear()
        pm_mod.DESK.update(saved)
    if not house:
        return {"measured": False,
                "reason": "pm.house_exposure could not tally a positive combined equity"}
    exposure = pm_mod.house_metrics(house)          # bars absent -> sector proxy, says so
    caps = house.get("caps") or {}
    return {
        "measured": True,
        "equity": house.get("equity"),
        "desks": house.get("desk_count"),
        "desks_loaded": present,
        "desks_missing": [d for d in inputs.desks if d not in present],
        "caps": caps,
        "sector_pct": house.get("sector_pct") or {},
        "symbol_pct": house.get("symbol_pct") or {},
        "exposure": exposure or {},
        "n_holdings": len(house.get("holdings") or []),
    }


# ---------------------------------------------------------------- coverage gaps
def last_trading_day(today):
    d = today - dt.timedelta(days=1)
    while not health_mod.is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def coverage_gaps(cov, sched, day):
    """Yesterday's expected runner events that left no row, drawn as GAPS.

    A slot with no coverage row is not a slot that went well and wrote nothing: pm.py
    writes a heartbeat on every invocation, so silence means the run did not happen. The
    expected-event set is `health.day_events`, so the brief and the health sheet grade the
    same schedule."""
    tz = health_mod._tz(sched["timezone"]) or dt.timezone.utc
    expected = health_mod.day_events(sched, day)
    if cov is None:
        return {"measured": False, "day": day.isoformat(), "expected": len(expected),
                "reason": "pm-coverage.json could not be read — gaps unknown, "
                          "not zero"}
    rows = health_mod.coverage_rows(cov, days={day.isoformat()})
    present = {health_mod._row_key(r, tz) for r in rows if not r.get("aborted")}
    missing = [e for e in expected if health_mod._event_key(e, tz) not in present]
    aborted = [{"slot": r.get("slot"), "reason": scrub(str(r.get("reason"))[:160])}
               for r in rows if r.get("aborted")]
    # Which STEPS this file has ever recorded, on any day. A missing event whose step has
    # never appeared is not evidence that a particular run failed: it is evidence that
    # nothing writes a coverage row for that step yet. Both are gaps; only one is news.
    steps_seen = {health_mod._row_key(r, tz)[1] for r in health_mod.coverage_rows(cov)}
    return {
        "measured": True,
        "day": day.isoformat(),
        "expected": len(expected),
        "present": len(expected) - len(missing),
        "steps_recorded": sorted(steps_seen),
        "missing": [{"slot": e["slot"], "step": e["step"],
                     "at": e["when"].astimezone(tz).strftime("%H:%M"),
                     "session": e.get("session"),
                     "never_recorded": e["step"] not in steps_seen} for e in missing],
        "aborted": aborted,
        "rows": len(rows),
    }


# ---------------------------------------------------------------- macro
def macro_view(scan, now_utc, today):
    """Today's calendar, with the gating ones marked by pm.py's own classifier.

    `pm._is_high_impact` decides which names are on the gating list and
    `pm.macro_events_pending` decides which of those are still pending inside the
    lookahead window. Both are imported, never re-derived: a second copy of that keyword
    list is precisely the defect `423ed33` fixed."""
    evs = ((scan or {}).get("meta") or {}).get("macro_events") or []
    if scan is None:
        return {"measured": False,
                "reason": "the latest scan could not be read — today's calendar is "
                          "unknown, not empty", "events": [], "pending": []}
    jrn = {"ts": health_mod.iso(now_utc), "warnings": []}
    pending = pm_mod.macro_events_pending(scan, today, jrn)
    out, n_today = [], 0
    for ev in evs:
        if not isinstance(ev, dict):
            continue
        name = ev.get("name") or "scheduled release"
        try:
            d_ev = dt.date.fromisoformat(str(ev.get("date")))
        except (ValueError, TypeError):
            d_ev = None
        when = "today" if d_ev == today else (
            "tomorrow" if d_ev == today + dt.timedelta(days=1) else str(ev.get("date")))
        if d_ev == today:
            n_today += 1
        label = f"{name}{' at ' + str(ev['time_et']) + ' ET' if ev.get('time_et') else ''}"
        out.append({
            "name": scrub(name),
            "time_et": ev.get("time_et"),
            "date": ev.get("date"),
            "when": when,
            # pm.py's classifier, imported. High impact == on the gating list at all.
            "high_impact": bool(pm_mod._is_high_impact(name)),
            # Gating NOW == high impact, dated today, not yet printed, inside the window.
            "gating_now": label in pending,
        })
    meta = (scan or {}).get("meta") or {}
    as_of = f"{meta.get('scan_date') or DASH} {meta.get('time') or ''}".strip()
    stale = None
    if n_today == 0:
        # The 08:45 brief runs BEFORE the day's 08:00 scan has usually landed in the
        # state repo, so the newest scan is normally yesterday's. Its calendar is
        # yesterday's too. Say that rather than printing an empty calendar as "nothing on".
        stale = (f"the newest scan on disk is the {as_of} run and it carries no release "
                 f"dated {today.isoformat()} — today's calendar has not been "
                 "collected yet. Unknown, not empty.")
    return {
        "measured": True,
        "events": out,
        "n_today": n_today,
        "stale_note": stale,
        "pending": [scrub(p) for p in pending],
        "lookahead_min": pm_mod.PM_RULES.get("macro_gate_lookahead_min"),
        "notes": [scrub(w) for w in jrn["warnings"]],
        "scan_as_of": as_of,
    }


# ---------------------------------------------------------------- earnings
_AM = ("am", "bmo", "before", "before-open", "pre")
_PM = ("pm", "amc", "after", "after-close", "post")


def next_open_et(now_et):
    """The next 09:30 ET regular open at or after `now_et`."""
    d = now_et.date()
    open_today = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    if health_mod.is_trading_day(d) and now_et <= open_today:
        return open_today
    d += dt.timedelta(days=1)
    while not health_mod.is_trading_day(d):
        d += dt.timedelta(days=1)
    return dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=now_et.tzinfo)


def earnings_before_next_open(desk_views, scan, now_et):
    """Held names reporting between now and the next regular open.

    Timing comes from the position row first and the scan row second. A same-day date
    with an `am` timing, or with no timing at all, counts: pm.py treats an unknown time as
    pending all day for the macro gate and the same reasoning applies here — an unknown
    time cannot be cleared. A `pm` timing on today's date reports AFTER the next open and
    is listed as context, not as an alert."""
    by_sym = {}
    for r in ((scan or {}).get("results") or []):
        if isinstance(r, dict) and r.get("ticker"):
            by_sym[r["ticker"]] = r
    nxt = next_open_et(now_et)
    today = now_et.date()
    hits, noted = [], []
    for dv in desk_views:
        for p in dv.get("positions") or []:
            sym = p.get("symbol")
            row = by_sym.get(sym) or {}
            date_s = p.get("next_earnings") or row.get("next_earnings")
            timing = (p.get("earnings_timing") or row.get("earnings_timing") or "")
            try:
                d_e = dt.date.fromisoformat(str(date_s)[:10])
            except (ValueError, TypeError):
                continue
            t = str(timing).strip().lower()
            item = {"desk": dv["desk"], "symbol": sym, "date": d_e.isoformat(),
                    "timing": t or None,
                    "source": "position" if p.get("next_earnings") else "scan"}
            if d_e < today:
                continue
            if d_e == today and t in _PM:
                item["note"] = "reports after today's close — after the next open"
                noted.append(item)
            elif d_e == today and (t in _AM or not t):
                item["note"] = ("before the open" if t in _AM
                                else "date today, time unknown — cannot be cleared")
                hits.append(item)
            elif d_e == today + dt.timedelta(days=1) and t in _AM:
                item["note"] = "tomorrow before the open — after the next open"
                noted.append(item)
            else:
                item["note"] = f"{(d_e - today).days} session(s) out"
                noted.append(item)
    return {"before_next_open": hits, "noted": noted,
            "next_open_et": nxt.isoformat(),
            "coverage": ("held names carry no next_earnings of their own; the scan row is "
                         "the only source and it is null for most names"
                         if not any(p.get("next_earnings")
                                    for dv in desk_views for p in dv.get("positions") or [])
                         else "next_earnings read from the position row where present")}


# ---------------------------------------------------------------- watchlist
def watchlist(scan, n=5):
    if scan is None:
        return {"measured": False,
                "reason": "the latest scan could not be read — no watchlist",
                "rows": []}
    rows = [r for r in (scan.get("results") or []) if isinstance(r, dict)]
    rows.sort(key=lambda r: (r.get("score") if _num(r.get("score")) else -1), reverse=True)
    meta = scan.get("meta") or {}
    return {
        "measured": True,
        "rows": [{
            "ticker": r.get("ticker"),
            "name": scrub(r.get("name")),
            "score": r.get("score"),
            "setup": scrub(r.get("setup")),
            "verdict": scrub(r.get("verdict")),
            "coverage_pct": r.get("coverage_pct"),
            "missing_pillars": r.get("missing_pillars") or [],
            "confidence": r.get("confidence"),
        } for r in rows[:n]],
        "n_rows": len(rows),
        "as_of": f"{meta.get('scan_date') or DASH} {meta.get('time') or ''}".strip(),
        "slot": meta.get("slot"),
        "coverage_avg": meta.get("coverage_avg"),
        "data_warnings": [scrub(w) for w in (meta.get("data_warnings") or [])],
    }


# ---------------------------------------------------------------- watch journal
def watch_view(watch, today):
    if watch is None:
        return {"measured": False,
                "reason": "the watch journal could not be read — the overnight is "
                          "unverified, not quiet"}
    entries = [e for e in (watch.get("entries") or []) if isinstance(e, dict)]
    if not entries:
        return {"measured": True, "n_entries": 0, "newest": None, "alerts": [],
                "stale_days": None,
                "note": "the watch journal is empty — quiet cannot be told from not-run"}
    newest = max(entries, key=lambda e: str(e.get("ts") or ""))
    try:
        nd = dt.date.fromisoformat(str(newest.get("date"))[:10])
        stale = (today - nd).days
    except (ValueError, TypeError):
        nd, stale = None, None
    todays = [e for e in entries if str(e.get("date") or "")[:10] == today.isoformat()]
    alerts = []
    for e in todays:
        for a in e.get("alerts") or []:
            alerts.append({"desk": e.get("desk"), "session": e.get("session"),
                           "severity": a.get("severity"), "symbol": a.get("symbol"),
                           "message": scrub(a.get("message") or a.get("push"))})
    return {
        "measured": True,
        "n_entries": len(entries),
        "newest": newest.get("date"),
        "stale_days": stale,
        "alerts": alerts,
        "note": (None if stale is not None and stale <= 1 else
                 f"the newest watch entry of any kind is {newest.get('date')}, "
                 f"{stale} day(s) back — a quiet overnight cannot be distinguished "
                 "from a watch that did not run"),
    }


# ---------------------------------------------------------------- alerts
# The order the body leads with. Index is the sort key; lower is more urgent.
ALERT_ORDER = ("UNPROTECTED", "KILL SWITCH", "LADDER", "HOUSE CAP", "UNJUDGED",
               "NEAR STOP", "EARNINGS", "INPUT ABSENT")


def _alert(kind, desk, text, symbol=None):
    return {"kind": kind, "rank": ALERT_ORDER.index(kind), "desk": desk,
            "symbol": symbol, "text": scrub(text)}


def build_alerts(desk_views, house, earnings, inputs):
    out = []
    for dv in desk_views:
        if not dv.get("present"):
            continue
        d = dv["desk"]
        for sym, kind in sorted((dv.get("unjudged") or {}).items()):
            if kind == "unprotected":
                out.append(_alert("UNPROTECTED", d,
                                  f"{d} {sym}: no usable price on the last decision run "
                                  "— no stop can fire", sym))
        if dv.get("halted"):
            out.append(_alert("KILL SWITCH", d,
                              f"{d} halted: {dv.get('halt_reason') or 'no reason recorded'}"))
        lad = dv.get("ladder") or {}
        if lad.get("halt"):
            out.append(_alert("LADDER", d, f"{d} ladder rung {lad.get('rung')} — HALT: "
                                           f"{lad.get('reason')}"))
        elif (lad.get("rung") or 0) > 0 or lad.get("entries_blocked") \
                or lad.get("cool_active") or lad.get("soft_daily_hit"):
            out.append(_alert("LADDER", d,
                              f"{d} ladder rung {lad.get('rung')} at "
                              f"{pct(lad.get('dd_pct'))} off the paper high-water mark"
                              + (", entries blocked" if lad.get("entries_blocked") else "")))
        for r in dv.get("house_refusals") or []:
            out.append(_alert("HOUSE CAP", d,
                              f"{d} {r.get('symbol') or '*'}: {r.get('reason')}",
                              r.get("symbol")))
        for sym, kind in sorted((dv.get("unjudged") or {}).items()):
            if kind == "unjudged":
                out.append(_alert("UNJUDGED", d,
                                  f"{d} {sym}: priced but unjudged on the last decision "
                                  "run — the score exits could not run", sym))
        for p in dv.get("positions") or []:
            if p.get("near_stop"):
                out.append(_alert(
                    "NEAR STOP", d,
                    f"{d} {p['symbol']} {price(p.get('price'))} vs stop "
                    f"{price(p.get('stop'))} ({pct(p.get('stop_distance_pct'))} away, "
                    f"inside {NEAR_STOP_MULT:.2f}x)", p.get("symbol")))
    if house.get("measured") and (house.get("exposure") or {}).get("block_new_entries"):
        out.append(_alert("HOUSE CAP", None,
                          "house exposure is enforcing: new entries refused house-wide "
                          + "; ".join((house.get("exposure") or {}).get("reasons") or [])))
    for e in earnings.get("before_next_open") or []:
        out.append(_alert("EARNINGS", e["desk"],
                          f"{e['desk']} {e['symbol']} reports {e['date']} "
                          f"({e.get('note')}) — held into the print", e["symbol"]))
    for a in inputs.absent:
        out.append(_alert("INPUT ABSENT", None,
                          f"{a['what']} unreadable: {a['reason']}"))
    out.sort(key=lambda a: (a["rank"], a.get("desk") or "", a.get("symbol") or ""))
    return out


# The seven checks the quiet line names, in the order build_alerts runs them.
QUIET_CHECKS = (
    "no UNPROTECTED holding",
    "no kill switch",
    "ladder rung 0 on every desk read",
    "no house-cap refusal",
    "no UNJUDGED holding",
    f"nothing inside {NEAR_STOP_MULT:.2f}x its stop",
    "no held name reporting before the next open",
)


# ---------------------------------------------------------------- the brief
def build(state_root, desks=DESKS, now=None, watchlist_n=5):
    now_utc = (parse_iso(now) if isinstance(now, str) else now) or \
        dt.datetime.now(dt.timezone.utc)
    now_utc = now_utc.astimezone(dt.timezone.utc)
    tz = _tz() or dt.timezone.utc
    now_et = now_utc.astimezone(tz)
    today = now_et.date()

    inputs = Inputs(state_root, desks)
    sched, sched_path = health_mod.load_schedule(engine_dir=os.path.dirname(_HERE))

    dvs = []
    for d in desks:
        b = inputs.books.get(d)
        if isinstance(b, dict):
            dvs.append(desk_view(d, b, inputs.journals.get(d), today))
        else:
            why = next((a["reason"] for a in inputs.absent if a["what"] == f"{d} book"),
                       "not found")
            dvs.append(absent_desk_view(d, why))

    house = house_view(inputs)
    yday = last_trading_day(today)
    cov = coverage_gaps(inputs.coverage, sched, yday)
    macro = macro_view(inputs.scan, now_utc, today)
    earn = earnings_before_next_open(dvs, inputs.scan, now_et)
    wl = watchlist(inputs.scan, watchlist_n)
    watch = watch_view(inputs.watch, today)
    alerts = build_alerts(dvs, house, earn, inputs)

    present = [dv for dv in dvs if dv.get("present")]
    n_legs = sum(dv.get("n_closed_legs") or 0 for dv in present)
    n_closed = sum(dv.get("n_round_trips") or 0 for dv in present)
    combined_equity = (round(sum(dv["equity"] for dv in present), 2) if present else None)
    combined_overnight = (round(sum(dv["overnight_usd"] for dv in present), 2)
                          if present else None)
    shadow_total = round(sum(float((dv.get("shadow") or {}).get("cum_gap_usd") or 0.0)
                             for dv in present), 2) if present else None
    shadow_fills = sum(int((dv.get("shadow") or {}).get("n_fills") or 0) for dv in present)

    return {
        "generated": health_mod.iso(now_utc),
        "date": today.isoformat(),
        "time_et": now_et.strftime("%H:%M"),
        "mode": "paper",
        "state_root": state_root,
        "schedule_from": sched_path,
        "desks": dvs,
        "desks_present": [dv["desk"] for dv in present],
        "combined": {
            "equity": combined_equity,
            "cash": round(sum(dv["cash"] for dv in present), 2) if present else None,
            "overnight_usd": combined_overnight,
            "overnight_pct": (round(combined_overnight / combined_equity * 100.0, 2)
                              if combined_equity else None),
            "n_desks": len(present),
            "n_positions": sum(dv["n_positions"] for dv in present) if present else None,
            "n_orders": sum(dv["n_orders"] for dv in present) if present else None,
            "n_closed_legs": n_legs,
            "n_closed": n_closed,
            "enough_closed": n_closed >= MIN_N,
            "shadow_gap_usd": shadow_total,
            "shadow_fills": shadow_fills,
        },
        "house": house,
        "coverage": cov,
        "macro": macro,
        "earnings": earn,
        "watchlist": wl,
        "watch": watch,
        "alerts": alerts,
        "absent": inputs.absent,
        "paths": {"coverage": inputs.coverage_path, "scan": inputs.scan_path,
                  "watch": inputs.watch_path},
        "rules": {
            "near_stop_mult": NEAR_STOP_MULT,
            "min_n": MIN_N,
            "house_caps": {"symbol_pct": pm_mod.PM_RULES["house_max_symbol_pct"],
                           "sector_pct": pm_mod.PM_RULES["house_max_sector_pct"]},
            "ladder": pm_mod.PM_RULES.get("ladder"),
            "macro_gate_lookahead_min": pm_mod.PM_RULES.get("macro_gate_lookahead_min"),
            "cost_budget_bps_per_year": pm_mod.PM_RULES.get("cost_budget_bps_per_year"),
        },
    }


# ---------------------------------------------------------------- text body
def _desk_line(dv):
    if not dv.get("present"):
        return f"{dv['desk']}: NO BOOK ({dv.get('absent_reason')})"
    lad = dv.get("ladder") or {}
    return (f"{dv['desk']} paper {money(dv['equity'], 0)} | "
            f"{dv['n_positions']} pos {dv['n_orders']} ord | "
            f"o/n {pct(dv.get('overnight_pct'), 0)} | rung {lad.get('rung', DASH)} | "
            f"run {str(dv.get('last_run') or DASH)[:10]}")


def text_body(brief, max_chars=MAX_CHARS):
    """The push / ntfy body. Alerts first, hard-capped, and never silently truncated."""
    n_alert = len(brief["alerts"])
    head = (f"PAPER brief {brief['date']} {brief['time_et']} ET | "
            + (f"{n_alert} need you" if n_alert else "nothing needs you"))
    lines = [head]

    if not n_alert:
        lines.append("Checked: " + "; ".join(QUIET_CHECKS) + ".")
    else:
        room_note = "+{n} more on the page"
        for i, a in enumerate(brief["alerts"]):
            cand = f"{a['kind']}: {a['text']}"
            remaining = n_alert - i
            tail = "\n" + room_note.format(n=remaining) if remaining else ""
            if len("\n".join(lines + [cand])) + len(tail) > max_chars:
                lines.append(room_note.format(n=remaining))
                break
            lines.append(cand)

    # The digest tail is the first thing dropped when the cap binds: an alert always wins.
    tail = []
    for dv in brief["desks"]:
        tail.append(_desk_line(dv))
    h = brief["house"]
    if h.get("measured"):
        ex = h.get("exposure") or {}
        tail.append(
            f"house paper {money(h.get('equity'), 0)} | n_eff {ex.get('n_eff', DASH)}"
            f"/{(ex.get('rules') or {}).get('min_n_eff', DASH)} ({ex.get('n_eff_basis')}) | "
            f"{ex.get('largest_sector') or DASH} {pct(ex.get('largest_sector_pct'))}"
            f"/{pct(h['caps'].get('sector_pct'))} | "
            f"{ex.get('top_symbol') or DASH} {pct(ex.get('top_symbol_pct'))}"
            f"/{pct(h['caps'].get('symbol_pct'))}")
    else:
        tail.append(f"house: {h.get('reason')}")
    c = brief["coverage"]
    if c.get("measured"):
        tail.append(f"{c['day']} coverage {c['present']}/{c['expected']} expected runs, "
                    f"{len(c['missing'])} gap(s)")
    else:
        tail.append(f"coverage: {c.get('reason')}")
    m = brief["macro"]
    gating = [e["name"] for e in (m.get("events") or []) if e.get("gating_now")]
    if not m.get("measured"):
        tail.append(f"macro: {m.get('reason')}")
    elif m.get("stale_note"):
        tail.append(f"macro: no release dated today in the {m.get('scan_as_of')} scan "
                    "— today's calendar is uncollected, not empty")
    else:
        tail.append("macro gating now: " + (", ".join(gating) if gating else "none"))
    tail.append("Paper books. Not investment advice.")

    for t in tail:
        if len("\n".join(lines + [t])) > max_chars:
            break
        lines.append(t)

    body = "\n".join(lines)
    if len(body) > max_chars:                 # belt and braces: the cap is hard
        body = body[:max_chars - 1].rstrip() + "…"
    return emit(body, "the text body")


# ---------------------------------------------------------------- HTML page
def chip(label, kind="mute"):
    return f'<span class="chip {kind}"><span class="dot"></span>{esc(label)}</span>'


def banner(kind, icon, title, body):
    return (f'<div class="banner {kind}"><span class="icon">{esc(icon)}</span>'
            f'<div class="body"><b>{esc(title)}</b> {esc(body)}</div></div>')


def _empty(msg):
    return f'<div class="empty">{esc(msg)}</div>'


ALERT_TONE = {"UNPROTECTED": "critical", "KILL SWITCH": "critical", "LADDER": "warning",
              "HOUSE CAP": "warning", "UNJUDGED": "warning", "NEAR STOP": "warning",
              "EARNINGS": "warning", "INPUT ABSENT": "critical"}


def alerts_card(brief):
    if not brief["alerts"]:
        checks = "".join(f"<li>{esc(c)}</li>" for c in QUIET_CHECKS)
        return (f'<div class="card"><h2>What needs you <span class="count">none</span></h2>'
                f'<div class="pad"><p>Nothing on this page needs a human this morning. '
                f'These are the seven checks that were made, all of them on the state the '
                f'last run left on disk:</p><ul>{checks}</ul></div></div>')
    rows = "".join(
        f'<li><div class="head"><span class="sym">{esc(a["kind"])}</span>'
        f'{chip(a.get("desk") or "house", ALERT_TONE.get(a["kind"], "mute"))}</div>'
        f'<div class="why">{esc(a["text"])}</div></li>' for a in brief["alerts"])
    return (f'<div class="card"><h2>What needs you '
            f'<span class="count">{len(brief["alerts"])}</span>'
            f'<span class="note">most urgent first</span></h2>'
            f'<ul class="log">{rows}</ul></div>')


def positions_card(dv):
    if not dv.get("present"):
        return _empty(f"No book for the {dv['desk']} desk: {dv.get('absent_reason')}. "
                      "Its equity, positions and orders are unknown, not zero.")
    ps = dv.get("positions") or []
    if not ps:
        return _empty("No open positions on this desk, so no overnight exposure and "
                      "nothing to protect.")
    rows = []
    for p in ps:
        cls = "flagged" if p["unpriced"] else ("warn" if p["near_stop"] else "")
        state = (chip("Unpriced", "critical") if p["unpriced"] else
                 chip("Near stop", "warning") if p["near_stop"] else chip("Holding", "good"))
        basis = p.get("stop_basis_short") or p.get("stop_basis_kind") or DASH
        rows.append(f"""<tr class="{cls}">
  <td><span class="sym">{esc(p.get('symbol') or DASH)}</span>
      <div class="sub2">opened {esc(p.get('opened') or DASH)} &middot;
        {esc(n_of(p.get('sessions_held') or 0, 'session'))} held</div></td>
  <td class="n">{price(p.get('price'))}
      <div class="sub2 nw">{esc(p.get('price_source') or DASH)}</div></td>
  <td class="n">{money(p.get('market_value'))}</td>
  <td class="n">{price(p.get('stop'))}</td>
  <td class="n">{pct(p.get('stop_distance_pct'))}</td>
  <td><div class="sub2 nw">{esc(basis)}</div>
      <div class="sub2 nw">{esc(p.get('stop_basis_kind') or DASH)} &middot;
        {esc(p.get('stop_policy') or DASH)}</div></td>
  <td class="st">{state}</td>
</tr>""")
    return f"""<div class="scroll"><table>
<thead><tr><th>Position</th><th class="n">Mark</th><th class="n">Paper value</th>
<th class="n">Stop</th><th class="n">Distance</th><th>Stop basis in force</th>
<th>State</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def orders_card(dv):
    if not dv.get("present"):
        return _empty("Unknown: the book could not be read.")
    os_ = dv.get("orders") or []
    if not os_:
        return _empty("No working orders.")
    rows = []
    for o in os_:
        rows.append(f"""<tr>
  <td><span class="sym">{esc(o.get('symbol') or DASH)}</span>
      <div class="sub2">placed {esc(o.get('placed_slot') or DASH)}</div></td>
  <td>{chip('Buy limit' if o.get('side') == 'buy' else 'Sell limit',
            'accent' if o.get('side') == 'buy' else 'mute')}</td>
  <td class="n">{price(o.get('limit_price'))}</td>
  <td class="n">{money(o.get('notional'))}</td>
  <td class="n">{price(o.get('stop'))}</td>
  <td>{chip('Expires at the roll', 'warning') if o.get('expires_at_roll')
           else chip('Carries', 'mute')}</td>
</tr>""")
    return f"""<div class="scroll"><table>
<thead><tr><th>Order</th><th>Type</th><th class="n">Limit</th><th class="n">Notional</th>
<th class="n">Stop if filled</th><th>Session roll</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def ladder_card(dv):
    lad = dv.get("ladder")
    if not dv.get("present") or not lad:
        return _empty("Unknown: the book could not be read, so the ladder was not "
                      "evaluated. That is not the same as rung 0.")
    rungs = (lad.get("rules") or {}).get("rungs") or []
    rung_s = ", ".join(f"{float(r.get('dd_pct', 0)):.0f}%" for r in rungs)
    blocked = lad.get("entries_blocked")
    return f"""<div class="pad">
  <p><b>Rung {esc(lad.get('rung'))}</b> &middot; drawdown {pct(lad.get('dd_pct'))} from a
     paper high-water mark of {money(lad.get('hwm'))} &middot;
     entries {'<b>blocked</b>' if blocked else 'allowed'}
     at &times;{esc(f"{lad.get('entry_size_mult'):.2f}")
                if _num(lad.get('entry_size_mult')) else DASH}.</p>
  <p class="sub2">Rungs at {esc(rung_s or DASH)} below the high-water mark; soft daily
     level &minus;{esc(f"{(lad.get('rules') or {}).get('soft_daily_pct', 0):.1f}")}%.
     Session P&amp;L {esc(pct(dv.get('day_pnl_pct')) if _num(dv.get('day_pnl_pct'))
                          else 'has not been struck yet — the session opens at 09:30 ET')}.</p>
  {f'<p class="sub2">{esc(lad.get("reason"))}</p>' if lad.get("reason") else ''}
</div>"""


def desk_card(dv):
    head = (f'<h2>{esc(dv["desk"])} desk '
            f'<span class="count">{esc(dv["n_positions"]) if dv.get("present") else DASH} open'
            f' &middot; {esc(dv["n_orders"]) if dv.get("present") else DASH} working</span>'
            f'<span class="note">paper equity {money(dv.get("equity"))} &middot; cash '
            f'{money(dv.get("cash"))} &middot; overnight {money(dv.get("overnight_usd"))} '
            f'({pct(dv.get("overnight_pct"))}) &middot; last run '
            f'{esc(dv.get("last_run") or DASH)}</span></h2>')
    unpriced = ""
    if dv.get("unpriced"):
        unpriced = (f'<div class="pad sub2">Paper equity excludes '
                    f'{esc(n_of(len(dv["unpriced"]), "position"))} the book cannot price '
                    f'({esc(", ".join(dv["unpriced"]))}) &mdash; excluded, not valued at '
                    f'zero.</div>')
    return (f'<div class="card">{head}{unpriced}{positions_card(dv)}</div>'
            f'<div class="card"><h2>{esc(dv["desk"])} working orders'
            f'<span class="note">a day order dies at the session roll</span></h2>'
            f'{orders_card(dv)}</div>'
            f'<div class="card"><h2>{esc(dv["desk"])} drawdown ladder</h2>'
            f'{ladder_card(dv)}</div>')


def house_card(brief):
    h = brief["house"]
    if not h.get("measured"):
        return f'<div class="card"><h2>House exposure</h2>{_empty(h.get("reason"))}</div>'
    ex = h.get("exposure") or {}
    caps = h.get("caps") or {}
    flags = ex.get("flags") or []
    rows = [
        ("Effective number of bets (N_eff)",
         f"{ex.get('n_eff', DASH)} ({esc(ex.get('n_eff_basis') or DASH)} basis, "
         f"{esc(n_of(ex.get('corr_pairs_measured') or 0, 'measured pair'))} of "
         f"{esc(ex.get('corr_pairs_total') or 0)})",
         f"floor {(ex.get('rules') or {}).get('min_n_eff', DASH)}",
         "n_eff" in flags),
        ("Desk overlap",
         f"{pct(ex.get('overlap_pct'))} of distinct names, "
         f"{pct(ex.get('overlap_equity_pct'))} of combined paper equity",
         "no cap — reported", False),
        ("Largest sector",
         f"{esc(ex.get('largest_sector') or DASH)} {pct(ex.get('largest_sector_pct'))}",
         f"cap {pct(caps.get('sector_pct'), 0)}",
         _num(ex.get("largest_sector_pct")) and _num(caps.get("sector_pct"))
         and ex["largest_sector_pct"] > caps["sector_pct"]),
        ("Top single name",
         f"{esc(ex.get('top_symbol') or DASH)} {pct(ex.get('top_symbol_pct'))}",
         f"cap {pct(caps.get('symbol_pct'), 0)}",
         _num(ex.get("top_symbol_pct")) and _num(caps.get("symbol_pct"))
         and ex["top_symbol_pct"] > caps["symbol_pct"]),
        ("Beta-weighted exposure",
         (f"{ex.get('beta_w')}" if _num(ex.get("beta_w")) else
          f"{DASH} no bars staged, so beta is unmeasured — not zero"),
         f"cap {(ex.get('rules') or {}).get('max_beta_w', DASH)}", "beta_w" in flags),
    ]
    body = "".join(
        f'<tr class="{"warn" if flag else ""}"><td>{esc(lab)}</td><td class="n">{val}</td>'
        f'<td class="n sub2">{esc(cap)}</td></tr>' for lab, val, cap, flag in rows)
    enforce = ("enforcing — a flagged breach refuses new entries house-wide"
               if ex.get("enforce") else
               "reported only: `enforce` is off, so these numbers gate nothing")
    return f"""<div class="card">
  <h2>House exposure
    <span class="count">{esc(n_of(h.get('desks') or 0, 'desk'))} &middot;
      paper {money(h.get('equity'))}</span>
    <span class="note">{esc(enforce)}</span></h2>
  <div class="scroll"><table><thead><tr><th>Measure</th><th class="n">Value</th>
    <th class="n">Limit</th></tr></thead><tbody>{body}</tbody></table></div>
  <div class="pad sub2">Desks read: {esc(", ".join(h.get("desks_loaded") or []) or DASH)}.
    {('Not read: ' + esc(", ".join(h["desks_missing"])) + ' — the house below is the '
      'part that could be measured, not the whole house.') if h.get("desks_missing") else ''}
  </div>
</div>"""


def shadow_card(brief):
    rows = []
    for dv in brief["desks"]:
        if not dv.get("present"):
            rows.append(f'<tr><td><span class="sym">{esc(dv["desk"])}</span></td>'
                        f'<td class="n">{DASH}</td><td class="n">{DASH}</td>'
                        f'<td class="n">{DASH}</td>'
                        f'<td class="sub2">book unreadable: {esc(dv.get("absent_reason"))}</td>'
                        f'</tr>')
            continue
        sh = dv.get("shadow") or {}
        cb = dv.get("cost_budget") or {}
        n = int(sh.get("n_fills") or 0)
        gap_s = signed_money(sh.get("cum_gap_usd")) if n else DASH
        used_s = pct(cb.get("share_used_pct")) if n else DASH
        rows.append(f"""<tr>
  <td><span class="sym">{esc(dv['desk'])}</span></td>
  <td class="n">{gap_s}</td>
  <td class="n">{esc(n)}</td>
  <td class="n">{used_s}</td>
  <td class="sub2">{esc('no shadow-priced fill yet — the gap is zero because nothing '
                        'has been priced, not because execution was free' if n == 0 else
                        f"{cb.get('ytd_cost_bps')} bp of the "
                        f"{cb.get('budget_bps')} bp/yr budget")}</td>
</tr>""")
    total = brief["combined"]["shadow_gap_usd"]
    fills_n = brief["combined"]["shadow_fills"]
    note = ("A positive gap means the booked paper fill flattered the desk against a "
            "marketable shadow fill; a negative gap means the shadow price was better. "
            f"Across every desk read: {signed_money(total)} over "
            f"{n_of(fills_n, 'shadow-priced fill')}"
            + (f" — {NOT_A_SAMPLE}." if fills_n < MIN_N else "."))
    return f"""<div class="card">
  <h2>Shadow ledger <span class="count">{esc(n_of(fills_n, 'fill'))}</span>
    <span class="note">records, never books</span></h2>
  <div class="scroll"><table><thead><tr><th>Desk</th><th class="n">Cumulative gap</th>
    <th class="n">n fills</th><th class="n">Budget used</th><th>Reading</th>
  </tr></thead><tbody>{''.join(rows)}</tbody></table></div>
  <div class="pad sub2">{esc(note)}</div>
</div>"""


def coverage_card(brief):
    c = brief["coverage"]
    if not c.get("measured"):
        return (f'<div class="card"><h2>Yesterday\'s coverage</h2>'
                f'{_empty(c.get("reason"))}</div>')
    if not c["missing"] and not c["aborted"]:
        body = _empty(f"Every one of the {c['expected']} expected runner events on "
                      f"{c['day']} left a coverage row.")
    else:
        cells = []
        for m in c["missing"]:
            if m.get("never_recorded"):
                why = ("no coverage row — and no run of this step has ever written "
                       "one, so this is a hole in the record, not a failed run")
                cls = "warn"
            else:
                why = "no coverage row — this run did not happen"
                cls = "flagged"
            sess = " " + m["session"] if m.get("session") else ""
            cells.append(
                f'<tr class="{cls}"><td><span class="sym">{esc(m["slot"])}</span></td>'
                f'<td>{esc(m["step"] + sess)}</td>'
                f'<td class="n">{esc(m["at"])} ET</td>'
                f'<td class="sub2">{esc(why)}</td></tr>')
        rows = "".join(cells)
        rows += "".join(
            f'<tr class="flagged"><td><span class="sym">{esc(a["slot"])}</span></td>'
            f'<td>aborted</td><td class="n">{DASH}</td>'
            f'<td class="sub2">{esc(a["reason"])}</td></tr>' for a in c["aborted"])
        body = (f'<div class="scroll"><table><thead><tr><th>Slot</th><th>Step</th>'
                f'<th class="n">Expected</th><th>What happened</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>')
    return f"""<div class="card">
  <h2>Yesterday&rsquo;s coverage &mdash; {esc(c['day'])}
    <span class="count">{esc(c['present'])}/{esc(c['expected'])} expected runs</span>
    <span class="note">{esc(n_of(len(c['missing']), 'gap'))} drawn as gaps, not skipped
      </span></h2>
  {body}
  <div class="pad sub2">A slot with no row is a slot that did not run: every invocation of
    the engine writes a heartbeat whether or not it decided anything, so silence here is
    an absence, never a quiet success. The expected set is <code>runner/slots.json</code>
    as <code>health.day_events</code> reads it; the only steps this coverage file has ever
    recorded are {esc(", ".join(c.get("steps_recorded") or []) or "none")}, so a gap on any
    other step is a hole in the record rather than a run that failed.</div>
</div>"""


def macro_card(brief):
    m = brief["macro"]
    if not m.get("measured"):
        return f'<div class="card"><h2>Macro calendar</h2>{_empty(m.get("reason"))}</div>'
    if m.get("stale_note"):
        body = _empty(m["stale_note"][0].upper() + m["stale_note"][1:])
    elif not m["events"]:
        body = _empty("The latest scan carries no macro events at all.")
    else:
        cells = []
        for e in m["events"]:
            if e["gating_now"]:
                tag = chip("GATES NOW", "critical")
            elif e["high_impact"]:
                tag = chip("On the gating list", "warning")
            else:
                tag = chip("Reported, does not gate", "mute")
            cls = "warn" if e["gating_now"] else ""
            cells.append(
                f'<tr class="{cls}">'
                f'<td><span class="sym">{esc(e.get("time_et") or DASH)}</span></td>'
                f'<td>{esc(e["name"])}<div class="sub2">{esc(e["when"])}</div></td>'
                f'<td>{tag}</td></tr>')
        body = ('<div class="scroll"><table><thead><tr><th class="n">Time</th>'
                '<th>Release</th><th>Entry gate</th></tr></thead>'
                f'<tbody>{"".join(cells)}</tbody></table></div>')
    return f"""<div class="card">
  <h2>Macro calendar
    <span class="count">{esc(n_of(m.get('n_today') or 0, 'release'))} dated today &middot;
      {esc(len(m['events']))} on the scan</span>
    <span class="note">scan of {esc(m.get('scan_as_of') or DASH)}</span></h2>
  {body}
  <div class="pad sub2">Which releases gate is <b>pm.py</b>&rsquo;s decision, not this
    page&rsquo;s: <code>pm._is_high_impact</code> names the gating list (FOMC, CPI, PCE,
    payrolls, GDP, Powell) and <code>pm.macro_events_pending</code> decides which of those
    are still pending inside the
    {esc(m.get('lookahead_min') or DASH)}-minute lookahead. A release that has already
    printed does not gate.</div>
</div>"""


def watchlist_card(brief):
    w = brief["watchlist"]
    if not w.get("measured"):
        return (f'<div class="card"><h2>Watchlist</h2>{_empty(w.get("reason"))}</div>')
    if not w["rows"]:
        body = _empty("The latest scan returned no rows.")
    else:
        rows = "".join(
            f'<tr><td><span class="sym">{esc(r["ticker"])}</span>'
            f'<div class="sub2">{esc(r.get("name") or "")}</div></td>'
            f'<td class="n">{esc(r.get("score"))}</td>'
            f'<td>{esc(r.get("setup") or DASH)}</td>'
            f'<td>{esc(r.get("verdict") or DASH)}</td>'
            f'<td class="n">{pct(r.get("coverage_pct"), 0)}'
            f'<div class="sub2 nw">{esc(", ".join(r.get("missing_pillars") or []) or "full")}'
            f'</div></td></tr>' for r in w["rows"])
        body = (f'<div class="scroll"><table><thead><tr><th>Name</th><th class="n">Score</th>'
                f'<th>Setup</th><th>Verdict</th><th class="n">Evidence</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>')
    warn = "".join(f"<li>{esc(x)}</li>" for x in (w.get("data_warnings") or [])[:6])
    return f"""<div class="card">
  <h2>Watchlist, not a buy list
    <span class="count">top {esc(len(w['rows']))} of {esc(w.get('n_rows'))}</span>
    <span class="note">{esc(w.get('slot') or DASH)} scan, {esc(w.get('as_of') or DASH)}
      </span></h2>
  {body}
  <div class="pad sub2"><b>This is a watchlist.</b> These are the names the last scan
    ranked highest. None of them has cleared the manager&rsquo;s entry gates, none has been
    sized, and a high score is a ranking, not a recommendation. The manager decides at its
    own slots, on its own prices.
    {f'<ul>{warn}</ul>' if warn else ''}</div>
</div>"""


def watch_card(brief):
    w = brief["watch"]
    if not w.get("measured"):
        return f'<div class="card"><h2>Overnight watch</h2>{_empty(w.get("reason"))}</div>'
    if w.get("alerts"):
        body = "".join(
            f'<li><div class="head"><span class="sym">{esc(a.get("symbol") or "*")}</span>'
            f'{chip(a.get("desk") or "?", "warning")}'
            f'{chip(a.get("session") or "?", "mute")}</div>'
            f'<div class="why">{esc(a.get("message") or "")}</div></li>'
            for a in w["alerts"])
        body = f'<ul class="log">{body}</ul>'
    else:
        body = _empty("No watch alert dated today.")
    note = w.get("note")
    return (f'<div class="card"><h2>Overnight watch '
            f'<span class="count">{esc(n_of(len(w.get("alerts") or []), "alert"))}</span>'
            f'<span class="note">newest entry {esc(w.get("newest") or DASH)}</span></h2>'
            f'{body}'
            + (f'<div class="pad sub2">{esc(note)}</div>' if note else '') + '</div>')


def rail(brief):
    c = brief["combined"]
    h = (brief["house"].get("exposure") or {})
    sample = ("" if c["enough_closed"] else
              f" &middot; {esc(NOT_A_SAMPLE)}")
    return f"""<div class="rail">
  <div class="tile hero"><div class="lab">Combined paper equity</div>
    <div class="val">{money(c['equity'])}</div>
    <div class="delta">{esc(n_of(c['n_desks'], 'desk'))} read &middot;
      {esc(n_of(c['n_positions'] or 0, 'open position'))} &middot;
      {esc(n_of(c['n_orders'] or 0, 'working order'))}</div></div>
  <div class="tile"><div class="lab">Paper cash</div><div class="val">{money(c['cash'])}</div>
    <div class="delta">not a live balance</div></div>
  <div class="tile"><div class="lab">Overnight exposure</div>
    <div class="val">{money(c['overnight_usd'])}</div>
    <div class="delta">{pct(c['overnight_pct'])} of paper equity</div></div>
  <div class="tile"><div class="lab">Shadow gap</div>
    <div class="val">{signed_money(c['shadow_gap_usd'])}</div>
    <div class="delta">{esc(n_of(c['shadow_fills'], 'fill'))}</div></div>
  <div class="tile"><div class="lab">Closed round trips</div>
    <div class="val">{esc(c['n_closed'])}</div>
    <div class="delta">n = {esc(c['n_closed'])}{sample}<br>
      {esc(c['n_closed_legs'])} sale legs incl. trims</div></div>
  <div class="tile"><div class="lab">N_eff</div>
    <div class="val">{esc(h.get('n_eff', DASH))}</div>
    <div class="delta">floor {esc((h.get('rules') or {}).get('min_n_eff', DASH))} &middot;
      {esc(h.get('n_eff_basis') or DASH)}</div></div>
</div>"""


def page_fragment(brief):
    """`<title>` + `<style>` + body, the shape render_pm.render() returns."""
    c = brief["combined"]
    banners = [banner("warning", "!", "Paper books.",
                      "Every position, order and P&L number on this page is simulated. "
                      "Nothing here was sent to a broker and no real money moved. Paper "
                      "P&L is not a live return.")]
    for a in brief["alerts"][:8]:
        banners.append(banner(ALERT_TONE.get(a["kind"], "warning"), "▲",
                              a["kind"] + ".", a["text"]))
    for ab in brief["absent"]:
        banners.append(banner("critical", "▲", "Input absent.",
                              f"{ab['what']}: {ab['reason']}. Everything it would have "
                              "filled reads as an em-dash on this page, never as zero."))
    honesty = (
        f"<p><b>The honesty budget.</b> Every aggregate on this page carries its n. "
        f"The paper record is {esc(n_of(c['n_closed'], 'closed round trip'))} across "
        f"{esc(n_of(c['n_desks'], 'desk'))}, from "
        f"{esc(n_of(c['n_closed_legs'], 'recorded sale'))} — a name trimmed twice and "
        f"then exited is one round trip, not three"
        + ("." if c["enough_closed"] else
           f", which is under {MIN_N} and therefore <b>{esc(NOT_A_SAMPLE)}</b>: no "
           "distribution on this page should be read as evidence of an edge.") +
        " " + DENIAL_TOKEN +
        " A file that could not be read is named with its reason and the fields it would "
        "have filled are drawn as an em-dash, never as zero.</p>")
    desks_html = "".join(desk_card(dv) for dv in brief["desks"])
    return f"""<title>@@TITLE@@</title>
<style>{render_pm.CSS}</style>
<div class="wrap">
  <div class="mast">
    <div>
      <h1>Morning brief</h1>
      <div class="sub">{esc(brief['date'])} &middot; {esc(brief['time_et'])} ET &middot;
        read from the state on disk, nothing fetched</div>
    </div>
    <div class="spacer"></div>
    <span class="badge paper">Paper &mdash; no orders sent</span>
    <span class="badge">{esc(n_of(len(brief['alerts']), 'alert'))}</span>
    <span class="badge">Built {esc(brief['generated'][11:16])} UTC</span>
  </div>
  {''.join(banners)}
  {rail(brief)}
  {alerts_card(brief)}
  {desks_html}
  {house_card(brief)}
  {shadow_card(brief)}
  {coverage_card(brief)}
  {macro_card(brief)}
  {watch_card(brief)}
  {watchlist_card(brief)}
  <div class="foot">
    {honesty}
    <p><b>What overnight exposure means here.</b> No desk flattens at the close, so every
    open position is carried overnight and the whole invested balance is at risk to the
    gap. Working buy orders are not overnight exposure: a day limit expires at the session
    roll before it can ever fill, which is why the working-order table marks each one.</p>
    <p><b>Where the numbers come from.</b> The ladder is
    <code>ladder.state_for</code>, the house block is
    <code>pm.house_exposure</code> and <code>pm.house_metrics</code>, the shadow ledger is
    <code>fills.shadow_summary</code>, the coverage gaps are graded against
    <code>health.day_events</code>, and the macro gate is
    <code>pm._is_high_impact</code>. This page re-derives none of them, so it cannot
    disagree with the manager.</p>
    <p>Paper books. Not investment advice.</p>
  </div>
</div>"""


def page_document(brief, title=None):
    """A standalone HTML file: the fragment inside a minimal document skeleton."""
    t = title or f"Morning brief {brief['date']}"
    frag = page_fragment(brief).replace("@@TITLE@@", esc(t))
    # The same refusal render_pm.emit() makes: a surviving placeholder means the template
    # and the publish step have drifted apart. DENIAL_TOKEN is the one legitimate
    # placeholder left at this point; emit() substitutes it after the honesty check.
    if "@@" in frag.replace(DENIAL_TOKEN, ""):
        raise BriefHonestyError("an unsubstituted @@ placeholder survived the template")
    doc = ("<!doctype html>\n<html lang=\"en\">\n<head>\n"
           "<meta charset=\"utf-8\">\n"
           "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
           f"{frag.split('</style>')[0]}</style>\n</head>\n<body>"
           f"{frag.split('</style>', 1)[1]}</body>\n</html>\n")
    return emit(doc, "the HTML page")


# ---------------------------------------------------------------- ntfy
def post_ntfy(topic_url, body, title=None, timeout=10):
    """POST the text body to an ntfy topic. Absent URL is a no-op.

    The topic name is the password for a public ntfy topic, so it is never committed: it
    comes from `engine-config.json` on the box (`alerts.ntfy_topic`) and is passed in on
    the command line. The host needs an egress-allowlist entry before a scheduled run can
    reach it (P-08)."""
    if not topic_url:
        return {"sent": False, "reason": "no --ntfy topic given"}
    import urllib.error
    import urllib.request
    req = urllib.request.Request(topic_url, data=body.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "text/plain; charset=utf-8")
    if title:
        # ntfy headers are latin-1 on the wire; the body carries the full text anyway.
        req.add_header("Title", title.encode("ascii", "replace").decode("ascii"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"sent": True, "status": r.status}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"sent": False, "reason": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- cli
def main(argv=None):
    ap = argparse.ArgumentParser(description="The 08:45 ET morning brief (U-04).")
    ap.add_argument("--state", required=True, help="state repo root, or a staged run dir")
    ap.add_argument("--desks", default=",".join(DESKS))
    ap.add_argument("--now", help="ISO-8601 instant to build for (default: now)")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--watchlist", type=int, default=5)
    ap.add_argument("--text", help="write the plain-text body here")
    ap.add_argument("--html", help="write the standalone HTML page here")
    ap.add_argument("--json", dest="json_out", help="write the computed brief here")
    ap.add_argument("--ntfy", help="ntfy topic URL to POST the text body to")
    a = ap.parse_args(argv)

    desks = [d.strip() for d in a.desks.split(",") if d.strip()]
    brief = build(a.state, desks, now=a.now, watchlist_n=a.watchlist)
    body = text_body(brief, a.max_chars)

    if a.text:
        with open(a.text, "w", encoding="utf-8") as f:
            f.write(body + "\n")
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(page_document(brief))
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8") as f:
            json.dump(brief, f, indent=2, sort_keys=True, default=str)

    print(body)
    res = post_ntfy(a.ntfy, body, f"AI-trading paper brief {brief['date']}")
    if a.ntfy:
        print(f"\nntfy: {'sent' if res['sent'] else 'NOT sent — ' + str(res.get('reason'))}",
              file=sys.stderr)

    if not brief["desks_present"] and not brief["watchlist"].get("measured"):
        print("FATAL: nothing could be read under " + a.state, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
