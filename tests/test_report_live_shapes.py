"""The eight defects the real project journals and books found in report.py /
render_review.py, each pinned against the shapes that caused it.

The inputs are the live state, scrubbed. `tests/fixtures/live/journals/pm-journal.json` is
new: the swing PM journal from 2026-08-31 (the plumbing-verification evening, before the
desk split and before the book was resized from $50 to $5,000) through 2026-09-11, which
no fixture in the repo carried. Everything else it needs — the pullback and momentum
journals, the three paper books with their real `closed_trades`, and the real COVER-01
coverage doc — is already in the repo under `tests/fixtures/state_seed/`, scrubbed by
`tests/test_seed_state.py`'s hygiene test, and is read from there rather than copied.

The three-desk fixture under `tests/fixtures/journals/` is synthetic and regular — 90 tidy
entries, one refusal string per skip, whole positions closed in one row — and every one of
these eight defects slipped past it for exactly that reason.

Each test names the number the broken code produced on this data, so a regression is
recognisable rather than merely red.
"""
import json
import re
from collections import Counter

import pytest

from conftest import FIX

LIVE = FIX / "live"
SEED = FIX / "state_seed"
SWING = LIVE / "journals" / "pm-journal.json"
JOURNALS = [str(SWING), str(SEED / "pm-journal-pullback.json"),
            str(SEED / "pm-journal-momentum.json")]
BOOKS = SEED                       # load_books skips every file that is not a book
COVERAGE = SEED / "pm-coverage.json"
MONDAY = "2026-09-07"          # what the runner prompt passes as --since


@pytest.fixture
def rp(run_dir):
    import importlib
    import report as m
    importlib.reload(m)
    return m


@pytest.fixture
def rr(run_dir):
    import importlib
    import render_review as m
    importlib.reload(m)
    return m


@pytest.fixture
def entries(rp):
    return rp.load_journals(JOURNALS)


@pytest.fixture
def books(rp):
    return rp.load_books(str(BOOKS))


# ------------------------------------------------------------------ the fixture itself
def test_the_live_fixture_carries_the_shapes_that_broke_the_report(rp, entries, books):
    """If this drifts, the tests below stop testing what they claim to."""
    assert len(entries) == 35 + 6 + 6
    raw = json.loads(SWING.read_text(encoding="utf-8"))["entries"]
    pre_split = [e for e in raw if "desk" not in e]
    assert len(pre_split) == 5, "the 2026-08-31 plumbing-verification runs"
    assert {e["date"] for e in pre_split} == {"2026-08-31"}
    assert {e["equity"] for e in pre_split} == {50.0}, "the pre-resize book"
    assert sum(1 for e in raw if e.get("sentinel")) == 5
    assert sum(1 for e in raw if not e.get("sentinel")) == 30, "what badged swing a sample"
    assert books["swing"]["resized"] == {"date": "2026-08-31", "from": 50.0, "to": 5000.0,
                                         "why": books["swing"]["resized"]["why"]}
    assert books["swing"]["starting_equity"] == 5000.0
    assert sum(len(b["closed_trades"]) for b in books.values()) == 41
    # no account mask, no artifact URL: tests/test_config.py scans the whole repo for both
    for p in sorted(LIVE.rglob("*.json")):
        text = p.read_text(encoding="utf-8")
        assert '"mirrors"' not in text, p.name
        assert "claude.ai/code/" + "artifact" not in text, p.name
        json.loads(text)


# ------------------------------------------------------------------ 1 · benchmark start
def test_the_swing_return_is_not_measured_across_the_capital_resize(rp, entries, books):
    """BEFORE: equity_start came off the first entry in the window — a 2026-08-31
    verification run carrying the pre-resize $50 — so the swing row read +10,083.0%."""
    B = rp.benchmark(entries, None, books)
    b = B["swing"]
    assert b["equity_start"] == 5000.0 and b["start"] == "2026-09-01"
    assert b["return_pct"] == pytest.approx(1.83, abs=0.01)
    assert b["return_pct"] < 100, "a +10,083% desk return is a resize, not a return"
    assert b["excluded_entries"] == {"sentinel": 5, "no_equity": 0, "no_desk": 5,
                                     "pre_capital_change": 0}
    assert B["pullback"]["excluded_entries"] == {"sentinel": 2, "no_equity": 0, "no_desk": 0,
                                                 "pre_capital_change": 0}


