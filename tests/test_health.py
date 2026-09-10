"""engine/health.py — the health sheet over a synthetic state repo (U-01)."""
import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
import health  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
UTC = dt.timezone.utc
# Thursday 2026-09-10, 13:30 ET — mid-session, after the 13:15 PM slot has had its grace.
NOW = dt.datetime(2026, 9, 10, 17, 40, tzinfo=UTC)
SLOTS = str(ROOT / "runner" / "slots.json")
DESKS = ["swing", "pullback", "momentum"]


def _et(hhmm, date="2026-09-10"):
    from zoneinfo import ZoneInfo
    y, m, d = map(int, date.split("-"))
    h, mi = map(int, hhmm.split(":"))
    return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def _git(repo, *args, when=None):
    import os
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


def make_state(tmp_path, last_run=None, git=True, branch="production"):
    """A state repo whose three books were last touched at `last_run` (default: fresh)."""
    st = tmp_path / "state"
    for sub in ("books", "journals", "coverage", "health"):
        (st / sub).mkdir(parents=True)
    last_run = last_run or health.iso(NOW - dt.timedelta(minutes=10))
    for desk, fname in (("swing", "book_swing.json"), ("pullback", "book_pullback.json"),
                        ("momentum", "book_momentum.json")):
        b = json.loads((FIX / fname).read_text(encoding="utf-8"))
        b["last_run"] = last_run
        b["broker_policy"] = "intraday_margin"
        (st / "books" / f"{desk}.json").write_text(json.dumps(b), encoding="utf-8")
        entry = {"ts": last_run, "date": last_run[:10], "slot": "midday", "run_key": f"{last_run[:10]}#midday",
                 "desk": desk, "broker_policy": "intraday_margin", "warnings": [], "decisions": [],
                 "ladder": {"rung": 0, "entries_blocked": False, "cool_active": False}}
        (st / "journals" / f"{desk}.json").write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps({"days": {}}), encoding="utf-8")
    (st / "health" / "heartbeat.json").write_text(json.dumps(
        {"ts": last_run, "slot": "midday", "desk": "all", "run_id": "r", "outcome": "committed"}), encoding="utf-8")
    if git:
        _git(st, "init", "-q", "-b", branch)
        _git(st, "add", "-A")
        _git(st, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed", when=last_run)
    return st


def full_coverage(st, upto=NOW):
    """Every expected non-health row for the day, up to `upto`, as the runner would write it."""
    sched, _ = health.load_schedule(SLOTS)
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")
    rows = []
    for e in health.day_events(sched, upto.astimezone(tz).date()):
        if e["when"] + dt.timedelta(minutes=20) > upto or e["step"] == "health":
            continue
        ts = health.iso(e["when"] + dt.timedelta(minutes=3))
        key = {"sentinel": f"sentinel-{e['when'].astimezone(tz).strftime('%H')}-all",
               "watch": f"watch-{e.get('session')}-all", "scan": f"{e['slot']}-scan"}.get(e["step"], f"{e['slot']}-all")
        row = {"ts": ts, "slot": e["slot"], "step": e["step"], "run_id": "r", "key": key,
               "desks": {d: {"quiet": True} for d in DESKS}}
        if e["step"] == "watch":
            row["session"] = e["session"]
        rows.append(row)
    doc = {"days": {upto.astimezone(tz).date().isoformat(): {"runs": rows}}}
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps(doc), encoding="utf-8")
    return rows


def by_name(doc):
    return {c["name"]: c for c in doc["checks"]}


def run_check(st, now=NOW, engine=None, **kw):
    return health.check(str(st), now, engine_dir=str(engine) if engine else None, slots_path=SLOTS, **kw)


# ------------------------------------------------------------------ the schedule
def test_default_schedule_matches_slots_json():
    sched, path = health.load_schedule(SLOTS)
    assert path and path.endswith("slots.json")
    for k in ("pm", "scan", "sentinel", "watch", "health", "desks", "timezone"):
        assert sched[k] == health.DEFAULT_SCHEDULE[k], k


