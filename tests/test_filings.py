"""P-02 / E16 — the 10-K/10-Q text-change signal (Cohen, Malloy & Nguyen 2020, "Lazy Prices").

Section extraction on two synthetic 10-K HTML filings (tests/fixtures/filings/) in which ONLY
the Risk Factors section changed; the three similarity measures on hand-checkable texts; the
change score's changer flag and its null-not-zero rule for a missing section; the batch CLI;
the EDGAR plan (URL construction, no network); and the scanner wiring — features attached
when the file is staged, nothing touched when it is not, golden unchanged.
"""
import json
import pathlib
import subprocess
import sys

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"
FILINGS = FIX / "filings"
ENGINE = pathlib.Path(__file__).resolve().parents[1] / "engine"


@pytest.fixture
def filings():
    import filings as f
    return f


@pytest.fixture
def prior_html():
    return (FILINGS / "acme_10k_prior.htm").read_text(encoding="utf-8")


@pytest.fixture
def current_html():
    return (FILINGS / "acme_10k_current.htm").read_text(encoding="utf-8")


# ------------------------------------------------------------------ section extraction
def test_all_four_10k_sections_are_located_in_html(filings, prior_html):
    s = filings.extract_sections(prior_html, "10-K")
    assert set(s) == {"risk_factors", "mdna", "legal_proceedings", "business"}
    assert s["business"].startswith("Acme Widgets, Inc. designs")
    assert s["risk_factors"].startswith("Investing in our common stock involves risk")
    assert s["legal_proceedings"].startswith("From time to time we are involved in litigation")
    assert s["mdna"].startswith("Revenue increased 6%")
    # each section ends where the next item begins — no bleed into 1B / 4 / 7A
    assert "Unresolved Staff Comments" not in s["risk_factors"]
    assert "Mine Safety" not in s["legal_proceedings"]
    assert "Quantitative and Qualitative" not in s["mdna"]
    assert "Risk Factors" not in s["business"]


def test_the_table_of_contents_and_cross_references_do_not_win(filings, prior_html):
    """The TOC lists every item first; a cross-reference ('including Item 7 of this report')
    sits inside Risk Factors. Neither is a heading: the body is the longest span."""
    s = filings.extract_sections(prior_html, "10-K")
    assert "7" != s["risk_factors"].strip()               # not the TOC's page number
    assert len(s["mdna"]) > 500
    assert "including Item 7 of this report" in s["risk_factors"]


def test_only_risk_factors_differs_between_the_two_fixtures(filings, prior_html, current_html):
    a = filings.extract_sections(prior_html)
    b = filings.extract_sections(current_html)
    for k in ("mdna", "legal_proceedings", "business"):
        assert a[k] == b[k], k
    assert a["risk_factors"] != b["risk_factors"]
    assert "Department of Commerce" in b["risk_factors"]
    assert "Department of Commerce" not in a["risk_factors"]


def test_missing_section_is_none_not_empty(filings):
    text = ("ITEM 1. BUSINESS\n" + "We make things. " * 40 + "\nITEM 1A. RISK FACTORS\n"
            + "Things may not sell. " * 40 + "\nITEM 1B. UNRESOLVED STAFF COMMENTS\nNone.\n")
    s = filings.extract_sections(text, "10-K")
    assert s["business"] and s["risk_factors"]
    assert s["mdna"] is None and s["legal_proceedings"] is None


def test_10q_items_are_part_aware(filings):
    """A 10-Q has Item 1 twice: Part I (financial statements) and Part II (legal
    proceedings). The title decides. No Business section in a 10-Q."""
    q = ("PART I. FINANCIAL INFORMATION\nItem 1. Financial Statements\n"
         + "Balance sheet numbers. " * 30
         + "\nItem 2. Management's Discussion and Analysis of Financial Condition and Results of Operations\n"
         + "Revenue rose because volumes rose. " * 30
         + "\nItem 3. Quantitative and Qualitative Disclosures About Market Risk\nNo change.\n"
         + "Item 4. Controls and Procedures\nEffective.\n"
         + "PART II. OTHER INFORMATION\nItem 1. Legal Proceedings\n"
         + "A customer sued us over a late delivery. " * 20
         + "\nItem 1A. Risk Factors\n"
         + "There have been material changes to the risk factors we disclosed. " * 20
         + "\nItem 2. Unregistered Sales of Equity Securities\nNone.\nItem 6. Exhibits\n")
    s = filings.extract_sections(q, "10-Q")
    assert s["business"] is None
    assert s["mdna"].startswith("Revenue rose")
    assert s["legal_proceedings"].startswith("A customer sued us")
    assert "Balance sheet" not in s["legal_proceedings"]
    assert s["risk_factors"].startswith("There have been material changes")
    assert "Unregistered" not in s["risk_factors"]


