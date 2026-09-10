"""engine/snapshots.py — the per-slot raw-input archives (S-01).

The scan snapshot is checked against the fixture scan the suite already scores; the chain
snapshot against a small synthetic chain whose derived values are computed by hand here.
"""
import datetime as dt
import gzip
import json
import math
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).parent / "fixtures"
AS_OF = dt.date(2026, 9, 10)                       # a Thursday


@pytest.fixture
def snapshots(run_dir):
    import importlib
    import snapshots as s
    importlib.reload(s)
    return s


def _stage_scan(run_dir, with_quotes=True):
    """scan_data.json scored into scan_results.json, the way scanner.py's CLI does it."""
    import scanner
    import archive
    data = json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))
    (run_dir / "scan_data.json").write_text(json.dumps(data), encoding="utf-8")
    out = scanner.scan(json.loads(json.dumps(data)))
    out["meta"]["run_id"] = archive.run_id(out["meta"])
    (run_dir / "scan_results.json").write_text(json.dumps(out), encoding="utf-8")
    if with_quotes:
        (run_dir / "quotes.json").write_text((FIX / "quotes.json").read_text(encoding="utf-8"),
                                             encoding="utf-8")
    return out


# ================================================================ scan snapshot
def test_scan_snapshot_header_and_row_count(snapshots, run_dir):
    out = _stage_scan(run_dir)
    path, n = snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"),
                                            {"run_id": out["meta"]["run_id"], "slot": "Midday"})
    assert path.endswith("archive/scan_snapshot/2026-09-02-midday.jsonl.gz")
    meta, rows = snapshots.read_snapshot(path)
    assert meta["kind"] == "scan_snapshot"
    assert meta["run_id"] == "2026-09-02-midday"
    assert meta["slot"] == "midday" and meta["date"] == "2026-09-02"
    assert meta["as_of"] == "2026-09-02T12:30"
    assert "engine_sha" in meta
    # every candidate the scanner was GIVEN is a row — including the one it dropped
    assert meta["n_rows"] == n == len(rows) == 5
    by = {r["symbol"]: r for r in rows}
    assert by["NOPX"]["dropped"] is True and by["NOPX"]["score"] is None
    assert by["NOPX"]["rank"] is None
    scored = [r for r in rows if not r["dropped"]]
    assert [r["rank"] for r in scored] == [1, 2, 3, 4]
    assert scored[0]["score"] == out["results"][0]["score"]


def test_scan_snapshot_carries_inputs_scores_pillars_verdict_and_regime(snapshots, run_dir):
    out = _stage_scan(run_dir)
    path, _ = snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"))
    _, rows = snapshots.read_snapshot(path)
    nvda = next(r for r in rows if r["symbol"] == "NVDA")
    cand = json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))["candidates"]["NVDA"]
    for k, v in cand.items():                      # the raw inputs, as the scanner saw them
        if k not in ("rel_volume",):               # scanner.py recomputes rel_volume
            assert nvda[k] == v, k
    scored = next(r for r in out["results"] if r["ticker"] == "NVDA")
    assert nvda["pillars"] == scored["pillars"]
    assert nvda["verdict"] == scored["verdict"] and nvda["setup"] == scored["setup"]
    assert nvda["coverage_pct"] == scored["coverage_pct"]
    assert nvda["regime"] == out["regime"]["label"]
    assert nvda["regime_multiplier"] == out["regime"]["multiplier"]
    for prose in ("reasons", "setup_note", "verdict_note", "confidence_note"):
        assert prose not in nvda


