"""runner/run.py — the engine on the box.

The gates (manifest, window, lock, idempotency) are unit-tested against synthetic inputs;
the whole thing is then driven end to end through runner/selftest.py, which runs the real
pm.py over the fixture books and compares bytes against a direct invocation.
"""
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import run as runner      # noqa: E402
import selftest           # noqa: E402

SLOTS = runner.load_slots()
UTC = dt.timezone.utc


def _et(hhmm, date=None, tz="America/New_York"):
    """Today (ET) at hh:mm ET, as an aware UTC datetime."""
    from zoneinfo import ZoneInfo
    d = date or runner.to_et(runner.utc_now(), tz)[0].date()
    h, m = map(int, hhmm.split(":"))
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(tz)).astimezone(UTC)


# ------------------------------------------------------------------ window
def test_window_accepts_as_of_near_the_slot():
    now = _et("13:20")
    runner.check_window(_et("13:05"), now, "midday", SLOTS["slots"]["midday"], SLOTS)


def test_window_refuses_as_of_far_from_the_slot():
    with pytest.raises(runner.Refused, match="more than 90 minutes"):
        runner.check_window(_et("10:00"), _et("10:05"), "midday", SLOTS["slots"]["midday"], SLOTS)


def test_window_refuses_inputs_from_another_day():
    yesterday = runner.to_et(runner.utc_now())[0].date() - dt.timedelta(days=1)
    with pytest.raises(runner.Refused, match="another day"):
        runner.check_window(_et("13:15", yesterday), _et("13:15"), "midday",
                            SLOTS["slots"]["midday"], SLOTS)


def test_window_refuses_stale_invocation_even_inside_the_window():
    with pytest.raises(runner.Refused, match="minutes old at invocation"):
        runner.check_window(_et("13:00"), _et("13:40"), "midday", SLOTS["slots"]["midday"], SLOTS)


def test_window_refuses_a_future_as_of():
    with pytest.raises(runner.Refused, match="future"):
        runner.check_window(_et("13:30"), _et("13:15"), "midday", SLOTS["slots"]["midday"], SLOTS)


def test_sentinel_and_watch_use_bands():
    runner.check_window(_et("15:40"), _et("15:45"), "sentinel", SLOTS["slots"]["sentinel"], SLOTS)
    with pytest.raises(runner.Refused, match="outside the sentinel window"):
        runner.check_window(_et("16:30"), _et("16:35"), "sentinel", SLOTS["slots"]["sentinel"], SLOTS)
    runner.check_window(_et("07:02"), _et("07:10"), "watch", SLOTS["slots"]["watch"], SLOTS,
                        session="pre-open")
    with pytest.raises(runner.Refused, match="watch after-hours window"):
        runner.check_window(_et("07:02"), _et("07:10"), "watch", SLOTS["slots"]["watch"], SLOTS,
                            session="after-hours")


def test_eastern_fallback_matches_zoneinfo_across_dst():
    from zoneinfo import ZoneInfo
    for stamp in ("2026-03-08T06:59:00Z", "2026-03-08T07:01:00Z", "2026-07-01T12:00:00Z",
                  "2026-11-01T05:59:00Z", "2026-11-01T06:01:00Z", "2026-12-25T15:00:00Z"):
        t = runner.parse_iso(stamp)
        assert t.astimezone(runner._us_eastern_fallback(t)).utcoffset() == \
            t.astimezone(ZoneInfo("America/New_York")).utcoffset(), stamp


# ------------------------------------------------------------------ manifest
def _inputs(tmp_path, now, files=None):
    files = files if files is not None else {"pm_quotes.json": selftest.fresh_quotes(now)}
    return selftest.write_inputs(tmp_path / "inputs", files, as_of=now)


def test_verify_inputs_returns_hashes(tmp_path):
    now = _et("13:15")
    d = _inputs(tmp_path, now)
    v = runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])
    assert set(v["files"]) == {"pm_quotes.json"}
    assert v["as_of"] == now


def test_verify_inputs_refuses_missing_manifest(tmp_path):
    (tmp_path / "inputs").mkdir()
    with pytest.raises(runner.Refused, match="no manifest.json"):
        runner.verify_inputs(tmp_path / "inputs", "pm", SLOTS, SLOTS["slots"]["midday"])


