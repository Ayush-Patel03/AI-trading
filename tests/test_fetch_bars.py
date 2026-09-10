"""runner/fetch_bars.py — Alpaca daily bars into the shape the harness reads.

Every request goes through a monkeypatched `urllib.request.urlopen`; nothing here touches
the network. The fake serves two pages for a batch (next_page_token on the first), one 429
before a success, and an empty `bars` for a delisted name.
"""
import io
import json
import pathlib
import sys
import urllib.error
import urllib.parse

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
sys.path.insert(0, str(ROOT / "engine"))
import fetch_bars  # noqa: E402
import backtest    # noqa: E402
import validate    # noqa: E402
import technicals  # noqa: E402


def _bar(day, px, vol=1000):
    return {"t": f"2024-01-{day:02d}T05:00:00Z", "o": px, "h": px + 1, "l": px - 1, "c": px + 0.5,
            "v": vol, "n": 10, "vw": px}


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAlpaca:
    """Serves /v2/stocks/bars: page 1 carries next_page_token 'p2', page 2 the rest.
    The first request ever answers 429 once. DEAD never has bars, and a dotted class-share
    ticker (BRK.B) is served only under its hyphen spelling (BRK-B)."""

    def __init__(self):
        self.calls = []          # parsed query dicts, in order
        self.headers_seen = []
        self.fail_429_once = True

    def __call__(self, req, timeout=None):
        url = req.full_url
        q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}
        self.calls.append(q)
        self.headers_seen.append({k.lower(): v for k, v in req.header_items()})
        if self.fail_429_once:
            self.fail_429_once = False
            raise urllib.error.HTTPError(url, 429, "Too Many Requests",
                                         {"Retry-After": "0"}, io.BytesIO(b"rate limited"))
        self.timeframes = getattr(self, "timeframes", set())
        self.timeframes.add(q["timeframe"])
        assert q["timeframe"] in ("1Day", "5Min") and q["feed"] == "iex" and q["adjustment"] == "all"
        assert q["limit"] == "10000"
        syms = q["symbols"].split(",")
        page = q.get("page_token")
        if page is None:
            bars = {s: [_bar(2, 100.0), _bar(3, 101.0)] for s in syms if s != "DEAD" and "." not in s}
            return FakeResponse({"bars": bars, "next_page_token": "p2"})
        assert page == "p2"
        bars = {s: [_bar(4, 102.0)] for s in syms if s != "DEAD" and "." not in s}
        return FakeResponse({"bars": bars, "next_page_token": None})


