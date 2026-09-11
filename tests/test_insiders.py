"""engine/insiders.py — Form 4 parsing, the routine/opportunistic rule, the cluster signal,
and the scanner pass-through (P-01, experiment E15).

Fixtures under tests/fixtures/form4/: a director's open-market purchase (raw XML with a
derivative table to ignore), an officer's sale wrapped in the EDGAR complete-submission
.txt (so the filing date is read from the header), and four filings of one ZZZ director
buying every March 2023–2026 under a 10b5-1 plan — the routine pattern. No network.
"""
import datetime as dt
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).parent / "fixtures"
FORM4 = FIX / "form4"


@pytest.fixture
def ins():
    import importlib
    import insiders as m
    importlib.reload(m)
    return m


def _txn(symbol, owner, date, code="P", shares=100.0, price=10.0, filed=None, **kw):
    t = {"symbol": symbol, "owner": owner, "owner_cik": kw.pop("owner_cik", owner),
         "date": date, "code": code, "shares": shares, "price": price,
         "value": shares * price, "filed": filed}
    t.update(kw)
    return t


# ------------------------------------------------------------------ parsing
def test_parses_a_director_open_market_purchase(ins):
    rows = ins.parse_form4_file(str(FORM4 / "0001214128-26-000123.xml"))
    assert len(rows) == 1, "the derivative table (an RSU grant) must be ignored"
    t = rows[0]
    assert t["symbol"] == "AAPL" and t["issuer_cik"] == "0000320193"
    assert t["owner"] == "DOE JANE" and t["owner_cik"] == "0001214128"
    assert t["relationship"] == ["director"] and t["officer_title"] is None
    assert t["date"] == "2026-08-20" and t["code"] == "P" and t["acquired_disposed"] == "A"
    assert t["shares"] == 1000.0 and t["price"] == 150.25 and t["value"] == 150250.0
    assert t["shares_after"] == 5000.0 and t["direct"] is True
    assert t["plan_10b5_1"] is False
    assert t["accession"] == "0001214128-26-000123"
    assert t["filed"] is None, "raw XML carries no filing date — never invented"


def test_parses_an_officer_sale_from_the_complete_submission_txt(ins):
    rows = ins.parse_form4_file(str(FORM4 / "0001127602-26-004410.txt"))
    assert [r["code"] for r in rows] == ["S", "F"]
    s = rows[0]
    assert s["symbol"] == "AAPL" and s["owner"] == "SMITH ROBERT T"
    assert s["relationship"] == ["officer"] and s["officer_title"] == "Senior Vice President"
    assert s["shares"] == 4000.0, "a comma-grouped share count parses"
    assert s["price"] == 152.10 and s["value"] == 608400.0 and s["acquired_disposed"] == "D"
    assert s["filed"] == "2026-08-27", "FILED AS OF DATE from the SGML header"
    assert s["accession"] == "0001127602-26-004410"


def test_parses_the_10b5_1_flag_on_the_routine_filings(ins):
    rows = ins.parse_form4_file(str(FORM4 / "0000990099-26-000061.xml"))
    assert len(rows) == 1
    assert rows[0]["plan_10b5_1"] is True and rows[0]["symbol"] == "ZZZ"
    assert rows[0]["owner_cik"] == "0001777777"


def test_parser_is_tolerant_of_garbage_and_missing_elements(ins):
    assert ins.parse_form4("not xml at all") == []
    assert ins.parse_form4("<other><issuer/></other>") == []
    minimal = ("<ownershipDocument><issuer><issuerTradingSymbol>abc</issuerTradingSymbol></issuer>"
               "<nonDerivativeTable><nonDerivativeTransaction>"
               "<transactionCoding><transactionCode>p</transactionCode></transactionCoding>"
               "</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>")
    rows = ins.parse_form4(minimal)
    assert len(rows) == 1
    t = rows[0]
    assert t["symbol"] == "ABC" and t["code"] == "P"
    assert t["date"] is None and t["shares"] is None and t["price"] is None and t["value"] is None
    assert t["owner"] is None and t["relationship"] == []
    # a row with no date cannot be windowed and is dropped by the staged-file normaliser
    assert ins.normalise_txn(t) is None


