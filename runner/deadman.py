"""deadman.py — the dead-man's switch (K-05). Independent of run.py on purpose.

    python deadman.py --state <dir> [--now ISO] [--max-missed 2] [--push] [--slots slots.json]

run.py writes `<state>/health/heartbeat.json` on every invocation. This program, run by a
DIFFERENT scheduled task on a different runtime (docs/runner/deadman-task.md), asks one
question: how many expected sentinel / PM runs have gone by during market hours since the
runner last said anything? When that count reaches `--max-missed` the box is presumed dead
and the switch trips:

  (a) `<state>/health/deadman.json` — {tripped: true, ts, missed, last_heartbeat, ...}
  (b) for every active desk's book: every working paper BUY is cancelled, every open
      position is stamped `protective_stop: {level, placed_at, reason: "deadman"}` where
      the level is the position's own stop or, failing that, entry − 2.5 × ATR(14) — in
      the PAPER ledger only — and a `deadman` entry is appended to the desk's journal;
  (c) an `aborted: true` coverage row names the trip;
  (d) one commit in the state repo (no push unless --push).

Idempotent: while tripped, a second run changes nothing. A heartbeat newer than the trip
clears `deadman.json` (`tripped: false`, `cleared_at`) on the next run; the stamps stay on
the positions as the record of what the switch did.

It does NOT import run.py and takes no lock: a runner that is broken enough to need the
switch is a runner this program must not depend on. A live `.runner.lock` (younger than
`lock_stale_min`) counts as a heartbeat — a run is in progress, the box is not dead.

The LIVE behaviour — placing broker-resident stop-limit orders at the stamped levels — is
specified in docs/PM.md ("Dead-man's switch") behind the two-key live mode and is not
implemented here. This program never touches an order tool.

Stdlib only. Exit 0 not tripped (or already tripped, nothing new), 3 tripped on this run,
2 on an error.
"""
import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

DEADMAN_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
SLOTS_PATH = HERE / "slots.json"
PM_SLOTS = ("pre-market", "opening-range", "midday", "power-hour")
DEFAULT_SENTINEL_TIMES = ["09:35", "10:35", "11:35", "12:35", "13:35", "14:35", "15:35"]
ATR_MULT = 2.5
GRACE_MIN = 20          # a run may land this long after its nominal time
EXIT_TRIPPED = 3


