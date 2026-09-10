"""history.py — the followed set (S-03): the survivorship-free roster behind validate.py.

A symbol enters on the first scan snapshot it appears in and stays open for twenty
business days after it was last seen, whether or not it is still in the universe.
"""
import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def history(run_dir):
    import importlib
    import history as h
    importlib.reload(h)
    return h


def test_business_days_after_skips_weekends(history):
    # Thu 2026-09-10 + 1 = Fri 11th; + 2 = Mon 14th; + 20 = Thu 2026-10-08
    assert history.business_days_after("2026-09-10", 1) == dt.date(2026, 9, 11)
    assert history.business_days_after("2026-09-10", 2) == dt.date(2026, 9, 14)
    assert history.business_days_after("2026-09-10", 20) == dt.date(2026, 10, 8)
    assert history.business_days_after(dt.date(2026, 9, 12), 0) == dt.date(2026, 9, 12)


def test_add_records_first_seen_slot_and_horizon(history, tmp_path):
    arch = str(tmp_path / "archive")
    doc, added, closed = history.update_followed(arch, ["nvda", "MU", ""], "2026-09-10",
                                                 slot="premarket", run_id="2026-09-10-premarket")
    assert added == ["NVDA", "MU"] and closed == []
    rec = doc["symbols"]["NVDA"]
    assert rec == {"symbol": "NVDA", "first_seen": "2026-09-10", "first_slot": "premarket",
                   "first_run_id": "2026-09-10-premarket", "runs": 1, "last_seen": "2026-09-10",
                   "last_slot": "premarket", "horizon_end_date": "2026-10-08", "status": "open"}
    assert doc["open"] == 2 and doc["updated"] == "2026-09-10"
    on_disk = json.loads((tmp_path / "archive" / "followed.json").read_text(encoding="utf-8"))
    assert on_disk["symbols"] == doc["symbols"]
    assert on_disk["horizon_sessions"] == 20


def test_a_repeat_sighting_keeps_first_seen_and_extends_the_horizon(history, tmp_path):
    arch = str(tmp_path / "archive")
    history.update_followed(arch, ["NVDA"], "2026-09-10", slot="premarket")
    doc, added, _ = history.update_followed(arch, ["NVDA", "XOM"], "2026-09-15", slot="midday")
    assert added == ["XOM"]
    nvda = doc["symbols"]["NVDA"]
    assert nvda["first_seen"] == "2026-09-10" and nvda["first_slot"] == "premarket"
    assert nvda["last_seen"] == "2026-09-15" and nvda["last_slot"] == "midday"
    assert nvda["horizon_end_date"] == history.business_days_after("2026-09-15", 20).isoformat()
    assert nvda["runs"] == 2
    # a late/stale run dated earlier must not pull the horizon back
    doc, _, _ = history.update_followed(arch, ["NVDA"], "2026-09-11", slot="power-hour")
    assert doc["symbols"]["NVDA"]["last_seen"] == "2026-09-15"


def test_closes_after_twenty_business_days_even_when_it_left_the_universe(history, tmp_path):
    arch = str(tmp_path / "archive")
    history.update_followed(arch, ["NVDA", "GONE"], "2026-09-10", slot="premarket")
    # GONE never scans again. On the horizon's last day it is still open...
    doc, _, closed = history.update_followed(arch, ["NVDA"], "2026-10-08", slot="premarket")
    assert closed == [] and doc["symbols"]["GONE"]["status"] == "open"
    assert history.followed_symbols(arch) == ["GONE", "NVDA"]
    # ...and the next session it is closed, while NVDA (seen daily) stays open.
    doc, _, closed = history.update_followed(arch, ["NVDA"], "2026-10-09", slot="premarket")
    assert closed == ["GONE"]
    assert doc["symbols"]["GONE"]["status"] == "closed"
    assert doc["symbols"]["GONE"]["closed_on"] == "2026-10-09"
    assert doc["symbols"]["GONE"]["first_seen"] == "2026-09-10"
    assert doc["symbols"]["NVDA"]["status"] == "open"
    assert history.followed_symbols(arch) == ["NVDA"]
    assert history.followed_symbols(arch, status="closed") == ["GONE"]
    assert history.followed_symbols(arch, status="all") == ["GONE", "NVDA"]
    # seen again later: reopened, first_seen intact
    doc, added, _ = history.update_followed(arch, ["GONE"], "2026-10-20", slot="midday")
    assert added == [] and doc["symbols"]["GONE"]["status"] == "open"
    assert doc["symbols"]["GONE"]["first_seen"] == "2026-09-10"
    assert doc["symbols"]["GONE"]["reopened_on"] == "2026-10-20"


