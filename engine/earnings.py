"""P-06 — the earnings feed, and the single-name event gate's view of it.

Stdlib only, like every other module here.

WHAT THIS READS
---------------
`$SCAN_DIR/earnings.json`: the RAW `get_earnings_calendar` response, staged by the
session before the runner is called, exactly as `pm_quotes.json` is. One connector call
covers the whole market for a date window, so there is no per-symbol fan-out and no
partial coverage caused by a rate limit part-way down a watchlist.

The payload shape, verified against the live connector on 2026-09-11:

    {"data": {"results": [{"symbol": "MU", "year": 2026, "quarter": 4,
                           "eps": {"estimate": "31.26", "actual": null},
                           "report": {"date": "2026-09-30", "timing": "pm",
                                      "verified": true}}, ...],
              "not_found": ["ZZZZQQ"]}}

Five facts about that payload drive everything below, and each one was observed, not
assumed:

1. `report.verified` is the confirmed/estimated flag. True means the company has put the
   date out; false means the vendor is guessing from last year's cadence. It is NOT a
   quality score — it is largely a function of how far out the date is. Every one of the
   eight names this book held on 2026-09-11 carried `verified: false` on its next report,
   because all eight are six to twelve weeks out and none has announced yet. MU, at
   nineteen days, was the only confirmed one. A gate that treats an estimated date as a
   fact will eventually refuse a real trade for an event that does not happen that day.
   So the distinction is carried on every record and printed everywhere the date is.

2. `report.timing` is "am" (before the open), "pm" (after the close), or **null**. Null is
   rare but real (IPHA, ABVX, PRTCY in the 14-day window). Unknown timing is reported as
   unknown and treated, for gating only, as the earliest moment it could be — the open of
   that day. An unknown time cannot be cleared, which is the same reasoning PM.md § 2
   applies to a macro release with no time given.

3. `report` itself can be **null** on a row (SNDK 2025 Q2). A row without a parseable date
   is dropped, never defaulted.

4. `eps.actual` non-null means the quarter has already been reported. This is the only
   thing that separates "reports on the 11th" from "reported on the 11th", and it is the
   field the scan row does not have — `scanner.py` writes the same `next_earnings` key for
   both, and says "Last reported {date} — earnings just cleared" from the same value. On
   the 2026-09-11 board KR carried `next_earnings: 2026-09-11`, already reported that
   morning. A gate reading the scan row alone would have refused it.

5. A symbol appears on MORE THAN ONE ROW — duplicates (YYAI twice on 09-14), and stale
   back-quarters parked on a future placeholder date (GAUZ on four rows for 09-22 carrying
   2025 Q3, 2025 Q4, 2026 Q1 and 2026 Q2). Rows are therefore deduplicated and the
   earliest still-unreported event wins, with a confirmed date preferred over an estimated
   one on the same day.

WHAT IT DOES NOT KNOW
---------------------
There is no NYSE holiday calendar here. Sessions are weekdays. A holiday inside the gate
window makes the window one session longer than it reads, which errs toward refusing — the
safe direction — but it is not exact, and a half-day close is not modelled at all.

The moments used for ordering are 08:00 ET for a before-open report and 16:00 ET for an
after-close one. They exist to sort events against session opens, not to time anything.
"""
import datetime as dt
import json
import os

FILENAME = "earnings.json"

# The gate window, in SESSIONS, not calendar days: a Friday run with the default of 2 sees
# through Monday's close and into Tuesday's open, which a calendar-day count would not.
# A desk overrides it as rules.earnings_gate_days in desks.json, the same way it names its
# stop policy; PM_RULES["earnings_gate_days"] overrides the default house-wide if it is
# ever set there.
GATE_SESSIONS = 2

# Per-desk policy for a name already held that reports inside the window.
# "hold"  — carry it through the print; the run says so and takes no action (default).
# "close" — the exit pass closes it before the print. An exit, so it is always live.
HOLD = "hold"
CLOSE = "close"
HOLD_THROUGH_DEFAULT = HOLD

