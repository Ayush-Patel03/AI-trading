"""veto.py — the deny-list feed: activist short reports, negative news, trading halts (P-03).

WHY. Appendix G of the 2026-09-10 research synthesis: a name hit by an activist short
report loses about 7% over the surrounding ±20 sessions and about 10% by day 100, and the
drift does not reverse. A high-severity negative headline (fraud allegation, restatement,
regulatory action, guidance withdrawal) carries the same shape over a shorter window. The
scoring model cannot see either — the catalyst pillar counts news items, it does not read
them — so a name can score 80 on the morning its auditor resigns. This module is the
override that says no.

WHAT IT IS NOT. It is not a scoring input. `scanner.py` scores every row exactly as before
and then, in a separate pass that runs only when `veto.json` was staged, overrides the
verdict of a vetoed row to "Avoid" and says why. Without the file the scan is byte-identical
to a scan that never heard of this module (the golden test in tests/test_scanner.py pins
that). `pm.py` refuses a new entry on a vetoed name with a journaled `veto: …` reason, and
flags a HELD name that picks up a fresh short report for review — a journal warning and
`review: "short-report"` on the position, never an automatic exit. A short report is an
argument, not a price; the manager's stop is what sells.

THE STAGED FILE — $SCAN_DIR/veto.json, written by the scheduled task from the sources in
docs/DATA.md ("Veto sources"):

    {
      "as_of": "2026-09-10",                       # optional, informational
      "short_reports": [{"symbol": "XYZ", "publisher": "Hindenburg Research",
                         "date": "2026-09-08", "url": "https://…", "title": "…"}],
      "negative_news": [{"symbol": "XYZ", "date": "2026-09-09", "headline": "…",
                         "source": "get_equity_news", "severity": "high" | "medium"}],
      "halts": [{"symbol": "XYZ", "date": "2026-09-10", "reason": "LULD / news pending"}]
    }

Every list is optional; a missing list is empty. Symbols are upper-cased on load. Dates
are YYYY-MM-DD (an ISO timestamp is truncated to its date). Publishers worth staging are
listed in docs/veto-publishers.md — a hand-maintained list, not a feed.

THE RULES (`RULES` below; a caller may pass an override dict to check()):

    short report on the symbol within the last `short_report_sessions` (20) sessions
        → veto, and `review` (the held-name flag)
    negative news with severity in `negative_news_severities` ("high") within the last
    `negative_news_sessions` (5) sessions
        → veto. Medium severity is reported in `notes`, never a veto.
    a halt dated today
        → veto

"Sessions" are weekdays: the module has no exchange calendar, so a market holiday counts
as a session and the window is at most a day or two shorter in real sessions than its
name says — the conservative direction for a deny list is a LONGER window, so the
constant is 20 and the docs say "20 weekdays". A future-dated item (after `as_of`) is
ignored: the run cannot know it yet, and a typo must not veto a name for a month.

API
    load(run_dir) -> feed dict or None            (None = not staged, or unreadable)
    check(symbol, as_of, feed, rules=None) -> {"veto", "reasons", "review", "notes"}
    apply_to_rows(rows, as_of, feed, rules=None) -> [tickers overridden]   (scanner.py)
"""
import json
import os
from datetime import date, datetime, timedelta

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
FEED_FILE = "veto.json"
LISTS = ("short_reports", "negative_news", "halts")

RULES = {
    "short_report_sessions": 20,        # Appendix G: −7% over ±20 sessions, −10% by day 100
    "negative_news_sessions": 5,
    "negative_news_severities": ("high",),
}

REVIEW_FLAG = "short-report"


