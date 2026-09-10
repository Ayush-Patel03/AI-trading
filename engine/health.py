"""health.py — the daily health sheet over the state repo (U-01).

    python3 health.py --state <dir> [--engine <dir>] [--mirror <dir>] [--slots slots.json]
                      [--now ISO] --out health.json [--md health.md]

Reads the state repo (`books/`, `journals/`, `coverage/`, `health/`, the git log) — or a
staged run directory that carries the same files under the engine's own names — and
answers one question per check: is the machine that protects the book actually running,
and is the book in the state the rules say it should be in? Every check reports
`pass` / `warn` / `fail` with the value it measured and the threshold it was held to; the
verdict is the worst status. Exit 0 pass, 1 warn, 2 fail.

Slots come from runner/slots.json (the sentinel's `times_et`, each decision slot's scan
and PM nominal times, the watch sessions, the health slot). "Two slots" means two expected
runner touches of the desk — a sentinel or a PM run — not two hours: at 10:50 ET the
previous two touches are the 10:35 sentinel and the 10:45 PM. Outside market hours the
yardstick is the last expected touch of the last trading day, so a quiet weekend does not
turn into a wall of failures. Weekends are skipped; exchange holidays are not known here
and read as one missed day — the health slot is not scheduled on them, so it never sees it.

Stdlib only. Never writes anything the caller did not ask for.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PM_SLOTS = ("pre-market", "opening-range", "midday", "power-hour")
STATUS_RANK = {"pass": 0, "warn": 1, "fail": 2}

# Built-in fallback when no slots.json can be found (a bare staged run dir). Kept in step
# with runner/slots.json by tests/test_health.py::test_default_schedule_matches_slots_json.
DEFAULT_SCHEDULE = {
    "timezone": "America/New_York",
    "desks": ["swing", "pullback", "momentum"],
    "pm": {"pre-market": "08:45", "opening-range": "10:45", "midday": "13:15", "power-hour": "15:45"},
    "scan": {"pre-market": "08:00", "opening-range": "10:00", "midday": "12:30", "power-hour": "15:00"},
    "sentinel": ["09:35", "10:35", "11:35", "12:35", "13:35", "14:35", "15:35"],
    "watch": {"pre-open": "07:00", "after-hours": "16:20"},
    "health": "16:15",
}

DEFAULT_RULES = {
    "grace_min": 20,                 # a run may land this long after its nominal time
    "book_warn_missed": 2, "book_fail_missed": 3,
    "coverage_warn_missing": 1, "coverage_fail_missing": 2,
    "unjudged_warn": 1, "unjudged_fail": 3,
    "shadow_warn_usd": 25.0, "shadow_fail_usd": 100.0,
    "commit_warn_missed": 2, "commit_fail_missed": 4,
    "push_warn_ahead": 1, "push_fail_ahead": 2,
    "mirror_warn_missed": 2, "mirror_fail_missed": 4,
    "heartbeat_warn_missed": 2, "heartbeat_fail_missed": 3,
    "production_branch": "production",
    "lookback_days": 10,
}


# ------------------------------------------------------------------ small helpers
def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def parse_iso(s):
    t = dt.datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def iso(ts):
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _tz(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _hhmm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def _git(path, *args):
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _worst(statuses):
    return max(statuses, key=lambda s: STATUS_RANK[s]) if statuses else "pass"


def _grade(value, warn_at, fail_at):
    """pass / warn / fail for a count against two thresholds (None never grades)."""
    if value is None:
        return "warn"
    if fail_at is not None and value >= fail_at:
        return "fail"
    if warn_at is not None and value >= warn_at:
        return "warn"
    return "pass"


# ------------------------------------------------------------------ the schedule
def schedule_from_slots(slots):
    """The compact schedule health.py works from, derived from runner/slots.json."""
    if not isinstance(slots, dict) or not slots.get("slots"):
        return dict(DEFAULT_SCHEDULE)
    sl = slots["slots"]
    sched = {"timezone": slots.get("timezone", "America/New_York"),
             "desks": list(slots.get("desks") or DEFAULT_SCHEDULE["desks"]),
             "pm": {}, "scan": {}, "sentinel": [], "watch": {}, "health": None}
    for name in PM_SLOTS:
        cfg = sl.get(name) or {}
        if cfg.get("nominal_et"):
            sched["pm"][name] = cfg["nominal_et"]
        if cfg.get("scan_nominal_et"):
            sched["scan"][name] = cfg["scan_nominal_et"]
    sched["sentinel"] = list((sl.get("sentinel") or {}).get("times_et") or DEFAULT_SCHEDULE["sentinel"])
    for sess, cfg in ((sl.get("watch") or {}).get("sessions") or {}).items():
        if cfg.get("nominal_et"):
            sched["watch"][sess] = cfg["nominal_et"]
    sched["health"] = (sl.get("health") or {}).get("nominal_et") or DEFAULT_SCHEDULE["health"]
    return sched


def load_schedule(slots_path=None, engine_dir=None):
    """slots.json from the explicit path, the engine clone, or this file's sibling runner/."""
    candidates = [slots_path] if slots_path else []
    if engine_dir:
        candidates += [os.path.join(engine_dir, "runner", "slots.json"),
                       os.path.join(engine_dir, "slots.json")]
    candidates += [os.path.join(HERE, "..", "runner", "slots.json"), os.path.join(HERE, "slots.json")]
    for c in candidates:
        if c and os.path.isfile(c):
            return schedule_from_slots(load_json(c)), os.path.normpath(c)
    return dict(DEFAULT_SCHEDULE), None