# ------------------------------------------------------------------ routine vs opportunistic
def test_same_month_in_each_of_three_prior_years_is_routine(ins):
    hist = [_txn("ZZZ", "R", d) for d in ("2023-03-10", "2024-03-12", "2025-03-11")]
    assert ins.classify_routine(hist, "2026-03-10") == "routine"


def test_a_covered_prior_year_without_a_trade_is_opportunistic(ins):
    # history is complete from 2022 but the insider skipped March 2024
    hist = [_txn("ZZZ", "R", d) for d in ("2022-03-10", "2023-03-10", "2025-03-11", "2025-09-01")]
    assert ins.classify_routine(hist, "2026-03-10") == "opportunistic"
    # and a trade in an entirely different month with a long history is opportunistic too
    assert ins.classify_routine(hist, "2026-08-20") == "opportunistic"


def test_short_history_is_unknown_not_routine_and_not_opportunistic(ins):
    # two prior Marches match, the third lies before the history starts
    hist = [_txn("ZZZ", "R", d) for d in ("2024-03-12", "2025-03-11")]
    assert ins.classify_routine(hist, "2026-03-10") == "unknown"
    assert ins.classify_routine([], "2026-03-10") == "unknown"
    # an explicit history_since that covers 2023 turns the same miss into opportunistic
    assert ins.classify_routine(hist, "2026-03-10", history_since="2022-01-01") == "opportunistic"


def test_routine_rule_matches_the_same_side_only(ins):
    hist = [_txn("ZZZ", "R", d, code="S") for d in ("2023-03-10", "2024-03-12", "2025-03-11")]
    assert ins.classify_routine(hist, "2026-03-10", code="S") == "routine"
    # three Marches of SELLING are not a buying pattern: the history covers 2023–2025 and
    # shows no March buy, so a March 2026 buy is opportunistic
    assert ins.classify_routine(hist, "2026-03-10", code="P") == "opportunistic"
    # a history that starts after the last prior March covers none of the three: unknown
    assert ins.classify_routine([_txn("ZZZ", "R", "2025-09-01", code="S")], "2026-03-10") == "unknown"


def test_the_fixture_routine_pattern_classifies_from_the_files(ins):
    txns, sources, warns = ins.load_staged(form4_dir=str(FORM4), staged_file=str(FORM4 / "none.json"))
    bysym = ins.by_symbol(txns)
    sig26 = ins.signal(bysym, "2026-03-20")["ZZZ"]
    assert sig26["routine_buy_usd_30d"] == 10000.0 and sig26["opportunistic_buy_usd_30d"] == 0.0
    assert sig26["opportunistic_buyers_30d"] == 0 and sig26["cluster_buy"] is False
    assert sig26["buy_classes_30d"] == {"routine": 1, "opportunistic": 0, "unknown": 0}
    # the 2025 buy only has two prior years of history behind it: unknown, counted as
    # not-routine, and flagged as such
    sig25 = ins.signal(bysym, "2025-03-20")["ZZZ"]
    assert sig25["buy_classes_30d"] == {"routine": 0, "opportunistic": 0, "unknown": 1}
    assert sig25["opportunistic_buyers_30d"] == 1 and sig25["unknown_history_buyers_30d"] == 1


# ------------------------------------------------------------------ cluster and windows
def test_two_distinct_opportunistic_buyers_in_30_days_is_a_cluster_one_is_not(ins):
    two = ins.by_symbol([_txn("AAA", "ALICE", "2026-09-01"), _txn("AAA", "BOB", "2026-09-05")])
    one = ins.by_symbol([_txn("AAA", "ALICE", "2026-09-01"), _txn("AAA", "ALICE", "2026-09-05")])
    s2, s1 = ins.signal(two, "2026-09-10")["AAA"], ins.signal(one, "2026-09-10")["AAA"]
    assert s2["cluster_buy"] is True and s2["opportunistic_buyers_30d"] == 2
    assert s1["cluster_buy"] is False and s1["opportunistic_buyers_30d"] == 1
    assert s1["opportunistic_buy_usd_30d"] == 2000.0, "the same owner's two buys both count in dollars"
    assert s2["last_buy_date"] == "2026-09-05"


