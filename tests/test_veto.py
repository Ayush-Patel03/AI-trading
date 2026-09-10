"""veto.py — the deny-list feed (P-03): each rule, the window boundaries, the scanner's
Avoid override (only with a feed), the manager's refusal and review flag, and the two
goldens that must not move when no feed is staged.
"""
import datetime as dt
import json

import pytest

from conftest import FIX, run_pm, _fresh_scan_meta


@pytest.fixture
def veto(run_dir):
    import importlib
    import veto as v
    importlib.reload(v)
    return v


TODAY = dt.date(2026, 9, 10)          # a Thursday


def _feed(short=(), news=(), halts=()):
    return {"short_reports": list(short), "negative_news": list(news), "halts": list(halts)}


def _short(sym, d, publisher="Hindenburg Research", title="Accounting irregularities"):
    return {"symbol": sym, "publisher": publisher, "date": d.isoformat(),
            "url": "https://example.invalid/report", "title": title}


def _news(sym, d, severity="high", headline="Auditor resigns"):
    return {"symbol": sym, "date": d.isoformat(), "headline": headline,
            "source": "get_equity_news", "severity": severity}


# ------------------------------------------------------------------ sessions
def test_sessions_are_weekdays():
    fri, mon = dt.date(2026, 9, 4), dt.date(2026, 9, 7)
    import veto as v
    assert v.sessions_between(fri, mon) == 1
    assert v.sessions_between(fri, fri) == 0
    assert v.sessions_between(mon, fri) == -1
    assert v.sessions_between(dt.date(2026, 9, 1), dt.date(2026, 9, 10)) == 7


# ------------------------------------------------------------------ the rules
def test_a_short_report_inside_twenty_sessions_vetoes_and_flags_review(veto):
    d = TODAY - dt.timedelta(days=3)
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(short=[_short("XYZ", d)])))
    assert out["veto"] is True and out["review"] is True
    assert "Hindenburg Research" in out["reasons"][0] and d.isoformat() in out["reasons"][0]


def test_the_short_report_window_boundary_is_twenty_sessions_inclusive(veto):
    # count back 20 weekdays from TODAY: inside; 21: outside
    inside = TODAY
    for _ in range(20):
        inside -= dt.timedelta(days=1)
        while inside.weekday() >= 5:
            inside -= dt.timedelta(days=1)
    outside = inside - dt.timedelta(days=1)
    while outside.weekday() >= 5:
        outside -= dt.timedelta(days=1)
    assert veto.sessions_between(inside, TODAY) == 20
    assert veto.sessions_between(outside, TODAY) == 21
    assert veto.check("XYZ", TODAY, veto.normalise(_feed(short=[_short("XYZ", inside)])))["veto"]
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(short=[_short("XYZ", outside)])))
    assert out["veto"] is False and out["review"] is False and out["reasons"] == []


def test_a_future_dated_report_is_not_known_yet(veto):
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(short=[_short("XYZ", TODAY + dt.timedelta(days=1))])))
    assert out["veto"] is False


def test_high_severity_news_inside_five_sessions_vetoes(veto):
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(news=[_news("XYZ", TODAY - dt.timedelta(days=1))])))
    assert out["veto"] is True and out["review"] is False
    assert "high-severity" in out["reasons"][0]


def test_the_news_window_boundary_is_five_sessions(veto):
    five = dt.date(2026, 9, 3)      # Thu -> Thu = 5 weekdays
    six = dt.date(2026, 9, 2)
    assert veto.sessions_between(five, TODAY) == 5
    assert veto.check("XYZ", TODAY, veto.normalise(_feed(news=[_news("XYZ", five)])))["veto"]
    assert not veto.check("XYZ", TODAY, veto.normalise(_feed(news=[_news("XYZ", six)])))["veto"]


def test_medium_severity_news_is_a_note_not_a_veto(veto):
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(news=[_news("XYZ", TODAY, severity="medium")])))
    assert out["veto"] is False and out["reasons"] == []
    assert out["notes"] and "medium-severity" in out["notes"][0]


def test_a_halt_vetoes_today_only(veto):
    today = {"symbol": "XYZ", "date": TODAY.isoformat(), "reason": "news pending"}
    yday = {"symbol": "XYZ", "date": (TODAY - dt.timedelta(days=1)).isoformat(), "reason": "LULD"}
    assert veto.check("XYZ", TODAY, veto.normalise(_feed(halts=[today])))["veto"]
    out = veto.check("XYZ", TODAY, veto.normalise(_feed(halts=[yday])))
    assert out["veto"] is False