def is_trading_day(d):
    """Weekdays. Exchange holidays are not known here (see the module docstring)."""
    return d.weekday() < 5


def day_events(sched, date):
    """Every expected runner event on `date` (ET), as [{slot, step, when (aware ET), touches}]."""
    tz = _tz(sched["timezone"]) or dt.timezone.utc
    if not is_trading_day(date):
        return []
    out = []

    def at(hhmm):
        h, m = _hhmm(hhmm)
        return dt.datetime(date.year, date.month, date.day, h, m, tzinfo=tz)
    for slot, t in sched["scan"].items():
        out.append({"slot": slot, "step": "scan", "when": at(t), "touches": False})
    for slot, t in sched["pm"].items():
        out.append({"slot": slot, "step": "pm", "when": at(t), "touches": True})
    for t in sched["sentinel"]:
        out.append({"slot": "sentinel", "step": "sentinel", "when": at(t), "touches": True})
    for sess, t in sched["watch"].items():
        out.append({"slot": "watch", "step": "watch", "when": at(t), "session": sess, "touches": False})
    if sched.get("health"):
        out.append({"slot": "health", "step": "health", "when": at(sched["health"]), "touches": False})
    out.sort(key=lambda e: e["when"])
    return out


def events_between(sched, since, until, touches_only=False, lookback_days=10):
    """Expected events with `since < when <= until`. `since` None means the lookback start."""
    tz = _tz(sched["timezone"]) or dt.timezone.utc
    until_et = until.astimezone(tz)
    start = (since.astimezone(tz).date() if since else until_et.date() - dt.timedelta(days=lookback_days))
    out = []
    d = start
    while d <= until_et.date():
        for e in day_events(sched, d):
            if touches_only and not e["touches"]:
                continue
            if e["when"] <= until and (since is None or e["when"] > since):
                out.append(e)
        d += dt.timedelta(days=1)
    return out


def missed_since(sched, last, now, rules, touches_only=False):
    """How many expected events fell due (nominal time + grace) after `last` and before now."""
    grace = dt.timedelta(minutes=float(rules.get("grace_min", 20)))
    return len(events_between(sched, last, now - grace, touches_only=touches_only,
                              lookback_days=int(rules.get("lookback_days", 10))))