def test_two_buyers_where_one_is_routine_is_not_a_cluster(ins):
    rows = [_txn("AAA", "ROUTINE", d) for d in ("2023-09-01", "2024-09-01", "2025-09-01", "2026-09-01")]
    rows.append(_txn("AAA", "NEW", "2026-09-03"))
    s = ins.signal(ins.by_symbol(rows), "2026-09-10")["AAA"]
    assert s["opportunistic_buyers_30d"] == 1 and s["cluster_buy"] is False
    assert s["routine_buy_usd_30d"] == 1000.0 and s["opportunistic_buy_usd_30d"] == 1000.0


def test_window_boundaries_are_inclusive_at_30_and_90_days(ins):
    as_of = dt.date(2026, 9, 10)
    d30, d31 = (as_of - dt.timedelta(days=30)).isoformat(), (as_of - dt.timedelta(days=31)).isoformat()
    d90, d91 = (as_of - dt.timedelta(days=90)).isoformat(), (as_of - dt.timedelta(days=91)).isoformat()
    rows = [_txn("AAA", "IN30", d30), _txn("AAA", "OUT31", d31),
            _txn("AAA", "IN90", d90, code="S", shares=50), _txn("AAA", "OUT91", d91, code="S", shares=500),
            _txn("AAA", "FUTURE", "2026-09-11")]
    s = ins.signal(ins.by_symbol(rows), as_of.isoformat())["AAA"]
    assert s["opportunistic_buyers_30d"] == 1, "day 30 is in, day 31 is out, tomorrow is out"
    assert s["opportunistic_buy_usd_30d"] == 1000.0
    # 90-day net: IN30 buy 1000 + OUT31 buy 1000 − IN90 sale 500; OUT91 and FUTURE excluded
    assert s["buy_usd_90d"] == 2000.0 and s["sell_usd_90d"] == 500.0
    assert s["net_insider_usd_90d"] == 1500.0
    assert s["n_txns"] == 5


def test_a_trade_filed_after_as_of_is_not_knowable_yet(ins):
    rows = [_txn("AAA", "A", "2026-09-08", filed="2026-09-12"), _txn("AAA", "B", "2026-09-08", filed="2026-09-09")]
    s = ins.signal(ins.by_symbol(rows), "2026-09-10")["AAA"]
    assert s["opportunistic_buyers_30d"] == 1 and s["cluster_buy"] is False
    later = ins.signal(ins.by_symbol(rows), "2026-09-12")["AAA"]
    assert later["cluster_buy"] is True


def test_grants_exercises_and_withholding_never_enter_the_signal(ins):
    rows = [_txn("AAA", "A", "2026-09-01", code="A"), _txn("AAA", "B", "2026-09-02", code="M"),
            _txn("AAA", "C", "2026-09-03", code="F"), _txn("AAA", "D", "2026-09-04", code="G")]
    s = ins.signal(ins.by_symbol(rows), "2026-09-10")["AAA"]
    assert s["opportunistic_buyers_30d"] == 0 and s["net_insider_usd_90d"] == 0.0
    assert s["n_txns"] == 4 and s["n_open_market_30d"] == 0 and s["last_buy_date"] is None


# ------------------------------------------------------------------ the signal document
def test_build_writes_the_documented_shape_and_dedupes(ins):
    a = _txn("AAA", "ALICE", "2026-09-01", accession="0000000001-26-000001")
    doc = ins.build([a, dict(a), _txn("AAA", "BOB", "2026-09-05", filed="2026-09-08")],
                    "2026-09-10", sources=["insiders.json"])
    m = doc["_meta"]
    assert set(doc) == {"_meta", "symbols"}
    assert m["as_of"] == "2026-09-10" and m["window_days"] == 30 and m["net_window_days"] == 90
    assert m["n_txns"] == 2, "an identical row staged twice is one transaction"
    assert m["n_symbols"] == 1 and m["n_cluster_buy"] == 1 and m["sources"] == ["insiders.json"]
    assert m["history_span"] == {"oldest": "2026-09-01", "newest": "2026-09-05"}
    assert m["n_with_filed_date"] == 1 and any("filing date" in w for w in m["warnings"])
    row = doc["symbols"]["AAA"]
    assert set(row) == {"opportunistic_buyers_30d", "unknown_history_buyers_30d",
                        "opportunistic_buy_usd_30d", "routine_buy_usd_30d", "buy_usd_90d",
                        "sell_usd_90d", "net_insider_usd_90d", "cluster_buy", "last_buy_date",
                        "n_txns", "n_open_market_30d", "buy_classes_30d"}
    json.dumps(doc)