# ---------------------------------------------------------------- dates
def _date(v):
    """A date from YYYY-MM-DD, an ISO timestamp, or a date object. None when unparseable."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def sessions_between(a, b):
    """Weekdays strictly after `a` up to and including `b`. Negative when b < a.
    sessions_between(Fri, Mon) == 1; sessions_between(d, d) == 0."""
    if b < a:
        return -sessions_between(b, a)
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ---------------------------------------------------------------- feed
def normalise(raw):
    """The feed with every list present, every symbol upper-cased and every row a dict.
    Rows with no symbol or no parseable date are dropped, and counted in _meta.dropped."""
    if not isinstance(raw, dict):
        return None
    out = {k: [] for k in LISTS}
    dropped = 0
    for k in LISTS:
        for row in raw.get(k) or []:
            if not isinstance(row, dict) or not row.get("symbol") or _date(row.get("date")) is None:
                dropped += 1
                continue
            r = dict(row)
            r["symbol"] = str(row["symbol"]).upper().strip()
            r["date"] = _date(row["date"]).isoformat()
            out[k].append(r)
    out["as_of"] = raw.get("as_of")
    out["_meta"] = {"dropped": dropped,
                    "counts": {k: len(out[k]) for k in LISTS}}
    return out


def load(run_dir=None, name=FEED_FILE):
    """The staged feed from <run_dir>/veto.json, normalised, or None when it is not there
    or does not parse. Absent is the normal case on a run that staged no veto sources, and
    absent means NO override anywhere — never an empty feed pretending to have looked."""
    p = os.path.join(run_dir or BASE, name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    feed = normalise(raw)
    if feed is not None:
        feed["_meta"]["path"] = p
    return feed


# ---------------------------------------------------------------- the check
def check(symbol, as_of, feed, rules=None):
    """{"veto": bool, "reasons": [...], "review": bool, "notes": [...]} for one symbol.

    `as_of` is the run's date. `feed` is what load() returned; None or empty gives a
    clean answer. `review` is True on a short report inside the window — the flag a held
    name carries — and is independent of `veto` only in name: today they coincide."""
    r = dict(RULES, **(rules or {}))
    out = {"veto": False, "reasons": [], "review": False, "notes": []}
    if not feed or not symbol:
        return out
    sym = str(symbol).upper().strip()
    today = _date(as_of)
    if today is None:
        return out

    for row in feed.get("short_reports") or []:
        if row["symbol"] != sym:
            continue
        d = _date(row["date"])
        n = sessions_between(d, today)
        if n < 0:
            continue                       # future-dated: not known yet
        if n <= r["short_report_sessions"]:
            who = row.get("publisher") or "unnamed publisher"
            title = row.get("title")
            out["veto"] = True
            out["review"] = True
            out["reasons"].append(
                f"short report by {who} on {row['date']} ({n} session(s) ago"
                f"{', ' + str(title)[:80] if title else ''})")

    for row in feed.get("negative_news") or []:
        if row["symbol"] != sym:
            continue
        d = _date(row["date"])
        n = sessions_between(d, today)
        if n < 0 or n > r["negative_news_sessions"]:
            continue
        sev = str(row.get("severity") or "").lower()
        head = str(row.get("headline") or "")[:80]
        src = row.get("source") or "news"
        text = f"{sev or 'unrated'}-severity negative news on {row['date']} via {src}: {head}"
        if sev in r["negative_news_severities"]:
            out["veto"] = True
            out["reasons"].append(text)
        else:
            out["notes"].append(text)

    for row in feed.get("halts") or []:
        if row["symbol"] != sym:
            continue
        if _date(row["date"]) == today:
            out["veto"] = True
            out["reasons"].append(f"trading halt today ({row.get('reason') or 'no reason given'})")
    return out


def apply_to_rows(rows, as_of, feed, rules=None):
    """scanner.py's override pass. Marks EVERY row `veto: False|True` (so the archive can
    tell "checked, clean" from "never checked"), and on a vetoed row overrides the verdict
    to Avoid, keeps the scored verdict as `pre_veto_verdict`, and lists the reasons.
    Scores and pillars are untouched. Returns the tickers overridden, in row order.
    Does nothing at all when `feed` is None — the caller must not call it then either,
    but a None feed is safe."""
    hit = []
    if feed is None:
        return hit
    for row in rows:
        v = check(row.get("ticker"), as_of, feed, rules)
        row["veto"] = v["veto"]
        row["veto_reasons"] = v["reasons"]
        if v["notes"]:
            row["veto_notes"] = v["notes"]
        if v["veto"]:
            row["pre_veto_verdict"] = row.get("verdict")
            row["verdict"] = "Avoid"
            row["verdict_note"] = "VETO — " + "; ".join(v["reasons"]) + (
                f" (scored {row['pre_veto_verdict']}; the score is unchanged, the verdict "
                "is overridden)")
            hit.append(row.get("ticker"))
    return hit