def in_market_hours(sched, now):
    tz = _tz(sched["timezone"]) or dt.timezone.utc
    et = now.astimezone(tz)
    if not is_trading_day(et.date()):
        return False
    ev = day_events(sched, et.date())
    if not ev:
        return False
    return ev[0]["when"] <= now <= ev[-1]["when"] + dt.timedelta(minutes=30)


# ------------------------------------------------------------------ state repo access
class State:
    """The state repo, or a staged run directory (paper_book*.json, pm_journal_current*.json)."""

    def __init__(self, root, desks, desk_cfg=None):
        self.root = root
        self.desks = list(desks)
        self.cfg = desk_cfg or {}

    def _first(self, *names):
        for n in names:
            p = os.path.join(self.root, n)
            if os.path.isfile(p):
                return p
        return None

    def book_path(self, desk):
        d = self.cfg.get(desk) or {}
        return self._first(os.path.join("books", f"{desk}.json"),
                           d.get("book") or "", f"paper_book_{desk}.json",
                           "paper_book.json" if desk == "swing" else "")

    def journal_path(self, desk):
        d = self.cfg.get(desk) or {}
        return self._first(os.path.join("journals", f"{desk}.json"),
                           d.get("journal") or "", f"pm_journal_current_{desk}.json",
                           "pm_journal_current.json" if desk == "swing" else "")

    def book(self, desk):
        p = self.book_path(desk)
        return load_json(p) if p else None

    def journal(self, desk):
        p = self.journal_path(desk)
        return (load_json(p) or {}) if p else {}

    def coverage(self):
        p = self._first(os.path.join("coverage", "pm-coverage.json"), "pm-coverage.json")
        return (load_json(p) or {}) if p else {}

    def health_file(self, name):
        p = self._first(os.path.join("health", name), name)
        return load_json(p) if p else None

    def engine_config(self, engine_dir=None, explicit=None):
        cands = [explicit] if explicit else []
        if engine_dir:
            cands.append(os.path.join(os.path.dirname(os.path.abspath(engine_dir)), "engine-config.json"))
        cands += [os.path.join(self.root, "engine-config.json")]
        for c in cands:
            if c and os.path.isfile(c):
                return load_json(c) or {}
        return {}


def latest_entry(journal, decision_only=False):
    entries = [e for e in (journal.get("entries") or []) if isinstance(e, dict)]
    if decision_only:
        entries = [e for e in entries if e.get("slot") in PM_SLOTS]
    return entries[-1] if entries else None


def desk_touches(state, desk, coverage_rows):
    """The last time the runner looked at `desk`: max(book.last_run, coverage rows naming it)."""
    stamps = []
    b = state.book(desk)
    if isinstance(b, dict) and b.get("last_run"):
        try:
            stamps.append(parse_iso(b["last_run"]))
        except (ValueError, TypeError):
            pass
    for row in coverage_rows:
        if row.get("aborted"):
            continue
        d = (row.get("desks") or {}).get(desk)
        if isinstance(d, dict) and not d.get("failed") and not d.get("abandoned"):
            try:
                stamps.append(parse_iso(row["ts"]))
            except (ValueError, TypeError, KeyError):
                pass
    return max(stamps) if stamps else None


def coverage_rows(cov, days=None):
    rows = []
    for date, day in sorted((cov.get("days") or {}).items()):
        if days is not None and date not in days:
            continue
        rows += [r for r in (day.get("runs") or []) if isinstance(r, dict)]
    return rows


# ------------------------------------------------------------------ the checks
def _row(name, status, value, threshold, detail):
    return {"name": name, "status": status, "value": value, "threshold": threshold, "detail": detail}


def check_tzdata(sched):
    ok = _tz(sched["timezone"]) is not None
    return _row("tzdata", "pass" if ok else "fail", ok, "zoneinfo resolves " + sched["timezone"],
                "tz database present" if ok else "no tz database — install tzdata")