_OPEN = dt.time(9, 30)
_BEFORE_OPEN = dt.time(8, 0)
_AFTER_CLOSE = dt.time(16, 0)
_MAX_WALK = 400          # days; a loop guard, never reached in practice


def _zone():
    """America/New_York, or None where the host has no tz database.

    None is a real answer and every caller handles it: the gate stays DORMANT rather
    than gating on a guessed clock. The runner box is Windows, where `zoneinfo` needs
    the `tzdata` package, so this is not hypothetical — `pm.py`'s macro gate carries the
    same guard for the same reason.
    """
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/New_York")
    except Exception:                                  # noqa: BLE001 — degrade, never raise
        return None


def _to_et(ts, tz):
    """An aware ET datetime from an ISO string (Z or offset) or a datetime. None if unusable."""
    if tz is None or ts is None:
        return None
    if isinstance(ts, dt.datetime):
        d = ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(tz)
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(tz)


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def session_open_after(as_of, n=1, tz=None):
    """The open (09:30 ET) of the n-th regular session that begins strictly after as_of.

    n=1 is "the next open" — the thing the power-hour report has been claiming. Weekday
    calendar only; see the module docstring.
    """
    tz = tz or _zone()
    et = _to_et(as_of, tz)
    if et is None or n < 1:
        return None
    probe, seen = et.date(), 0
    for _ in range(_MAX_WALK):
        if probe.weekday() < 5:
            op = dt.datetime.combine(probe, _OPEN, tzinfo=tz)
            if op > et:
                seen += 1
                if seen >= n:
                    return op
        probe += dt.timedelta(days=1)
    return None


def next_session_open(as_of, tz=None):
    return session_open_after(as_of, 1, tz)


def moment(date_, timing, tz):
    """When the print lands, to the precision that matters for ordering against an open.

    An unknown timing is treated as before the open — the earliest it could be — so the
    gate errs toward refusing. `describe()` still says the time of day is unknown; the
    conservative treatment never becomes a claim.
    """
    if date_ is None or tz is None:
        return None
    t = _AFTER_CLOSE if str(timing or "").lower() == "pm" else _BEFORE_OPEN
    return dt.datetime.combine(date_, t, tzinfo=tz)


def _rows(payload):
    """The result rows out of a raw connector response, a bare {"results": []}, or a list."""
    if isinstance(payload, list):
        return payload, []
    if not isinstance(payload, dict):
        return [], []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = data.get("results")
    nf = data.get("not_found")
    return (rows if isinstance(rows, list) else []), (nf if isinstance(nf, list) else [])