def test_html_stripping_drops_scripts_tables_become_lines(filings):
    html = ("<html><head><style>p{}</style><script>var x=1;</script></head><body>"
            "<table><tr><td>Item 1A.</td><td>Risk Factors</td></tr></table>"
            "<p>Line one\ncontinues here.</p><p>Line two.</p></body></html>")
    t = filings.strip_html(html)
    assert "var x" not in t and "p{}" not in t
    assert "Item 1A. Risk Factors" in t
    assert "Line one continues here." in t          # in-markup newline is whitespace
    assert t.count("\n") >= 2                       # blocks are line breaks


# ------------------------------------------------------------------ similarity
def test_identical_texts_score_one_on_every_measure(filings):
    a = "The company depends on a few large customers. Losing one would hurt revenue."
    s = filings.similarity(a, a)
    assert s["cosine"] == 1.0 and s["jaccard"] == 1.0 and s["minimum_edit_ratio"] == 1.0


def test_disjoint_texts_score_zero_on_every_measure(filings):
    a = "Copper prices rose sharply. Tariffs apply to imports."
    b = "Software subscriptions renewed. Engineers were hired."
    s = filings.similarity(a, b)
    assert s["cosine"] == 0.0 and s["jaccard"] == 0.0 and s["minimum_edit_ratio"] == 0.0


def test_hand_checked_cosine_and_jaccard(filings):
    """tokens: a = {copper, prices, rose}, b = {copper, prices, fell}.
    cosine = 2 / (sqrt(3) * sqrt(3)) = 0.6667; jaccard = 2 / 4 = 0.5."""
    s = filings.similarity("Copper prices rose.", "Copper prices fell.")
    assert s["cosine"] == pytest.approx(2 / 3, abs=1e-4)
    assert s["jaccard"] == pytest.approx(0.5, abs=1e-4)
    # one sentence each, different: ratio 0; two sentences with one shared: 2*1/4 = 0.5
    assert s["minimum_edit_ratio"] == 0.0
    s2 = filings.similarity("Copper prices rose. Demand was firm.", "Copper prices fell. Demand was firm.")
    assert s2["minimum_edit_ratio"] == pytest.approx(0.5, abs=1e-4)


def test_stopwords_and_numbers_do_not_count(filings):
    assert filings.tokens("The 2025 revenue of the company was 842") == ["revenue"]
    assert filings.similarity("", "")["cosine"] is None
    assert filings.similarity("", "words")["cosine"] == 0.0


# ------------------------------------------------------------------ change score
def test_change_score_flags_the_changer_on_the_fixtures(filings, prior_html, current_html):
    cs = filings.change_score(filings.extract_sections(current_html),
                              filings.extract_sections(prior_html))
    assert cs["n_sections_compared"] == 4
    for k in ("mdna", "legal_proceedings", "business"):
        assert cs["sections"][k]["cosine"] == 1.0, k
    assert cs["mdna_change"] == 0.0
    assert cs["risk_factors_change"] > 0.3
    assert cs["overall_change"] >= filings.CHANGER_THRESHOLD
    assert cs["changer"] is True
    assert cs["threshold"] == filings.CHANGER_THRESHOLD
    # the same filing against itself is a non-changer, not a null
    same = filings.change_score(filings.extract_sections(prior_html),
                                filings.extract_sections(prior_html))
    assert same["overall_change"] == 0.0 and same["changer"] is False


def test_missing_section_is_null_not_zero(filings):
    cur = {"risk_factors": "Copper prices rose. " * 20, "mdna": None,
           "legal_proceedings": "", "business": "We make widgets. " * 20}
    pri = {"risk_factors": "Copper prices rose. " * 20, "mdna": "Revenue rose. " * 20,
           "legal_proceedings": "Sued. " * 20, "business": None}
    cs = filings.change_score(cur, pri)
    assert cs["sections"]["mdna"] is None
    assert cs["sections"]["legal_proceedings"] is None
    assert cs["sections"]["business"] is None
    assert cs["mdna_change"] is None
    assert cs["n_sections_compared"] == 1
    assert cs["overall_change"] == 0.0 and cs["changer"] is False
    nothing = filings.change_score({}, {})
    assert nothing["overall_change"] is None and nothing["changer"] is None
    assert nothing["n_sections_compared"] == 0


