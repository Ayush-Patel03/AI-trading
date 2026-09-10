"""runner/deadman.py — the dead-man's switch over the paper ledger (K-05)."""
import datetime as dt
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import deadman   # noqa: E402
import selftest  # noqa: E402

UTC = dt.timezone.utc
DESKS = ("swing", "pullback", "momentum")


def _et(hhmm, date="2026-09-10"):
    """Thursday 2026-09-10 by default — a trading day."""
    from zoneinfo import ZoneInfo
    y, m, d = map(int, date.split("-"))
    h, mi = map(int, hhmm.split(":"))
    return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


def heartbeat(state, when, slot="sentinel", outcome="committed"):
    deadman.write_json(state / "health" / "heartbeat.json",
                       {"ts": deadman.iso(when), "slot": slot, "desk": "all", "run_id": "r", "outcome": outcome})


def make_state(tmp_path, hb_at=None):
    st = selftest.make_state_repo(tmp_path / "state")
    b = json.loads((st / "books" / "swing.json").read_text(encoding="utf-8"))
    b["working_orders"] = [
        {"id": "b1", "symbol": "AAPL", "side": "buy", "kind": "entry", "status": "working", "shares": 2, "limit_price": 100.0},
        {"id": "s1", "symbol": "MU", "side": "sell", "kind": "exit", "status": "working", "shares": 0.1, "limit_price": 999.0}]
    b["positions"][0].pop("stop", None)                 # MU: no stop → entry − 2.5×ATR
    (st / "books" / "swing.json").write_text(json.dumps(b, indent=2), encoding="utf-8")
    heartbeat(st, hb_at or _et("09:37"))
    subprocess.run(["git", "-C", str(st), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(st), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "hb"], check=True)
    return st


def run(state, now, *extra):
    return deadman.main(["--state", str(state), "--now", deadman.iso(now), *extra])


def head(state):
    return subprocess.run(["git", "-C", str(state), "rev-parse", "HEAD"], capture_output=True, text=True).stdout


def books(state):
    return {d: json.loads((state / "books" / f"{d}.json").read_text(encoding="utf-8")) for d in DESKS}


# ------------------------------------------------------------------ the schedule
def test_expected_runs_and_market_hours():
    slots = deadman.load_json(deadman.SLOTS_PATH)
    times = deadman.expected_times(slots)
    assert (9, 35) in times and (8, 45) in times and (15, 45) in times and len(times) == 11
    assert deadman.expected_today(slots, _et("12:00", "2026-09-12")) == []
    assert deadman.market_hours(slots, _et("12:00")) and not deadman.market_hours(slots, _et("08:00"))
    assert not deadman.market_hours(slots, _et("17:00"))
    # 09:37 heartbeat, 11:50 clock, grace 20 → 10:35 and 10:45 due; 11:35 not yet
    missed = deadman.missed_runs(slots, _et("09:37"), _et("11:50"))
    assert [e.strftime("%H:%M") for e in missed] == ["10:35", "10:45"]


# ------------------------------------------------------------------ no trip
def test_no_trip_with_a_fresh_heartbeat(tmp_path):
    st = make_state(tmp_path, hb_at=_et("11:40"))
    h = head(st)
    assert run(st, _et("12:00")) == 0
    assert not (st / "health" / "deadman.json").exists()
    assert head(st) == h
    assert books(st)["swing"]["working_orders"][0]["status"] == "working"


def test_one_missed_run_is_not_a_trip(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))       # 10:35 due at 10:55
    assert run(st, _et("11:00")) == 0
    assert not (st / "health" / "deadman.json").exists()


def test_no_trip_outside_market_hours(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37", "2026-09-09"))     # a day-old heartbeat
    assert run(st, _et("07:30")) == 0                                # before the first run
    assert run(st, _et("12:00", "2026-09-12")) == 0                  # Saturday
    assert run(st, _et("18:00")) == 0                                # after the close
    assert not (st / "health" / "deadman.json").exists()


def test_a_live_runner_lock_counts_as_a_heartbeat(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))
    (st / ".runner.lock").write_text(json.dumps({"pid": 1, "started": deadman.iso(deadman.utc_now())}), encoding="utf-8")
    assert run(st, _et("12:30")) == 0
    assert not (st / "health" / "deadman.json").exists()