def test_load_staged_reads_insiders_json_in_either_shape_and_merges_form4(ins, tmp_path):
    (tmp_path / "insiders.json").write_text(json.dumps(
        [_txn("QQQ", "X", "2026-09-01"), {"symbol": "BAD"}]), encoding="utf-8")
    txns, sources, warns = ins.load_staged(str(tmp_path), form4_dir=str(FORM4))
    syms = {t["symbol"] for t in txns}
    assert syms == {"QQQ", "AAPL", "ZZZ"}
    assert sources[0] == "insiders.json" and "form4/" in sources[1]
    assert any("skipped" in w for w in warns)
    (tmp_path / "insiders.json").write_text(json.dumps(
        {"_meta": {}, "transactions": [_txn("QQQ", "X", "2026-09-01")]}), encoding="utf-8")
    txns2, _, _ = ins.load_staged(str(tmp_path), form4_dir=str(tmp_path / "no-such-dir"))
    assert [t["symbol"] for t in txns2] == ["QQQ"]
    assert ins.load_staged(str(tmp_path / "empty"))[0] == []


# ------------------------------------------------------------------ EDGAR index → URLs
FORM_IDX = """Description:           Master Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    September 10, 2026
Comments:              webmaster@sec.gov
Anonymous FTP:         ftp://ftp.sec.gov/edgar/

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
4           Apple Inc.                                                    320193      2026-08-21  edgar/data/320193/0001214128-26-000123.txt
4           DOE JANE                                                      1214128     2026-08-21  edgar/data/1214128/0001214128-26-000123.txt
4           Zenith Zinc Holdings Inc.                                     990099      2026-03-11  edgar/data/990099/0000990099-26-000061.txt
4/A         Apple Inc.                                                    320193      2026-07-02  edgar/data/320193/0001214128-26-000100.txt
10-K        Apple Inc.                                                    320193      2025-11-01  edgar/data/320193/0000320193-25-000079.txt
"""


def test_form_idx_rows_become_issuer_form4_urls(ins):
    rows = ins.parse_form_idx(FORM_IDX)
    assert len(rows) == 5 and rows[0]["cik"] == "0000320193" and rows[0]["form"] == "4"
    tickers = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
               "1": {"cik_str": 990099, "ticker": "ZZZ", "title": "Zenith Zinc"}}
    ciks = ins.symbols_to_ciks(["aapl", "ZZZ", "NOPE"], tickers)
    assert ciks == {"AAPL": "0000320193", "ZZZ": "0000990099"}
    urls = ins.form4_urls(rows, ciks)
    assert [u["url"] for u in urls] == [
        "https://www.sec.gov/Archives/edgar/data/320193/0001214128-26-000123.txt",
        "https://www.sec.gov/Archives/edgar/data/990099/0000990099-26-000061.txt",
        "https://www.sec.gov/Archives/edgar/data/320193/0001214128-26-000100.txt"]
    assert urls[0]["symbol"] == "AAPL" and urls[0]["accession"] == "0001214128-26-000123"
    assert "the reporting owner's own row" and all(u["cik"] != "0001214128" for u in urls)
    assert [u["form"] for u in ins.form4_urls(rows, ciks, since="2026-08-01")] == ["4"]
    assert ins.full_index_url("2026-09-10").endswith("/2026/QTR3/form.idx")


def test_fetch_refuses_without_a_contact_user_agent(ins, tmp_path, monkeypatch):
    idx = tmp_path / "form.idx"
    idx.write_text(FORM_IDX, encoding="utf-8")
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    rc = ins.main(["--edgar-index", str(idx), "--ciks", "AAPL=320193", "--fetch",
                   "--form4-dir", str(tmp_path / "f4")])
    assert rc == 2 and not (tmp_path / "f4").exists()