def check_engine_branch(engine_dir, rules):
    want = rules.get("production_branch", "production")
    if not engine_dir:
        return _row("engine_branch", "warn", None, f"== {want}", "not checked — no --engine given")
    branch = _git(engine_dir, "rev-parse", "--abbrev-ref", "HEAD")
    sha = _git(engine_dir, "rev-parse", "HEAD")
    if branch is None:
        return _row("engine_branch", "warn", None, f"== {want}", f"{engine_dir} is not a git clone")
    return _row("engine_branch", "pass" if branch == want else "fail", branch, f"== {want}",
                f"engine {sha[:10] if sha else '?'} on {branch}")


def check_book_freshness(state, sched, now, rules, rows):
    per = {}
    statuses = []
    for desk in state.desks:
        if state.book(desk) is None:
            per[desk] = {"last": None, "missed": None, "status": "fail", "note": "no book"}
            statuses.append("fail")
            continue
        last = desk_touches(state, desk, rows)
        missed = missed_since(sched, last, now, rules, touches_only=True)
        st = _grade(missed, rules["book_warn_missed"], rules["book_fail_missed"])
        per[desk] = {"last": iso(last) if last else None, "missed": missed, "status": st}
        statuses.append(st)
    worst = max((p["missed"] for p in per.values() if p["missed"] is not None), default=None)
    return _row("book_freshness", _worst(statuses), {"missed_max": worst, "desks": per},
                f"< {rules['book_warn_missed']} expected touches (sentinel/PM) since the last run; "
                f"fail at {rules['book_fail_missed']}",
                ", ".join(f"{d}: {p['missed'] if p['missed'] is not None else '?'} missed"
                          for d, p in per.items()))


def _event_key(e, tz):
    if e["step"] == "sentinel":
        return ("sentinel", "sentinel", e["when"].astimezone(tz).strftime("%H"))
    if e["step"] == "watch":
        return ("watch", "watch", e.get("session"))
    return (e["slot"], e["step"], None)


def _row_key(r, tz):
    try:
        ts = parse_iso(r.get("ts")).astimezone(tz)
    except (ValueError, TypeError):
        ts = None
    slot, step = r.get("slot"), r.get("step") or ("sentinel" if r.get("slot") == "sentinel" else "pm")
    if slot == "sentinel":
        # the runner's key carries the ET hour; fall back to the row's own clock
        key = r.get("key") or ""
        hh = key.split("-")[1] if key.startswith("sentinel-") and len(key.split("-")) > 1 else \
            (ts.strftime("%H") if ts else None)
        return ("sentinel", "sentinel", hh)
    if slot == "watch":
        return ("watch", "watch", r.get("session"))
    return (slot, step, None)


def check_coverage_today(state, sched, now, rules, cov):
    tz = _tz(sched["timezone"]) or dt.timezone.utc
    today = now.astimezone(tz).date()
    rows = coverage_rows(cov, days={today.isoformat()})
    grace = dt.timedelta(minutes=float(rules.get("grace_min", 20)))
    expected = [e for e in day_events(sched, today) if e["when"] + grace <= now and e["step"] != "health"]
    present = {_row_key(r, tz) for r in rows if not r.get("aborted")}
    missing = [e for e in expected if _event_key(e, tz) not in present]
    aborted = [r for r in rows if r.get("aborted")]
    value = {"expected": len(expected), "present": len(expected) - len(missing),
             "missing": [f"{e['slot']}/{e['step']}@{e['when'].strftime('%H:%M')}" for e in missing],
             "aborted": len(aborted),
             "aborted_reasons": [str(r.get("reason"))[:120] for r in aborted][:10]}
    if not is_trading_day(today):
        return _row("coverage_today", "pass", value, "every expected slot has a non-aborted row",
                    f"{today} is not a trading day")
    st = _grade(len(missing), rules["coverage_warn_missing"], rules["coverage_fail_missing"])
    if aborted and st == "pass":
        st = "warn"
    detail = f"{value['present']}/{value['expected']} expected rows so far"
    if missing:
        detail += "; missing " + ", ".join(value["missing"])
    if aborted:
        detail += f"; {len(aborted)} aborted row(s)"
    return _row("coverage_today", st, value, "every expected slot has a non-aborted row; "
                f"warn at {rules['coverage_warn_missing']} missing or any aborted, "
                f"fail at {rules['coverage_fail_missing']} missing", detail)