def test_overall_change_weights_risk_factors_most(filings):
    """Risk Factors carries the paper's largest alpha; a change confined to it moves the
    overall score more than the same change confined to Business."""
    base = {k: "Steady words here. " * 30 for k in filings.SECTIONS}
    rf = dict(base, risk_factors="Entirely new language. " * 30)
    bz = dict(base, business="Entirely new language. " * 30)
    assert (filings.change_score(rf, base)["overall_change"]
            > filings.change_score(bz, base)["overall_change"])


# ------------------------------------------------------------------ staged file + features
def test_load_staged_returns_none_when_absent_and_uppercases_symbols(filings, tmp_path):
    assert filings.load_staged(str(tmp_path)) is None
    (tmp_path / "filings_signal.json").write_text(json.dumps({
        "_meta": {"threshold": 0.15},
        "acme": {"filed": "2026-08-01", "form": "10-K", "prior_filed": "2025-08-01",
                 "change_score": {"overall_change": 0.2, "changer": True}},
        "junk": "not a row",
    }), encoding="utf-8")
    st = filings.load_staged(str(tmp_path))
    assert set(st) == {"ACME", "_meta"}
    (tmp_path / "filings_signal.json").write_text("{not json", encoding="utf-8")
    assert filings.load_staged(str(tmp_path)) is None


def test_features_for_is_point_in_time(filings):
    row = {"filed": "2026-08-01", "form": "10-K",
           "change_score": {"overall_change": 0.21, "changer": True, "threshold": 0.15}}
    f = filings.features_for(row, "2026-09-10")
    assert f == {"filing_change_score": 0.21, "filing_changer": True, "filing_days_since": 40}
    # the filing is not knowable before its filing date
    assert filings.features_for(row, "2026-07-31") == {k: None for k in filings.FEATURE_KEYS}
    # a null score is a null feature, and a missing row is all nulls
    assert filings.features_for({"filed": "2026-08-01", "change_score": {"overall_change": None}},
                                "2026-09-10")["filing_change_score"] is None
    assert filings.features_for(None, "2026-09-10")["filing_changer"] is None
    # changer derived from the threshold when the flag is absent
    g = filings.features_for({"filed": "2026-08-01", "change_score": {"overall_change": 0.05}},
                             "2026-09-10")
    assert g["filing_changer"] is False


# ------------------------------------------------------------------ EDGAR plan (no network)
def test_edgar_plan_builds_the_urls_and_states_fair_access(filings):
    plan = filings.edgar_plan(["aapl", "ZZZZ"], {"AAPL": 320193}, "10-K")
    a = plan["symbols"]["AAPL"]
    assert a["cik"] == "0000320193"
    assert a["steps"][0]["get"] == "https://data.sec.gov/submissions/CIK0000320193.json"
    z = plan["symbols"]["ZZZZ"]
    assert z["cik"] is None
    assert z["steps"][0]["get"] == "https://www.sec.gov/files/company_tickers.json"
    assert "User-Agent" in plan["_fair_access"]["user_agent"]
    assert "10 requests per second" in plan["_fair_access"]["rate"]
    assert (filings.primary_document_url(320193, "0000320193-24-000123", "aapl-20240928.htm")
            == "https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm")


def test_pick_filing_pair_prefers_the_year_earlier_filing_and_skips_amendments(filings):
    sub = {"filings": {"recent": {
        "form": ["10-Q", "10-K/A", "10-K", "10-Q", "10-K", "10-K"],
        "filingDate": ["2026-05-01", "2026-03-01", "2026-02-20", "2025-11-01", "2025-02-18", "2024-02-15"],
        "accessionNumber": ["a", "b", "c", "d", "e", "f"],
        "primaryDocument": ["q.htm", "ka.htm", "k26.htm", "q2.htm", "k25.htm", "k24.htm"],
        "reportDate": ["2026-03-31", "2025-12-31", "2025-12-31", "2025-09-30", "2024-12-31", "2023-12-31"],
    }}}
    cur, prior = filings.pick_filing_pair(sub, "10-K")
    assert cur["accession"] == "c" and prior["accession"] == "e"
    cur, prior = filings.pick_filing_pair(sub, "10-Q")
    assert cur["accession"] == "a" and prior["accession"] == "d"   # fallback: previous 10-Q
    assert filings.pick_filing_pair({}, "10-K") == (None, None)