def test_verify_inputs_refuses_a_listed_file_that_is_missing(tmp_path):
    d = _inputs(tmp_path, _et("13:15"))
    (d / "pm_quotes.json").unlink()
    with pytest.raises(runner.Refused, match="missing"):
        runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])


def test_verify_inputs_refuses_a_hash_mismatch(tmp_path):
    d = _inputs(tmp_path, _et("13:15"))
    (d / "pm_quotes.json").write_text("{}", encoding="utf-8")
    with pytest.raises(runner.Refused, match="sha256 mismatch"):
        runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])


def test_verify_inputs_refuses_quotes_older_than_60s_at_as_of(tmp_path):
    now = _et("13:15")
    old = selftest.fresh_quotes(now - dt.timedelta(minutes=3))
    d = _inputs(tmp_path, now, {"pm_quotes.json": old})
    with pytest.raises(runner.Refused, match="older than as_of"):
        runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])


def test_verify_inputs_refuses_hand_typed_prices_without_venue_times(tmp_path):
    d = _inputs(tmp_path, _et("13:15"), {"pm_quotes.json": {"MU": {"price": 941.9}}})
    with pytest.raises(runner.Refused, match="no venue timestamps"):
        runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])


def test_verify_inputs_enforces_the_required_file_per_step(tmp_path):
    d = _inputs(tmp_path, _et("12:30"), {"pm_broker.json": {"data": {"results": []}}})
    with pytest.raises(runner.Refused, match="pm_quotes.json is required"):
        runner.verify_inputs(d, "pm", SLOTS, SLOTS["slots"]["midday"])
    with pytest.raises(runner.Refused, match="scan_data.json is required"):
        runner.verify_inputs(d, "scan", SLOTS, SLOTS["slots"]["midday"])


def test_newest_quote_ts_reads_the_raw_payload():
    now = runner.utc_now()
    q = selftest.fresh_quotes(now)
    assert runner.newest_quote_ts(q) == now - dt.timedelta(seconds=10)
    assert runner.newest_quote_ts({"data": {"results": []}}) is None


# ------------------------------------------------------------------ lock, keys, coverage
def test_lock_is_exclusive_and_released(tmp_path):
    a = runner.Lock(tmp_path).acquire()
    with pytest.raises(runner.Refused, match="another run holds"):
        runner.Lock(tmp_path).acquire()
    a.release()
    assert not (tmp_path / ".runner.lock").exists()
    runner.Lock(tmp_path).acquire().release()


def test_stale_lock_is_taken_over(tmp_path):
    old = runner.iso(runner.utc_now() - dt.timedelta(minutes=50))
    (tmp_path / ".runner.lock").write_text(json.dumps({"pid": 1, "started": old}), encoding="utf-8")
    lk = runner.Lock(tmp_path, stale_min=45).acquire()
    assert json.loads((tmp_path / ".runner.lock").read_text(encoding="utf-8"))["pid"] != 1
    lk.release()
    (tmp_path / ".runner.lock").write_text("garbage", encoding="utf-8")
    runner.Lock(tmp_path).acquire().release()       # unreadable = stale


def test_run_keys_distinguish_scan_pm_sentinel_hour_and_watch_session():
    now_et = runner.to_et(_et("10:41"))[0]
    assert runner.run_key("midday", "pm", "all", now_et) == "midday-all"
    assert runner.run_key("midday", "scan", "all", now_et) == "midday-scan"
    assert runner.run_key("sentinel", "sentinel", "all", now_et) == "sentinel-10-all"
    assert runner.run_key("watch", "watch", "swing", now_et, "pre-open") == "watch-pre-open-swing"
    assert runner.make_run_id("sentinel", "sentinel", now_et).endswith("-sentinel-1041")
    assert runner.make_run_id("midday", "pm", now_et).endswith("-midday")