def unjudged_names(book, entry):
    """Held names the latest decision entry could not judge, as pm.py marks them.

    pm.py writes `<SYM>: not in this scan's universe — priced but unjudged` when a holding
    has a price but no scan row (the score-based exits cannot run), and `UNPROTECTED: <SYM>`
    when it has no usable price at all (no stop can fire). Both are held names the engine
    did not fully manage on that run.
    """
    held = {p.get("symbol") for p in ((book or {}).get("positions") or []) if isinstance(p, dict)}
    out = {}
    for w in ((entry or {}).get("warnings") or []):
        if not isinstance(w, str):
            continue
        if "priced but unjudged" in w:
            sym = w.split(":", 1)[0].strip()
            kind = "unjudged"
        elif w.startswith("UNPROTECTED:"):
            sym = w[len("UNPROTECTED:"):].strip().split(" ", 1)[0].strip(",.")
            kind = "unprotected"
        else:
            continue
        if sym in held:
            out[sym] = kind
    return out


def check_unjudged(state, rules):
    per = {}
    n = 0
    for desk in state.desks:
        found = unjudged_names(state.book(desk), latest_entry(state.journal(desk), decision_only=True))
        if found:
            per[desk] = found
            n += len(found)
    return _row("unjudged", _grade(n, rules["unjudged_warn"], rules["unjudged_fail"]),
                {"count": n, "desks": per},
                f"0 held names unjudged/unprotected on the latest decision entry; "
                f"warn at {rules['unjudged_warn']}, fail at {rules['unjudged_fail']}",
                "; ".join(f"{d}: " + ", ".join(f"{s} ({k})" for s, k in v.items())
                          for d, v in per.items()) or "every held name was judged")


def check_shadow_gap(state, sched, now, rules):
    tz = _tz(sched["timezone"]) or dt.timezone.utc
    today = now.astimezone(tz).date().isoformat()
    per = {}
    day_total = 0.0
    any_model = False
    budget_over = []
    for desk in state.desks:
        b = state.book(desk) or {}
        sh = b.get("shadow") if isinstance(b.get("shadow"), dict) else None
        if sh is None:
            continue
        any_model = True
        entries = [e for e in (state.journal(desk).get("entries") or []) if isinstance(e, dict)]
        today_e = [e for e in entries if e.get("date") == today and isinstance(e.get("shadow"), dict)]
        before = [e for e in entries if (e.get("date") or "") < today and isinstance(e.get("shadow"), dict)]
        end = float((today_e[-1]["shadow"].get("cum_gap_usd") if today_e else sh.get("cum_gap_usd")) or 0.0)
        start = float(before[-1]["shadow"].get("cum_gap_usd") or 0.0) if before else (0.0 if today_e else end)
        gap = round(end - start, 2)
        day_total += gap
        budget = (today_e[-1]["shadow"].get("cost_budget") if today_e else None) or {}
        per[desk] = {"day_gap_usd": gap, "cum_gap_usd": sh.get("cum_gap_usd"),
                     "gap_share_of_realized_pct": sh.get("gap_share_of_realized_pct"),
                     "budget_share_used_pct": budget.get("share_used_pct")}
        if (budget.get("share_used_pct") or 0) > 100:
            budget_over.append(desk)
    if not any_model:
        return _row("shadow_gap", "pass", None, f"day gap < ${rules['shadow_warn_usd']:.0f}",
                    "shadow fill model off — no shadow block on any book")
    day_total = round(day_total, 2)
    st = "pass"
    if day_total >= rules["shadow_fail_usd"]:
        st = "fail"
    elif day_total >= rules["shadow_warn_usd"] or budget_over:
        st = "warn"
    detail = f"${day_total:.2f} shadow gap booked today across {len(per)} desk(s)"
    if budget_over:
        detail += "; cost budget exceeded on " + ", ".join(budget_over)
    return _row("shadow_gap", st, {"day_gap_usd": day_total, "desks": per},
                f"day gap < ${rules['shadow_warn_usd']:.0f} (fail at ${rules['shadow_fail_usd']:.0f}); "
                "cost budget under 100%", detail)