class Feed:
    """The staged earnings calendar, indexed by symbol. Never raises on bad input."""

    def __init__(self, payload=None, path=None, staged=False):
        self.path = path
        self.tz = _zone()
        self.staged = bool(staged)
        self.by_symbol = {}
        self.not_found = set()
        self.rows = 0
        self.dropped = 0
        self.reason = None
        if not self.staged:
            self.reason = f"{FILENAME} was not staged — the earnings gate is dormant"
        elif self.tz is None:
            self.staged = False
            self.reason = ("no America/New_York time zone on this host (install tzdata) — "
                           "the earnings gate stays dormant rather than gate on a guessed clock")
        else:
            self._index(payload)

    # ------------------------------------------------------------------ build
    def _index(self, payload):
        rows, nf = _rows(payload)
        self.not_found = {str(s).upper().strip() for s in nf if s}
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                self.dropped += 1
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            rep = row.get("report")
            d = _date(rep.get("date")) if isinstance(rep, dict) else None
            if not sym or d is None:
                # report: null is a real shape (SNDK 2025 Q2). Dropped, never defaulted.
                self.dropped += 1
                continue
            eps = row.get("eps") if isinstance(row.get("eps"), dict) else {}
            rec = {
                "symbol": sym,
                "date": d.isoformat(),
                "timing": (str(rep.get("timing")).lower()
                           if rep.get("timing") in ("am", "pm", "AM", "PM") else None),
                "confirmed": bool(rep.get("verified")),
                "reported": eps.get("actual") is not None,
                "year": row.get("year"),
                "quarter": row.get("quarter"),
            }
            key = (sym, rec["date"], rec["timing"], rec["year"], rec["quarter"], rec["reported"])
            if key in seen:                    # YYAI appears twice, identically
                continue
            seen.add(key)
            self.by_symbol.setdefault(sym, []).append(rec)
            self.rows += 1

    @property
    def available(self):
        return bool(self.staged and self.tz is not None)

    def __bool__(self):
        return self.available

    # ------------------------------------------------------------------ query
    def lookup(self, symbol, as_of):
        """The next unreported event for `symbol` as of `as_of`.

        Returns a dict whose `status` is one of:
          "unavailable" — no feed staged. Not a statement about the symbol.
          "absent"      — the feed is staged and carries NO row for this symbol. Also not
                          a statement that it has no earnings: the window may simply not
                          reach its next report, or the vendor may not cover it. The board
                          must print this as absent, never as "no earnings".
          "cleared"     — the feed has rows for it and every one is already reported or
                          already past. Nothing ahead inside the staged window.
          "event"       — an event ahead, with date, timing and confirmed.
        """
        if not self.available:
            return {"status": "unavailable", "symbol": symbol, "reason": self.reason}
        sym = str(symbol or "").upper().strip()
        rows = self.by_symbol.get(sym)
        if not rows:
            return {"status": "absent", "symbol": sym,
                    "not_found": sym in self.not_found}
        et = _to_et(as_of, self.tz)
        ahead = []
        for r in rows:
            if r["reported"]:
                continue
            m = moment(_date(r["date"]), r["timing"], self.tz)
            if m is None or (et is not None and m <= et):
                continue
            ahead.append((m, r))
        if not ahead:
            return {"status": "cleared", "symbol": sym}
        # Earliest wins. On the same moment prefer a confirmed date over an estimated one,
        # then the latest fiscal quarter — a stale back-quarter parked on a placeholder
        # date should never outrank the real forward one (GAUZ, MSS, STAI all do this).
        ahead.sort(key=lambda mr: (mr[0], not mr[1]["confirmed"],
                                   -(mr[1]["year"] or 0), -(mr[1]["quarter"] or 0)))
        m, r = ahead[0]
        return dict(r, status="event", moment=m.isoformat(), source="feed")


def describe(info):
    """One phrase naming the date, the session half and whether the company confirmed it.

    This is what reaches the page, the journal and the refusal string, and it is the one
    place the confirmed/estimated distinction is allowed to be rendered — precisely so it
    cannot be dropped somewhere downstream by a caller formatting the date itself.
    """
    if not isinstance(info, dict):
        return "no earnings information"
    st = info.get("status")
    if st == "unavailable":
        return "no earnings feed staged this run"
    if st == "absent":
        return "not in the staged earnings feed (absent, which is not the same as none)"
    if st == "cleared":
        return "nothing scheduled inside the staged window"
    when = {"am": " before the open", "pm": " after the close"}.get(info.get("timing"),
                                                                   " (time of day not given)")
    how = ("confirmed by the company" if info.get("confirmed")
           else "date ESTIMATED, not confirmed by the company")
    return f"{info.get('date')}{when} — {how}"


