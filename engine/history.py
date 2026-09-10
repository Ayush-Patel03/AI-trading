"""Deterministic merge for claude/scan-history.json.

The history file is written by four scheduled sessions a day that can overlap, and
a session can survive for DAYS and resume: on 2026-08-31 a scan fired the previous
Friday finished at 16:43 UTC and wrote Friday-dated scores over Monday's file, five
minutes after it had been reset — and earlier the same morning another stale session
destroyed that day's pre-market entry twelve seconds after it was written.

Telling an agent to "merge carefully" does not survive that. This does the merge as a
pure function instead, so the only judgement left is which file to write back.

This file holds TODAY only, and resets every morning. That is safe as of 2026-08-31
because it is no longer the only record: engine/archive.py writes a permanent per-run
record under claude/scans/ and a row in claude/scan-index.json, and each run publishes
its own dashboard artifact. What resets here is the intraday score trail, not history.

Entries may carry extra keys — `run_id`, `artifact_url`, `doc` — and they are preserved
untouched. `artifact_url` is what lets the next scan's board turn each completed slot in
the tape into a link to that slot's own frozen board. Build the entry with
`archive.py --history-entry` rather than by hand.

Usage
-----
    python3 history.py --current current.json --entry entry.json [--today YYYY-MM-DD] \
                       [--out merged.json]

  --current  what project_read gave you for claude/scan-history.json.
             Pass /dev/null or omit if the doc does not exist yet.
  --entry    this scan's entry: {date, slot, time, regime_label, avg, top,
             coverage_avg, scores:{TICKER: score}}
  --today    defaults to the system date in US/Eastern terms (UTC date is close
             enough for a market-hours scan; pass it explicitly to be sure).

Writes the merged document to --out (default stdout) and prints a one-line summary
to stderr. Exit codes:
    0  merged, safe to write back
    2  refused - the entry is not for today, or is malformed

Rules
-----
1. An entry whose date is not `today` is REFUSED. A stale session must never write.
2. If the stored document is for an earlier day, the day starts fresh: scans=[] and
   date=today, before the entry is added. Yesterday's tape is not carried over.
3. Entries are keyed by slot. Re-running a slot replaces that slot's entry rather
   than appending a duplicate.
4. Entries are kept sorted by time.
5. If an entry for a LATER slot already exists, this run is the late one: it is
   still recorded, flagged `_late: true`, and it does not disturb the later entry.

The followed set (S-03)
-----------------------
`<archive_dir>/followed.json` is the survivorship-free roster: every symbol that has ever
appeared in a scan snapshot, from the first archived slot, with the date it entered and
the date its forward-return horizon ends. A name that drops out of the universe — stops
scoring, falls off the screen, delists — stays here until its horizon has run, so the
bar fetch that feeds validate.py / backtest.py keeps pulling its prices and the record
keeps its losers. Bars are the only thing validate.py needs for a symbol that is no longer
in a record (it observes each (date, ticker) pair from the records themselves), so the
seam is the bar-fetching prompt: `history.py --followed --archive <dir>` prints the open
list for it.

    {symbol: {symbol, first_seen, first_slot, last_seen, last_slot, horizon_end_date,
              status: "open" | "closed", runs}}

The horizon is HORIZON_SESSIONS business days after `last_seen` (weekdays; no exchange
holiday calendar, so a holiday week closes one weekday early — harmless, the bar fetch
overshoots by design). A symbol is closed once `as_of` is past its horizon_end_date and
reopens, first_seen intact, if it is seen again.

    python3 history.py --followed --archive <dir> [--as-of YYYY-MM-DD] [--all] [--json]
"""
import argparse, json, os, sys
from datetime import datetime, timedelta, timezone

SLOT_ORDER = {"Pre-market": "08:00", "Opening range": "10:00",
              "Midday": "12:30", "Power hour": "15:00"}
FOLLOWED_FILE = "followed.json"
HORIZON_SESSIONS = 20
FOLLOWED_README = (
    "The followed set: every symbol that has appeared in a scan snapshot since the first "
    "archived slot, with the date it entered and the business day its forward-return horizon "
    "ends. Open symbols need bars fetched whether or not they are still in the universe. "
    "Written by engine/history.py from engine/snapshots.py; do not hand-edit.")