def check_halt_state(state):
    per = {}
    statuses = []
    for desk in state.desks:
        b = state.book(desk) or {}
        day = b.get("day") or {}
        e = latest_entry(state.journal(desk)) or {}
        lad = e.get("ladder") if isinstance(e.get("ladder"), dict) else {}
        rec = {"halted": bool(day.get("halted")), "halt_reason": day.get("halt_reason"),
               "ladder_rung": lad.get("rung"), "entries_blocked": bool(lad.get("entries_blocked")),
               "cool_until": b.get("cool_until"), "cool_active": bool(lad.get("cool_active")),
               "ladder_halt": b.get("ladder_halt")}
        st = "pass"
        if rec["halted"] or (rec["ladder_rung"] or 0) >= 1 or rec["cool_active"] or rec["entries_blocked"]:
            st = "warn"
        if (rec["ladder_rung"] or 0) >= 3 or lad.get("flatten"):
            st = "fail"
        rec["status"] = st
        per[desk] = rec
        statuses.append(st)
    flags = [f"{d}: " + ", ".join(k for k in ("halted", "cool_active", "entries_blocked") if r[k])
             + (f", rung {r['ladder_rung']}" if r["ladder_rung"] else "")
             for d, r in per.items() if r["status"] != "pass"]
    return _row("halt_state", _worst(statuses), per,
                "no desk halted, ladder rung 0, no cool-off; fail on a rung-3 flatten",
                "; ".join(flags) or "no halt, ladder rung 0 on every desk")


def check_broker_policy(state, engine_dir, config_path):
    cfg = state.engine_config(engine_dir, config_path)
    expect = cfg.get("broker_policy") if isinstance(cfg, dict) else None
    per = {}
    for desk in state.desks:
        b = state.book(desk) or {}
        e = latest_entry(state.journal(desk)) or {}
        desk_rules = (state.cfg.get(desk) or {}).get("rules") or {}
        per[desk] = {"in_effect": b.get("broker_policy") or e.get("broker_policy"),
                     "expected": desk_rules.get("broker_policy") or expect}
    names = {v["in_effect"] for v in per.values() if v["in_effect"]}
    bad = [d for d, v in per.items() if v["expected"] and v["in_effect"] and v["in_effect"] != v["expected"]]
    if bad:
        st, detail = "fail", "policy differs from engine-config on " + ", ".join(bad)
    elif len(names) > 1:
        st, detail = "fail", "desks disagree: " + ", ".join(f"{d}={v['in_effect']}" for d, v in per.items())
    elif not names:
        st, detail = "warn", "no desk reports a policy yet (no journal entry)"
    else:
        st, detail = "pass", f"{names.pop()} on every desk" + ("" if expect else " (engine-config sets no expectation)")
    return _row("broker_policy", st, {"expected": expect, "desks": per},
                "one policy on every desk, equal to engine-config's broker_policy when set", detail)


def check_state_commit_age(state, sched, now, rules):
    ct = _git(state.root, "log", "-1", "--format=%ct")
    if ct is None or not ct.isdigit():
        return _row("state_commit_age", "warn", None,
                    f"< {rules['commit_warn_missed']} expected runs since the last commit",
                    "not a git repository, or no commits")
    last = dt.datetime.fromtimestamp(int(ct), dt.timezone.utc)
    missed = missed_since(sched, last, now, rules)
    age_h = round((now - last).total_seconds() / 3600, 2)
    return _row("state_commit_age", _grade(missed, rules["commit_warn_missed"], rules["commit_fail_missed"]),
                {"last_commit": iso(last), "age_h": age_h, "missed": missed},
                f"< {rules['commit_warn_missed']} expected runs since the last commit; "
                f"fail at {rules['commit_fail_missed']}",
                f"last commit {age_h}h ago, {missed} expected run(s) since")