# ------------------------------------------------------------------ the trip
def test_trip_after_two_missed_runs_stamps_the_paper_books(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))
    before = books(st)
    code = run(st, _et("11:50"))
    assert code == deadman.EXIT_TRIPPED
    dm = json.loads((st / "health" / "deadman.json").read_text(encoding="utf-8"))
    assert dm["tripped"] is True and dm["missed"] == 2 and dm["last_heartbeat"] == deadman.iso(_et("09:37"))
    assert dm["ts"] == deadman.iso(_et("11:50")) and "paper" in dm["live_action"].lower()
    after = books(st)
    swing = after["swing"]
    # working buys cancelled, the resting sell left alone
    assert [o["id"] for o in swing["working_orders"]] == ["s1"]
    # every open position stamped, level = its stop, or entry − 2.5×ATR when it has none
    for d in DESKS:
        assert after[d]["positions"], d
        for p in after[d]["positions"]:
            ps = p["protective_stop"]
            assert ps["reason"] == "deadman" and ps["placed_at"] == dm["ts"] and ps["paper"] is True
            if p.get("stop"):
                assert ps["level"] == round(p["stop"], 2)
        assert after[d]["revision"] == before[d]["revision"] + 1
        assert after[d]["based_on_revision"] == before[d]["revision"]
    mu = next(p for p in swing["positions"] if p["symbol"] == "MU")
    assert mu["protective_stop"]["level"] == round(mu["avg_cost"] - 2.5 * mu["atr_14"], 2)
    assert "ATR" in mu["protective_stop"]["basis"]
    # the journal entry
    for d in DESKS:
        j = json.loads((st / "journals" / f"{d}.json").read_text(encoding="utf-8"))
        e = j["entries"][-1]
        assert e["slot"] == "deadman" and e["deadman"] is True and e["desk"] == d
        acts = {x["action"] for x in e["decisions"]}
        assert "protective-stop" in acts
        if d == "swing":
            assert "cancel" in acts and [x["symbol"] for x in e["decisions"] if x["action"] == "cancel"] == ["AAPL"]
    # the aborted coverage row
    cov = json.loads((st / "coverage" / "pm-coverage.json").read_text(encoding="utf-8"))
    row = cov["days"]["2026-09-10"]["runs"][-1]
    assert row["aborted"] is True and row["slot"] == "deadman" and "2 expected" in row["reason"]
    assert row["engine_source"] == "deadman"
    # committed, tree clean, not pushed
    log = subprocess.run(["git", "-C", str(st), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("deadman TRIPPED")
    assert subprocess.run(["git", "-C", str(st), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


def test_second_run_while_tripped_does_nothing_new(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))
    assert run(st, _et("12:00")) == deadman.EXIT_TRIPPED
    h = head(st)
    snap = {d: (st / "books" / f"{d}.json").read_bytes() for d in DESKS}
    dm = (st / "health" / "deadman.json").read_bytes()
    jr = (st / "journals" / "swing.json").read_bytes()
    assert run(st, _et("13:00")) == 0
    assert head(st) == h
    assert {d: (st / "books" / f"{d}.json").read_bytes() for d in DESKS} == snap
    assert (st / "health" / "deadman.json").read_bytes() == dm
    assert (st / "journals" / "swing.json").read_bytes() == jr


def test_a_fresh_heartbeat_clears_the_trip(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))
    assert run(st, _et("11:50")) == deadman.EXIT_TRIPPED
    heartbeat(st, _et("12:40"), slot="sentinel")
    assert run(st, _et("12:45")) == 0
    dm = json.loads((st / "health" / "deadman.json").read_text(encoding="utf-8"))
    assert dm["tripped"] is False and dm["cleared_at"] == deadman.iso(_et("12:45"))
    assert dm["cleared_by_heartbeat"] == deadman.iso(_et("12:40")) and dm["missed"] == 2
    log = subprocess.run(["git", "-C", str(st), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    assert log.startswith("deadman CLEARED")
    # the stamps stay as the record; a later, second silence trips again without re-stamping
    assert books(st)["swing"]["positions"][0]["protective_stop"]["reason"] == "deadman"
    assert run(st, _et("15:30")) == deadman.EXIT_TRIPPED         # 13:15, 13:35, 14:35 missed
    assert json.loads((st / "health" / "deadman.json").read_text(encoding="utf-8"))["tripped"] is True
    ps = books(st)["swing"]["positions"][0]["protective_stop"]
    assert ps["placed_at"] == deadman.iso(_et("11:50")), "the first stamp is kept"


def test_dry_run_reports_without_writing(tmp_path):
    st = make_state(tmp_path, hb_at=_et("09:37"))
    h = head(st)
    assert run(st, _et("12:00"), "--dry-run") == deadman.EXIT_TRIPPED
    assert not (st / "health" / "deadman.json").exists() and head(st) == h


def test_protective_level_rules():
    assert deadman.protective_level({"stop": 12.345}) == (12.35, "current stop")
    assert deadman.protective_level({"avg_cost": 100.0, "atr_14": 4.0}) == (90.0, "entry - 2.5x ATR(14)")
    assert deadman.protective_level({"avg_cost": 100.0}) == (None, "no stop and no ATR on the position")


def test_cli_and_not_a_repo(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "runner" / "deadman.py"), "--state", str(tmp_path),
                        "--now", deadman.iso(_et("12:00"))], capture_output=True, text=True)
    assert r.returncode == 2 and "not a git repository" in r.stdout