def test_fetch_paces_requests_and_sets_the_user_agent(ins, tmp_path):
    """No network: a fake opener records the requests and the sleeps between them."""
    import io
    calls, sleeps = [], []

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def urlopen(self, req, timeout=None):
            calls.append(req)
            return _Resp(b"<ownershipDocument/>")

    urls = [{"url": f"https://www.sec.gov/Archives/edgar/data/1/000000000{i}-26-00000{i}.txt",
             "accession": f"000000000{i}-26-00000{i}"} for i in range(1, 4)]
    n, errs = ins.fetch_form4s(urls, str(tmp_path / "f4"), "Jane Doe jane@example.com",
                               opener=_Opener(), sleep=sleeps.append)
    assert n == 3 and errs == []
    assert all(r.get_header("User-agent") == "Jane Doe jane@example.com" for r in calls)
    assert len(sleeps) >= 2 and all(0 < s <= ins.SEC_MIN_INTERVAL_S for s in sleeps)
    assert sorted(p.name for p in (tmp_path / "f4").iterdir())[0] == "0000000001-26-000001.txt"


# ------------------------------------------------------------------ scanner pass-through
@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


def _scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def test_scanner_attaches_insider_features_when_the_signal_is_staged(ins, scanner, run_dir):
    rows = [_txn("NVDA", "ALICE", "2026-08-20"), _txn("NVDA", "BOB", "2026-08-28"),
            _txn("MU", "CARL", "2026-08-01", code="S", shares=10)]
    doc = ins.build(rows, "2026-09-02")
    (run_dir / "insiders_signal.json").write_text(json.dumps(doc), encoding="utf-8")
    out = scanner.scan(_scan_data())
    by = {r["ticker"]: r for r in out["results"]}
    nv = by["NVDA"]["features"]
    assert nv["insider_cluster_buy"] is True
    assert nv["insider_opportunistic_buy_usd_30d"] == 2000.0
    assert nv["insider_net_usd_90d"] == 2000.0
    assert by["MU"]["features"] == {"insider_cluster_buy": False,
                                    "insider_opportunistic_buy_usd_30d": 0.0,
                                    "insider_net_usd_90d": -100.0}
    assert by["XOM"]["features"] == {k: None for k in scanner.INSIDER_FEATURE_KEYS}, \
        "a name the signal has no transactions for is null, never 0"
    assert out["meta"]["insider_signal_meta"]["covered"] == ["MU", "NVDA"]
    assert out["meta"]["insider_signal_meta"]["n_cluster_buy"] == 1
    assert out["insider_panel"] is None, "the hand-collected panel is untouched"


def test_scanner_scores_are_unchanged_by_the_insider_features(ins, scanner, run_dir):
    plain = json.loads(json.dumps(scanner.scan(_scan_data()), sort_keys=True))
    golden = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    assert plain == golden, "nothing staged: the golden output is byte-identical"
    doc = ins.build([_txn("NVDA", "A", "2026-08-20"), _txn("NVDA", "B", "2026-08-21")], "2026-09-02")
    (run_dir / "insiders_signal.json").write_text(json.dumps(doc), encoding="utf-8")
    with_f = scanner.scan(_scan_data())
    for a, b in zip(plain["results"], sorted(with_f["results"], key=lambda r: -r["score"])):
        assert a["ticker"] == b["ticker"]
        for k in ("score", "pillars", "raw_score", "normalized_score", "verdict", "setup",
                  "coverage_pct", "reasons"):
            assert a[k] == b[k], f"{k} moved on {a['ticker']} when insider features were attached"


def test_insider_features_merge_with_the_technicals_features_not_over_them(ins, scanner, run_dir):
    data = _scan_data()
    data["candidates"]["NVDA"]["features"] = {"ret_12_7": 0.1}
    doc = ins.build([_txn("NVDA", "A", "2026-08-20")], "2026-09-02")
    out = scanner.scan(data, insider_signal=doc)
    f = next(r for r in out["results"] if r["ticker"] == "NVDA")["features"]
    assert f["ret_12_7"] == 0.1 and f["insider_cluster_buy"] is False
    assert f["insider_opportunistic_buy_usd_30d"] == 1000.0