def test_expected_events_skip_weekends_and_count_touches():
    sched, _ = health.load_schedule(SLOTS)
    assert health.day_events(sched, dt.date(2026, 9, 12)) == []
    ev = health.day_events(sched, dt.date(2026, 9, 10))
    assert len(ev) == 4 + 4 + 7 + 2 + 1
    touches = [e for e in ev if e["touches"]]
    assert len(touches) == 11
    rules = dict(health.DEFAULT_RULES)
    # 13:30 ET, grace 20: due touches are up to 13:10 → the 13:15 PM is not yet due
    assert health.missed_since(sched, _et("10:50"), _et("13:30"), rules, touches_only=True) == 2   # 11:35, 12:35
    assert health.missed_since(sched, _et("12:40"), _et("13:30"), rules, touches_only=True) == 0
    assert health.missed_since(sched, _et("12:40"), _et("13:40"), rules, touches_only=True) == 1
    # over a weekend nothing is expected after Friday's 15:45 PM
    assert health.missed_since(sched, _et("15:50", "2026-09-11"), _et("10:00", "2026-09-13"), rules,
                               touches_only=True) == 0
    assert health.in_market_hours(sched, _et("13:30")) and not health.in_market_hours(sched, _et("22:00"))
    assert not health.in_market_hours(sched, _et("12:00", "2026-09-12"))


# ------------------------------------------------------------------ book freshness
def test_book_freshness_pass_warn_fail(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["book_freshness"]["status"] == "pass"
    st2 = make_state(tmp_path / "w", last_run=health.iso(_et("10:50")))
    c = by_name(run_check(st2))["book_freshness"]
    # missed 11:35, 12:35 sentinels and the 13:15 PM = 3 → fail; at 12:50 it is 2 → warn
    assert c["status"] == "fail" and c["value"]["missed_max"] == 3
    c = by_name(run_check(st2, now=_et("13:00")))["book_freshness"]
    assert c["status"] == "warn" and c["value"]["missed_max"] == 2


def test_book_freshness_counts_a_quiet_sentinel_coverage_row_as_a_touch(tmp_path):
    st = make_state(tmp_path, last_run=health.iso(_et("10:50")))
    rows = [{"ts": health.iso(_et("13:37")), "slot": "sentinel", "step": "sentinel", "key": "sentinel-13-all",
             "desks": {d: {"quiet": True} for d in DESKS}}]
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps({"days": {"2026-09-10": {"runs": rows}}}), encoding="utf-8")
    assert by_name(run_check(st, now=_et("13:45")))["book_freshness"]["status"] == "pass"


def test_book_freshness_fails_on_a_missing_book_and_outside_hours_uses_the_last_slot(tmp_path):
    st = make_state(tmp_path, last_run=health.iso(_et("15:50", "2026-09-11")))
    (st / "books" / "momentum.json").unlink()
    c = by_name(run_check(st, now=_et("09:00", "2026-09-12")))["book_freshness"]
    assert c["status"] == "fail" and c["value"]["desks"]["momentum"]["note"] == "no book"
    assert c["value"]["desks"]["swing"]["status"] == "pass"


# ------------------------------------------------------------------ coverage
def test_coverage_today_pass_warn_fail(tmp_path):
    st = make_state(tmp_path)
    rows = full_coverage(st)
    c = by_name(run_check(st))["coverage_today"]
    assert c["status"] == "pass" and c["value"]["missing"] == [] and c["value"]["expected"] == len(rows)
    rows.pop()                                   # drop the last row → one missing → warn
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps({"days": {"2026-09-10": {"runs": rows}}}), encoding="utf-8")
    c = by_name(run_check(st))["coverage_today"]
    assert c["status"] == "warn" and len(c["value"]["missing"]) == 1
    rows.pop()
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps({"days": {"2026-09-10": {"runs": rows}}}), encoding="utf-8")
    assert by_name(run_check(st))["coverage_today"]["status"] == "fail"


def test_coverage_aborted_row_warns_and_weekend_passes(tmp_path):
    st = make_state(tmp_path)
    rows = full_coverage(st)
    rows.append({"ts": health.iso(NOW), "slot": "midday", "step": "pm", "key": "midday-swing",
                 "aborted": True, "reason": "sha256 mismatch", "desks": {}})
    (st / "coverage" / "pm-coverage.json").write_text(json.dumps({"days": {"2026-09-10": {"runs": rows}}}), encoding="utf-8")
    c = by_name(run_check(st))["coverage_today"]
    assert c["status"] == "warn" and c["value"]["aborted"] == 1 and "sha256" in c["value"]["aborted_reasons"][0]
    c = by_name(run_check(st, now=_et("12:00", "2026-09-12")))["coverage_today"]
    assert c["status"] == "pass" and "not a trading day" in c["detail"]


