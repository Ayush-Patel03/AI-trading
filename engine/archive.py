"""One identity per scan run — so a scan can never overwrite an earlier one.

Before this, every slot wrote the same four names: scan_data.json, scan_results.json,
scan-desk.html, and one dashboard artifact. Four scans a day meant the 08:00 board only
existed until 10:00, and a re-run or a stale session could quietly erase a good board.
Everything a run produces now carries a run id — `<scan_date>-<slot-slug>` — and the
shared names are kept only as a "latest" convenience copy.

The four scheduled slots deliberately get NO time component in their run id, so a re-run
of a slot REPLACES that slot's snapshot. That matches history.py rule 3: a slot has one
entry per day, not one per attempt. An ad-hoc scan gets its collection time appended, so
two ad-hoc runs an hour apart are two separate records.

What lives where
----------------
  artifact per run   the full board, human-readable, permanent, free (artifacts do not
                     count against the project's knowledge budget)
  claude/scans/*     a COMPACT record per run — scores, pillars, verdicts, the regime,
                     and the artifact URL. Deliberately not the full scan_results.json:
                     at ~150 KB a run, four runs a day would exhaust the project's 2 MB
                     knowledge budget inside a week. The prose reasons live in the
                     artifact; this file is the machine-readable audit trail.
  claude/scan-index.json  one line per run, newest first, so any future session can find
                     any past board without opening every doc.
  claude/scan-history.json  unchanged role: TODAY's score trail only, reset each morning.
                     Safe to reset now precisely because the archive is permanent.

Usage
-----
    python3 archive.py --stamp
        Print this run's id and every file name it owns. Reads scan_results.json.

    python3 archive.py --record [--artifact-url URL] [--live-url URL]
        Write scan-record-<run_id>.json (the compact record) from
        scan_results-<run_id>.json. project_write that to claude/scans/<run_id>.json.

    python3 archive.py --history-entry history_entry.json
        Write the exact entry history.py expects. Removes the hand-built JSON step.

    python3 archive.py --index --current index_current.json [--keep 40] --out index_merged.json
        Merge this run into the archive index, newest first, replacing a same-run_id row
        rather than duplicating it. Prints PRUNE: lines for docs beyond --keep so they can
        be project_delete'd; it never deletes anything itself.

Paths resolve from SCAN_DIR, like every other file in the engine.
"""
import argparse, hashlib, json, os, re, sys
from datetime import datetime

import config

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

# The four scheduled slots. A known slot gets a stable, time-free id so a re-run replaces
# rather than accumulates. Anything else is ad-hoc and gets its time appended.
SLOT_SLUGS = {
    "pre-market": "premarket",
    "premarket": "premarket",
    "opening range": "opening-range",
    "midday": "midday",
    "power hour": "power-hour",
}
SLOT_TIMES = {"premarket": "08:00", "opening-range": "10:00",
              "midday": "12:30", "power-hour": "15:00"}
DEFAULT_KEEP = 40          # 10 trading days of four scans
# Board URLs are identifiers and live in the PRIVATE engine-config.json, never here.
# None is a valid answer: a run without the config republishes nothing rather than
# creating a second rolling board.
# Resolved from the PRIVATE engine-config.json staged into $SCAN_DIR — never a literal
# in this repo. None when the config was not staged, and None is a real answer: a run
# without it republishes nothing rather than forking a second rolling board.
#
# RENDER-01 (2026-09-03): this was a bare `None` that nothing ever resolved, so
# render.py's `archive.LIVE_BOARD_URL` fallback was always None and the run died in
# html.escape(None) AFTER the scan had succeeded. The migration moved the identifier
# out and never wired the lookup back in; the equivalence proof compared pm_state.json
# and could not have caught it.
LIVE_BOARD_URL = config.board_url("scan_desk")


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "adhoc"


def slot_slug(slot):
    return SLOT_SLUGS.get((slot or "").strip().lower(), slugify(slot))