def test_symbols_match_case_insensitively_and_other_names_are_clean(veto):
    feed = veto.normalise(_feed(short=[_short("xyz", TODAY)]))
    assert veto.check("XYZ", TODAY, feed)["veto"]
    assert veto.check("ABC", TODAY, feed) == {"veto": False, "reasons": [], "review": False, "notes": []}


def test_an_empty_or_missing_feed_is_clean(veto):
    assert veto.check("XYZ", TODAY, None)["veto"] is False
    assert veto.check("XYZ", TODAY, veto.normalise({}))["veto"] is False


def test_rows_without_a_symbol_or_date_are_dropped_and_counted(veto):
    feed = veto.normalise({"short_reports": [{"publisher": "x", "date": "2026-09-01"},
                                             {"symbol": "A", "date": "not a date"},
                                             _short("B", TODAY)]})
    assert feed["_meta"]["dropped"] == 2 and feed["_meta"]["counts"]["short_reports"] == 1


# ------------------------------------------------------------------ load()
def test_load_returns_none_when_not_staged_and_the_feed_when_it_is(veto, run_dir):
    assert veto.load(str(run_dir)) is None
    (run_dir / "veto.json").write_text(json.dumps(_feed(short=[_short("XYZ", TODAY)])), encoding="utf-8")
    feed = veto.load(str(run_dir))
    assert feed["_meta"]["counts"] == {"short_reports": 1, "negative_news": 0, "halts": 0}
    (run_dir / "veto.json").write_text("{not json", encoding="utf-8")
    assert veto.load(str(run_dir)) is None


# ------------------------------------------------------------------ scanner
@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


@pytest.fixture
def scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def test_the_golden_is_unchanged_without_a_feed(scanner, scan_data):
    got = json.loads(json.dumps(scanner.scan(scan_data), sort_keys=True))
    want = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    assert got == want
    assert not any("veto" in r for r in got["results"])
    assert "veto" not in got["meta"]


def test_the_scanner_overrides_only_the_verdict_and_only_with_a_feed(scanner, scan_data, veto):
    base = scanner.scan(json.loads(json.dumps(scan_data)))
    scan_day = dt.date.fromisoformat(scan_data["meta"]["scan_date"])
    feed = veto.normalise(_feed(short=[_short("NVDA", scan_day - dt.timedelta(days=2))]))
    out = scanner.scan(json.loads(json.dumps(scan_data)), veto_feed=feed)
    by, by0 = ({r["ticker"]: r for r in o["results"]} for o in (out, base))
    nv = by["NVDA"]
    assert nv["verdict"] == "Avoid" and nv["veto"] is True
    assert nv["pre_veto_verdict"] == by0["NVDA"]["verdict"] != "Avoid"
    assert nv["score"] == by0["NVDA"]["score"] and nv["pillars"] == by0["NVDA"]["pillars"]
    assert nv["verdict_note"].startswith("VETO")
    # every other row is marked checked-and-clean, and otherwise identical
    for tk, r in by.items():
        if tk == "NVDA":
            continue
        assert r["veto"] is False and r["verdict"] == by0[tk]["verdict"]
    assert out["meta"]["veto"]["applied"] == ["NVDA"]
    assert any(n.startswith("VETO: NVDA") for n in out["notable"])
    # the ranking is by score, so the vetoed name keeps its place
    assert [r["ticker"] for r in out["results"]] == [r["ticker"] for r in base["results"]]


def test_the_archive_record_keeps_the_veto_fields(scanner, scan_data, veto):
    import archive
    scan_day = dt.date.fromisoformat(scan_data["meta"]["scan_date"])
    feed = veto.normalise(_feed(halts=[{"symbol": "MU", "date": scan_day.isoformat(), "reason": "LULD"}]))
    out = scanner.scan(scan_data, veto_feed=feed)
    rec = archive.record(out)
    mu = next(r for r in rec["results"] if r["ticker"] == "MU")
    assert mu["veto"] is True and mu["verdict"] == "Avoid" and "halt" in mu["veto_reasons"][0]


# ------------------------------------------------------------------ pm
def _stage_feed(run_dir, feed):
    (run_dir / "veto.json").write_text(json.dumps(feed), encoding="utf-8")