def test_either_guard_alone_excludes_the_pre_split_entries(rp, entries, books):
    """The two exclusions are independent: the entries carry no `desk` AND sit on the far
    side of the book's own `resized.date`. Each is enough on its own."""
    no_books = rp.benchmark(entries, None, None)["swing"]
    assert no_books["equity_start"] == 5000.0
    assert no_books["excluded_entries"]["no_desk"] == 5
    # with the desk stamped on, the book's resized.date is what still excludes them
    stamped = [dict(e, desk="swing", desk_declared=True) for e in entries]
    by_resize = rp.benchmark(stamped, None, books)["swing"]
    assert by_resize["equity_start"] == 5000.0
    assert by_resize["excluded_entries"] == {"sentinel": 7, "no_equity": 0, "no_desk": 0,
                                             "pre_capital_change": 5}
    assert rp.capital_change_dates(books) == {"swing": "2026-08-31"}


def test_excluded_entries_accounts_for_every_entry_the_benchmark_dropped(rp, rr, entries, books):
    """BEFORE: `excluded_entries` counted `pre_capital_change` and `no_desk` only, while
    benchmark() also dropped sentinel runs and entries with no `equity` in silence. On the
    live swing journal 35 raw entries became 25 and the field owned up to five of the ten,
    so a reader could not reconcile the journal against the row's n — which is the whole
    reason the exclusions were surfaced."""
    B = rp.benchmark(entries, None, books)
    b = B["swing"]
    assert set(b["excluded_entries"]) == set(rp.EXCLUSIONS)
    assert b["excluded_entries"] == {"sentinel": 5, "no_equity": 0, "no_desk": 5,
                                     "pre_capital_change": 0}
    assert b["n_entries"] == 25 and b["n_entries_excluded"] == 10
    # every entry the window carried for a desk is either in n or in one of the four
    raw = Counter(e["desk"] for e in entries)
    assert raw["swing"] == 35
    for desk, row in B.items():
        assert row["n_entries"] + row["n_entries_excluded"] == raw[desk], desk
        assert row["n_entries_excluded"] == sum(row["excluded_entries"].values()), desk

    # an entry that records no equity is counted too, not skipped in silence
    stripped = [{k: v for k, v in e.items() if not (e["desk"] == "momentum" and k == "equity")}
                for e in entries]
    m = rp.benchmark(stripped, None, books)["momentum"]
    assert m["excluded_entries"]["no_equity"] == 6 and m["n_entries"] == 0
    assert m["n_entries"] + m["n_entries_excluded"] == raw["momentum"]

    # and the page can be reconciled against the journal without opening the JSON
    md = rp.markdown(rp.build(entries, books, None, (5,)))
    doc = rr.benchmark_section({"benchmark": B})
    for page in (md, doc):
        assert "25 of 35" in page, page[:200]
        assert "7 sentinel" in page
        assert "5 with no `desk`" in page or "5 with no <code>desk</code>" in page


# ------------------------------------------------------------------ 2 · the honesty chip
def test_the_absurd_row_no_longer_badges_itself_a_sample(rp, rr, entries, books):
    """BEFORE: the five bogus entries pushed swing to 30, so the one impossible row was the
    only benchmark row WITHOUT a not-a-sample chip while the two honest ones carried it."""
    B = rp.benchmark(entries, None, books)
    assert B["swing"]["n_entries"] == 25 and B["swing"]["sample"] == rp.NOT_A_SAMPLE
    assert {b["sample"] for b in B.values()} == {rp.NOT_A_SAMPLE}
    doc = rr.benchmark_section({"benchmark": B})
    assert doc.count("not a sample") == 3, "every desk row carries the chip"
    assert "resized.date" in doc and "capital base" in doc


