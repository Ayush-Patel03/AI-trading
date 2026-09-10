"""universe_history.py — point-in-time index membership from Wikipedia, offline.

Everything here runs against a saved, trimmed copy of the real page (tests/fixtures/
wiki_sp500_trimmed.html) so the suite never touches the network. The column layout it
pins was verified against the live page on 2026-09-10:

    constituents: Symbol | Security | GICS Sector | GICS Sub-Industry | Headquarters Location
                  | Date added | CIK | Founded
    changes:      Effective Date | Added (Ticker, Security) | Removed (Ticker, Security) | Reason
"""
import json
import urllib.error

import pytest

from conftest import FIX

FIXTURE = FIX / "wiki_sp500_trimmed.html"


@pytest.fixture
def uh():
    import importlib
    import universe_history as m
    importlib.reload(m)
    return m


@pytest.fixture
def page(uh):
    return uh.fetch_wikipedia_tables("sp500", html=FIXTURE.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ parsing the real markup
def test_the_constituents_column_is_found_and_share_classes_keep_their_dot(page):
    cur = page["current"]
    assert cur[:3] == ["MMM", "AOS", "ABT"]
    assert "BRK.B" in cur and "BF.B" in cur
    assert len(cur) == 39


def test_the_changes_columns_are_read_from_the_two_level_header(page):
    first = page["changes"][0]
    assert first["date"] == "2026-06-30" and first["raw_date"] == "June 30, 2026"
    assert first["added"] == [] and first["removed"] == ["CAG"]
    assert first["reason"].startswith("Market capitalization change")
    both = next(c for c in page["changes"] if c["date"] == "2026-06-22" and "MRVL" in c["added"])
    assert both["removed"] == ["POOL"]


def test_rowspan_dates_and_reasons_are_carried_down_every_row(page):
    """March 23, 2026 is one date cell spanning four rows on the page."""
    rows = [c for c in page["changes"] if c["date"] == "2026-03-23"]
    assert [(c["added"], c["removed"]) for c in rows] == [
        (["VRT"], ["MTCH"]), (["LITE"], ["MOH"]), (["COHR"], ["LW"]), (["SATS"], ["PAYC"])]
    assert all(c["reason"] == "Market capitalization change." for c in rows)


def test_footnote_superscripts_never_reach_a_ticker_or_a_reason(page):
    q = next(c for c in page["changes"] if c["date"] == "2025-11-03")
    assert q["added"] == ["Q"]                         # the cell reads Q<sup>[614]</sup>
    assert not any("[" in c["reason"] for c in page["changes"])


def test_the_whole_fixture_parses_with_no_unparseable_rows(page):
    assert len(page["changes"]) == 41
    assert page["unparseable_rows"] == 0
    assert page["notes"] == []


def test_a_row_without_a_ticker_is_counted_not_guessed(uh):
    html = FIXTURE.read_text(encoding="utf-8").replace(
        "<td>CAG</td>", "<td></td>", 1)                 # security named, ticker blank
    doc = uh.fetch_wikipedia_tables("sp500", html=html)
    assert doc["unparseable_rows"] == 1
    assert not any(c["date"] == "2026-06-30" for c in doc["changes"])


def test_two_tickers_in_one_cell_are_both_read(uh):
    tbl = ('<table id="changes"><tr><th rowspan="2">Date</th><th colspan="2">Added</th>'
           '<th colspan="2">Removed</th><th rowspan="2">Reason</th></tr>'
           '<tr><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th></tr>'
           '<tr><td>January 2, 2020</td><td>AAA<br/>BBB</td><td>A, B</td><td>CCC, DDD</td>'
           '<td>C and D</td><td>x</td></tr></table>')
    t = uh.parse_tables(tbl)[0]
    rows, bad, _ = uh.parse_changes(t)
    assert rows[0]["added"] == ["AAA", "BBB"] and rows[0]["removed"] == ["CCC", "DDD"]


def test_parse_date_accepts_the_page_formats_and_refuses_prose(uh):
    assert uh.parse_date("June 30, 2026") == "2026-06-30"
    assert uh.parse_date("Jun 2, 2026") == "2026-06-02"
    assert uh.parse_date("2026-06-30") == "2026-06-30"
    assert uh.parse_date("Various dates") is None


def test_normalize_ticker_maps_the_share_class_dot_only(uh):
    assert uh.normalize_ticker("BRK.B") == "BRK-B"
    assert uh.normalize_ticker("bf.b") == "BF-B"
    assert uh.normalize_ticker("MMM") == "MMM"


# ------------------------------------------------------------------ membership
def test_membership_walks_added_removed_readded(uh):
    """Hand-checkable: XYZ joins, leaves, joins again and is current."""
    changes = [
        {"date": "2024-01-10", "added": ["XYZ"], "removed": ["OLD"]},
        {"date": "2024-06-01", "added": ["NEW"], "removed": ["XYZ"]},
        {"date": "2025-03-01", "added": ["XYZ"], "removed": ["NEW"]},
    ]
    notes = []
    mem = uh.build_membership(["AAA", "XYZ"], changes, notes=notes)
    assert mem["XYZ"] == [["2024-01-10", "2024-06-01"], ["2025-03-01", None]]
    assert mem["OLD"] == [[None, "2024-01-10"]]
    assert mem["NEW"] == [["2024-06-01", "2025-03-01"]]
    assert mem["AAA"] == [[None, None]]
    assert notes == []


def test_members_on_before_and_after_each_change(uh):
    changes = [
        {"date": "2024-01-10", "added": ["XYZ"], "removed": ["OLD"]},
        {"date": "2024-06-01", "added": ["NEW"], "removed": ["XYZ"]},
        {"date": "2025-03-01", "added": ["XYZ"], "removed": ["NEW"]},
    ]
    mem = uh.build_membership(["AAA", "XYZ"], changes)
    on = lambda d: uh.members_on(mem, d)
    assert on("2024-01-09") == {"AAA", "OLD"}
    assert on("2024-01-10") == {"AAA", "XYZ"}, "the effective date is the first day IN"
    assert on("2024-05-31") == {"AAA", "XYZ"}
    assert on("2024-06-01") == {"AAA", "NEW"}, "and the first day OUT for the removed name"
    assert on("2025-02-28") == {"AAA", "NEW"}
    assert on("2025-03-01") == {"AAA", "XYZ"}
    assert on("2026-01-01") == {"AAA", "XYZ"}


def test_since_clamps_the_window_and_drops_intervals_that_ended_before_it(uh):
    changes = [{"date": "2024-01-10", "added": ["XYZ"], "removed": ["OLD"]}]
    mem = uh.build_membership(["AAA", "XYZ"], changes, start_date="2024-06-01")
    assert "OLD" not in mem
    assert mem["AAA"] == [["2024-06-01", None]]
    assert mem["XYZ"] == [["2024-06-01", None]]


def test_an_added_name_that_vanished_without_a_row_is_noted_not_hidden(uh):
    notes = []
    mem = uh.build_membership(["AAA"], [{"date": "2024-01-10", "added": ["GONE"], "removed": []}],
                              notes=notes)
    assert mem["GONE"] == [["2024-01-10", None]]
    assert any(n.startswith("GONE: added 2024-01-10") for n in notes)


def test_membership_from_the_fixture_is_consistent_with_its_current_list(uh, page):
    mem = uh.build_membership(page["current"], page["changes"])
    assert mem["SOLS"] == [["2025-10-30", "2025-12-22"]]        # in, then out
    assert mem["AMTM"] == [["2024-09-30", "2024-12-23"]]
    assert mem["CAG"] == [[None, "2026-06-30"]]
    assert mem["SATS"] == [["2026-03-23", None]]
    assert uh.current_members(mem) == set(page["current"])
    assert "SOLS" in uh.members_on(mem, "2025-11-15")
    assert "SOLS" not in uh.members_on(mem, "2025-12-22")


# ------------------------------------------------------------------ the bias statement
def test_bias_statement_counts_the_names_a_current_list_would_drop(uh):
    changes = [
        {"date": "2024-01-10", "added": ["XYZ"], "removed": ["OLD"]},
        {"date": "2024-06-01", "added": ["NEW"], "removed": ["XYZ"]},
    ]
    mem = uh.build_membership(["AAA", "NEW"], changes)
    s = uh.bias_statement(mem, "2023-06-01", "2024-12-31", unparseable=3, index="test")
    assert "2 members on 2023-06-01" in s and "2 on 2024-12-31" in s
    assert "4 distinct names" in s
    assert "2 of those are NOT in the current constituent list (OLD, XYZ)" in s
    assert "3 change rows carried no parseable ticker" in s
    assert "RESIDUAL BIAS" in s


def test_bias_statement_window_excludes_names_gone_before_it(uh):
    mem = uh.build_membership(["AAA"], [{"date": "2024-01-10", "added": [], "removed": ["OLD"]}])
    s = uh.bias_statement(mem, "2024-01-10", "2024-12-31")
    assert "1 distinct names" in s and "0 of those are NOT" in s


# ------------------------------------------------------------------ files and the CLI
def test_save_and_load_round_trip_and_load_refuses_a_foreign_file(uh, tmp_path):
    p = tmp_path / "u.json"
    uh.save(str(p), {"membership": {"AAA": [[None, None]]}, "index": "t"})
    assert uh.load(str(p))["membership"]["AAA"] == [[None, None]]
    (tmp_path / "bad.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        uh.load(str(tmp_path / "bad.json"))


def test_the_cli_parses_a_saved_page_and_prints_members_on(uh, tmp_path, capsys):
    out = tmp_path / "sp500.json"
    rc = uh.main(["--index", "sp500", "--html", str(FIXTURE), "--out", str(out),
                  "--members-on", "2025-11-15"])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["name"] == "sp500-history" and doc["fetched_at"]
    assert doc["source"]["kind"] == "wikipedia-saved-html"
    assert doc["n_current"] == 39 and doc["unparseable_rows"] == 0
    assert "RESIDUAL BIAS" in doc["bias"]
    text = capsys.readouterr().out
    assert "MEMBERS sp500 on 2025-11-15: 39" in text
    assert " SOLS " in text and "CAG" in text


def test_the_cli_reads_an_existing_file_for_members_on(uh, tmp_path, capsys):
    out = tmp_path / "sp500.json"
    uh.main(["--index", "sp500", "--html", str(FIXTURE), "--out", str(out), "--quiet"])
    assert uh.main(["--in", str(out), "--members-on", "2026-07-01", "--quiet"]) == 0
    text = capsys.readouterr().out
    assert "MEMBERS sp500 on 2026-07-01: 39" in text and "CAG" not in text


def test_network_failure_is_exit_2_and_no_file(uh, tmp_path, monkeypatch, capsys):
    def boom(req, timeout=None):
        raise urllib.error.URLError("egress denied")
    monkeypatch.setattr(uh.urllib.request, "urlopen", boom)
    out = tmp_path / "never.json"
    assert uh.main(["--index", "sp500", "--out", str(out)]) == 2
    assert not out.exists()
    assert "FETCH FAILED" in capsys.readouterr().err


def test_the_fetch_sends_a_user_agent_naming_the_project(uh, monkeypatch):
    seen = {}

    class R:
        status = 200
        headers = type("H", (), {"get_content_charset": staticmethod(lambda: "utf-8")})()

        def read(self):
            return FIXTURE.read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        seen["ua"] = req.get_header("User-agent")
        seen["url"] = req.full_url
        seen["n"] = seen.get("n", 0) + 1
        return R()
    monkeypatch.setattr(uh.urllib.request, "urlopen", fake)
    doc = uh.fetch_wikipedia_tables("sp500")
    assert "ai-trading-engine" in seen["ua"] and "github.com" in seen["ua"]
    assert seen["url"] == uh.PAGES["sp500"] and seen["n"] == 1, "one request per page"
    assert len(doc["current"]) == 39


def test_changes_csv_import_matches_the_html_route(uh, tmp_path):
    (tmp_path / "c.csv").write_text('date,add,remove\n2024-01-10,"XYZ","OLD"\n'
                                    '2024-06-01,"NEW,TWO","XYZ"\nbad-date,"A","B"\n',
                                    encoding="utf-8")
    (tmp_path / "s.csv").write_text("Symbol,Security\nAAA,A\nNEW,N\nTWO,T\n", encoding="utf-8")
    rc = uh.main(["--changes-csv", str(tmp_path / "c.csv"), "--current-csv", str(tmp_path / "s.csv"),
                  "--out", str(tmp_path / "o.json"), "--quiet"])
    assert rc == 0
    doc = json.loads((tmp_path / "o.json").read_text(encoding="utf-8"))
    assert doc["unparseable_rows"] == 1 and doc["source"]["kind"] == "csv"
    assert doc["membership"]["XYZ"] == [["2024-01-10", "2024-06-01"]]


# ------------------------------------------------------------------ the backtest intersection
def _bar(day, close, v=1_000_000):
    return {"begins_at": f"{day}T00:00:00Z", "open_price": f"{close}",
            "high_price": f"{close * 1.01}", "low_price": f"{close * 0.99}",
            "close_price": f"{close}", "volume": v, "interpolated": False}


def _series(n, start=100.0, step=0.5):
    out, d = [], 0
    for i in range(n):
        d += 1
        out.append(_bar(f"2025-{(d // 28) + 1:02d}-{(d % 28) + 1:02d}", start + i * step))
    return out


def test_the_backtest_scores_a_name_only_on_its_member_dates(uh, run_dir, monkeypatch):
    import importlib
    import backtest as bt
    importlib.reload(bt)

    hist = _series(260)
    days = [bt.bar_date(b) for b in hist]
    payload = {"data": {"results": [{"symbol": s, "bars": hist} for s in ("AAA", "BBB", "SPY")]}}
    bars_path = run_dir / "bars.json"
    bars_path.write_text(json.dumps(payload), encoding="utf-8")

    # BBB joins the index on the 255th session and leaves on the 258th; AAA is always in.
    join, leave = days[254], days[257]
    membership = {"AAA": [[None, None]], "BBB": [[join, leave]]}
    monkeypatch.setattr(uh, "load", lambda p: {"membership": membership, "index": "t",
                                               "name": "t-history", "unparseable_rows": 0})
    monkeypatch.setattr(bt, "universe_history", uh)

    rc = bt.main(["--bars", str(bars_path), "--start", days[250], "--end", days[-1],
                  "--every", "1", "--out-records", str(run_dir / "rec"),
                  "--universe-history", "ignored.json", "--summary", "bt.json",
                  "--ledger", str(run_dir / "ledger.jsonl"), "--hypothesis", "membership"])
    assert rc == 0

    scored = {}
    for p in sorted((run_dir / "rec").glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        scored[rec["date"]] = {r["ticker"] for r in rec["results"]}
    assert scored, "records were written"
    for d, names in scored.items():
        assert "AAA" in names
        assert ("BBB" in names) == (join <= d < leave), \
            f"BBB scored on {d}; member window is [{join}, {leave})"
    assert any("BBB" in n for n in scored.values()) and not all("BBB" in n for n in scored.values())

    summary = json.loads((run_dir / "bt.json").read_text(encoding="utf-8"))
    assert summary["universe"]["name"] == "t-history"
    assert summary["universe"]["n_symbols_on_start"] == 1
    assert summary["universe"]["n_ever"] == 2
    assert any("REDUCED NOT REMOVED" in w for w in summary["_warnings"])

    row = json.loads((run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["universe"]["name"] == "t-history"
    assert row["universe"]["n_symbols_on_end"] == 1 and row["universe"]["n_ever"] == 2
    assert "RESIDUAL BIAS" in row["universe_bias"]


def test_without_the_flag_the_backtest_is_unchanged(run_dir):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    hist = _series(260)
    payload = {"data": {"results": [{"symbol": s, "bars": hist} for s in ("AAA", "SPY")]}}
    p = run_dir / "bars.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    d = bt.bar_date(hist[-1])
    rc = bt.main(["--bars", str(p), "--start", d, "--end", d, "--every", "1",
                  "--out-records", str(run_dir / "rec"), "--summary", "bt.json"])
    assert rc == 0
    summary = json.loads((run_dir / "bt.json").read_text(encoding="utf-8"))
    assert summary["universe"] == {"name": None, "n_symbols": 1, "point_in_time": False}
    assert any(w.startswith("SURVIVORSHIP BIAS") for w in summary["_warnings"])


def test_a_share_class_is_matched_however_the_bars_spell_it(run_dir):
    import importlib
    import backtest as bt
    importlib.reload(bt)
    allowed = bt.allowed_on({"BRK.B": [[None, None]]}, "2025-01-01")
    assert allowed == {"BRK.B", "BRK-B"}