def is_scheduled(slot):
    return (slot or "").strip().lower() in SLOT_SLUGS


def run_id(meta):
    """`<scan_date>-<slot-slug>`, plus `-HHMM` for an ad-hoc slot.

    Deterministic: it never probes the filesystem for a free name, because two sessions
    can run at once and a probe would race.
    """
    date = meta.get("scan_date") or "undated"
    slot = meta.get("slot") or meta.get("session") or "scan"
    slug = slot_slug(slot)
    if is_scheduled(slot):
        return f"{date}-{slug}"
    tm = re.sub(r"[^0-9]", "", meta.get("time") or "")[:4]
    return f"{date}-{slug}" + (f"-{tm}" if tm else "")


def run_label(meta):
    """Human title fragment: 'Pre-market · Mon 31 Aug 2026'."""
    slot = meta.get("slot") or meta.get("session") or "Scan"
    try:
        d = datetime.strptime(meta.get("scan_date", ""), "%Y-%m-%d")
        when = d.strftime("%a %-d %b %Y")
    except (ValueError, TypeError):
        when = meta.get("scan_date") or ""
    return f"{slot} · {when}".strip(" ·")


def title_for(meta):
    """Artifact title for this run's frozen board.

    Distinct per run so the gallery is browsable — 'Scan Desk' alone on four boards a day
    is unnavigable. The live board keeps the bare name.
    """
    slot = meta.get("slot") or meta.get("session") or "Scan"
    try:
        when = datetime.strptime(meta["scan_date"], "%Y-%m-%d").strftime("%-d %b %Y")
    except (ValueError, TypeError, KeyError):
        when = meta.get("scan_date") or ""
    return f"Scan Desk — {slot}, {when}".rstrip(", ")


def stamped(name, rid):
    """'scan-desk.html' + rid -> 'scan-desk-<rid>.html'."""
    root, ext = os.path.splitext(name)
    return f"{root}-{rid}{ext}"


def files_for(rid):
    return {
        "data": stamped("scan_data.json", rid),
        "results": stamped("scan_results.json", rid),
        "board": stamped("scan-desk.html", rid),
        "record": stamped("scan-record.json", rid),
        "doc": f"claude/scans/{rid}.json",
    }


# ---------------------------------------------------------------- compact record
# Kept small on purpose. Everything dropped here (the per-pillar prose reasons, the news
# catalysts, the transcript body, the IPO and insider panels) is already rendered into
# the run's own artifact, which costs the project nothing.
ROW_KEEP = (
    "ticker", "name", "price", "day_change_pct", "setup", "score", "raw_score",
    "normalized_score", "coverage_pct", "missing_pillars", "verdict", "pillars",
    "gics", "sector", "upside_pct", "analyst_rating", "analyst_target",
    "vs_ma50_pct", "vs_ma200_pct", "week52_change_pct", "rel_volume", "rsi_14",
    "atr_pct", "gap_pct", "gap_basis", "ret_20d_pct", "ret_60d_pct",
    "rs_20d_vs_spy", "rs_60d_vs_spy",
    "next_earnings", "earnings_timing", "implied_move_pct",
    "confidence", "score_delta", "is_new", "entered_top5",
)
REGIME_KEEP = ("label", "multiplier", "vix", "notes", "spy", "qqq", "iwm", "breadth")