@pytest.fixture
def alpaca(monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setattr(fetch_bars.urllib.request, "urlopen", fake)
    monkeypatch.setattr(fetch_bars.time, "sleep", lambda s: None)
    monkeypatch.setenv("APCA_API_KEY_ID", "k-test")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s-test")
    return fake


@pytest.fixture
def universe(tmp_path):
    doc = {"name": "sp500-history", "index": "sp500", "membership": {
        "AAPL": [["2019-01-18", None]],
        "DEAD": [["2019-01-18", "2024-06-01"]],       # left inside the window, no bars served
        "OLD": [["2019-01-18", "2020-01-01"]],        # left before --since: never requested
        "BRK.B": [["2019-01-18", None]],
    }}
    p = tmp_path / "universe.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _run(tmp_path, universe, *extra):
    out = tmp_path / "bars_all.json"
    rc = fetch_bars.main(["--universe", str(universe), "--since", "2023-01-01",
                          "--symbols", "SPY,XLK", "--start", "2024-01-01", "--end", "2024-01-31",
                          "--out", str(out), "--batch", "10", *extra])
    return rc, out


# ------------------------------------------------------------------ shape
def test_output_is_the_shape_every_loader_reads(tmp_path, universe, alpaca):
    rc, out = _run(tmp_path, universe)
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert set(doc) == {"meta", "data"}
    results = doc["data"]["results"]
    by_sym = {r["symbol"]: r["bars"] for r in results}
    # AAPL, BRK.B (via the hyphen retry), SPY, XLK have bars; DEAD does not; OLD was never asked.
    assert set(by_sym) == {"AAPL", "BRK.B", "SPY", "XLK"}
    bars = by_sym["AAPL"]
    assert [b["begins_at"][:10] for b in bars] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert set(bars[0]) == {"begins_at", "open_price", "high_price", "low_price", "close_price", "volume"}
    assert bars[2]["close_price"] == 102.5 and bars[2]["volume"] == 1000
    assert doc["meta"]["adjustment"] == "all" and doc["meta"]["feed"] == "iex"

    # The three readers agree on it.
    bb = backtest.load_bars(str(out))
    assert bb["AAPL"][-1]["close_price"] == 102.5 and len(bb["SPY"]) == 3
    vb = validate.load_bars(str(out))
    assert vb["XLK"] == [("2024-01-02", 100.5), ("2024-01-03", 101.5), ("2024-01-04", 102.5)]
    assert technicals._clean_bars(bb["AAPL"])[0]["t"] == "2024-01-02"
    assert technicals.derive(bb["AAPL"])["bars_used"] == 3


def test_pagination_retry_and_headers(tmp_path, universe, alpaca):
    rc, out = _run(tmp_path, universe)
    assert rc == 0
    # batch 1 (AAPL, BRK.B, DEAD, SPY, XLK): the 429, then page 1, then page 2;
    # then the alternate-spelling pass for BRK.B (two pages). "OLD" is never requested.
    tokens = [c.get("page_token") for c in alpaca.calls]
    assert tokens == [None, None, "p2", None, "p2"]
    assert all("OLD" not in c["symbols"].split(",") for c in alpaca.calls)
    assert alpaca.calls[0]["symbols"].split(",") == ["AAPL", "BRK.B", "DEAD", "SPY", "XLK"]
    assert alpaca.calls[3]["symbols"] == "BRK-B"
    assert alpaca.calls[0]["start"] == "2024-01-01" and alpaca.calls[0]["end"] == "2024-01-31"
    h = alpaca.headers_seen[0]
    assert h["apca-api-key-id"] == "k-test" and h["apca-api-secret-key"] == "s-test"


# ------------------------------------------------------------------ missing list
def test_missing_list_is_the_residual_survivorship_statement(tmp_path, universe, alpaca):
    rc, out = _run(tmp_path, universe)
    miss = json.loads((tmp_path / "bars_missing.json").read_text(encoding="utf-8"))
    assert miss["missing"] == ["DEAD"]
    assert miss["missing_former_members"] == ["DEAD"]
    assert miss["missing_current_members"] == []
    assert miss["n_requested"] == 5 and miss["n_missing"] == 1
    assert "NOT COVERED" in miss["_what"]


# ------------------------------------------------------------------ resume
def test_resume_skips_symbols_already_in_the_file(tmp_path, universe, alpaca):
    rc, out = _run(tmp_path, universe)
    assert rc == 0
    n_calls = len(alpaca.calls)
    alpaca.fail_429_once = False
    # Second run with --resume: only DEAD is still missing, so one batch of one symbol.
    rc, out = _run(tmp_path, universe, "--resume")
    assert rc == 0
    new = alpaca.calls[n_calls:]
    assert [c["symbols"] for c in new] == ["DEAD", "DEAD"]      # its two pages, nothing else
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert {r["symbol"] for r in doc["data"]["results"]} == {"AAPL", "BRK.B", "SPY", "XLK"}
    assert len(next(r for r in doc["data"]["results"] if r["symbol"] == "AAPL")["bars"]) == 3


def test_a_failed_batch_writes_a_partial_file_and_exits_2(tmp_path, universe, monkeypatch):
    def always_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 503, "down", {}, io.BytesIO(b""))
    monkeypatch.setattr(fetch_bars.urllib.request, "urlopen", always_500)
    monkeypatch.setattr(fetch_bars.time, "sleep", lambda s: None)
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    rc, out = _run(tmp_path, universe)
    assert rc == 2
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["meta"]["partial"] is True and "503" in doc["meta"]["error"]
    assert doc["data"]["results"] == []


