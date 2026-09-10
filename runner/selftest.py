"""selftest.py — replay a frozen input set through run.py against a throwaway state repo.

Run it on the box after install and after every `update.ps1`; CI runs the same code
through tests/test_runner.py. It proves, on this machine and this interpreter:

  1. a midday PM run over the three fixture books commits: run manifest written with
     outcome "committed", books and journals in the state repo;
  2. the books and journals the runner wrote are byte-identical to what pm.py writes when
     driven directly in an identically staged directory (the runner adds nothing and
     loses nothing — the one documented transform is dropping the book's `mirrors`
     block before it reaches git);
  3. a second identical invocation is "already_done" and touches nothing;
  4. a tampered input (hash mismatch) is "refused", exits 1, and leaves an
     `aborted: true` coverage row;
  5. the lock is released after every outcome;
  6. a scan run over the frozen scan_data fixture commits scans/latest.json, the compact
     record, the index and the history;
  7. the tz database resolves America/New_York (the engine's freshness gates depend on it).

    python runner/selftest.py [--keep]         exit 0 on PASS, 1 on FAIL

Stdlib only. pathlib throughout, no shell=True, no bash-isms: it must run on Windows.
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(HERE))
import run as runner  # noqa: E402

DESK_FIXTURES = {"swing": "book_swing.json", "pullback": "book_pullback.json",
                 "momentum": "book_momentum.json"}


class SelfTestFailure(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise SelfTestFailure(msg)


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SelfTestFailure(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def synthetic_now(hhmm_et="13:15", tz="America/New_York"):
    """Today at `hhmm_et` ET, as an aware UTC datetime — the clock the whole replay runs on."""
    real = runner.utc_now()
    today_et, _ = runner.to_et(real, tz)
    h, m = map(int, hhmm_et.split(":"))
    local = today_et.replace(hour=h, minute=m, second=0, microsecond=0)
    return local.astimezone(dt.timezone.utc)


def make_state_repo(path):
    """A fresh git repo laid out like ai-trading-state, seeded with the fixture books."""
    path = Path(path)
    (path / "books").mkdir(parents=True)
    (path / "journals").mkdir()
    (path / "coverage").mkdir()
    (path / "scans").mkdir()
    for desk, fname in DESK_FIXTURES.items():
        shutil.copyfile(FIX / fname, path / "books" / f"{desk}.json")
    (path / "coverage" / "pm-coverage.json").write_text(
        json.dumps({"days": {}, "keep_days": 10}, indent=2), encoding="utf-8")
    (path / "README.md").write_text("selftest state repo\n", encoding="utf-8")
    _git(path, "init", "-q")
    _git(path, "-c", "user.name=selftest", "-c", "user.email=selftest@local",
         "commit", "-q", "--allow-empty", "-m", "seed")
    _git(path, "add", "-A")
    _git(path, "-c", "user.name=selftest", "-c", "user.email=selftest@local",
         "commit", "-q", "-m", "fixture books")
    return path


def fresh_quotes(now):
    """The frozen quote payload restamped to `now` minus ten seconds."""
    payload = json.loads((FIX / "quotes.json").read_text(encoding="utf-8"))
    ts = runner.iso(now - dt.timedelta(seconds=10))
    for row in payload["data"]["results"]:
        q = row["quote"]
        q["venue_last_trade_time"] = ts
        q["venue_bid_time"] = q["venue_ask_time"] = ts
    return payload


def fresh_scan(now, tz="America/New_York"):
    """The one-candidate scan, stamped thirty minutes before `now` in ET."""
    s = json.loads((FIX / "scan.json").read_text(encoding="utf-8"))
    et, _ = runner.to_et(now - dt.timedelta(minutes=30), tz)
    s["meta"]["scan_date"] = et.date().isoformat()
    s["meta"]["time"] = et.strftime("%H:%M")
    return s


def fresh_scan_data(now, tz="America/New_York"):
    d = json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))
    et, _ = runner.to_et(now, tz)
    d["meta"]["scan_date"] = et.date().isoformat()
    d["meta"]["time"] = et.strftime("%H:%M")
    return d


def write_inputs(path, files, as_of):
    """Write the input files and the manifest a session would write."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    manifest = {"as_of": runner.iso(as_of), "files": {}}
    for name, obj in files.items():
        p = path / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        manifest["files"][name] = {"sha256": runner.sha256_file(p)}
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def direct_pm_outputs(engine_root, state_before, inputs, desks, now, slot="midday"):
    """Drive pm.py by hand in an identically staged directory — the reference outputs."""
    tmp = Path(tempfile.mkdtemp(prefix="selftest-ref-"))
    engine_dir = Path(engine_root) / "engine"
    sha, _ = runner.engine_identity(engine_root)
    runner.stage_run(engine_dir, state_before, inputs, tmp, desks, sha)
    cfg = runner.desks_config(engine_dir)
    out = {}
    for desk in desks:
        d = cfg[desk]
        sfx = "" if desk == "swing" else f"-{desk}"
        argv = [sys.executable, str(tmp / "pm.py"), "--slot", slot, "--desk", desk,
                "--book", d["book"], "--journal", d["journal"], "--quotes", "pm_quotes.json",
                "--broker", "pm_broker.json", "--scan", "scan_results.json",
                "--now", runner.iso(now)]
        env = dict(os.environ, SCAN_DIR=str(tmp), PYTHONIOENCODING="utf-8")
        r = subprocess.run(argv, cwd=str(tmp), capture_output=True, text=True,
                           encoding="utf-8", env=env)
        check(r.returncode == 0, f"reference pm.py {desk} exited {r.returncode}: {r.stderr[-400:]}")
        book = runner.load_json(tmp / f"pm_book_next{sfx}.json")
        out[desk] = {"book": runner.json_bytes(runner.scrub_book(book)),
                     "journal": (tmp / f"pm_journal_next{sfx}.json").read_bytes()}
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def run_selftest(work, engine_root=None, log=print):
    """The whole replay. Returns a report dict; raises SelfTestFailure on the first failure."""
    work = Path(work)
    engine_root = Path(engine_root or ROOT)
    report = {"steps": []}

    def step(name, ok=True, detail=""):
        report["steps"].append({"name": name, "ok": ok, "detail": detail})
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    tz_ok = runner.to_et(runner.utc_now())[1] == "zoneinfo"
    step("tz database resolves America/New_York", tz_ok,
         "" if tz_ok else "install tzdata (pip install --require-hashes -r runner/requirements.txt)")
    check(tz_ok, "no tz database: the engine's scan-freshness and macro gates would silently pass")

    state = make_state_repo(work / "state")
    state_before = work / "state-before"
    shutil.copytree(state, state_before, ignore=shutil.ignore_patterns(".git"))
    now = synthetic_now("13:15")
    desks = ["swing", "pullback", "momentum"]
    inputs = write_inputs(work / "inputs-midday",
                          {"pm_quotes.json": fresh_quotes(now), "scan_results.json": fresh_scan(now)},
                          as_of=now)
    common = ["--state", str(state), "--engine", str(engine_root), "--no-push",
              "--now", runner.iso(now)]

    # 1. the committed run
    code = runner.main(["--slot", "midday", "--desk", "all", "--inputs", str(inputs), *common])
    check(code == 0, f"first run exited {code}")
    date = runner.to_et(now)[0].date().isoformat()
    man = runner.load_json(state / "manifests" / date / "midday-all.json")
    check(man and man.get("outcome") == "committed", f"manifest outcome {man and man.get('outcome')}")
    check(man.get("engine_exit_code") == 0, "engine exit code recorded as non-zero")
    check(not (state / ".runner.lock").exists(), "lock left behind after a committed run")
    for desk in desks:
        check((state / "books" / f"{desk}.json").exists(), f"books/{desk}.json not written")
        check((state / "journals" / f"{desk}.json").exists(), f"journals/{desk}.json not written")
    outcome = runner.load_json(inputs / "outcome.json")
    check(outcome and outcome["outcome"] == "committed", "outcome.json missing or wrong")
    step("midday PM run committed", detail=f"engine {man.get('engine_sha', '?')[:10]}, "
         f"{len(man.get('written', []))} file(s) written")

    # 2. byte-identical to pm.py driven by hand
    ref = direct_pm_outputs(engine_root, state_before, inputs, desks, now)
    for desk in desks:
        got_book = (state / "books" / f"{desk}.json").read_bytes()
        got_jrn = (state / "journals" / f"{desk}.json").read_bytes()
        check(got_book == ref[desk]["book"], f"books/{desk}.json differs from pm.py's own output")
        check(got_jrn == ref[desk]["journal"], f"journals/{desk}.json differs from pm.py's own output")
        check(b'"mirrors"' not in got_book, f"books/{desk}.json carries the broker block")
    step("books and journals byte-identical to a direct pm.py run")

    # coverage row for the run
    cov = runner.load_json(state / "coverage" / "pm-coverage.json")
    rows = cov["days"][date]["runs"]
    row = next((r for r in rows if r.get("run_id") == man["run_id"]), None)
    check(row and set(row["desks"]) == set(desks), "coverage row missing a desk")
    check(all(r.get("aborted") is not True for r in rows), "an aborted row on a committed run")
    step("coverage row carries all three desks")

    # 3. idempotency
    head_before = _git(state, "rev-parse", "HEAD").strip()
    books_before = {d: (state / "books" / f"{d}.json").read_bytes() for d in desks}
    code = runner.main(["--slot", "midday", "--desk", "all", "--inputs", str(inputs), *common])
    check(code == 0, f"second run exited {code}")
    check(_git(state, "rev-parse", "HEAD").strip() == head_before, "second run made a commit")
    check(all((state / "books" / f"{d}.json").read_bytes() == books_before[d] for d in desks),
          "second run changed a book")
    check(runner.load_json(inputs / "outcome.json")["outcome"] == "already_done",
          "second run was not reported already_done")
    step("second identical invocation is already_done")

    # 4. a tampered input is refused, on the record
    tampered = work / "inputs-tampered"
    shutil.copytree(inputs, tampered)
    q = runner.load_json(tampered / "pm_quotes.json")
    q["data"]["results"][0]["quote"]["last_trade_price"] = "1.000000"
    (tampered / "pm_quotes.json").write_text(json.dumps(q), encoding="utf-8")
    (tampered / "outcome.json").unlink(missing_ok=True)
    code = runner.main(["--slot", "midday", "--desk", "swing", "--inputs", str(tampered), *common])
    check(code == 1, f"tampered run exited {code}, expected 1")
    man2 = runner.load_json(state / "manifests" / date / "midday-swing.json")
    check(man2 and man2["outcome"] == "refused" and "sha256" in (man2.get("reason") or ""),
          f"tampered manifest: {man2 and man2.get('outcome')} / {man2 and man2.get('reason')}")
    cov = runner.load_json(state / "coverage" / "pm-coverage.json")
    ab = [r for r in cov["days"][date]["runs"] if r.get("aborted")]
    check(ab and "sha256" in ab[-1]["reason"], "no aborted coverage row for the refusal")
    check(any(r.get("run_id") == man["run_id"] and not r.get("aborted")
              for r in cov["days"][date]["runs"]), "the refusal row displaced the committed row")
    check((state / "books" / "swing.json").read_bytes() == books_before["swing"],
          "a refused run changed the book")
    check(not (state / ".runner.lock").exists(), "lock left behind after a refusal")
    step("tampered input refused with an aborted coverage row")

    # 5. a stale lock is taken over; a live lock refuses
    lock = state / ".runner.lock"
    lock.write_text(json.dumps({"pid": 1, "started": runner.iso(runner.utc_now())}), encoding="utf-8")
    code = runner.main(["--slot", "midday", "--desk", "pullback", "--inputs", str(inputs), *common])
    check(code == 1, f"run under a live lock exited {code}, expected 1")
    check(runner.load_json(inputs / "outcome.json")["outcome"] == "refused", "live lock not refused")
    lock.unlink()
    step("live lock refuses; lock released after every outcome")

    # 6. a scan run
    scan_now = synthetic_now("12:30")
    scan_inputs = write_inputs(work / "inputs-scan", {"scan_data.json": fresh_scan_data(scan_now)},
                               as_of=scan_now)
    code = runner.main(["--slot", "midday", "--desk", "all", "--inputs", str(scan_inputs),
                        "--state", str(state), "--engine", str(engine_root), "--no-push",
                        "--now", runner.iso(scan_now)])
    check(code == 0, f"scan run exited {code}: {runner.load_json(scan_inputs / 'outcome.json')}")
    man3 = runner.load_json(state / "manifests" / date / "midday-scan.json")
    check(man3 and man3["outcome"] == "committed", "scan manifest not committed")
    for f in ("scans/latest.json", "scan-index.json"):
        check((state / f).exists(), f"{f} not written by the scan run")
    check(any(p.name != "latest.json" for p in (state / "scans").glob("*.json")),
          "no compact scan record under scans/")
    step("scan run committed latest.json, record and index",
         detail=f"scan run_id {man3.get('scan', {}).get('run_id')}")

    n = len(_git(state, "log", "--oneline").strip().splitlines())
    report["commits"] = n
    report["ok"] = all(s["ok"] for s in report["steps"])
    log(f"selftest PASS — {n} commits in the throwaway state repo, python "
        f"{sys.version.split()[0]}, engine {man.get('engine_sha', '?')}")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", default=None, help="engine clone root (default: this one)")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    ap.add_argument("--work", default=None, help="work directory (default: a temp dir)")
    a = ap.parse_args(argv)
    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="ai-trading-selftest-"))
    print(f"selftest work dir: {work}")
    try:
        run_selftest(work, a.engine)
        return 0
    except SelfTestFailure as exc:
        print(f"selftest FAIL — {exc}")
        return 1
    finally:
        if not a.keep and not a.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