# ------------------------------------------------------------------ 3 · the --since join
def test_since_narrows_the_trades_reported_not_the_decisions_they_join_to(rp, entries, books):
    """BEFORE: `attribution` built its place-buy map from the already-windowed entries, so
    every position opened before Monday lost its entry score — 6 of the swing desk's 8
    in-window exits (75%) landed in bucket `unknown`."""
    windowed = [e for e in entries if e["date"] >= MONDAY]
    swing = {"swing": books["swing"]}
    broken = rp.attribution(windowed, swing, since=MONDAY)
    assert broken["n_closed"] == 8
    assert broken["by_score_bucket"]["unknown"]["n"] == 6

    fixed = rp.attribution(windowed, swing, since=MONDAY, join_entries=entries)
    assert fixed["n_closed"] == 8, "the window still decides which trades are reported"
    assert "unknown" not in fixed["by_score_bucket"]
    assert fixed["n_unmatched_to_a_decision"] == 0
    assert set(fixed["by_score_bucket"]) == {"60-69", "70-79"}
    # and build()/main() are wired to pass the unwindowed journal through
    res = rp.build(windowed, swing, None, (5,), since=MONDAY, all_entries=entries)
    assert "unknown" not in res["attribution"]["by_score_bucket"]


def test_the_cli_joins_against_the_whole_journal(rp, tmp_path):
    out = tmp_path / "r.json"
    assert rp.main(["--journals", *JOURNALS, "--books", str(BOOKS), "--since", MONDAY,
                    "--json", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    A = res["attribution"]
    assert res["window"]["first"] == "2026-09-08"
    assert A["n_closed"] == 23 and A["by_desk"]["swing"]["n"] == 8
    # 21 of the 23 landed in `unknown` before the fix; the 15 that remain are the peer
    # desks', whose journals were trimmed to a week and carry no place-buy for them
    assert A["by_score_bucket"]["unknown"]["n"] == 15
    assert A["n_unmatched_to_a_decision"] == 15
    assert {k for k in A["by_score_bucket"]} == {"60-69", "70-79", "unknown"}


def test_the_attribution_docstring_quotes_only_figures_this_data_gives(rp, entries, books):
    """The docstring said the broken join left "74% of the week's closed trades" unknown.
    Nothing measures 74%: it is 6 of the swing desk's 8 in-window exits (75%), or 21 of the
    23 across all three desks (91%), and 15 of 23 (65%) remain unknown after the fix
    because the peer journals were trimmed to a week. A reader must not be able to derive a
    number the tests contradict, so the prose is pinned to the data it describes."""
    windowed = [e for e in entries if e["date"] >= MONDAY]
    broken_swing = rp.attribution(windowed, {"swing": books["swing"]}, since=MONDAY)
    broken_all = rp.attribution(windowed, books, since=MONDAY)
    fixed_all = rp.attribution(windowed, books, since=MONDAY, join_entries=entries)
    counts = {"swing_broken": (broken_swing["by_score_bucket"]["unknown"]["n"],
                               broken_swing["n_closed"]),
              "all_broken": (broken_all["by_score_bucket"]["unknown"]["n"],
                             broken_all["n_closed"]),
              "all_fixed": (fixed_all["by_score_bucket"]["unknown"]["n"],
                            fixed_all["n_closed"])}
    assert counts == {"swing_broken": (6, 8), "all_broken": (21, 23), "all_fixed": (15, 23)}

    doc = " ".join((rp.attribution.__doc__ or "").split())   # prose wraps; the claim does not
    supported = {round(100.0 * hit / n) for hit, n in counts.values()}
    assert supported == {75, 91, 65}
    claimed = {int(x) for x in re.findall(r"(\d+)\s*%", doc)}
    assert claimed, "the docstring quotes no share at all"
    assert claimed <= supported, (f"{sorted(claimed - supported)} is not a share this data "
                                  f"measures; the shares it measures are {sorted(supported)}")
    for phrase in ("6 of the swing desk's 8", "(75%)", "21 of the 23", "15 of the 23"):
        assert phrase in doc, phrase


# ------------------------------------------------------------------ 4 · the dead refusal
def test_the_no_bars_refusal_reaches_the_page_and_still_exits_two(rp, rr, tmp_path, capsys):
    """BEFORE: main() returned 2 before build() ran, so the `no bars` branch was dead code
    and the page fell back to a generic 'Not run this week.' with no reason."""
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    rc = rp.main(["--journals", *JOURNALS, "--counterfactual",
                  "--json", str(out_json), "--md", str(out_md)])
    assert rc == 2, "a measurement that was asked for and did not run is still a refusal"
    assert "REFUSED: --counterfactual needs --bars" in capsys.readouterr().err
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert "no bars" in res["counterfactual"]["refused"]
    assert "no bars" in out_md.read_text(encoding="utf-8")
    card = rr.counterfactual_section(res)
    assert "no bars" in card and "--bars" in card
    assert "Not run this week" not in card
    # every other number still stands
    assert res["refusals"]["n"] and res["window"]["n_entries"]


# ------------------------------------------------------------------ 5 · absent vs zero
def test_an_absent_number_carries_no_count(rp, rr, entries, books):
    """BEFORE: `_agg(None, n)` rendered `— n=25` for SPY / exposure × SPY / excess, and a
    desk whose shadow model has priced no fill rendered `+$0.00 n=0`."""
    assert 'class="n agg"' not in rr._agg(None, 25)
    assert "n=25" not in rr._agg(None, 25)
    assert "absent" in rr._agg(None, 25)
    assert "not measured" in rr._agg(0.0, 0), "no observations is not a measured zero"
    assert 'class="n agg"' in rr._agg(1.5, 3) and "n=3" in rr._agg(1.5, 3)
    assert rp._n(None, 25) == "—" and rp._n(None, 0) == "—"
    assert rp._n(1.5, 25) == "+1.50% (n=25)"

    # the real benchmark with no bars staged: SPY is absent, not zero, and carries no n
    res = rp.build(entries, books, None, (5,))
    sec = rr.benchmark_section(res)
    row = sec.split('<td class="sym">swing</td>')[1].split("</tr>")[0]
    assert row.count('class="n absent"') == 3, "SPY, exposure × SPY and excess"
    assert "n=25" in row, "the desk's own return still carries its n"

    # the real swing journal carries a shadow block with zero fills
    assert res["shadow"]["swing"] == {"model": "on", "cum_gap_usd": 0.0, "n_fills": 0,
                                      "gap_share_of_realized_pct": 0.0,
                                      "window_gap_usd": 0.0, "n_entries_with_shadow": 2}
    sh = rr.shadow_section(res)
    assert "no fills priced" in sh
    assert "$0.00" not in sh, "a gap of zero over zero fills was never measured"


# ------------------------------------------------------------------ 6 · slices vs trades
def test_the_sample_gate_counts_round_trips_not_exit_slices(rp, entries, books):
    """BEFORE: 41 exit rows badged themselves `sample: ok` at the 30 bar. They are 17
    desk-positions in 9 names; nine rows are cap-rebalance shavings and two are under $5
    of notional (the smallest is $1.23)."""
    A = rp.attribution(entries, books)
    assert A["n_closed"] == 41
    assert A["n_desk_positions"] == 17 and A["n_names"] == 9
    assert A["n_round_trips"] == 11 and A["n_positions_still_open"] == 6
    assert A["all"]["sample"] == rp.NOT_A_SAMPLE, "17 positions is not 41 trades"
    assert A["by_exit_reason"]["rebalance"]["n"] == 9
    # the slices stay visible: nothing is hidden by counting round trips
    assert sum(v["n"] for v in A["by_exit_reason"].values()) == 41

    # equal-weighting a $1.23 shaving against a $700 position moves the mean
    assert A["all"]["pnl_pct_mean"] == pytest.approx(5.262, abs=0.001)
    assert A["all"]["pnl_pct_mean_notional_weighted"] == pytest.approx(3.992, abs=0.001)

    smallest = min(t["shares"] * t["entry"] for b in books.values()
                   for t in b["closed_trades"])
    assert smallest == pytest.approx(1.23, abs=0.01)
    tiny = [t for b in books.values() for t in b["closed_trades"]
            if t["shares"] * t["entry"] < 5.0]
    assert len(tiny) == 2


def test_a_position_still_being_shaved_is_not_a_completed_round_trip(rp, entries, books):
    """SNDK, KLAC and MDB are still held on the swing book; their trim rows closed nothing."""
    A = rp.attribution(entries, {"swing": books["swing"]})
    sw = A["by_desk"]["swing"]
    assert sw["n"] == 14 and sw["n_round_trips"] == 4 and sw["n_positions_still_open"] == 3
    held = {(p["symbol"], p["opened"]) for p in books["swing"]["positions"]}
    assert ("SNDK", "2026-09-01") in held and ("KLAC", "2026-09-09") in held


# ------------------------------------------------------------------ 7 · composite refusals
def test_a_composite_refusal_counts_every_gate_it_names(rp, entries):
    """BEFORE: `classify` split on '; ' and kept the first segment, so SSL's string — an
    evidence-coverage refusal AND a sector limit AND a 15% cap trim — counted only as
    `coverage`. The split also cut the coverage message at its own semicolon."""
    real = ("Only 40% of the evidence base was available (catalyst, fundamentals, "
            "intelligence) — under the 70% floor. The score is normalised to what was "
            "there, so a thin row can outrank a complete one; it is not sized on that "
            "basis; Sector limit: already 3 positions in None (max 3); Trimmed to the 15% "
            "max-position cap")
    c = rp.classify(real)
    assert c["rule"] == "coverage", "the first hard reason is still the one-line answer"
    assert c["rules"] == ["coverage", "sector_cap", "size_trim"]
    assert c["unknown"] == [], "the coverage message's own semicolon is not a new reason"
    assert c["detail"] == real
    # the mid-sentence continuation is re-joined, not counted
    segs = rp.segments(real)
    assert len(segs) == 3 and segs[0].endswith("not sized on that basis")

    R = rp.refusals(entries)
    assert R["other"] == [], "the real corpus classifies completely — do not regress this"
    assert R["n_composite"] == 4
    assert R["n"] == 138 and R["n_rules"] == 143
    assert R["by_rule"]["coverage"] == 4 and R["by_rule"]["size_trim"] == 4
    assert R["by_rule"]["sector_cap"] == 44          # 43 plain + SSL's second segment
    ssl = [r for r in R["_rows"] if r["symbol"] == "SSL"]
    assert len(ssl) == 4 and all("coverage" in r["rules"] for r in ssl)
    assert sum(1 for r in ssl if "sector_cap" in r["rules"]) == 1
    # the list rows keep their shape: one row per skipped name
    assert set(R["list"][0]) == {"date", "desk", "slot", "symbol", "rule", "side"}


def test_a_sizing_note_is_counted_but_never_measured_as_a_gate(rp, entries, tmp_path):
    """`size_trim` refused nothing, so E5 has no question to ask of it."""
    assert rp.NON_GATE_RULES == ("size_trim",)
    series = rp.load_bars_oc(str(_bars(tmp_path)))
    C = rp.counterfactual(entries, series, horizons=(5,))
    assert "size_trim" not in C["rules"] and "size_trim" not in C["unmeasured"]
    assert C["not_a_gate"] == {"size_trim": 4}
    # SSL's forward return is an observation for BOTH gates that turned it away
    assert ("2026-09-09", "SSL") in {(d, s) for (d, s) in _refused(C, rp, entries, "coverage")}
    assert ("2026-09-09", "SSL") in {(d, s) for (d, s) in _refused(C, rp, entries, "sector_cap")}


def _refused(C, rp, entries, rule):
    refs = rp.refusals(entries)
    return {(r["date"], r["symbol"]) for r in refs["_rows"]
            if rule in r["rules"] and r["side"] == "entry"}


def _bars(tmp_path):
    """A bars file covering the fixture's dates for every symbol it names."""
    import datetime as dt
    days, d = [], dt.date(2026, 8, 28)
    while len(days) < 30:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    syms = ["SPY", "SSL", "AMD", "AEHR", "MU", "NVDA", "MDB", "HPE", "DELL", "PANW", "AVGO",
            "MRVL", "CRDO", "HOOD", "KLAC", "ALAB", "ANET", "SNDK", "DDOG", "VSXY", "FIVE",
            "PATH", "IOT", "PL", "ASAN", "UCTT", "ROIV", "AVAV", "TTAN", "RKLB", "NAVN",
            "COO", "SWKS", "RH", "ORCL", "KR", "CPRT"]
    results = [{"symbol": s, "bars": [
        {"begins_at": f"{x}T00:00:00Z", "close_price": f"{100 + i:.4f}",
         "open_price": f"{100 + i:.4f}", "interpolated": False}
        for i, x in enumerate(days)]} for s in syms]
    p = tmp_path / "bars.json"
    p.write_text(json.dumps({"data": {"results": results}}), encoding="utf-8")
    return p


# ------------------------------------------------------------------ 8 · coverage
def test_pm_coverage_has_a_flag_and_a_table(rp, rr, tmp_path):
    """BEFORE: the runner prompt named pm-coverage.json as an input to the review and
    neither script could read it, so the expected-vs-actual run table was hand-written."""
    cov = rp.coverage(rp.load_coverage(str(COVERAGE)))
    assert cov["decision_slots"] == list(rp.DECISION_SLOTS)
    assert cov["sentinels_per_day"] == 7
    # 2026-09-10 is the doc's own `updated` day: shown, but not counted as a miss
    assert cov["totals"] == {"expected": 22, "present": 11, "missing": 11, "aborted": 0,
                             "desk_rows": 57, "days": 3, "partial_days": 1}
    d8 = cov["days"]["2026-09-08"]
    assert d8["decision_slots_missing"] == ["opening-range"]
    assert d8["sentinels_present"] == 0 and not d8["partial"]
    assert d8["engine_sha"] == ["8fdab37"]
    assert cov["days"]["2026-09-09"]["decision_slots_missing"] == ["pre-market"]
    assert cov["days"]["2026-09-09"]["sentinels_present"] == 5
    assert cov["days"]["2026-09-09"]["quiet_desk_rows"] == 13
    d10 = cov["days"]["2026-09-10"]
    assert d10["partial"] and d10["decision_slots_missing"] == ["power-hour"]

    out = tmp_path / "r.json"
    assert rp.main(["--journals", *JOURNALS, "--books", str(BOOKS),
                    "--coverage", str(COVERAGE), "--json", str(out)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["coverage"]["totals"]["missing"] == 11
    page = rr.coverage_section(res)
    assert "2026-09-08" in page and "3/4" in page and "0/7" in page
    assert "opening-range" in page and "7 sentinel(s)" in page
    assert "partial" in page
    assert "11/22" in page
    assert "No coverage doc staged" in rr.coverage_section({})
    assert "Coverage" in rp.markdown(res) and "| 2026-09-08 | 3/4 | 0/7 |" in rp.markdown(res)


# ------------------------------------------------------------------ the page, end to end
def test_the_live_page_renders_and_keeps_the_honesty_budget(rp, rr, entries, books, tmp_path):
    import ledger as lg
    from conftest import ROOT
    res = rp.build(entries, books, None, (5, 10), with_cf=True,
                   cov=rp.load_coverage(str(COVERAGE)))
    doc = rr.render(res, lg.trials(str(ROOT / "experiments" / "ledger.jsonl")),
                    generated_at="2026-09-11T20:35:00Z")
    low = doc.lower()
    for word in ("win rate", "win-rate", "winrate", "sharpe"):
        assert word not in low, word
    import re
    for cell in re.findall(r'<td class="n agg">(.*?)</td>', doc, re.S):
        assert re.search(r"n=(\d+|unknown)", cell), cell
        assert "—" not in cell and "n=0<" not in cell, cell
    for cell in re.findall(r'<td class="n absent">(.*?)</td>', doc, re.S):
        assert "n=" not in cell, cell
    # the swing row is no longer the only one without a chip
    assert doc.count("not a sample") >= 3
    assert "10,083" not in doc and "10083" not in doc
    assert "Coverage" in doc and "11/22" in doc
    import mirror
    assert mirror.scan_text(doc) == []
    assert "claude.ai/code/" + "artifact" not in doc