def test_scan_snapshot_bid_ask_last_from_the_staged_quotes(snapshots, run_dir):
    _stage_scan(run_dir, with_quotes=True)
    path, _ = snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"))
    meta, rows = snapshots.read_snapshot(path)
    by = {r["symbol"]: r for r in rows}
    assert by["MU"]["bid"] == 941.62 and by["MU"]["ask"] == 942.1 and by["MU"]["last"] == 941.9
    assert by["MU"]["quote_ts"] == "2026-09-02T14:34:24Z"
    assert by["NVDA"]["last"] == 222.255
    # a candidate the quote payload did not cover is null, never 0
    assert by["XOM"]["bid"] is None and by["XOM"]["ask"] is None and by["XOM"]["last"] is None
    assert by["XOM"]["quote_ts"] is None
    assert meta["n_quoted"] == 2 and "quotes.json" in meta["source_files"]


def test_scan_snapshot_without_quotes_is_all_null(snapshots, run_dir):
    _stage_scan(run_dir, with_quotes=False)
    path, _ = snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"))
    meta, rows = snapshots.read_snapshot(path)
    assert all(r["bid"] is None and r["ask"] is None and r["last"] is None for r in rows)
    assert meta["n_quoted"] == 0


def test_scan_snapshot_updates_the_followed_set(snapshots, run_dir):
    import history
    _stage_scan(run_dir)
    snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"))
    assert set(history.followed_symbols(str(run_dir / "archive"))) == \
        {"NVDA", "MU", "XOM", "THIN", "NOPX"}