def attach(feed, symbol, as_of, scan_row=None):
    """The earnings block for one held position or entry candidate, or None.

    The feed is the authority. Where the feed is staged but carries no row for the symbol,
    the scan row's `next_earnings` is used FOR DISPLAY ONLY, marked `source: "scan"` and
    `earnings_confirmed: None` — unknown, because the scan row cannot say. It never gates:
    `scanner.py` writes the same field for a report already delivered (KR on the 2026-09-11
    board carried 2026-09-11, reported that morning) and never carries a timing for a
    forward date — on that board every one of the thirteen populated `next_earnings` values
    had `earnings_timing: null` except the five that had already reported.

    Returns None when there is nothing at all to say, so a run with no feed and a scan
    that carries no earnings column writes exactly the keys it writes today.
    """
    info = feed.lookup(symbol, as_of) if feed is not None else {"status": "unavailable"}
    st = info.get("status")
    if st == "event":
        return {"next_earnings": info["date"], "earnings_timing": info["timing"],
                "earnings_confirmed": info["confirmed"], "earnings_source": "feed",
                "earnings_status": "event", "earnings_note": describe(info)}
    if st == "cleared":
        # The feed KNOWS this name has nothing ahead. It outranks the scan row, which on
        # the 2026-09-11 board said KR: 2026-09-11 for a report already delivered that
        # morning. Falling back here would resurrect a cleared event as a live one.
        return {"next_earnings": None, "earnings_timing": None, "earnings_confirmed": None,
                "earnings_source": "feed", "earnings_status": "cleared",
                "earnings_note": describe(info)}
    row_date = (scan_row or {}).get("next_earnings") if isinstance(scan_row, dict) else None
    if row_date:
        return {"next_earnings": row_date,
                "earnings_timing": (scan_row or {}).get("earnings_timing"),
                "earnings_confirmed": None, "earnings_source": "scan",
                "earnings_status": "scan-only",
                "earnings_note": (f"{row_date} from the scan row — the scan does not record "
                                  "whether this is the next report or the last one, and "
                                  "carries no confirmation")}
    if st == "absent":
        return {"next_earnings": None, "earnings_timing": None, "earnings_confirmed": None,
                "earnings_source": "feed", "earnings_status": "absent",
                "earnings_note": describe(info)}
    return None


def within_gate(feed, symbol, as_of, sessions=GATE_SESSIONS):
    """(gated, info). True when a FEED event lands before the open `sessions` sessions out.

    Only ever True on a feed event — see `attach()` for why a scan row does not gate.
    """
    info = feed.lookup(symbol, as_of) if feed is not None else {"status": "unavailable"}
    if info.get("status") != "event":
        return False, info
    horizon = session_open_after(as_of, max(1, int(sessions)), feed.tz)
    m = _to_et(info.get("moment"), feed.tz)
    if horizon is None or m is None:
        return False, info
    return m < horizon, info


def before_next_open(feed, symbol, as_of):
    """(bool, info) — the narrow question the power-hour report actually asks."""
    return within_gate(feed, symbol, as_of, 1)


def refusal(symbol, info, sessions=GATE_SESSIONS):
    """The entry refusal. `report.classify()` maps any reason naming earnings to
    `earnings_gate`; nothing ahead of that rule in its precedence list matches this text."""
    return (f"earnings gate: {symbol} reports {describe(info)} — inside the "
            f"{sessions}-session earnings window, no new entry into the print")


def hold_policy(desk_rules):
    """A desk's hold_through_earnings setting, normalised. Unknown values fall back to
    holding: an unreadable policy must never sell something."""
    v = str((desk_rules or {}).get("hold_through_earnings") or HOLD_THROUGH_DEFAULT).lower()
    return CLOSE if v in (CLOSE, "flat", "exit", "false", "no") else HOLD


def gate_sessions(desk_rules=None, pm_rules=None):
    """The window, in sessions: the desk's rules first, then PM_RULES, then the default."""
    for src in (desk_rules or {}, pm_rules or {}):
        v = src.get("earnings_gate_days")
        if isinstance(v, (int, float)) and v >= 1:
            return int(v)
    return GATE_SESSIONS


def load(path=FILENAME, base=None, reader=None):
    """The staged feed, or a dormant one. Never raises.

    `reader` is the caller's own loader (pm.py passes its `_load`, which refuses a path
    resolving outside the run directory — M5). Absent, the file is read from `base` or
    $SCAN_DIR.
    """
    payload, staged = None, False
    try:
        if reader is not None:
            payload = reader(path)
            staged = payload is not None
        else:
            root = base or os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
            full = os.path.join(root, path)
            if os.path.exists(full):
                with open(full, encoding="utf-8") as f:
                    payload = json.load(f)
                staged = True
    except (OSError, ValueError):
        payload, staged = None, False
    return Feed(payload, path=path, staged=staged)