def test_merge_coverage_replaces_only_its_own_key_and_prunes(tmp_path):
    ts = "2026-09-10T17:15:00Z"
    runner.merge_coverage(tmp_path, {"ts": ts, "slot": "midday", "run_id": "r", "key": "midday-all",
                                     "desks": {"swing": {"quiet": False}}})
    runner.merge_coverage(tmp_path, runner.aborted_row("midday", "pm", "swing", "r", "abc", ts,
                                                       "refused", "why", key="midday-swing"))
    runner.merge_coverage(tmp_path, {"ts": ts, "slot": "midday", "run_id": "r", "key": "midday-all",
                                     "desks": {"swing": {"quiet": True}}})
    doc = runner.load_json(tmp_path / "coverage" / "pm-coverage.json")
    rows = doc["days"]["2026-09-10"]["runs"]
    assert len(rows) == 2
    assert [r for r in rows if r["key"] == "midday-all"][0]["desks"]["swing"]["quiet"] is True
    assert [r for r in rows if r.get("aborted")][0]["reason"] == "why"
    for i in range(12):
        runner.merge_coverage(tmp_path, {"ts": f"2026-08-{i + 10:02d}T12:00:00Z", "slot": "x",
                                         "run_id": f"x{i}", "key": f"x{i}", "desks": {}})
    doc = runner.load_json(tmp_path / "coverage" / "pm-coverage.json")
    assert len(doc["days"]) == 10 and "2026-09-10" in doc["days"]


def test_scrub_book_drops_only_the_broker_block():
    b = {"revision": 3, "mirrors": {"display": "<masked>", "id": "<id>"}, "positions": []}
    assert runner.scrub_book(b) == {"revision": 3, "positions": []}


# ------------------------------------------------------------------ end to end
def test_selftest_end_to_end(tmp_path):
    report = selftest.run_selftest(tmp_path, ROOT, log=lambda *_: None)
    assert report["ok"]
    assert report["commits"] >= 5


def _state_and_inputs(tmp_path, hhmm="13:15"):
    state = selftest.make_state_repo(tmp_path / "state")
    now = selftest.synthetic_now(hhmm)
    inputs = selftest.write_inputs(tmp_path / "inputs",
                                   {"pm_quotes.json": selftest.fresh_quotes(now),
                                    "scan_results.json": selftest.fresh_scan(now)}, as_of=now)
    argv = ["--slot", "midday", "--desk", "all", "--inputs", str(inputs), "--state", str(state),
            "--engine", str(ROOT), "--no-push", "--now", runner.iso(now)]
    return state, inputs, now, argv


def test_one_failing_desk_is_recorded_and_the_others_still_land(tmp_path, monkeypatch):
    state, inputs, now, argv = _state_and_inputs(tmp_path)
    real_run = runner.Ctx.run

    def flaky(self, name, script, *args, fatal=True):
        if name == "pm-pullback":
            rec = {"name": name, "argv": [script, *args], "exit_code": 3, "seconds": 0.0,
                   "stdout": "", "stderr": "Traceback: simulated engine crash"}
            self.steps.append(rec)
            (self.run_dir / f"{name}.stdout.txt").write_text("", encoding="utf-8")
            (self.run_dir / f"{name}.stderr.txt").write_text(rec["stderr"], encoding="utf-8")
            return rec
        return real_run(self, name, script, *args, fatal=fatal)
    monkeypatch.setattr(runner.Ctx, "run", flaky)

    code = runner.main(argv)
    assert code == 2
    date = runner.to_et(now)[0].date().isoformat()
    man = runner.load_json(state / "manifests" / date / "midday-all.json")
    assert man["outcome"] == "failed" and "pullback (exit 3)" in man["reason"]
    assert man["engine_exit_code"] == 3
    assert "books/swing.json" in man["written"] and "books/momentum.json" in man["written"]
    assert "books/pullback.json" not in man["written"]
    pullback = runner.load_json(state / "books" / "pullback.json")
    assert pullback["revision"] == 7, "the failed desk's book must be untouched"
    cov = runner.load_json(state / "coverage" / "pm-coverage.json")
    row = cov["days"][date]["runs"][-1]
    assert row["desks"]["pullback"]["failed"] is True
    assert row["desks"]["swing"]["quiet"] is False
    assert not (state / ".runner.lock").exists()
    log = subprocess.run(["git", "-C", str(state), "log", "-1", "--format=%s"],
                         capture_output=True, text=True).stdout
    assert "FAILED" in log
    # a failed key is not "already done": the retry runs
    monkeypatch.setattr(runner.Ctx, "run", real_run)
    assert runner.main(argv) == 0
    assert runner.load_json(inputs / "outcome.json")["outcome"] == "committed"