def check_push_backlog(state, rules):
    sb = _git(state.root, "status", "-sb")
    if sb is None:
        return _row("push_backlog", "warn", None, "0 commits ahead of the remote", "not a git repository")
    head = sb.splitlines()[0] if sb else ""
    ahead = 0
    if "[ahead " in head:
        try:
            ahead = int(head.split("[ahead ")[1].split("]")[0].split(",")[0].strip())
        except (ValueError, IndexError):
            ahead = 1
    dirty = len([ln for ln in sb.splitlines()[1:] if ln.strip()])
    return _row("push_backlog", _grade(ahead, rules["push_warn_ahead"], rules["push_fail_ahead"]),
                {"ahead": ahead, "dirty_files": dirty, "branch": head[3:] if head.startswith("## ") else head},
                f"0 commits ahead of the remote (warn at {rules['push_warn_ahead']}, fail at "
                f"{rules['push_fail_ahead']})",
                f"{ahead} unpushed commit(s), {dirty} uncommitted file(s)")


def check_mirror_age(mirror_dir, sched, now, rules):
    thr = f"< {rules['mirror_warn_missed']} expected runs since manifest.json generated_at"
    if not mirror_dir:
        return _row("mirror_age", "pass", None, thr, "not checked — no --mirror given")
    man = load_json(os.path.join(mirror_dir, "manifest.json"))
    if not isinstance(man, dict) or not man.get("generated_at"):
        return _row("mirror_age", "warn", None, thr, f"no manifest.json with generated_at under {mirror_dir}")
    try:
        gen = parse_iso(man["generated_at"])
    except (ValueError, TypeError):
        return _row("mirror_age", "warn", None, thr, f"unreadable generated_at {man['generated_at']!r}")
    missed = missed_since(sched, gen, now, rules)
    age_h = round((now - gen).total_seconds() / 3600, 2)
    return _row("mirror_age", _grade(missed, rules["mirror_warn_missed"], rules["mirror_fail_missed"]),
                {"generated_at": iso(gen), "age_h": age_h, "missed": missed},
                thr + f"; fail at {rules['mirror_fail_missed']}",
                f"mirror generated {age_h}h ago, {missed} expected run(s) since")


def check_runner_heartbeat(state, sched, now, rules):
    thr = (f"< {rules['heartbeat_warn_missed']} expected sentinel/PM runs since health/heartbeat.json; "
           f"fail at {rules['heartbeat_fail_missed']}")
    hb = state.health_file("heartbeat.json")
    if not isinstance(hb, dict) or not hb.get("ts"):
        return _row("runner_heartbeat", "warn", None, thr, "no health/heartbeat.json — the runner has never run here")
    try:
        ts = parse_iso(hb["ts"])
    except (ValueError, TypeError):
        return _row("runner_heartbeat", "warn", None, thr, f"unreadable heartbeat ts {hb.get('ts')!r}")
    missed = missed_since(sched, ts, now, rules, touches_only=True)
    age_min = round((now - ts).total_seconds() / 60, 1)
    return _row("runner_heartbeat", _grade(missed, rules["heartbeat_warn_missed"], rules["heartbeat_fail_missed"]),
                {"ts": iso(ts), "age_min": age_min, "missed": missed, "slot": hb.get("slot"),
                 "outcome": hb.get("outcome"), "run_id": hb.get("run_id")},
                thr, f"last heartbeat {age_min} min ago ({hb.get('slot')} {hb.get('outcome')}), "
                     f"{missed} expected run(s) since")


def check_deadman(state):
    dm = state.health_file("deadman.json")
    if not isinstance(dm, dict):
        return _row("deadman", "pass", None, "health/deadman.json absent or tripped: false", "never tripped")
    if dm.get("tripped"):
        return _row("deadman", "fail", dm, "health/deadman.json absent or tripped: false",
                    f"TRIPPED at {dm.get('ts')} after {dm.get('missed')} missed heartbeat(s) — "
                    "see docs/runbooks/deadman-tripped.md")
    return _row("deadman", "pass", dm, "health/deadman.json absent or tripped: false",
                f"cleared at {dm.get('cleared_at')}" if dm.get("cleared_at") else "not tripped")