def record(results, artifact_url=None, live_url=None):
    meta = dict(results.get("meta") or {})
    rows = results.get("results") or []
    rid = run_id(meta)
    reg = results.get("regime") or {}
    return {
        "_readme": ("Compact archive record for one Scan Desk run. The full board, with "
                    "every pillar reason, is the artifact at `artifact_url`. This file is "
                    "the machine-readable trail: scores, pillars, verdicts and the regime."),
        "run_id": rid,
        "run_label": run_label(meta),
        "date": meta.get("scan_date"),
        "slot": meta.get("slot") or meta.get("session"),
        "time": meta.get("time"),
        "artifact_url": artifact_url or meta.get("artifact_url"),
        "live_board_url": (live_url or meta.get("live_board_url")
                           or config.board_url("scan_desk")),
        "stale": bool(meta.get("stale")),
        "minutes_late": meta.get("minutes_late"),
        "coverage_avg": meta.get("coverage_avg"),
        "dropped": meta.get("dropped") or [],
        "data_warnings": meta.get("data_warnings") or [],
        "sources": meta.get("sources") or [],
        "macro_events": meta.get("macro_events") or [],
        "regime": {k: reg.get(k) for k in REGIME_KEEP if reg.get(k) is not None},
        "sector_concentration": results.get("sector_concentration") or {},
        "notable": results.get("notable") or [],
        "avg": round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0,
        "count": len(rows),
        "results": [{k: r[k] for k in ROW_KEEP if k in r} for r in rows],
    }


def history_entry(results, artifact_url=None):
    """Exactly the shape history.py wants, built from the results rather than by hand."""
    meta = results.get("meta") or {}
    rows = results.get("results") or []
    slot = meta.get("slot") or meta.get("session") or "scan"
    rid = run_id(meta)
    return {
        "date": meta.get("scan_date"),
        "slot": slot,
        "time": meta.get("time") or SLOT_TIMES.get(slot_slug(slot), ""),
        "regime_label": (results.get("regime") or {}).get("label"),
        "avg": round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0,
        "top": rows[0]["ticker"] if rows else None,
        "coverage_avg": meta.get("coverage_avg"),
        "run_id": rid,
        "artifact_url": artifact_url or meta.get("artifact_url"),
        "doc": files_for(rid)["doc"],
        "scores": {r["ticker"]: r["score"] for r in rows},
    }


# ---------------------------------------------------------------- index
def index_row(rec):
    return {k: rec.get(k) for k in
            ("run_id", "date", "slot", "time", "run_label", "avg", "count",
             "coverage_avg", "artifact_url")} | {
        "regime_label": (rec.get("regime") or {}).get("label"),
        "top": (rec.get("results") or [{}])[0].get("ticker"),
        "doc": files_for(rec["run_id"])["doc"],
        "stale": bool(rec.get("stale")),
    }


def merge_index(current, row, keep=DEFAULT_KEEP):
    """Newest first, one row per run_id, pruned to `keep`. Returns (doc, pruned_docs).

    Replacing rather than appending on a repeated run_id is what makes a re-run of a slot
    safe: the snapshot artifact, the record doc and this row all key off the same id, so a
    re-run overwrites all three consistently instead of leaving two half-boards behind.
    """
    doc = dict(current) if isinstance(current, dict) else {}
    runs = [r for r in (doc.get("runs") or [])
            if isinstance(r, dict) and r.get("run_id") and r["run_id"] != row["run_id"]]
    runs.append(row)
    runs.sort(key=lambda r: (r.get("date") or "", r.get("time") or "", r.get("run_id") or ""),
              reverse=True)
    pruned = [r["doc"] for r in runs[keep:] if r.get("doc")]
    doc["runs"] = runs[:keep]
    doc["keep"] = keep
    doc["updated"] = row.get("date")
    doc["_readme"] = (
        "Index of every archived Scan Desk run, newest first. Each row points at that "
        "run's permanent dashboard artifact and its compact record under claude/scans/. "
        f"Trimmed to the most recent {keep} runs (~{keep // 4} trading days) to stay inside "
        "the project's knowledge budget; the artifacts themselves are never deleted, so an "
        "older board is still reachable from the artifact gallery by its dated title.")
    doc["_write_protocol"] = (
        "Do not hand-edit. Run engine/archive.py --index with this file as --current and "
        "write back exactly what it returns. It replaces a re-run's row rather than "
        "duplicating it, and prints PRUNE: lines for records that fell past the window.")
    return doc, pruned