def _load(path):
    if not path or path == "/dev/null" or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return {}
    return json.loads(text)


def _time_of(entry):
    """Sort key. Prefer the stated time, fall back to the slot's canonical time."""
    t = entry.get("time") or SLOT_ORDER.get(entry.get("slot"), "")
    return (t or "99:99", entry.get("slot") or "")


def merge(current, entry, today):
    if not isinstance(entry, dict):
        raise ValueError("entry must be a JSON object")
    for key in ("slot", "scores"):
        if not entry.get(key):
            raise ValueError(f"entry is missing required key: {key}")

    entry = dict(entry)
    entry.setdefault("date", today)
    entry.setdefault("time", SLOT_ORDER.get(entry["slot"], ""))

    if entry["date"] != today:
        raise SystemExit(
            f"REFUSED: entry is dated {entry['date']} but today is {today}. "
            "A session that outlived its trading day must not write history. "
            "Report the run as stale and leave the file alone."
        )

    doc = dict(current) if isinstance(current, dict) else {}
    fresh_day = doc.get("date") != today
    scans = [] if fresh_day else list(doc.get("scans") or [])

    # Drop anything not dated today, whatever the header said.
    scans = [s for s in scans if isinstance(s, dict) and s.get("date") == today]

    later = [s for s in scans if _time_of(s) > _time_of(entry)]
    if later:
        entry["_late"] = True

    scans = [s for s in scans if s.get("slot") != entry["slot"]]
    scans.append(entry)
    scans.sort(key=_time_of)

    doc["date"] = today
    doc["scans"] = scans
    doc.setdefault("_readme", (
        "Intraday score history for the Scan Desk dashboard. Each scheduled scan reads "
        "this file, uses `scans` as its history input if `date` matches today, then writes "
        "the merged array back. Keep only the current day."))
    doc["_write_protocol"] = (
        "Do not hand-merge this file. project_read it, run engine/history.py with your "
        "entry, and write back exactly what it returns. It refuses stale-dated entries, "
        "resets on a new day, replaces rather than duplicates a re-run slot, keeps entries "
        "in time order, and flags a late run without disturbing a later slot's entry. "
        "This file covers TODAY only; the permanent per-run archive is claude/scans/ plus "
        "claude/scan-index.json, and each run's own dashboard artifact.")
    doc["_entry_shape"] = ("{date, slot, time, regime_label, avg, top, coverage_avg, "
                           "run_id, artifact_url, doc, scores:{TICKER: score}}  - build it "
                           "with engine/archive.py --history-entry, not by hand")
    doc.pop("_note", None)
    return doc, fresh_day, bool(later)


# ---------------------------------------------------------------- the followed set
def _date(s):
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def business_days_after(start, n):
    """The date `n` weekdays after `start` (a date or ISO string). n=0 is start itself."""
    d = _date(start) if not hasattr(start, "isoformat") else start
    left = int(n)
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def followed_path(archive_dir):
    return os.path.join(archive_dir, FOLLOWED_FILE)


def load_followed(archive_dir):
    doc = _load(followed_path(archive_dir))
    if not isinstance(doc, dict):
        doc = {}
    doc.setdefault("_readme", FOLLOWED_README)
    doc.setdefault("horizon_sessions", HORIZON_SESSIONS)
    syms = doc.get("symbols")
    doc["symbols"] = syms if isinstance(syms, dict) else {}
    return doc


def close_expired(doc, as_of):
    """Mark every open symbol whose horizon_end_date is before `as_of` as closed."""
    as_of_d = _date(as_of)
    closed = []
    for sym, rec in doc.get("symbols", {}).items():
        if rec.get("status") != "open":
            continue
        try:
            end = _date(rec.get("horizon_end_date"))
        except (ValueError, TypeError):
            continue
        if as_of_d > end:
            rec["status"] = "closed"
            rec["closed_on"] = as_of_d.isoformat()
            closed.append(sym)
    return closed