def test_followed_symbols_evaluates_the_horizon_read_only(history, tmp_path):
    """The bar-fetching prompt may run days after the last scan: the list it gets is
    evaluated at ITS date, without writing the file."""
    arch = str(tmp_path / "archive")
    history.update_followed(arch, ["A", "B"], "2026-09-10", slot="premarket")
    before = (tmp_path / "archive" / "followed.json").read_bytes()
    assert history.followed_symbols(arch, as_of="2026-10-08") == ["A", "B"]
    assert history.followed_symbols(arch, as_of="2026-10-09") == []
    assert (tmp_path / "archive" / "followed.json").read_bytes() == before
    assert history.followed_symbols(str(tmp_path / "nothing-here")) == []


def test_update_refuses_a_missing_date(history, tmp_path):
    with pytest.raises(ValueError):
        history.update_followed(str(tmp_path), ["A"], None)


def test_cli_prints_the_open_list(history, tmp_path):
    arch = tmp_path / "archive"
    history.update_followed(str(arch), ["NVDA", "GONE"], "2026-09-10", slot="premarket")
    history.update_followed(str(arch), ["NVDA"], "2026-10-09", slot="premarket")
    exe = [sys.executable, str(ROOT / "engine" / "history.py"), "--followed", "--archive", str(arch)]
    r = subprocess.run(exe + ["--as-of", "2026-10-09"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["NVDA"]
    assert "1 open symbol(s) followed as of 2026-10-09" in r.stderr
    r = subprocess.run(exe + ["--as-of", "2026-10-09", "--all"], capture_output=True, text=True)
    assert r.stdout.split() == ["GONE", "NVDA"]
    r = subprocess.run(exe + ["--json"], capture_output=True, text=True)
    doc = json.loads(r.stdout)
    assert set(doc["symbols"]) == {"GONE", "NVDA"}
    # the merge path still demands --entry
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "history.py")],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "--entry is required" in r.stderr


def test_validate_reports_followed_symbols_without_bars(history, tmp_path):
    import validate
    arch = tmp_path / "archive"
    history.update_followed(str(arch), ["NVDA", "GONE"], "2026-09-10", slot="premarket")
    recs = tmp_path / "records"
    recs.mkdir()
    (recs / "2026-09-10-premarket.json").write_text(json.dumps({
        "date": "2026-09-10", "slot": "premarket", "time": "08:00",
        "results": [{"ticker": "NVDA", "price": 100.0, "score": 80.0},
                    {"ticker": "GONE", "price": 10.0, "score": 20.0}]}), encoding="utf-8")
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps({"data": {"results": [
        {"symbol": "NVDA", "bars": [{"begins_at": "2026-09-10T00:00:00Z", "close_price": "100"},
                                    {"begins_at": "2026-09-11T00:00:00Z", "close_price": "101"}]}]}}),
                    encoding="utf-8")
    out = tmp_path / "v.json"
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "validate.py"), "--records", str(recs),
                        "--bars", str(bars), "--horizons", "1", "--out", str(out),
                        "--md", str(tmp_path / "v.md"), "--archive", str(arch), "--as-of", "2026-09-12"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["followed_open"] == ["GONE", "NVDA"]
    assert res["followed_without_bars"] == ["GONE"]
    assert "GONE" in r.stderr
    assert validate.__doc__