# ------------------------------------------------------------------ helpers (deliberately not run.py's)
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(json.dumps(obj, indent=2).encode("utf-8"))


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(ts):
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s):
    t = dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def git(state, *args):
    exe = os.environ.get("RUNNER_GIT", "git")
    return subprocess.run([exe, "-C", str(state), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ the schedule
def expected_times(slots):
    """[(HH, MM)] of every sentinel and PM run on a trading day, from slots.json."""
    sl = (slots or {}).get("slots") or {}
    times = list((sl.get("sentinel") or {}).get("times_et") or DEFAULT_SENTINEL_TIMES)
    for name in PM_SLOTS:
        t = (sl.get(name) or {}).get("nominal_et")
        if t:
            times.append(t)
    out = []
    for t in times:
        h, m = str(t).split(":")
        out.append((int(h), int(m)))
    return sorted(set(out))


def expected_today(slots, now):
    """Aware datetimes of today's (ET) expected sentinel/PM runs; [] off a weekday."""
    tz = _tz((slots or {}).get("timezone", "America/New_York")) or dt.timezone.utc
    et = now.astimezone(tz)
    if et.weekday() >= 5:
        return []
    return [dt.datetime(et.year, et.month, et.day, h, m, tzinfo=tz) for h, m in expected_times(slots)]


def market_hours(slots, now, grace_min=GRACE_MIN):
    """From the first expected run to grace past the last, on a weekday."""
    ev = expected_today(slots, now)
    return bool(ev) and ev[0] <= now <= ev[-1] + dt.timedelta(minutes=grace_min)


def missed_runs(slots, last_hb, now, grace_min=GRACE_MIN):
    """Today's expected runs that fell due (nominal + grace) after the last heartbeat."""
    due = now - dt.timedelta(minutes=grace_min)
    return [e for e in expected_today(slots, now) if e <= due and (last_hb is None or e > last_hb)]


# ------------------------------------------------------------------ the heartbeat
def last_heartbeat(state, slots):
    """(ts, source) — the newest of health/heartbeat.json and a live .runner.lock."""
    hb = load_json(Path(state) / "health" / "heartbeat.json") or {}
    best, src = None, None
    try:
        best, src = parse_iso(hb["ts"]), "heartbeat"
    except (KeyError, ValueError, TypeError):
        pass
    lock = load_json(Path(state) / ".runner.lock") or {}
    try:
        started = parse_iso(lock["started"])
        stale = float(((slots or {}).get("defaults") or {}).get("lock_stale_min") or 45)
        if (utc_now() - started).total_seconds() / 60 <= stale and (best is None or started > best):
            best, src = started, "lock"
    except (KeyError, ValueError, TypeError):
        pass
    return best, src


# ------------------------------------------------------------------ the paper ledger
def protective_level(pos):
    """The position's own stop, else entry − 2.5 × ATR(14), else None (recorded as such)."""
    stop = pos.get("stop")
    if isinstance(stop, (int, float)) and not isinstance(stop, bool) and stop > 0:
        return round(float(stop), 2), "current stop"
    atr = pos.get("atr_14")
    cost = pos.get("avg_cost")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (atr, cost)) and atr > 0:
        return round(float(cost) - ATR_MULT * float(atr), 2), f"entry - {ATR_MULT}x ATR(14)"
    return None, "no stop and no ATR on the position"


def stamp_book(book, ts, missed, desk):
    """Cancel working buys, stamp every open position; returns (changed, journal_entry)."""
    date = ts[:10]
    entry = {"ts": ts, "date": date, "slot": "deadman", "run_key": f"{date}#deadman-{ts[11:13]}{ts[14:16]}",
             "mode": book.get("mode", "paper"), "desk": desk, "deadman": True, "sentinel": False,
             "decisions": [], "skipped": [], "warnings": [], "missed_heartbeats": missed}
    changed = False
    for o in book.get("working_orders") or []:
        if o.get("side") == "buy" and o.get("status") == "working":
            o["status"] = "cancelled"
            o["closed"] = ts
            o["cancel_reason"] = "deadman"
            changed = True
            entry["decisions"].append({"action": "cancel", "symbol": o.get("symbol"),
                                       "shares": o.get("shares"), "price": o.get("limit_price"),
                                       "reason": "dead-man's switch",
                                       "detail": f"runner silent for {missed} expected run(s); "
                                                 "no entry may rest while nobody is watching"})
    book["working_orders"] = [o for o in (book.get("working_orders") or []) if o.get("status") == "working"]
    for p in book.get("positions") or []:
        if isinstance(p.get("protective_stop"), dict) and p["protective_stop"].get("reason") == "deadman":
            continue        # already stamped by an earlier trip; keep the first record
        level, basis = protective_level(p)
        p["protective_stop"] = {"level": level, "placed_at": ts, "reason": "deadman", "basis": basis,
                                "paper": True}
        changed = True
        entry["decisions"].append({"action": "protective-stop", "symbol": p.get("symbol"),
                                   "shares": p.get("shares"), "price": level,
                                   "reason": "dead-man's switch",
                                   "detail": f"protective stop {level} ({basis}) recorded on the paper "
                                             "ledger; live mode would place a broker-resident stop-limit"})
        if level is None:
            entry["warnings"].append(f"UNPROTECTED: {p.get('symbol')} — no stop and no ATR; the "
                                     "dead-man's switch could not derive a level")
    if not book.get("positions") and not entry["decisions"]:
        entry["warnings"].append("flat book — nothing to protect")
    entry["positions"] = len(book.get("positions") or [])
    entry["working_orders"] = len(book.get("working_orders") or [])
    if changed:
        book["based_on_revision"] = book.get("revision", 0)
        book["revision"] = book.get("revision", 0) + 1
        book["last_run"] = ts
    return changed, entry


def append_journal(path, entry):
    journal = load_json(path, {"entries": []}) or {"entries": []}
    entries = [e for e in (journal.get("entries") or []) if e.get("run_key") != entry["run_key"]]
    entries.append(entry)
    journal["entries"] = entries[-500:]
    write_json(path, journal)


def append_coverage(state, ts, missed, last_hb, desks):
    path = Path(state) / "coverage" / "pm-coverage.json"
    doc = load_json(path) or {}
    days = doc.setdefault("days", {})
    date = ts[:10]
    runs = days.setdefault(date, {}).setdefault("runs", [])
    key = f"deadman-{ts[11:13]}{ts[14:16]}"
    runs[:] = [r for r in runs if r.get("key") != key]
    runs.append({"ts": ts, "slot": "deadman", "step": "deadman", "desk": "all",
                 "run_id": f"{date}-deadman-{ts[11:13]}{ts[14:16]}", "key": key,
                 "engine_source": "deadman", "deadman_version": DEADMAN_VERSION,
                 "aborted": True, "outcome": "tripped",
                 "reason": f"dead-man's switch tripped: {missed} expected sentinel/PM run(s) missed "
                           f"since the last runner heartbeat ({iso(last_hb) if last_hb else 'none'})",
                 "desks": {d: {"protective_stops": True} for d in desks}})
    runs.sort(key=lambda r: r.get("ts", ""))
    doc["updated"] = ts
    write_json(path, doc)


def commit(state, message, push=False, log=print):
    r = git(state, "add", "-A")
    if r.returncode != 0:
        raise RuntimeError(f"git add: {r.stderr.strip()[:300]}")
    if not git(state, "status", "--porcelain").stdout.strip():
        return {"committed": False, "pushed": False}
    r = git(state, "-c", "user.name=ai-trading-deadman", "-c", "user.email=deadman@ai-trading.local",
            "commit", "-q", "-m", message)
    if r.returncode != 0:
        raise RuntimeError(f"git commit: {r.stderr.strip()[:300]}")
    out = {"committed": True, "sha": git(state, "rev-parse", "HEAD").stdout.strip(), "pushed": False}
    log(f"git: committed {out['sha'][:10]}")
    if push:
        p = git(state, "push", "-q")
        out["pushed"] = p.returncode == 0
        if p.returncode != 0:
            out["push_error"] = (p.stderr or p.stdout).strip()[-300:]
            log(f"git: PUSH FAILED — the commit stays local: {out['push_error'][:160]}")
    return out


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True, help="local clone of ai-trading-state")
    ap.add_argument("--now", default=None, help="ISO clock override (tests only; recorded)")
    ap.add_argument("--max-missed", type=int, default=2,
                    help="trip when this many expected sentinel/PM runs have passed without a heartbeat")
    ap.add_argument("--grace-min", type=float, default=GRACE_MIN)
    ap.add_argument("--slots", default=None, help="alternative slots.json")
    ap.add_argument("--push", action="store_true", help="push the trip commit (default: commit only)")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    state = Path(a.state).resolve()
    slots = load_json(a.slots or SLOTS_PATH) or {}
    desks = list(slots.get("desks") or ["swing", "pullback", "momentum"])
    now = parse_iso(a.now) if a.now else utc_now()
    ts = iso(now)
    dm_path = state / "health" / "deadman.json"
    dm = load_json(dm_path) or {}

    def log(msg):
        print(f"[deadman {ts}] {msg}", flush=True)

    if not (state / ".git").exists():
        log(f"ERROR: {state} is not a git repository")
        return 2
    hb, src = last_heartbeat(state, slots)
    tripped = bool(dm.get("tripped"))

    # A heartbeat newer than the trip: the runner is back. Clear, on the record.
    if tripped and hb is not None and hb > parse_iso(dm.get("ts") or ts):
        log(f"heartbeat {iso(hb)} ({src}) is newer than the trip at {dm.get('ts')} — clearing")
        if a.dry_run:
            return 0
        cleared = dict(dm, tripped=False, cleared_at=ts, cleared_by_heartbeat=iso(hb))
        write_json(dm_path, cleared)
        commit(state, f"deadman CLEARED {ts[:10]} heartbeat {iso(hb)}", push=a.push, log=log)
        print(f"DEADMAN CLEARED {ts} — runner heartbeat {iso(hb)}", flush=True)
        return 0

    if not market_hours(slots, now, a.grace_min):
        log("outside market hours — nothing expected, nothing to judge")
        print(f"DEADMAN QUIET {ts} — outside market hours", flush=True)
        return 0
    missed = missed_runs(slots, hb, now, a.grace_min)
    log(f"last heartbeat {iso(hb) if hb else 'none'} ({src or 'no record'}); "
        f"{len(missed)} expected run(s) missed: "
        + ", ".join(e.strftime("%H:%M") for e in missed))
    if tripped:
        log(f"already tripped at {dm.get('ts')} — nothing new")
        print(f"DEADMAN TRIPPED (standing) since {dm.get('ts')}", flush=True)
        return 0
    if len(missed) < a.max_missed:
        print(f"DEADMAN OK {ts} — {len(missed)} of {a.max_missed} allowed missed", flush=True)
        return 0

    log(f"TRIPPING: {len(missed)} >= {a.max_missed}")
    if a.dry_run:
        print(f"DEADMAN WOULD TRIP {ts} (dry run)", flush=True)
        return EXIT_TRIPPED
    summary = {}
    for desk in desks:
        bp = state / "books" / f"{desk}.json"
        book = load_json(bp)
        if not isinstance(book, dict):
            summary[desk] = {"missing_book": True}
            continue
        changed, entry = stamp_book(book, ts, len(missed), desk)
        if changed:
            write_json(bp, book)
        append_journal(state / "journals" / f"{desk}.json", entry)
        summary[desk] = {"changed": changed, "cancelled": len([d for d in entry["decisions"] if d["action"] == "cancel"]),
                         "stamped": len([d for d in entry["decisions"] if d["action"] == "protective-stop"]),
                         "unprotected": len(entry["warnings"])}
        log(f"{desk}: {summary[desk]}")
    append_coverage(state, ts, len(missed), hb, [d for d in desks if not summary[d].get("missing_book")])
    record = {"tripped": True, "ts": ts, "missed": len(missed),
              "missed_runs": [e.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z") for e in missed],
              "last_heartbeat": iso(hb) if hb else None, "last_heartbeat_source": src,
              "max_missed": a.max_missed, "desks": summary, "host": platform.node(),
              "deadman_version": DEADMAN_VERSION, "clock_override": a.now,
              "live_action": "NOT TAKEN — paper mode; see docs/PM.md 'Dead-man's switch'"}
    write_json(dm_path, record)
    # Like the runner's manifest, the record is written before the commit that carries it,
    # so the tree is clean afterwards; the commit sha is in git log, not in the file.
    try:
        commit(state, f"deadman TRIPPED {ts[:10]} {len(missed)} missed engine deadman",
               push=a.push, log=log)
    except RuntimeError as exc:
        log(f"git: {exc} — the trip record and the stamps stay on disk, uncommitted")
    print(f"DEADMAN TRIPPED {ts} — {len(missed)} missed run(s), protective stops stamped on "
          f"{sum(v.get('stamped', 0) for v in summary.values())} position(s)", flush=True)
    return EXIT_TRIPPED


if __name__ == "__main__":
    sys.exit(main())