def test_dry_run_touches_nothing(tmp_path):
    state, inputs, now, argv = _state_and_inputs(tmp_path)
    head = subprocess.run(["git", "-C", str(state), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout
    before = (state / "books" / "swing.json").read_bytes()
    assert runner.main(argv + ["--dry-run"]) == 0
    assert (state / "books" / "swing.json").read_bytes() == before
    assert not (state / "manifests").exists()
    assert subprocess.run(["git", "-C", str(state), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout == head
    assert runner.load_json(inputs / "outcome.json")["outcome"] == "dry_run"


def test_push_failure_keeps_the_commit_and_says_so(tmp_path):
    state, inputs, now, argv = _state_and_inputs(tmp_path)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(state), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(state), "push", "-q", "-u", "origin", "HEAD"], check=True)
    # someone else pushes first: the box's push is now non-fast-forward
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True)
    (other / "README.md").write_text("remote edit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(other), "-c", "user.name=x", "-c", "user.email=x@x",
                    "commit", "-qam", "remote edit"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q"], check=True)

    code = runner.main([a for a in argv if a != "--no-push"])
    assert code == 0
    out = runner.load_json(inputs / "outcome.json")
    assert out["outcome"] == "committed" and out["git"]["push_failed"] is True
    date = runner.to_et(now)[0].date().isoformat()
    man = runner.load_json(state / "manifests" / date / "midday-all.json")
    assert man["push_failed"] is True and man["outcome"] == "committed"
    status = subprocess.run(["git", "-C", str(state), "status", "-sb"],
                            capture_output=True, text=True).stdout
    assert "[ahead" in status
    # and a clean remote takes the push
    subprocess.run(["git", "-C", str(other), "reset", "-q", "--hard", "HEAD~1"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "--force"], check=True)
    inputs2 = selftest.write_inputs(tmp_path / "inputs2",
                                    {"pm_quotes.json": selftest.fresh_quotes(now)}, as_of=now)
    argv2 = ["--slot", "midday", "--desk", "swing", "--inputs", str(inputs2), "--state", str(state),
             "--engine", str(ROOT), "--now", runner.iso(now)]
    assert runner.main(argv2) == 0
    assert runner.load_json(inputs2 / "outcome.json")["git"]["pushed"] is True


def test_sentinel_quiet_run_leaves_only_coverage(tmp_path):
    state = selftest.make_state_repo(tmp_path / "state")
    now = selftest.synthetic_now("10:38")
    inputs = selftest.write_inputs(tmp_path / "inputs", {"pm_quotes.json": selftest.fresh_quotes(now)},
                                   as_of=now)
    before = {d: (state / "books" / f"{d}.json").read_bytes() for d in ("swing", "pullback", "momentum")}
    code = runner.main(["--slot", "sentinel", "--desk", "all", "--inputs", str(inputs), "--state",
                        str(state), "--engine", str(ROOT), "--no-push", "--now", runner.iso(now)])
    assert code == 0
    out = runner.load_json(inputs / "outcome.json")
    assert out["outcome"] == "committed" and out["written"] == ["coverage/pm-coverage.json"]
    assert all((state / "books" / f"{d}.json").read_bytes() == before[d] for d in before)
    date = runner.to_et(now)[0].date().isoformat()
    row = runner.load_json(state / "coverage" / "pm-coverage.json")["days"][date]["runs"][-1]
    assert row["key"] == "sentinel-10-all"
    assert all(v["quiet"] for v in row["desks"].values())
    assert (state / "manifests" / date / "sentinel-10-all.json").exists()


def test_missing_stored_book_is_refused_on_the_record(tmp_path):
    state, inputs, now, argv = _state_and_inputs(tmp_path)
    (state / "books" / "momentum.json").unlink()
    assert runner.main(argv) == 1
    out = runner.load_json(inputs / "outcome.json")
    assert out["outcome"] == "refused" and "momentum" in out["reason"]
    date = runner.to_et(now)[0].date().isoformat()
    row = runner.load_json(state / "coverage" / "pm-coverage.json")["days"][date]["runs"][-1]
    assert row["aborted"] is True


def test_stdlib_only():
    """The runner may import nothing the box does not ship with."""
    import ast
    # The runner may also import the engine's own stdlib-only modules (they sit next to it
    # in the clone): fetch_bars.py reads the membership file through universe_history.
    allowed = set(sys.stdlib_module_names) | {"run", "selftest", "deadman", "universe_history"}
    for p in (ROOT / "runner").glob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for n in names:
                assert n in allowed, f"{p.name} imports {n}"


# ------------------------------------------------------------------ S-01 / S-03 archive
def test_followed_set_is_staged_in_and_snapshots_written_back(tmp_path):
    """The followed set must round-trip through the run dir, or every scan would start the
    roster from nothing; the snapshot subdirs come back under archive/ unchanged."""
    state = selftest.make_state_repo(tmp_path / "state")
    (state / "archive").mkdir()
    seed = {"symbols": {"OLD": {"symbol": "OLD", "first_seen": "2026-08-01", "status": "open",
                                "horizon_end_date": "2026-08-29"}}}
    runner.write_json(state / "archive" / "followed.json", seed)
    now = selftest.synthetic_now("12:30")
    inputs = selftest.write_inputs(tmp_path / "inputs",
                                   {"scan_data.json": selftest.fresh_scan_data(now)}, as_of=now)
    run_dir = tmp_path / "run"
    staged = runner.stage_run(ROOT / "engine", state, inputs, run_dir, [], None)
    assert "archive/followed.json" in staged["state"]
    assert runner.load_json(run_dir / "archive" / "followed.json") == seed

    # what the engine would leave behind
    (run_dir / "archive" / "scan_snapshot").mkdir(parents=True)
    (run_dir / "archive" / "chain_snapshot").mkdir(parents=True)
    (run_dir / "archive" / "scan_snapshot" / "2026-09-10-midday.jsonl.gz").write_bytes(b"x")
    (run_dir / "archive" / "chain_snapshot" / "2026-09-10-midday.jsonl.gz").write_bytes(b"y")
    (run_dir / "archive" / "scan_snapshot" / "notes.txt").write_text("ignored", encoding="utf-8")
    runner.write_json(run_dir / "archive" / "followed.json", {"symbols": {"NEW": {}}})
    written = runner.write_back_archive(state, run_dir)
    assert written == ["archive/scan_snapshot/2026-09-10-midday.jsonl.gz",
                       "archive/chain_snapshot/2026-09-10-midday.jsonl.gz",
                       "archive/followed.json"]
    assert (state / "archive" / "scan_snapshot" / "2026-09-10-midday.jsonl.gz").read_bytes() == b"x"
    assert (state / "archive" / "chain_snapshot" / "2026-09-10-midday.jsonl.gz").read_bytes() == b"y"
    assert not (state / "archive" / "scan_snapshot" / "notes.txt").exists()
    assert runner.load_json(state / "archive" / "followed.json") == {"symbols": {"NEW": {}}}
    # a run that produced nothing writes nothing
    assert runner.write_back_archive(state, tmp_path / "empty-run") == []


def test_scan_run_commits_the_snapshot_and_follows_the_names(tmp_path):
    state = selftest.make_state_repo(tmp_path / "state")
    now = selftest.synthetic_now("12:30")
    inputs = selftest.write_inputs(tmp_path / "inputs",
                                   {"scan_data.json": selftest.fresh_scan_data(now)}, as_of=now)
    code = runner.main(["--slot", "midday", "--desk", "all", "--inputs", str(inputs),
                        "--state", str(state), "--engine", str(ROOT), "--no-push",
                        "--now", runner.iso(now)])
    assert code == 0
    date = runner.to_et(now)[0].date().isoformat()
    man = runner.load_json(state / "manifests" / date / "midday-scan.json")
    assert f"archive/scan_snapshot/{date}-midday.jsonl.gz" in man["written"]
    assert "archive/followed.json" in man["written"]
    sys.path.insert(0, str(ROOT / "engine"))
    import snapshots
    meta, rows = snapshots.read_snapshot(state / "archive" / "scan_snapshot" / f"{date}-midday.jsonl.gz")
    assert meta["run_id"] == f"{date}-midday" and meta["n_rows"] == len(rows) == 5
    assert meta["engine_sha"] == man["engine_sha"]
    followed = runner.load_json(state / "archive" / "followed.json")
    assert set(followed["symbols"]) == {r["symbol"] for r in rows}
    assert all(v["first_seen"] == date and v["first_slot"] == "midday"
               for v in followed["symbols"].values())
    tracked = subprocess.run(["git", "-C", str(state), "ls-files", "archive"],
                             capture_output=True, text=True).stdout.split()
    assert f"archive/scan_snapshot/{date}-midday.jsonl.gz" in tracked
    assert "archive/followed.json" in tracked


# ------------------------------------------------------------------ U-01 / K-05
def test_heartbeat_is_written_on_every_outcome(tmp_path):
    state, inputs, now, argv = _state_and_inputs(tmp_path)
    hb = state / "health" / "heartbeat.json"
    assert runner.main(argv + ["--dry-run"]) == 0 and not hb.exists()      # a dry run touches nothing
    assert runner.main(argv) == 0
    doc = runner.load_json(hb)
    assert doc["outcome"] == "committed" and doc["slot"] == "midday" and doc["desk"] == "all"
    assert doc["run_id"].endswith("-midday") and doc["ts"] == runner.iso(now)
    tracked = subprocess.run(["git", "-C", str(state), "ls-files", "health"], capture_output=True, text=True).stdout
    assert "health/heartbeat.json" in tracked
    assert runner.main(argv) == 0                                            # already_done
    assert runner.load_json(hb)["outcome"] == "already_done"
    (inputs / "pm_quotes.json").write_text("{}", encoding="utf-8")           # refused (a fresh key)
    assert runner.main([a if a != "all" else "swing" for a in argv]) == 1
    assert runner.load_json(hb)["outcome"] == "refused" and runner.load_json(hb)["desk"] == "swing"


def test_health_slot_runs_engine_health_and_keeps_the_runner_checks(tmp_path):
    state = selftest.make_state_repo(tmp_path / "state")
    now = selftest.synthetic_now("16:15")
    inputs = selftest.write_inputs(tmp_path / "inputs", {}, as_of=now)
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "manifest.json").write_text(json.dumps({"generated_at": runner.iso(now)}), encoding="utf-8")
    code = runner.main(["--slot", "health", "--inputs", str(inputs), "--state", str(state),
                        "--engine", str(ROOT), "--no-push", "--now", runner.iso(now), "--mirror", str(mirror)])
    assert code == 0
    date = runner.to_et(now)[0].date().isoformat()
    doc = runner.load_json(state / "health" / f"{date}.json")
    names = [c["name"] for c in doc["checks"]]
    assert names[:13] == ["tzdata", "engine_branch", "book_freshness", "coverage_today", "unjudged",
                          "shadow_gap", "halt_state", "broker_policy", "state_commit_age", "push_backlog",
                          "mirror_age", "runner_heartbeat", "deadman"]
    assert names[13:] == ["lock_free", "engine_sha"]
    by = {c["name"]: c for c in doc["checks"]}
    assert by["mirror_age"]["status"] == "pass" and by["lock_free"]["status"] == "pass"
    assert by["engine_sha"]["status"] == "pass" and by["tzdata"]["status"] == "pass"
    assert doc["verdict"] in ("pass", "warn", "fail") and doc["engine_health_exit"] in (0, 1, 2)
    assert (state / "health" / f"{date}.md").read_text(encoding="utf-8").startswith("# Health — ")
    man = runner.load_json(state / "manifests" / date / "health-1615.json")
    assert man["outcome"] == "committed" and man["health"]["engine_sha"] == "pass"
    assert man["health_verdict"] == doc["verdict"]
    assert f"health/{date}.json" in man["written"] and f"health/{date}.md" in man["written"]