def test_the_manager_refuses_a_vetoed_entry_and_names_the_rule(pm, run_dir, quotes, scan):
    today = dt.datetime.now(dt.timezone.utc).date()
    _stage_feed(run_dir, _feed(short=[_short("SCHW", today - dt.timedelta(days=1))]))
    pm.load_veto("veto.json")
    assert pm.VETO["feed"] is not None
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert not [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    reasons = [s["reason"] for s in jrn["skipped"] if s["symbol"] == "SCHW"]
    assert reasons and reasons[0].startswith("veto: short report by Hindenburg Research")
    import report
    assert report.classify(reasons[0])["rule"] == "veto"
    assert any(w.startswith("VETO") for w in jrn["warnings"])


def test_without_a_feed_the_same_entry_is_placed(pm, run_dir, quotes, scan):
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert [d for d in jrn["decisions"] if d["action"] == "place-buy" and d["symbol"] == "SCHW"]
    assert "review_flags" not in jrn


def test_a_row_the_scanner_vetoed_is_refused_even_with_no_feed_staged(pm, run_dir, quotes, scan):
    s = json.loads((run_dir / "scan_results.json").read_text(encoding="utf-8"))
    s["results"][0]["veto"] = True
    s["results"][0]["veto_reasons"] = ["short report by Muddy Waters on 2026-09-01 (3 session(s) ago)"]
    (run_dir / "scan_results.json").write_text(json.dumps(s), encoding="utf-8")
    _, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    reasons = [x["reason"] for x in jrn["skipped"] if x["symbol"] == "SCHW"]
    assert reasons and "Muddy Waters" in reasons[0] and reasons[0].startswith("veto:")


def test_a_held_name_with_a_fresh_short_report_is_flagged_not_sold(pm, run_dir, quotes):
    today = dt.datetime.now(dt.timezone.utc).date()
    _stage_feed(run_dir, _feed(short=[_short("NVDA", today, publisher="Culper Research")]))
    pm.load_veto("veto.json")
    book, jrn, state = run_pm(pm, run_dir, slot="sentinel")
    nv = next(p for p in book["positions"] if p["symbol"] == "NVDA")
    assert nv["review"] == "short-report" and nv["review_since"] == today.isoformat()
    assert jrn["review_flags"] == ["NVDA"]
    assert any(w.startswith("REVIEW NVDA") and "Culper Research" in w for w in jrn["warnings"])
    assert not [d for d in jrn["decisions"] if d["symbol"] == "NVDA" and d["action"].startswith("sell")]
    # the flag rides on the state's position rows too
    assert next(p for p in state["positions"] if p["symbol"] == "NVDA")["review"] == "short-report"


def test_the_review_warning_fires_once_then_the_flag_persists_quietly(pm, run_dir, quotes):
    today = dt.datetime.now(dt.timezone.utc).date()
    _stage_feed(run_dir, _feed(short=[_short("NVDA", today)]))
    pm.load_veto("veto.json")
    book, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert any(w.startswith("REVIEW NVDA") for w in jrn["warnings"])
    (run_dir / "paper_book.json").write_text(json.dumps(book), encoding="utf-8")
    book2, jrn2, _ = run_pm(pm, run_dir, slot="sentinel")
    assert next(p for p in book2["positions"] if p["symbol"] == "NVDA")["review"] == "short-report"
    assert not any(w.startswith("REVIEW NVDA") for w in jrn2["warnings"])


def test_the_flag_clears_when_the_report_leaves_the_window(pm, run_dir, quotes):
    today = dt.datetime.now(dt.timezone.utc).date()
    book = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in book["positions"]:
        if p["symbol"] == "NVDA":
            p["review"] = "short-report"; p["review_since"] = "2026-01-05"
    (run_dir / "paper_book.json").write_text(json.dumps(book), encoding="utf-8")
    _stage_feed(run_dir, _feed(short=[_short("NVDA", today - dt.timedelta(days=60))]))
    pm.load_veto("veto.json")
    book2, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    nv = next(p for p in book2["positions"] if p["symbol"] == "NVDA")
    assert "review" not in nv and jrn["review_flags"] == []


def test_no_feed_leaves_an_existing_flag_alone(pm, run_dir, quotes):
    book = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    for p in book["positions"]:
        if p["symbol"] == "NVDA":
            p["review"] = "short-report"
    (run_dir / "paper_book.json").write_text(json.dumps(book), encoding="utf-8")
    book2, jrn, _ = run_pm(pm, run_dir, slot="sentinel")
    assert next(p for p in book2["positions"] if p["symbol"] == "NVDA")["review"] == "short-report"
    assert "review_flags" not in jrn


def test_the_manifest_and_ci_allowlist_carry_the_module():
    from conftest import ROOT, ENGINE
    assert "veto.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert '"veto"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