# ------------------------------------------------------------------ CLI
def test_batch_cli_writes_the_staged_file_shape(tmp_path):
    batch = tmp_path / "batch"
    for sym, cur, pri in (("ACME", "acme_10k_current.htm", "acme_10k_prior.htm"),
                          ("SAME", "acme_10k_prior.htm", "acme_10k_prior.htm")):
        d = batch / sym
        d.mkdir(parents=True)
        (d / "current.htm").write_bytes((FILINGS / cur).read_bytes())
        (d / "prior.htm").write_bytes((FILINGS / pri).read_bytes())
        (d / "meta.json").write_text(json.dumps({
            "filed": "2026-08-01", "form": "10-K", "prior_filed": "2025-08-01",
            "accession": "0000000001-26-000001", "prior_accession": "0000000001-25-000001"}),
            encoding="utf-8")
    (batch / "NOPE").mkdir()                       # no files: skipped, named, not fatal
    out = tmp_path / "filings_signal.json"
    r = subprocess.run([sys.executable, str(ENGINE / "filings.py"), "--batch", str(batch),
                        "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    st = json.loads(out.read_text(encoding="utf-8"))
    assert set(st) == {"ACME", "SAME", "_meta"}
    assert st["ACME"]["filed"] == "2026-08-01" and st["ACME"]["form"] == "10-K"
    assert st["ACME"]["prior_accession"] == "0000000001-25-000001"
    assert st["ACME"]["change_score"]["changer"] is True
    assert st["SAME"]["change_score"]["changer"] is False
    assert st["SAME"]["change_score"]["overall_change"] == 0.0
    assert st["_meta"]["n_symbols"] == 2 and st["_meta"]["n_changers"] == 1
    assert any(s.startswith("NOPE") for s in st["_meta"]["skipped"])
    assert "2 symbol(s), 1 changer(s)" in r.stdout


def test_pair_cli_and_edgar_plan_cli(tmp_path):
    r = subprocess.run([sys.executable, str(ENGINE / "filings.py"),
                        "--current", str(FILINGS / "acme_10k_current.htm"),
                        "--prior", str(FILINGS / "acme_10k_prior.htm"), "--form", "10-K"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    cs = json.loads(r.stdout)
    assert cs["changer"] is True and cs["form"] == "10-K"
    assert cs["sections_found"]["current"] == ["risk_factors", "mdna", "legal_proceedings", "business"]
    cik = tmp_path / "cik.json"
    cik.write_text(json.dumps({"MSFT": "789019"}), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ENGINE / "filings.py"), "--edgar-plan",
                        "--symbols", "MSFT", "--cik-map", str(cik)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "CIK0000789019.json" in r.stdout


# ------------------------------------------------------------------ scanner wiring
@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


def _scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def test_scanner_attaches_the_features_when_the_file_is_staged(scanner, filings, run_dir):
    data = _scan_data()
    today = data["meta"].get("scan_date") or data["meta"]["date"]
    syms = [t for t, c in data["candidates"].items() if isinstance(c.get("price"), (int, float))]
    first, second = syms[0], syms[1]
    (run_dir / "filings_signal.json").write_text(json.dumps({
        "_meta": {"threshold": 0.15},
        first: {"filed": "2026-01-15", "form": "10-K", "prior_filed": "2025-01-15",
                "change_score": {"overall_change": 0.22, "changer": True, "threshold": 0.15}},
        second: {"filed": "2099-01-01", "form": "10-Q",             # future-dated: not knowable
                 "change_score": {"overall_change": 0.02, "changer": False}},
    }), encoding="utf-8")
    staged = filings.load_staged(str(run_dir))
    out = scanner.scan(data, filings_signal=staged)
    by = {r["ticker"]: r for r in out["results"]}
    f1 = by[first]["features"]
    assert f1["filing_change_score"] == 0.22 and f1["filing_changer"] is True
    from datetime import date
    assert f1["filing_days_since"] == (date.fromisoformat(today) - date(2026, 1, 15)).days
    assert by[second]["features"] == {k: None for k in filings.FEATURE_KEYS}
    for tk in syms[2:]:
        assert by[tk]["features"]["filing_change_score"] is None      # staged but not named
    m = out["meta"]["filings_signal"]
    assert m["n_symbols"] == 2 and m["n_matched"] == 1 and m["n_changers"] == 1
    # scores did not move
    plain = {r["ticker"]: r for r in scanner.scan(_scan_data())["results"]}
    for tk in by:
        for k in ("score", "pillars", "verdict", "setup", "raw_score", "coverage_pct"):
            assert by[tk][k] == plain[tk][k], (tk, k)


def test_scanner_without_the_file_is_untouched_and_golden_holds(scanner):
    out = scanner.scan(_scan_data())
    assert "filings_signal" not in out["meta"]
    assert all("features" not in r for r in out["results"])
    got = json.loads(json.dumps(out, sort_keys=True))
    want = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    assert got == want


def test_existing_features_survive_the_attach(scanner, filings):
    data = _scan_data()
    tk = next(t for t, c in data["candidates"].items() if isinstance(c.get("price"), (int, float)))
    data["candidates"][tk]["features"] = {"ret_1m": 0.05}
    out = scanner.scan(data, filings_signal={tk: {"filed": "2026-01-01",
                                                  "change_score": {"overall_change": 0.1}}})
    f = next(r for r in out["results"] if r["ticker"] == tk)["features"]
    assert f["ret_1m"] == 0.05 and f["filing_change_score"] == 0.1 and f["filing_changer"] is False
    assert data["candidates"][tk]["features"] == {"ret_1m": 0.05}     # the input was not mutated


# ------------------------------------------------------------------ history form + replay
def test_a_filing_history_picks_the_latest_knowable_row(filings):
    hist = [
        {"filed": "2025-02-10", "form": "10-K", "change_score": {"overall_change": 0.30, "changer": True}},
        {"filed": "2025-05-05", "form": "10-Q", "change_score": {"overall_change": 0.05, "changer": False}},
        {"filed": "2025-08-04", "form": "10-Q", "change_score": {"overall_change": 0.20, "changer": True}},
    ]
    assert filings.features_for(hist, "2025-01-31") == {k: None for k in filings.FEATURE_KEYS}
    assert filings.features_for(hist, "2025-03-01")["filing_change_score"] == 0.30
    f = filings.features_for(hist, "2025-06-01")
    assert f["filing_change_score"] == 0.05 and f["filing_changer"] is False and f["filing_days_since"] == 27
    assert filings.features_for(hist, "2025-12-31")["filing_change_score"] == 0.20


def test_load_file_accepts_the_history_form_and_the_envelope(filings, tmp_path):
    p = tmp_path / "anything.json"
    p.write_text(json.dumps({"_meta": {"threshold": 0.2},
                             "symbols": {"aaa": [{"filed": "2025-02-10",
                                                  "change_score": {"overall_change": 0.3}}]}}),
                 encoding="utf-8")
    st = filings.load_file(str(p))
    assert st["_meta"]["threshold"] == 0.2 and isinstance(st["AAA"], list)
    assert filings.load_file(str(tmp_path / "missing.json")) is None


def test_backtest_replay_lays_filings_over_dates_point_in_time(run_dir):
    """A replay on a date before the filing sees null; on a date after it sees the score.
    Same file, same symbol — the guard is the date, not the caller."""
    import importlib
    import backtest as bt
    importlib.reload(bt)
    from test_backtest import _series, _bars_file
    hist = _series(300)
    bars = bt.load_bars(_bars_file(run_dir, {"AAA": hist, "SPY": hist}))
    d_early, d_late = bt.bar_date(hist[-40]), bt.bar_date(hist[-1])
    filed = bt.bar_date(hist[-20])
    sig = {"AAA": [{"filed": filed, "form": "10-Q",
                    "change_score": {"overall_change": 0.25, "changer": True}}]}
    early = bt.replay(bars, d_early, filings_signal=sig)["results"][0]
    late = bt.replay(bars, d_late, filings_signal=sig)["results"][0]
    assert early["features"]["filing_change_score"] is None
    assert late["features"]["filing_change_score"] == 0.25 and late["features"]["filing_changer"] is True
    assert late["score"] == bt.replay(bars, d_late)["results"][0]["score"]
    # the archive record carries the features, which is what ic.py --by-feature reads
    import archive
    out = bt.replay(bars, d_late, filings_signal=sig)
    out["meta"]["run_id"] = f"{d_late}-backtest"
    rec = archive.record(out)
    row = next(r for r in rec["results"] if r["ticker"] == "AAA")
    assert row["features"]["filing_changer"] is True
