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
"""
import argparse, json, os, sys
from datetime import datetime, timezone

SLOT_ORDER = {"Pre-market": "08:00", "Opening range": "10:00",
              "Midday": "12:30", "Power hour": "15:00"}


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--current")
    ap.add_argument("--entry", required=True)
    ap.add_argument("--today")
    ap.add_argument("--out")
    a = ap.parse_args()

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