# ------------------------------------------------------------------ unjudged, shadow, halt, policy
def _set_entry(st, desk, **fields):
    p = st / "journals" / f"{desk}.json"
    j = json.loads(p.read_text(encoding="utf-8"))
    j["entries"][-1].update(fields)
    p.write_text(json.dumps(j), encoding="utf-8")


def test_unjudged_reads_pm_warnings_for_held_names(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["unjudged"]["status"] == "pass"
    _set_entry(st, "swing", warnings=["MU: not in this scan's universe — priced but unjudged",
                                      "ZZZ: not in this scan's universe — priced but unjudged"])
    c = by_name(run_check(st))["unjudged"]
    assert c["status"] == "warn" and c["value"] == {"count": 1, "desks": {"swing": {"MU": "unjudged"}}}
    _set_entry(st, "swing", warnings=["MU: not in this scan's universe — priced but unjudged",
                                      "UNPROTECTED: SNDK has no fresh price — no price at all this slot.",
                                      "UNPROTECTED: NVDA broke its stop and the policy refused the sale"])
    c = by_name(run_check(st))["unjudged"]
    assert c["status"] == "fail" and c["value"]["desks"]["swing"]["SNDK"] == "unprotected"


def test_shadow_gap_for_the_day(tmp_path):
    st = make_state(tmp_path)
    assert "model off" in by_name(run_check(st))["shadow_gap"]["detail"]
    p = st / "books" / "swing.json"
    b = json.loads(p.read_text(encoding="utf-8"))
    b["shadow"] = {"cum_gap_usd": 40.0, "n_fills": 3, "gap_share_of_realized_pct": 10.0}
    p.write_text(json.dumps(b), encoding="utf-8")
    jp = st / "journals" / "swing.json"
    j = json.loads(jp.read_text(encoding="utf-8"))
    j["entries"] = [{"ts": "2026-09-09T19:00:00Z", "date": "2026-09-09", "slot": "power-hour", "desk": "swing",
                     "shadow": {"cum_gap_usd": 10.0}},
                    dict(j["entries"][-1], shadow={"cum_gap_usd": 40.0, "cost_budget": {"share_used_pct": 50}})]
    jp.write_text(json.dumps(j), encoding="utf-8")
    c = by_name(run_check(st))["shadow_gap"]
    assert c["status"] == "warn" and c["value"]["day_gap_usd"] == 30.0
    j["entries"][-1]["shadow"]["cum_gap_usd"] = 150.0
    jp.write_text(json.dumps(j), encoding="utf-8")
    assert by_name(run_check(st))["shadow_gap"]["status"] == "fail"
    j["entries"][-1]["shadow"] = {"cum_gap_usd": 12.0, "cost_budget": {"share_used_pct": 120}}
    jp.write_text(json.dumps(j), encoding="utf-8")
    c = by_name(run_check(st))["shadow_gap"]
    assert c["status"] == "warn" and "budget exceeded" in c["detail"]


def test_halt_state_warns_on_a_halt_and_fails_on_rung_three(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["halt_state"]["status"] == "pass"
    p = st / "books" / "pullback.json"
    b = json.loads(p.read_text(encoding="utf-8"))
    b["day"]["halted"] = True
    b["day"]["halt_reason"] = "kill switch"
    p.write_text(json.dumps(b), encoding="utf-8")
    c = by_name(run_check(st))["halt_state"]
    assert c["status"] == "warn" and c["value"]["pullback"]["halted"] is True
    _set_entry(st, "momentum", ladder={"rung": 3, "flatten": True, "entries_blocked": True, "cool_active": False})
    c = by_name(run_check(st))["halt_state"]
    assert c["status"] == "fail" and c["value"]["momentum"]["ladder_rung"] == 3


def test_broker_policy_agreement_and_engine_config_expectation(tmp_path):
    st = make_state(tmp_path)
    c = by_name(run_check(st))["broker_policy"]
    assert c["status"] == "pass" and "intraday_margin" in c["detail"]
    (st / "engine-config.json").write_text(json.dumps({"broker_policy": "cash_settled"}), encoding="utf-8")
    c = by_name(run_check(st))["broker_policy"]
    assert c["status"] == "fail" and "differs from engine-config" in c["detail"]
    (st / "engine-config.json").unlink()
    p = st / "books" / "swing.json"
    b = json.loads(p.read_text(encoding="utf-8"))
    b["broker_policy"] = "legacy_pdt"
    p.write_text(json.dumps(b), encoding="utf-8")
    c = by_name(run_check(st))["broker_policy"]
    assert c["status"] == "fail" and "desks disagree" in c["detail"]


# ------------------------------------------------------------------ git, mirror, heartbeat, deadman, branch, tz
def test_state_commit_age_and_push_backlog(tmp_path):
    st = make_state(tmp_path)
    c = by_name(run_check(st))
    assert c["state_commit_age"]["status"] == "pass" and c["state_commit_age"]["value"]["missed"] == 0
    assert c["push_backlog"]["status"] == "pass" and c["push_backlog"]["value"]["ahead"] == 0
    # an old commit: judged by expected runs since, not by hours
    (st / "note.txt").write_text("x", encoding="utf-8")
    _git(st, "add", "-A")
    _git(st, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "old", when="2026-09-10T15:45:00Z")
    c = by_name(run_check(st))["state_commit_age"]        # 11:45 ET → 12:30, 12:35, 13:15 since = 3
    assert c["status"] == "warn" and c["value"]["missed"] == 3
    (st / "note.txt").write_text("x2", encoding="utf-8")
    _git(st, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "older", when="2026-09-10T14:00:00Z")
    c = by_name(run_check(st))["state_commit_age"]        # 10:00 ET → 6 expected runs since
    assert c["status"] == "fail" and c["value"]["missed"] == 6
    # ahead of a remote
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(st), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(st), "push", "-q", "-u", "origin", "HEAD"], check=True)
    (st / "note.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "-C", str(st), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local"], check=True)
    c = by_name(run_check(st))["push_backlog"]
    assert c["status"] == "warn" and c["value"]["ahead"] == 1
    (st / "note.txt").write_text("z", encoding="utf-8")
    subprocess.run(["git", "-C", str(st), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "local2"], check=True)
    assert by_name(run_check(st))["push_backlog"]["status"] == "fail"