# ================================================================ Portfolio Manager
# The Trade Desk has the same problem the Scan Desk had — four runs a day republishing one
# artifact — but it is a book, not a screen, and that changes two things.
#
# 1. `claude/paper-book.json` stays ONE living file. It is state, not a report; the
#    revision / based_on_revision pair and pm.py --check are the concurrency defence, and
#    forking it per run would defeat both. What gets archived is an immutable COPY of each
#    revision under claude/book-history/, so the revision chain is auditable and a clobber
#    is recoverable.
# 2. A snapshot board is published only when the book actually CHANGED — not when the marks
#    moved. Four identical boards a day of a book that did nothing is noise, and noise is
#    how a real UNPROTECTED banner gets scrolled past.
PM_SLOTS = ("pre-market", "opening-range", "midday", "power-hour")
# pm.py speaks in slugs; a title should not. Naive de-slugging turns "pre-market" into
# "Pre market", which is not the name of anything.
PM_SLOT_LABELS = {"pre-market": "Pre-market", "opening-range": "Opening range",
                  "midday": "Midday", "power-hour": "Power hour", "ad-hoc": "Ad-hoc"}
PM_CLOSE_SLOT = "power-hour"
PM_BOARD_URL = config.board_url("trade_desk")   # same as above — see RENDER-01
CLOSE_REASON = "close-of-day record of an open book"
RERUN_REASON = "re-run of a slot whose board is already published"
# Only the swing desk publishes boards (PM.md section 13), and the sentinel prompt is told
# to skip the frozen snapshot and leave freezing to the next decision slot (section 12).
PM_BOARD_DESK = "swing"


def pm_run_id(date, slot, ts=None):
    """`<date>-<slot>`; an ad-hoc run gets its timestamp appended.

    The four scheduled slots keep a stable id so a re-run replaces its own snapshot,
    matching pm.py's own replace-by-run_key journal merge and its equity-curve dedupe.
    """
    slug = slugify(slot)
    if slot in PM_SLOTS:
        return f"{date}-{slug}"
    tm = ""
    if ts:
        digits = re.sub(r"[^0-9]", "", str(ts))
        tm = digits[8:12]            # HHMM out of YYYYMMDDHHMMSS
    return f"{date}-{slug}" + (f"-{tm}" if tm else "")


def pm_slot_label(slot):
    s = (slot or "run").strip()
    return PM_SLOT_LABELS.get(s.lower(), s.replace("-", " ").capitalize())


def pm_run_label(date, slot):
    try:
        when = datetime.strptime(date, "%Y-%m-%d").strftime("%a %-d %b %Y")
    except (ValueError, TypeError):
        when = date or ""
    return f"{pm_slot_label(slot)} · {when}".strip(" ·")


def pm_title_for(date, slot):
    try:
        when = datetime.strptime(date, "%Y-%m-%d").strftime("%-d %b %Y")
    except (ValueError, TypeError):
        when = date or ""
    return f"Trade Desk — {pm_slot_label(slot)}, {when}".rstrip(", ")


def pm_files_for(rid):
    return {
        "book": f"pm_book_next-{rid}.json",
        "state": f"pm_state-{rid}.json",
        "board": f"trade-desk-{rid}.html",
        "doc_book": f"claude/book-history/{rid}.json",
    }