def update_followed(archive_dir, symbols, as_of, slot=None, run_id=None, write=True):
    """Fold this slot's symbols into followed.json. Returns (doc, added, closed).

    A symbol already open keeps its first_seen; last_seen and the horizon move forward
    to this slot. A closed symbol seen again reopens, first_seen intact. Every open
    symbol past its horizon is closed on the way through — the roster is maintained on
    every scan, never by a separate sweep that might not run.
    """
    if not as_of:
        raise ValueError("update_followed needs an as_of date")
    as_of_d = _date(as_of)
    doc = load_followed(archive_dir)
    horizon = int(doc.get("horizon_sessions") or HORIZON_SESSIONS)
    end = business_days_after(as_of_d, horizon).isoformat()
    syms = doc["symbols"]
    added = []
    for s in symbols or []:
        sym = str(s or "").upper().strip()
        if not sym:
            continue
        rec = syms.get(sym)
        if not isinstance(rec, dict):
            rec = {"symbol": sym, "first_seen": as_of_d.isoformat(), "first_slot": slot,
                   "first_run_id": run_id, "runs": 0}
            syms[sym] = rec
            added.append(sym)
        elif rec.get("status") == "closed":
            rec.pop("closed_on", None)
            rec["reopened_on"] = as_of_d.isoformat()
        # A late or re-run slot must not pull the horizon backwards.
        if not rec.get("last_seen") or rec["last_seen"] <= as_of_d.isoformat():
            rec["last_seen"] = as_of_d.isoformat()
            rec["last_slot"] = slot
            rec["horizon_end_date"] = end
        rec["status"] = "open"
        rec["runs"] = int(rec.get("runs") or 0) + 1
    closed = close_expired(doc, as_of_d)
    doc["symbols"] = dict(sorted(syms.items()))
    doc["updated"] = as_of_d.isoformat()
    doc["last_run_id"] = run_id
    doc["open"] = sum(1 for r in doc["symbols"].values() if r.get("status") == "open")
    if write:
        os.makedirs(archive_dir, exist_ok=True)
        tmp = followed_path(archive_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(tmp, followed_path(archive_dir))
    return doc, added, closed


def followed_symbols(archive_dir, as_of=None, status="open"):
    """Sorted symbols whose horizon has not run out at `as_of` (default: every open one).

    Read-only: it evaluates the horizon against `as_of` without writing the file, so a
    bar-fetching prompt run days after the last scan still gets the right list.
    """
    doc = load_followed(archive_dir)
    if as_of:
        close_expired(doc, as_of)
    if status in (None, "", "all"):
        return sorted(doc["symbols"])
    return sorted(s for s, r in doc["symbols"].items() if r.get("status") == status)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--current")
    ap.add_argument("--entry", help="this scan's entry (required unless --followed)")
    ap.add_argument("--today")
    ap.add_argument("--out")
    ap.add_argument("--followed", action="store_true",
                    help="print the followed set from --archive instead of merging history")
    ap.add_argument("--archive", help="archive dir holding followed.json")
    ap.add_argument("--as-of", help="evaluate horizons at this date (default: today)")
    ap.add_argument("--all", action="store_true", help="closed symbols too")
    ap.add_argument("--json", action="store_true", help="print the whole document")
    a = ap.parse_args()

    if a.followed:
        if not a.archive:
            ap.error("--followed needs --archive <dir>")
        as_of = a.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if a.json:
            doc = load_followed(a.archive)
            close_expired(doc, as_of)
            sys.stdout.write(json.dumps(doc, indent=2) + "\n")
            return
        syms = followed_symbols(a.archive, as_of, status="all" if a.all else "open")
        for s in syms:
            print(s)
        print(f"{len(syms)} {'symbol' if a.all else 'open symbol'}(s) followed as of {as_of}",
              file=sys.stderr)
        return
    if not a.entry:
        ap.error("--entry is required (or pass --followed)")

    today = a.today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        doc, fresh, late = merge(_load(a.current), _load(a.entry), today)
    except SystemExit as ex:
        print(ex, file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, json.JSONDecodeError) as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        raise SystemExit(2)

    out = json.dumps(doc, indent=2) + "\n"
    if a.out:
        open(a.out, "w", encoding="utf-8").write(out)
    else:
        sys.stdout.write(out)

    slots = ", ".join(f"{s['slot']}{' (late)' if s.get('_late') else ''}" for s in doc["scans"])
    note = " [new trading day, tape reset]" if fresh else ""
    note += " [recorded as LATE - a later slot was already present]" if late else ""
    print(f"merged {len(doc['scans'])} slot(s) for {today}: {slots}{note}", file=sys.stderr)


if __name__ == "__main__":
    main()