def test_not_a_repo_reports_null_values(tmp_path):
    st = make_state(tmp_path, git=False)
    c = by_name(run_check(st))
    assert c["state_commit_age"]["status"] == "warn" and c["state_commit_age"]["value"] is None
    assert c["push_backlog"]["status"] == "warn" and c["push_backlog"]["value"] is None


def test_mirror_age(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["mirror_age"]["status"] == "pass"
    m = tmp_path / "mirror"
    m.mkdir()
    assert by_name(run_check(st, mirror_dir=str(m)))["mirror_age"]["status"] == "warn"
    (m / "manifest.json").write_text(json.dumps({"generated_at": health.iso(_et("13:20"))}), encoding="utf-8")
    assert by_name(run_check(st, mirror_dir=str(m)))["mirror_age"]["status"] == "pass"
    (m / "manifest.json").write_text(json.dumps({"generated_at": health.iso(_et("12:00"))}), encoding="utf-8")
    c = by_name(run_check(st, mirror_dir=str(m)))["mirror_age"]
    assert c["status"] == "warn" and c["value"]["missed"] == 3           # 12:30 scan, 12:35, 13:15
    (m / "manifest.json").write_text(json.dumps({"generated_at": health.iso(_et("08:50"))}), encoding="utf-8")
    assert by_name(run_check(st, mirror_dir=str(m)))["mirror_age"]["status"] == "fail"


def test_runner_heartbeat_and_deadman(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["runner_heartbeat"]["status"] == "pass"
    (st / "health" / "heartbeat.json").unlink()
    assert by_name(run_check(st))["runner_heartbeat"]["status"] == "warn"
    (st / "health" / "heartbeat.json").write_text(json.dumps({"ts": health.iso(_et("10:50")), "slot": "sentinel"}), encoding="utf-8")
    c = by_name(run_check(st))["runner_heartbeat"]
    assert c["status"] == "fail" and c["value"]["missed"] == 3
    c = by_name(run_check(st, now=_et("13:00")))["runner_heartbeat"]
    assert c["status"] == "warn" and c["value"]["missed"] == 2
    assert by_name(run_check(st))["deadman"]["status"] == "pass"
    (st / "health" / "deadman.json").write_text(json.dumps({"tripped": True, "ts": health.iso(NOW), "missed": 3}), encoding="utf-8")
    c = by_name(run_check(st))["deadman"]
    assert c["status"] == "fail" and "TRIPPED" in c["detail"]
    (st / "health" / "deadman.json").write_text(json.dumps({"tripped": False, "cleared_at": health.iso(NOW)}), encoding="utf-8")
    assert by_name(run_check(st))["deadman"]["status"] == "pass"


def test_engine_branch_and_tzdata(tmp_path):
    st = make_state(tmp_path)
    assert by_name(run_check(st))["engine_branch"]["status"] == "warn"
    eng = tmp_path / "engine"
    eng.mkdir()
    _git(eng, "init", "-q", "-b", "production")
    (eng / "x").write_text("x", encoding="utf-8")
    _git(eng, "add", "-A")
    _git(eng, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    c = by_name(run_check(st, engine=eng))
    assert c["engine_branch"]["status"] == "pass" and c["engine_branch"]["value"] == "production"
    _git(eng, "checkout", "-q", "-b", "main")
    assert by_name(run_check(st, engine=eng))["engine_branch"]["status"] == "fail"
    assert c["tzdata"]["status"] == "pass"


# ------------------------------------------------------------------ verdict, CLI, markdown
def test_verdict_is_the_worst_status():
    assert health._worst(["pass", "pass"]) == "pass"
    assert health._worst(["pass", "warn", "pass"]) == "warn"
    assert health._worst(["warn", "fail"]) == "fail"
    assert health._worst([]) == "pass"


def _cli(st, out, *extra):
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "health.py"), "--state", str(st),
                        "--now", health.iso(NOW), "--slots", SLOTS, "--out", str(out), *extra],
                       capture_output=True, text=True)
    return r.returncode, r.stdout


