"""COVER-01 — a quiet sentinel must leave proof it ran, and nothing else.

Run through pm.py's CLI rather than run(), because the whole point is which FILES a quiet
run does and does not write.
"""
import json
import os
import subprocess
import sys

from conftest import ENGINE


def _cli(run_dir, *args):
    """Drive the real engine/pm.py the way a scheduled slot does.

    The module is invoked from the repo, not from a copy in the run directory: the run
    directory holds STATE. pm.py resolves every path from $SCAN_DIR, so this is the same
    contract a real run has, and it keeps one executable copy of the engine.

    Two portability notes, both found on 2026-09-09 when the suite was first run on
    Windows. The child's output is decoded as UTF-8 explicitly, and the child is told to
    emit UTF-8: the engine prints em-dashes and arrows, and the platform default (cp1252)
    raised UnicodeDecodeError here before the first assertion ran. And the environment is
    inherited rather than replaced with a hardcoded POSIX PATH, which named directories
    that do not exist off Linux."""
    env = dict(os.environ)
    env["SCAN_DIR"] = str(run_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(ENGINE / "pm.py"), *args], cwd=run_dir,
                          capture_output=True, text=True,
                          encoding="utf-8", env=env)


def test_quiet_sentinel_writes_only_a_heartbeat(run_dir, quotes):
    before = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))["revision"]
    r = _cli(run_dir, "--slot", "sentinel")
    assert "SENTINEL QUIET" in r.stdout, r.stdout + r.stderr
    assert (run_dir / "pm_heartbeat.json").exists(), "coverage proof is not optional"
    for never in ("pm_book_next.json", "pm_state.json", "pm_journal_next.json"):
        assert not (run_dir / never).exists(), f"a quiet run must not write {never}"
    after = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))["revision"]
    assert after == before == 16, "the stored book must not churn"


def test_heartbeat_reports_the_stored_revision_not_the_incremented_one(run_dir, quotes):
    _cli(run_dir, "--slot", "sentinel", "--desk", "momentum")
    hb = json.loads((run_dir / "pm_heartbeat-momentum.json").read_text(encoding="utf-8"))
    assert hb["quiet"] is True
    assert hb["desk"] == "momentum"
    assert hb["book_revision"] == 10, "a quiet run writes nothing, so revision 11 does not exist"


def test_a_run_that_acted_reports_the_new_revision(run_dir, quotes):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in b["positions"]:
        if p["symbol"] == "NVDA":
            p["stop"] = 240.00
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    _cli(run_dir, "--slot", "sentinel")
    hb = json.loads((run_dir / "pm_heartbeat.json").read_text(encoding="utf-8"))
    assert hb["quiet"] is False
    assert hb["decisions"] == 1
    assert hb["book_revision"] == 17


def test_every_desk_leaves_its_own_heartbeat(run_dir, quotes):
    for desk, name in (("swing", "pm_heartbeat.json"),
                       ("pullback", "pm_heartbeat-pullback.json"),
                       ("momentum", "pm_heartbeat-momentum.json")):
        args = ["--slot", "sentinel"] + ([] if desk == "swing" else ["--desk", desk])
        _cli(run_dir, *args)
        assert (run_dir / name).exists(), f"{desk} left no coverage record"
        assert json.loads((run_dir / name).read_text(encoding="utf-8"))["desk"] == desk