# ------------------------------------------------------------------ the sheet
def check(state_dir, now, rules=None, engine_dir=None, mirror_dir=None, slots_path=None,
          config_path=None, desks=None):
    """The health sheet: {"checks": [...], "verdict", "as_of", ...}."""
    rules = dict(DEFAULT_RULES, **(rules or {}))
    sched, slots_used = load_schedule(slots_path, engine_dir)
    desk_cfg = {}
    for cand in ([os.path.join(engine_dir, "engine", "desks.json"), os.path.join(engine_dir, "desks.json")]
                 if engine_dir else []) + [os.path.join(HERE, "desks.json"), os.path.join(state_dir, "desks.json")]:
        if os.path.isfile(cand):
            desk_cfg = (load_json(cand) or {}).get("desks") or {}
            break
    st = State(state_dir, desks or sched["desks"], desk_cfg)
    cov = st.coverage()
    rows = coverage_rows(cov)
    checks = [
        check_tzdata(sched),
        check_engine_branch(engine_dir, rules),
        check_book_freshness(st, sched, now, rules, rows),
        check_coverage_today(st, sched, now, rules, cov),
        check_unjudged(st, rules),
        check_shadow_gap(st, sched, now, rules),
        check_halt_state(st),
        check_broker_policy(st, engine_dir, config_path),
        check_state_commit_age(st, sched, now, rules),
        check_push_backlog(st, rules),
        check_mirror_age(mirror_dir, sched, now, rules),
        check_runner_heartbeat(st, sched, now, rules),
        check_deadman(st),
    ]
    return {"as_of": iso(now), "verdict": _worst([c["status"] for c in checks]),
            "market_hours": in_market_hours(sched, now), "state": os.path.abspath(state_dir),
            "engine": os.path.abspath(engine_dir) if engine_dir else None,
            "mirror": os.path.abspath(mirror_dir) if mirror_dir else None,
            "slots": slots_used, "desks": st.desks, "rules": rules, "checks": checks}


def to_markdown(doc):
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"# Health — {doc['as_of']} — **{mark[doc['verdict']]}**", "",
             f"state `{doc.get('state')}`" + (f", engine `{doc.get('engine')}`" if doc.get("engine") else "")
             + (", market hours" if doc.get("market_hours") else ", outside market hours"), "",
             "| check | status | detail | threshold |", "|---|---|---|---|"]
    for c in doc["checks"]:
        lines.append(f"| `{c['name']}` | {mark[c['status']]} | {str(c['detail']).replace('|', '/')} | "
                     f"{str(c['threshold']).replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True)
    ap.add_argument("--engine", default=None, help="engine clone root (branch check, slots.json, desks.json)")
    ap.add_argument("--mirror", default=None, help="the served mirror directory (manifest.json)")
    ap.add_argument("--slots", default=None, help="runner/slots.json (default: found via --engine or this clone)")
    ap.add_argument("--config", default=None, help="engine-config.json (broker_policy expectation)")
    ap.add_argument("--now", default=None, help="ISO clock override")
    ap.add_argument("--out", required=True, help="health.json")
    ap.add_argument("--md", default=None, help="also write a markdown sheet")
    ap.add_argument("--rules", default=None, help="JSON file overriding DEFAULT_RULES")
    a = ap.parse_args(argv)
    now = parse_iso(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    rules = load_json(a.rules) if a.rules else None
    doc = check(a.state, now, rules, engine_dir=a.engine, mirror_dir=a.mirror,
                slots_path=a.slots, config_path=a.config)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(doc))
    for c in doc["checks"]:
        print(f"[{c['status'].upper():4}] {c['name']}: {c['detail']}")
    print(f"HEALTH {doc['verdict'].upper()} {doc['as_of']}")
    return STATUS_RANK[doc["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