def test_cli_exit_codes_and_markdown(tmp_path):
    st = make_state(tmp_path)
    full_coverage(st)
    eng = tmp_path / "engine"
    eng.mkdir()
    _git(eng, "init", "-q", "-b", "production")
    (eng / "x").write_text("x", encoding="utf-8")
    _git(eng, "add", "-A")
    _git(eng, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    out, md = tmp_path / "h.json", tmp_path / "h.md"
    code, stdout = _cli(st, out, "--engine", str(eng), "--md", str(md))
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert code == 0 and doc["verdict"] == "pass", stdout
    assert {c["name"] for c in doc["checks"]} == {
        "tzdata", "engine_branch", "book_freshness", "coverage_today", "unjudged", "shadow_gap",
        "halt_state", "broker_policy", "state_commit_age", "push_backlog", "mirror_age",
        "runner_heartbeat", "deadman"}
    assert all(set(c) == {"name", "status", "value", "threshold", "detail"} for c in doc["checks"])
    text = md.read_text(encoding="utf-8")
    assert text.startswith("# Health — ") and "**PASS**" in text and "| `book_freshness` | PASS |" in text
    assert "HEALTH PASS" in stdout
    # warn → 1
    (st / "health" / "heartbeat.json").unlink()
    code, _ = _cli(st, out, "--engine", str(eng))
    assert code == 1 and json.loads(out.read_text(encoding="utf-8"))["verdict"] == "warn"
    # fail → 2
    (st / "health" / "deadman.json").write_text(json.dumps({"tripped": True, "ts": health.iso(NOW), "missed": 2}), encoding="utf-8")
    code, _ = _cli(st, out, "--engine", str(eng), "--md", str(md))
    assert code == 2 and "**FAIL**" in md.read_text(encoding="utf-8")


def test_health_works_over_a_staged_run_dir(tmp_path):
    """A run directory carries the books under the engine's own names and no git."""
    rd = tmp_path / "run"
    rd.mkdir()
    for fname, dest in (("book_swing.json", "paper_book.json"), ("book_pullback.json", "paper_book_pullback.json"),
                        ("book_momentum.json", "paper_book_momentum.json")):
        b = json.loads((FIX / fname).read_text(encoding="utf-8"))
        b["last_run"] = health.iso(NOW - dt.timedelta(minutes=5))
        (rd / dest).write_text(json.dumps(b), encoding="utf-8")
    doc = health.check(str(rd), NOW, slots_path=SLOTS)
    c = by_name(doc)
    assert c["book_freshness"]["status"] == "pass"
    assert c["state_commit_age"]["value"] is None
    assert doc["verdict"] in ("warn", "fail")