def test_scanner_cli_writes_the_snapshot_and_records_it_in_meta(run_dir):
    (run_dir / "scan_data.json").write_text((FIX / "scan_data.json").read_text(encoding="utf-8"),
                                            encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "scanner.py")], cwd=str(run_dir),
                       capture_output=True, text=True, env={"SCAN_DIR": str(run_dir),
                                                            "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    res = json.loads((run_dir / "scan_results.json").read_text(encoding="utf-8"))
    assert res["meta"]["scan_snapshot"] == "archive/scan_snapshot/2026-09-02-midday.jsonl.gz"
    assert (run_dir / res["meta"]["scan_snapshot"]).exists()
    assert (run_dir / "archive" / "followed.json").exists()
    assert not any("snapshot NOT written" in w for w in res["meta"]["data_warnings"])


def test_scanner_cli_survives_a_snapshot_failure(run_dir, monkeypatch):
    """The archive dir is a FILE, so the snapshot cannot be written: the scan still
    completes and the results meta says the snapshot is missing."""
    (run_dir / "scan_data.json").write_text((FIX / "scan_data.json").read_text(encoding="utf-8"),
                                            encoding="utf-8")
    (run_dir / "archive").write_text("not a directory", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "scanner.py")], cwd=str(run_dir),
                       capture_output=True, text=True, env={"SCAN_DIR": str(run_dir),
                                                            "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    res = json.loads((run_dir / "scan_results.json").read_text(encoding="utf-8"))
    assert any("scan snapshot NOT written" in w for w in res["meta"]["data_warnings"])
    assert res["results"], "the ranking itself must be untouched"


# ================================================================ chain snapshot
def _c(sym, exp, strike, typ, bid, ask, iv, delta=None, oi=None, vol=None, last=None):
    return {"chain_symbol": sym, "expiration_date": exp, "strike_price": str(strike),
            "type": typ, "bid_price": str(bid), "ask_price": str(ask),
            "implied_volatility": iv, "delta": delta, "open_interest": oi, "volume": vol,
            "last_trade_price": last, "gamma": 0.01, "theta": -0.02, "vega": 0.1}


def synthetic_chain():
    """ACME at spot 100 on 2026-09-10. Four expiries at 1 / 8 / 36 / 71 DTE.

    Hand-computed expectations (see test_chain_derived_row_matches_hand_computation):
      atm_iv_30d   = 0.38 + (0.30 - 0.38) * (30 - 8) / (36 - 8)   = 0.317143
      atm_iv_60d   = 0.30 + (0.28 - 0.30) * (60 - 36) / (71 - 36) = 0.286286
      em_1sd       = 100 * 0.40 * sqrt(1 / 365)                   = 2.093700
      straddle     = (1.0 + 1.2) / 2 + (0.9 + 1.1) / 2             = 2.1
      skew25       = (0.36 - 0.27) / 0.30                          = 0.3
      cpiv         = (-0.03 * 400 + 0 * 1000 - 0.02 * 200) / 1600  = -0.01
      os_ratio     = 5000 contracts * 100 / 1,000,000 shares       = 0.5
    """
    S = "ACME"
    rows = []
    # 1 DTE: ATM IV 0.40 both legs; the straddle legs; volume 1000 + 1000
    rows += [_c(S, "2026-09-11", 100, "call", 1.0, 1.2, 0.40, 0.50, 10, 1000),
             _c(S, "2026-09-11", 100, "put", 0.9, 1.1, 0.40, -0.50, 10, 1000),
             _c(S, "2026-09-11", 95, "put", 0.2, 0.3, 0.45, -0.20, 5, 500),
             _c(S, "2026-09-11", 105, "call", 0.2, 0.3, 0.42, 0.20, 5, 500)]
    # 8 DTE: ATM IV 0.38
    rows += [_c(S, "2026-09-18", 100, "call", 2.0, 2.2, 0.38, 0.52, 50, 500),
             _c(S, "2026-09-18", 100, "put", 1.9, 2.1, 0.38, -0.48, 50, 500)]
    # 36 DTE: the 30-45 window. ATM 0.30; 25-delta put 0.36, 25-delta call 0.27
    rows += [_c(S, "2026-10-16", 90, "call", 11.0, 11.4, 0.31, 0.80, 100, 100),
             _c(S, "2026-10-16", 90, "put", 1.0, 1.2, 0.34, -0.25, 300, 200),
             _c(S, "2026-10-16", 100, "call", 4.0, 4.4, 0.30, 0.52, 500, 100),
             _c(S, "2026-10-16", 100, "put", 3.9, 4.3, 0.30, -0.48, 500, 100),
             _c(S, "2026-10-16", 110, "call", 1.0, 1.2, 0.27, 0.25, 100, 50),
             _c(S, "2026-10-16", 110, "put", 10.0, 10.4, 0.29, -0.78, 100, 50),
             _c(S, "2026-10-16", 85, "put", 0.5, 0.7, 0.36, -0.25, 40, 100)]
    # the 90 put sits at -0.32: the 85 put (0.36) is the one nearest -0.25, so the
    # nearest-to-target rule is exercised rather than a strike lookup
    rows[-6]["delta"] = -0.32
    # 71 DTE: ATM 0.28
    rows += [_c(S, "2026-11-20", 100, "call", 6.0, 6.4, 0.28, 0.54, 20, 100),
             _c(S, "2026-11-20", 100, "put", 5.9, 6.3, 0.28, -0.46, 20, 100)]
    # an expired contract: must be ignored everywhere
    rows += [_c(S, "2026-09-04", 100, "call", 0.0, 0.05, 0.90, 0.5, 1, 100000)]
    return {"data": {"results": rows}}


def test_chain_derived_row_matches_hand_computation(snapshots):
    chains = snapshots.normalise_chain(synthetic_chain())
    assert set(chains) == {"ACME"}
    assert len(chains["ACME"]["contracts"]) == 16          # the reader keeps the expired one
    chains["ACME"]["spot"] = 100.0
    chains["ACME"]["share_volume"] = 1_000_000.0
    d = snapshots.derived_row("ACME", chains["ACME"], AS_OF)
    assert d["_derived"] is True and d["symbol"] == "ACME" and d["spot"] == 100.0
    assert d["nearest_expiry"] == "2026-09-11" and d["nearest_dte"] == 1
    assert d["expiry_30_45"] == "2026-10-16"
    assert d["expiries"] == ["2026-09-11", "2026-09-18", "2026-10-16", "2026-11-20"]
    assert d["atm_iv_nearest"] == pytest.approx(0.40)
    assert d["atm_iv_30d"] == pytest.approx(0.38 + (0.30 - 0.38) * 22 / 28, abs=1e-6)
    assert d["atm_iv_60d"] == pytest.approx(0.30 + (0.28 - 0.30) * 24 / 35, abs=1e-6)
    assert d["em_1sd"] == pytest.approx(100 * 0.40 * math.sqrt(1 / 365), abs=1e-4)
    assert d["em_1sd_pct"] == pytest.approx(0.40 * math.sqrt(1 / 365) * 100, abs=1e-4)
    assert d["straddle_price"] == pytest.approx(2.1)
    assert d["straddle_pct"] == pytest.approx(2.1)
    assert d["skew25"] == pytest.approx((0.36 - 0.27) / 0.30, abs=1e-6)
    assert d["cpiv"] == pytest.approx((-0.03 * 400 + 0.0 * 1000 - 0.02 * 200) / 1600, abs=1e-6)
    # every live contract's volume counts: 3000 + 1000 + 700 + 200 = 4900; the expired
    # contract's 100000 must NOT be in it
    assert d["os_ratio"] == pytest.approx(4900 * 100 / 1_000_000, abs=1e-6)
    assert d["n_contracts"] == 15                          # the derived row drops it


def test_chain_derived_nulls_are_null_not_zero(snapshots):
    """No 30-45 DTE expiry, no 60-day bracket, no share volume, no deltas."""
    rows = [_c("ZZ", "2026-09-11", 50, "call", 1.0, 1.2, 0.5),
            _c("ZZ", "2026-09-11", 50, "put", 1.0, 1.2, 0.5),
            _c("ZZ", "2026-09-25", 50, "call", 2.0, 2.2, 0.45)]
    chains = snapshots.normalise_chain(rows)
    chains["ZZ"]["spot"] = 50.0
    d = snapshots.derived_row("ZZ", chains["ZZ"], AS_OF)
    assert d["atm_iv_30d"] is None and d["atm_iv_60d"] is None
    assert d["skew25"] is None and d["expiry_30_45"] is None
    assert d["os_ratio"] is None and d["share_volume"] is None
    assert d["straddle_price"] == pytest.approx(2.2)
    assert d["em_1sd"] == pytest.approx(50 * 0.5 * math.sqrt(1 / 365), abs=1e-6)
    # cpiv falls back to the nearest expiry: one matched strike, no OI -> plain mean
    assert d["cpiv"] == pytest.approx(0.0)
    # no spot at all: nothing derived, everything null
    chains["ZZ"]["spot"] = None
    d2 = snapshots.derived_row("ZZ", chains["ZZ"], AS_OF)
    assert d2["em_1sd"] is None and d2["straddle_price"] is None and d2["atm_iv_30d"] is None


def test_chain_rows_are_trimmed_to_four_expiries_and_ten_strikes_a_side(snapshots):
    rows = []
    for i, exp in enumerate(["2026-09-11", "2026-09-18", "2026-09-25", "2026-10-02",
                             "2026-10-16", "2026-11-20"]):
        for k in range(50, 151, 2):                # 51 strikes a side
            rows.append(_c("BIG", exp, k, "call", 1, 1.1, 0.3))
            rows.append(_c("BIG", exp, k, "put", 1, 1.1, 0.3))
    chains = snapshots.normalise_chain(rows)
    out = snapshots.chain_rows(chains, AS_OF, spots={"BIG": 100.0})
    contracts = [r for r in out if not r.get("_derived")]
    derived = [r for r in out if r.get("_derived")]
    assert len(derived) == 1 and derived[0]["symbol"] == "BIG"
    assert sorted({r["expiry"] for r in contracts}) == ["2026-09-11", "2026-09-18",
                                                        "2026-09-25", "2026-10-02"]
    per = {}
    for r in contracts:
        per.setdefault((r["expiry"], r["type"]), []).append(r["strike"])
    for strikes in per.values():
        assert len(strikes) == 21 and min(strikes) == 80 and max(strikes) == 120
    # the derived row saw ALL six expiries, so the 60-day point is bracketed
    assert derived[0]["atm_iv_60d"] == pytest.approx(0.3)
    row = contracts[0]
    assert set(row) >= {"symbol", "expiry", "dte", "strike", "type", "bid", "ask", "mid",
                        "last", "volume", "open_interest", "iv", "delta", "gamma", "theta",
                        "vega", "spot"}
    assert row["mid"] == pytest.approx(1.05) and row["spot"] == 100.0


def test_chain_reader_accepts_cboe_occ_codes_and_symbol_maps(snapshots):
    cboe = {"data": {"symbol": "AAPL", "current_price": 150.25, "options": [
        {"option": "AAPL260918C00150000", "bid": 3.0, "ask": 3.2, "iv": 0.31,
         "open_interest": 100, "volume": 10, "delta": 0.5},
        {"option": "AAPL260918P00150000", "bid": 2.9, "ask": 3.1, "iv": 0.33,
         "open_interest": 120, "volume": 12, "delta": -0.5},
        {"option": "AAPL261016C00160000", "bid": 1.0, "ask": 1.1, "iv": 29.0}]}}   # IV in %
    chains = snapshots.normalise_chain(cboe)
    a = chains["AAPL"]
    assert a["spot"] == 150.25 and len(a["contracts"]) == 3
    c0 = a["contracts"][0]
    assert (c0["expiry"], c0["strike"], c0["type"]) == ("2026-09-18", 150.0, "call")
    assert a["contracts"][2]["iv"] == pytest.approx(0.29)
    # {SYMBOL: {...}} map with plain rows lacking a symbol of their own
    m = {"MSFT": {"spot": 400, "share_volume": 20_000_000,
                  "contracts": [{"expiry": "2026-10-16", "strike": 400, "type": "P",
                                 "bid": 10, "ask": 10.5, "iv": 0.25, "volume": 3000}]}}
    chains = snapshots.normalise_chain(m)
    assert chains["MSFT"]["spot"] == 400 and chains["MSFT"]["share_volume"] == 20_000_000
    assert chains["MSFT"]["contracts"][0]["type"] == "put"
    d = snapshots.derived_row("MSFT", chains["MSFT"], AS_OF)
    assert d["os_ratio"] == pytest.approx(3000 * 100 / 20_000_000)


def test_chain_snapshot_file_with_spot_from_quotes_and_volume_from_scan_data(snapshots, run_dir):
    _stage_scan(run_dir)                            # quotes.json carries MU at 941.9
    chain = synthetic_chain()
    for r in chain["data"]["results"]:
        r["chain_symbol"] = "MU"
    (run_dir / "option_chains.json").write_text(json.dumps(chain), encoding="utf-8")
    path, n = snapshots.write_chain_snapshot(str(run_dir), str(run_dir / "archive"),
                                             {"slot": "power-hour", "as_of": "2026-09-10T19:45:00Z"})
    assert path.endswith("archive/chain_snapshot/2026-09-10-power-hour.jsonl.gz")
    meta, rows = snapshots.read_snapshot(path)
    assert meta["kind"] == "chain_snapshot" and meta["slot"] == "power-hour"
    assert meta["date"] == "2026-09-10" and meta["n_rows"] == n == len(rows)
    assert meta["source_files"] == ["option_chains.json"] and meta["n_symbols"] == 1
    d = rows[-1]
    assert d["_derived"] and d["symbol"] == "MU"
    assert d["spot"] == 941.9, "spot falls back to the staged quote"
    mu_vol = json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))["candidates"]["MU"]["volume"]
    assert d["share_volume"] == mu_vol
    assert d["os_ratio"] == pytest.approx(4900 * 100 / mu_vol)
    assert all(r["spot"] == 941.9 for r in rows if not r.get("_derived"))