# ------------------------------------------------------------------ sector map
def test_sector_map_from_csv(tmp_path, universe, alpaca):
    csv_path = tmp_path / "sectors.csv"
    csv_path.write_text("Symbol,Security,GICS Sector\nAAPL,Apple,Information Technology\n"
                        "BRK-B,Berkshire,Financials\nDEAD,Gone,Energy\n", encoding="utf-8")
    smap_path = tmp_path / "sector_map.json"
    rc, out = _run(tmp_path, universe, "--sectors-csv", str(csv_path),
                   "--sector-map-out", str(smap_path))
    assert rc == 0
    smap = json.loads(smap_path.read_text(encoding="utf-8"))
    # keyed by the universe spelling; the CSV's hyphen form still matches; ETFs/SPY excluded
    assert smap == {"AAPL": "XLK", "BRK.B": "XLF", "DEAD": "XLE"}


def test_sector_map_from_the_universe_file(tmp_path, alpaca):
    doc = {"name": "u", "membership": {"MSFT": [[None, None]], "XOM": [[None, None]]},
           "sectors": {"MSFT": "Information Technology", "XOM": "Energy"}}
    u = tmp_path / "u.json"
    u.write_text(json.dumps(doc), encoding="utf-8")
    smap_path = tmp_path / "sector_map.json"
    rc, out = _run(tmp_path, u, "--sector-map-out", str(smap_path))
    assert json.loads(smap_path.read_text(encoding="utf-8")) == {"MSFT": "XLK", "XOM": "XLE"}


# ------------------------------------------------------------------ keys and offline source
def test_keyfile_outside_the_repo_is_read_and_inside_is_refused(tmp_path):
    kf = tmp_path / "alpaca.env"
    kf.write_text("APCA_API_KEY_ID=abc\nAPCA_API_SECRET_KEY='xyz'\n", encoding="utf-8")
    assert fetch_bars.load_keys(str(kf)) == ("abc", "xyz")
    inside = ROOT / "runner" / "alpaca.env.test-should-not-exist"
    with pytest.raises(SystemExit, match="inside the engine repo"):
        fetch_bars.load_keys(str(inside))
    with pytest.raises(SystemExit, match="no Alpaca credentials"):
        fetch_bars.load_keys(None, env={})


def test_source_file_re_emits_a_robinhood_dump(tmp_path):
    rh = {"data": {"results": [{"symbol": "nvda", "bars": [
        {"begins_at": "2024-01-03T00:00:00Z", "open_price": "2", "high_price": "3",
         "low_price": "1", "close_price": "2.5", "volume": "9"},
        {"begins_at": "2024-01-02T00:00:00Z", "open_price": "1", "high_price": "2",
         "low_price": "1", "close_price": "1.5", "volume": "8"}]}]}}
    src = tmp_path / "rh.json"
    src.write_text(json.dumps(rh), encoding="utf-8")
    out = tmp_path / "out.json"
    rc = fetch_bars.main(["--source", "file", "--file", str(src), "--start", "2024-01-01",
                          "--end", "2024-01-31", "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["data"]["results"][0]["symbol"] == "NVDA"
    assert [b["begins_at"][:10] for b in doc["data"]["results"][0]["bars"]] == ["2024-01-02", "2024-01-03"]
    assert not (tmp_path / "bars_missing.json").exists()


# ------------------------------------------------------------------ D-03: 5-minute bars
def test_timeframe_5min_is_sent_stamped_and_read_by_orb(tmp_path, universe, alpaca):
    """--timeframe 5Min goes to Alpaca as the timeframe, is recorded in meta, and every
    result carries Robinhood's `interval "5minute"` so orb.bars_by_symbol accepts the file
    (and a daily file, with no stamp, is still what backtest.load_bars reads)."""
    rc, out = _run(tmp_path, universe, "--timeframe", "5Min")
    assert rc == 0 and alpaca.timeframes == {"5Min"}
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["meta"]["timeframe"] == "5Min"
    assert all(r["interval"] == "5minute" for r in doc["data"]["results"])
    import orb
    by_sym, notes = orb.bars_by_symbol(doc)
    assert notes == [] and "AAPL" in by_sym and by_sym["AAPL"][0]["bucket"]
    # daily stays unstamped
    rc, out = _run(tmp_path, universe)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["meta"]["timeframe"] == "1Day"
    assert all("interval" not in r for r in doc["data"]["results"])
    with pytest.raises(SystemExit):
        _run(tmp_path, universe, "--timeframe", "1Min")
