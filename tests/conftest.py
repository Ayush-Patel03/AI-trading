import datetime as dt
import json
import pathlib
import shutil
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
FIX = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(ENGINE))


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    """A staged run directory: the engine modules plus the three books.

    Mirrors what the clone step produces, so a test exercises the same layout a real
    scheduled run does.
    """
    for f in ENGINE.iterdir():
        if f.suffix in (".py", ".json"):
            shutil.copy(f, tmp_path / f.name)
    for name, dest in (("book_swing.json", "paper_book.json"),
                       ("book_pullback.json", "paper_book_pullback.json"),
                       ("book_momentum.json", "paper_book_momentum.json")):
        shutil.copy(FIX / name, tmp_path / dest)
    monkeypatch.setenv("SCAN_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def quotes(run_dir):
    """The frozen quote payload, restamped to now so the 30-minute age gate passes."""
    payload = json.loads((FIX / "quotes.json").read_text())
    ts = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    for row in payload["data"]["results"]:
        q = row["quote"]
        q["venue_last_trade_time"] = ts
        q["venue_bid_time"] = q["venue_ask_time"] = ts
    (run_dir / "pm_quotes.json").write_text(json.dumps(payload))
    return payload


def _fresh_scan_meta(s):
    """Stamp the scan as 30 minutes old in America/New_York.

    A fixed clock time (the fixture ships "10:00") silently ages past pm.py's
    240-minute freshness limit as the day goes on, so the same test passes in the
    morning and freezes entries in the afternoon. Freshness is relative to now or the
    suite is not deterministic.
    """
    from zoneinfo import ZoneInfo
    now_et = dt.datetime.now(ZoneInfo("America/New_York")) - dt.timedelta(minutes=30)
    s["meta"]["scan_date"] = now_et.date().isoformat()
    s["meta"]["time"] = now_et.strftime("%H:%M")
    return s


@pytest.fixture
def scan(run_dir):
    """A one-candidate scan, fresh by construction, so freshness and macro gates behave."""
    s = _fresh_scan_meta(json.loads((FIX / "scan.json").read_text()))
    (run_dir / "scan_results.json").write_text(json.dumps(s))
    return s


@pytest.fixture
def pm(run_dir):
    """The engine, freshly imported against this run_dir. Reload matters: pm caches the
    active desk and the peer books in module globals, so a stale import would leak one
    test's desk selection into the next."""
    import importlib
    import pm as _pm
    importlib.reload(_pm)
    _pm.DESK.update({"name": "swing", "filter": {}, "suffix": ""})
    _pm.PEERS.update({"books": {}, "loaded": [], "missing": []})
    return _pm


def run_pm(pm, run_dir, slot="opening-range", book="paper_book.json", desk=None,
           with_scan=False, peers=True):
    """Drive one engine run the way a scheduled slot does."""
    if desk:
        cfg = json.loads((run_dir / "desks.json").read_text())["desks"][desk]
        pm.DESK.update({"name": desk, "filter": cfg.get("filter") or {}, "suffix": f"-{desk}"})
        book = cfg["book"]
    if peers:
        pm.load_peers("desks.json", pm.DESK["name"], book)
    b = json.loads((run_dir / book).read_text())
    prices = {}
    qpath = run_dir / "pm_quotes.json"
    if qpath.exists():
        prices, _ = pm.quotes_to_prices(json.loads(qpath.read_text()), pm._now())
    s = None
    spath = run_dir / "scan_results.json"
    if with_scan and spath.exists():
        s = json.loads(spath.read_text())
    return pm.run(b, s, prices, slot, None, "paper")