def test_chain_snapshot_without_a_chain_file_is_header_only(snapshots, run_dir):
    assert snapshots.chain_files(str(run_dir)) == []
    path, n = snapshots.write_chain_snapshot(str(run_dir), str(run_dir / "archive"),
                                             {"slot": "midday", "as_of": "2026-09-10T17:15:00Z",
                                              "run_id": "2026-09-10-midday"})
    assert n == 0
    meta, rows = snapshots.read_snapshot(path)
    assert rows == [] and meta["n_rows"] == 0 and meta["source_files"] == []
    assert meta["run_id"] == "2026-09-10-midday"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert len(fh.read().strip().splitlines()) == 1


def test_sentinel_chain_snapshot_carries_the_clock(snapshots, run_dir):
    (run_dir / "option_chains.json").write_text(json.dumps(synthetic_chain()), encoding="utf-8")
    path, _ = snapshots.write_chain_snapshot(str(run_dir), str(run_dir / "archive"),
                                             {"slot": "sentinel", "as_of": "2026-09-10T14:35:10Z"})
    assert path.endswith("2026-09-10-sentinel-1435.jsonl.gz")


def test_pm_writes_the_chain_snapshot_when_a_chain_is_staged(run_dir, quotes, scan):
    (run_dir / "option_chains.json").write_text(json.dumps(synthetic_chain()), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "pm.py"), "--slot", "midday",
                        "--desk", "swing", "--book", "paper_book.json",
                        "--journal", "pm_journal_current.json"],
                       cwd=str(run_dir), capture_output=True, text=True,
                       env={"SCAN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    state = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    snap = state["chain_snapshot"]
    assert snap["path"].startswith("archive/chain_snapshot/") and snap["rows"] > 0
    assert (run_dir / snap["path"]).exists()
    assert "chain_snapshot" not in state["journal"], "the decision record is untouched"


def test_pm_without_a_chain_file_writes_no_chain_snapshot(run_dir, quotes, scan):
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "pm.py"), "--slot", "midday",
                        "--desk", "swing", "--book", "paper_book.json",
                        "--journal", "pm_journal_current.json"],
                       cwd=str(run_dir), capture_output=True, text=True,
                       env={"SCAN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    state = json.loads((run_dir / "pm_state.json").read_text(encoding="utf-8"))
    assert "chain_snapshot" not in state
    assert not (run_dir / "archive" / "chain_snapshot").exists()


# ================================================================ round trip and CLI
def test_read_snapshot_round_trip(snapshots, tmp_path):
    path = str(tmp_path / "x" / "y.jsonl.gz")
    rows = [{"symbol": "A", "v": 1.5, "n": None}, {"symbol": "B", "v": [1, 2], "d": {"k": "v"}}]
    snapshots.write_jsonl(path, {"run_id": "r", "kind": "test"}, rows)
    meta, back = snapshots.read_snapshot(path)
    assert back == rows
    assert meta["run_id"] == "r" and meta["n_rows"] == 2 and meta["schema"] == snapshots.SCHEMA


def test_cli_writes_both_snapshots(run_dir):
    _stage_scan(run_dir)
    (run_dir / "option_chains.json").write_text(json.dumps(synthetic_chain()), encoding="utf-8")
    out = run_dir / "arch"
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "snapshots.py"),
                        "--run-dir", str(run_dir), "--out", str(out), "--slot", "midday",
                        "--run-id", "2026-09-02-midday", "--both"],
                       capture_output=True, text=True, env={"SCAN_DIR": str(run_dir),
                                                            "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "scan snapshot" in r.stdout and "chain snapshot" in r.stdout
    assert (out / "scan_snapshot" / "2026-09-02-midday.jsonl.gz").exists()
    assert (out / "chain_snapshot" / "2026-09-02-midday.jsonl.gz").exists()
    assert (out / "followed.json").exists()
    r2 = subprocess.run([sys.executable, str(ROOT / "engine" / "snapshots.py"),
                         "--run-dir", str(run_dir / "nowhere"), "--out", str(out), "--scan"],
                        capture_output=True, text=True, env={"SCAN_DIR": str(run_dir),
                                                             "PATH": "/usr/bin:/bin"})
    assert r2.returncode == 1 and "FAILED" in r2.stderr