def book_fingerprint(book):
    """A hash of what the book IS, deliberately excluding what it is WORTH.

    Positions, working orders, the halt flag, realised P&L and the closed-trade count are
    in. Prices, market values, unrealised P&L, equity, `revision` and `last_run` are out —
    those move on every run without a decision having been taken, and if they counted, the
    "publish only when the book changed" rule would degrade to "publish always" the moment
    the book held a single position.
    """
    if not isinstance(book, dict):
        return None

    def r(v, dp=6):
        return round(v, dp) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    pos = sorted([
        [p.get("symbol"), r(p.get("shares")), r(p.get("avg_cost"), 4), r(p.get("stop"), 4),
         r(p.get("target"), 4), p.get("stop_basis_kind"), p.get("trim_count") or 0]
        for p in (book.get("positions") or []) if isinstance(p, dict)
    ], key=lambda x: str(x[0]))
    orders = sorted([
        [o.get("symbol"), o.get("side"), r(o.get("shares")), r(o.get("limit_price"), 4)]
        for o in (book.get("working_orders") or []) if isinstance(o, dict)
    ], key=lambda x: (str(x[0]), str(x[1])))
    payload = {
        "positions": pos,
        "working_orders": orders,
        "halted": bool((book.get("day") or {}).get("halted")),
        "realized_pnl": r(book.get("realized_pnl"), 2),
        "closed": len(book.get("closed_trades") or []),
        "mode": book.get("mode"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def publishes_boards(jrn):
    """Does this KIND of run publish a frozen board at all?

    Two runs change the book and are still not board-publishing runs, and until
    2026-09-01 the engine did not know it:

      * the SENTINEL — its prompt is explicitly told to skip the frozen snapshot and let
        the next decision slot freeze the book (PM.md section 12), and
      * any desk other than `swing` — "Desks publish no boards" (section 13).

    should_publish() used to return True for both, so every sentinel and every desk run
    wrote a journal entry claiming `publish_snapshot: true` with `artifact_url: null` —
    which PM.md section 8b defines as "a board nothing can find". Observed on
    2026-09-01#sentinel-1545 and on all five momentum entries, and written up in
    claude/health/2026-09-01-midday-pm-notes.md.

    The REASONS are unaffected: this gates publication only, so `changed`, the book
    fingerprint and the book-history snapshot still record a sentinel fill exactly as
    before. Getting that wrong would have stopped archiving real book revisions.
    """
    if jrn.get("sentinel") or jrn.get("slot") == "sentinel":
        return False
    desk = jrn.get("desk")
    return desk in (None, "", PM_BOARD_DESK)


def should_publish(jrn, book, prev_entry=None):
    """(publish?, reasons). Reasons are the audit trail for why a board exists.

    A run publishes when something happened to the book, or when it is the close-of-day run
    on a book that holds something — so a day with an open position always leaves one board
    behind even if no rule fired.
    """
    reasons = []
    if jrn.get("decisions"):
        n = len(jrn["decisions"])
        reasons.append(f"{n} decision{'s' if n != 1 else ''} taken")
    if jrn.get("warnings"):
        reasons.append("warnings raised")

    now_fp = jrn.get("book_fingerprint") or book_fingerprint(book)
    prev_fp = (prev_entry or {}).get("book_fingerprint")
    has_book = bool((book.get("positions") or []) or (book.get("working_orders") or [])
                    or (book.get("closed_trades") or []))
    if prev_fp is None:
        # No comparable predecessor: either the first run ever, or a journal entry written
        # before fingerprints existed. Do not manufacture a "change" out of an empty book —
        # that would publish a board of nothing on the migration run.
        if has_book:
            reasons.append("no prior fingerprint to compare against")
    elif prev_fp != now_fp:
        reasons.append("book state changed")

    changed = bool(reasons)
    if not changed and jrn.get("slot") == PM_CLOSE_SLOT and has_book:
        return publishes_boards(jrn), [CLOSE_REASON]
    if changed and not publishes_boards(jrn):
        # The reasons stand — they drive `changed`, the book snapshot and the journal's
        # change_reasons. Only the publish flag is withheld.
        return False, reasons
    return changed, reasons


# ---------------------------------------------------------------- CLI
def _load(path):
    if not path or path == "/dev/null" or not os.path.exists(path):
        return {}
    with open(path) as fh:
        text = fh.read().strip()
    return json.loads(text) if text else {}


def _results(path=None):
    p = path or os.path.join(BASE, "scan_results.json")
    if not os.path.exists(p):
        raise SystemExit(f"REFUSED: {p} does not exist. Run scanner.py first.")
    return json.load(open(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", help="defaults to $SCAN_DIR/scan_results.json")
    ap.add_argument("--stamp", action="store_true")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--history-entry", metavar="OUT")
    ap.add_argument("--index", action="store_true")
    ap.add_argument("--current")
    ap.add_argument("--out")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--artifact-url")
    ap.add_argument("--live-url")
    a = ap.parse_args()

    if not (a.stamp or a.record or a.history_entry or a.index):
        ap.error("nothing to do: pass --stamp, --record, --history-entry or --index")

    res = _results(a.results)
    meta = res.get("meta") or {}
    rid = run_id(meta)
    f = files_for(rid)

    if a.stamp:
        print(f"run_id      {rid}")
        print(f"run_label   {run_label(meta)}")
        for k in ("data", "results", "board", "record"):
            print(f"{k:<11} {f[k]}")
        print(f"project doc {f['doc']}")

    if a.record:
        rec = record(res, a.artifact_url, a.live_url)
        out = a.out or os.path.join(BASE, f["record"])
        # Written compact, not pretty. Four of these a day for ten days sit inside the
        # project's 2 MB knowledge budget only because they are not indented; indent=2
        # costs about 40% on a file this nested, for readability nothing actually reads.
        json.dump(rec, open(out, "w"), separators=(",", ":"))
        size = os.path.getsize(out)
        print(f"record -> {out}  ({size:,} bytes, {rec['count']} rows)", file=sys.stderr)
        if size > 40_000:
            print(f"WARNING: {size:,} bytes is large for one record. At four scans a day "
                  f"the archive would reach {size * DEFAULT_KEEP // 1024:,} KB against a "
                  "2 MB project budget. Trim ROW_KEEP before this becomes a problem.",
                  file=sys.stderr)
        if not rec["artifact_url"]:
            print("NOTE: no --artifact-url given. Publish the snapshot board first, then "
                  "re-run --record with its URL so the archive can find it.", file=sys.stderr)

    if a.history_entry:
        entry = history_entry(res, a.artifact_url)
        json.dump(entry, open(a.history_entry, "w"), indent=2)
        print(f"history entry -> {a.history_entry}  ({entry['slot']}, {len(entry['scores'])} names)",
              file=sys.stderr)

    if a.index:
        # The index row's artifact_url comes from --artifact-url, and every scan prompt
        # passes it to --record and then omits it here. That is why scan-index.json rows
        # carried `artifact_url: null` for runs whose board had published perfectly well
        # (observed 2026-09-01 on the opening-range and midday rows). Fall back to the
        # record file this run already wrote, which has the URL, before giving up.
        url = a.artifact_url
        if not url:
            prior = _load(os.path.join(BASE, f["record"]))
            url = prior.get("artifact_url")
            if url:
                print(f"index: no --artifact-url given; reusing the URL from {f['record']}",
                      file=sys.stderr)
        rec = record(res, url, a.live_url)
        if not rec["artifact_url"]:
            print("WARNING: this index row will carry artifact_url: null — the board it "
                  "points at will be unfindable from the archive. Publish first, then pass "
                  "--artifact-url.", file=sys.stderr)
        doc, pruned = merge_index(_load(a.current), index_row(rec), a.keep)
        text = json.dumps(doc, indent=2) + "\n"
        if a.out:
            open(a.out, "w").write(text)
        else:
            sys.stdout.write(text)
        print(f"index: {len(doc['runs'])} run(s), newest {doc['runs'][0]['run_id']}",
              file=sys.stderr)
        for p in pruned:
            print(f"PRUNE: {p}", file=sys.stderr)
        # The project's knowledge budget is a hard 2 MB shared with the engine and runbook.
        # A silently growing archive would eat it, so say the number out loud every run.
        est = os.path.getsize(os.path.join(BASE, f["record"])) * len(doc["runs"]) \
            if os.path.exists(os.path.join(BASE, f["record"])) else 0
        if est:
            print(f"archive is roughly {est // 1024:,} KB across {len(doc['runs'])} records "
                  f"(project budget 2 MB). Lower --keep if that climbs past ~800 KB.",
                  file=sys.stderr)


if __name__ == "__main__":
    main()