def test_the_scan_snapshot_carries_the_insider_features(ins, scanner, run_dir):
    import importlib
    import archive
    import snapshots
    importlib.reload(snapshots)
    data = _scan_data()
    (run_dir / "scan_data.json").write_text(json.dumps(data), encoding="utf-8")
    doc = ins.build([_txn("NVDA", "A", "2026-08-20"), _txn("NVDA", "B", "2026-08-21")], "2026-09-02")
    (run_dir / "insiders_signal.json").write_text(json.dumps(doc), encoding="utf-8")
    out = scanner.scan(json.loads(json.dumps(data)))
    out["meta"]["run_id"] = archive.run_id(out["meta"])
    (run_dir / "scan_results.json").write_text(json.dumps(out), encoding="utf-8")
    path, n = snapshots.write_scan_snapshot(str(run_dir), str(run_dir / "archive"))
    _, rows = snapshots.read_snapshot(path)
    nv = next(r for r in rows if r["symbol"] == "NVDA")
    assert nv["features"]["insider_cluster_buy"] is True
    assert nv["features"]["insider_net_usd_90d"] == 2000.0


# ------------------------------------------------------------------ CLI
def test_cli_on_the_fixture_dir_writes_the_signal(tmp_path):
    out = tmp_path / "insiders_signal.json"
    dump = tmp_path / "insiders.json"
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "insiders.py"),
                        "--form4-dir", str(FORM4), "--as-of", "2026-09-10",
                        "--out", str(out), "--dump-transactions", str(dump)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["_meta"]["as_of"] == "2026-09-10" and doc["_meta"]["n_txns"] == 7
    aapl = doc["symbols"]["AAPL"]
    assert aapl["opportunistic_buyers_30d"] == 1 and aapl["cluster_buy"] is False
    assert aapl["opportunistic_buy_usd_30d"] == 150250.0
    assert aapl["net_insider_usd_90d"] == 150250.0 - 608400.0, "the F withholding is not a sale"
    assert aapl["last_buy_date"] == "2026-08-20"
    assert doc["symbols"]["ZZZ"]["n_txns"] == 4 and doc["symbols"]["ZZZ"]["last_buy_date"] is None
    assert "AAPL" in r.stdout and "7 transaction(s)" in r.stdout
    staged = json.loads(dump.read_text(encoding="utf-8"))
    assert len(staged["transactions"]) == 7 and staged["transactions"][0]["symbol"]
    # an --as-of inside the ZZZ March window sees the routine buy as routine
    r2 = subprocess.run([sys.executable, str(ROOT / "engine" / "insiders.py"),
                         "--form4-dir", str(FORM4), "--as-of", "2026-03-20", "--out", str(out)],
                        capture_output=True, text=True, timeout=60)
    assert r2.returncode == 0
    assert json.loads(out.read_text(encoding="utf-8"))["symbols"]["ZZZ"]["routine_buy_usd_30d"] == 10000.0


def test_cli_with_nothing_staged_exits_2(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "engine" / "insiders.py"),
                        "--run-dir", str(tmp_path), "--as-of", "2026-09-10"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2 and "NO INPUT" in r.stdout
    assert (tmp_path / "insiders_signal.json").exists(), "an empty signal is still a file"


def test_insiders_is_in_the_manifest_and_stdlib_only():
    assert "insiders.py" in (ROOT / "engine" / "MANIFEST.txt").read_text(encoding="utf-8").split()
    import ast
    tree = ast.parse((ROOT / "engine" / "insiders.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            mods.add((node.module or "").split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)


def test_ic_reads_the_boolean_cluster_flag_as_a_two_level_feature():
    """BACKTEST.md §6d relies on it: `ic.py --by-feature` turns insider_cluster_buy into a
    0/1 feature, so its quantile spread is cluster-minus-rest; a null stays out."""
    import ic
    f = ic._features_of({"features": {"insider_cluster_buy": True, "insider_net_usd_90d": None,
                                      "insider_opportunistic_buy_usd_30d": 2000.0}})
    assert f["insider_cluster_buy"] == 1.0 and f["insider_opportunistic_buy_usd_30d"] == 2000.0
    assert "insider_net_usd_90d" not in f
