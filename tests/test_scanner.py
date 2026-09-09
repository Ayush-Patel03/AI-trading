"""The scoring model — 514 statements that decide what the system buys, and until now the
only major module in the engine with no test at all.

Every expectation here is the behaviour the engine already had on 2026-09-02. These tests
pin it; they do not propose a different model. The exception is the coverage floor, which
this suite found: see test_a_thin_row_is_not_sized_on_a_normalised_score.
"""
import json
import pathlib

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"


@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


@pytest.fixture
def scan_data():
    return json.loads((FIX / "scan_data.json").read_text())


@pytest.fixture
def scanned(scanner, scan_data):
    out = scanner.scan(scan_data)
    return out, {r["ticker"]: r for r in out["results"]}


# ------------------------------------------------------------------ setup classification
@pytest.mark.parametrize("price,ma50,ma200,want", [
    (110, 100, 90, "Momentum"),                 # above both, 50 over 200
    (95, 100, 90, "Pullback in Uptrend"),       # under the 50 but over the 200, 50 over 200
    (105, 100, 110, "Early Recovery"),          # reclaimed the 50 while 50 is under 200
    (80, 100, 90, "Broken Trend"),              # under the 200
    (None, 100, 90, "Unclassified"),
])
def test_setup_classification(scanner, price, ma50, ma200, want):
    setup, note = scanner.classify_setup({"price": price, "ma_50": ma50, "ma_200": ma200})
    assert setup == want
    assert note


# ------------------------------------------------------------------ RSI in context
def test_rsi_is_scored_against_the_setup_not_in_the_abstract(scanner):
    """The single most opinionated rule in the model: RSI 44 is the entry on a pullback
    and a failing trend on a momentum name. Context-free RSI would reward the wrong half
    of the board."""
    pull, _ = scanner.score_rsi(44.0, "Pullback in Uptrend")
    mom, _ = scanner.score_rsi(44.0, "Momentum")
    assert pull == 3.0, "44 on a pullback is the mean-reversion entry — full marks"
    assert mom == 1.0, "the same 44 on a momentum name is a trend failing behind intact MAs"
    assert pull > mom


@pytest.mark.parametrize("rsi,setup,want", [
    (25, "Pullback in Uptrend", 1.5),   # oversold, but a falling knife
    (45, "Pullback in Uptrend", 3.0),
    (55, "Pullback in Uptrend", 2.0),
    (75, "Pullback in Uptrend", 1.0),
    (85, "Momentum", 0.5),
    (75, "Momentum", 1.5),
    (60, "Momentum", 3.0),
    (48, "Momentum", 2.0),
    (30, "Momentum", 1.0),
    (25, "Neutral", 1.0),
])
def test_rsi_bands(scanner, rsi, setup, want):
    assert scanner.score_rsi(rsi, setup)[0] == want


def test_a_missing_rsi_scores_nothing_and_says_nothing(scanner):
    assert scanner.score_rsi(None, "Momentum") == (0.0, None)


# ------------------------------------------------------------------ pillar ceilings
def test_no_pillar_can_exceed_its_maximum(scanner, scan_data):
    """The weights are the model. A pillar that can overflow its cap silently reweights
    everything else."""
    today = __import__("datetime").date(2026, 9, 2)
    for tk, c in scan_data["candidates"].items():
        if not c.get("price"):
            continue
        setup, _ = scanner.classify_setup(c)
        for fn, key in ((lambda x: scanner.score_trend(x), "trend"),
                        (lambda x: scanner.score_momentum(x, setup), "momentum"),
                        (lambda x: scanner.score_fundamentals(x), "fundamentals"),
                        (lambda x: scanner.score_catalyst(x, today), "catalyst"),
                        (lambda x: scanner.score_intelligence(x), "intelligence")):
            pts, _ = fn(c)
            assert 0 <= pts <= scanner.PILLAR_MAX[key], f"{tk} {key} scored {pts}"


def test_the_pillar_weights_sum_to_the_full_scale(scanner):
    assert sum(scanner.PILLAR_MAX.values()) == scanner.FULL_SCALE == 100


def test_trend_scores_a_pullback_as_opportunity_not_weakness(scanner):
    """-12% to -2% below the 50-day is the constructive pullback zone and outscores
    riding the line, which outscores being extended."""
    def t(price):
        return scanner.score_trend({"price": price, "ma_50": 100.0, "ma_200": 80.0})[0]
    assert t(94) > t(101) > t(112) > t(130)


# ------------------------------------------------------------------ regime gate
@pytest.mark.parametrize("vix,off_high,above50,want_label,want_mult", [
    (14.0, -1.0, 70.0, "Risk-On", 1.05),
    (18.0, -5.0, 55.0, "Constructive", 1.00),
    (20.0, -10.0, 55.0, "Mixed / Cautious", 0.93),
    (30.0, -20.0, 30.0, "Risk-Off", 0.85),
])
def test_the_regime_multiplier_moves_with_the_tape(scanner, vix, off_high, above50,
                                                   want_label, want_mult):
    mult, label, notes = scanner.score_regime(
        {"vix": vix, "spy": {"pct_off_52w_high": off_high},
         "breadth": {"pct_above_50dma": above50}})
    assert (label, mult) == (want_label, want_mult)
    assert notes, "the regime must always explain itself"


def test_an_absent_regime_is_partial_not_a_crash(scanner):
    mult, label, notes = scanner.score_regime({})
    assert 0.85 <= mult <= 1.05
    assert any("unavailable" in n for n in notes)


# ------------------------------------------------------------------ coverage normalisation
def test_a_pillar_with_no_inputs_is_no_evidence_not_a_zero(scanner, scanned):
    """The 2026-08-31 audit's biggest scoring bug: a dead source used to score zero, which
    silently rescaled the whole board and made Strong Buy unreachable."""
    _, by = scanned
    thin = by["THIN"]
    assert thin["coverage_pct"] == 25.0
    assert set(thin["missing_pillars"]) == {"momentum", "fundamentals", "catalyst",
                                            "intelligence"}
    assert thin["score"] > 0, "a row with one strong pillar is not a zero"


def test_full_coverage_is_reported_as_such(scanned):
    _, by = scanned
    assert by["NVDA"]["coverage_pct"] == 100.0
    assert by["NVDA"]["missing_pillars"] == []


def test_a_thin_row_cannot_earn_a_strong_buy(scanned, scanner):
    _, by = scanned
    thin = by["THIN"]
    assert thin["coverage_pct"] < scanner.MIN_COVERAGE_FOR_STRONG
    assert thin["verdict"] != "Strong Buy"
    assert "Capped from Strong Buy" in thin["verdict_note"]


# ------------------------------------------------------------------ verdict
@pytest.mark.parametrize("score,want", [
    (90, "Strong Buy"), (72, "Strong Buy"), (71.9, "Buy"), (60, "Buy"),
    (59.9, "Watch"), (48, "Watch"), (47.9, "Hold"), (35, "Hold"), (34.9, "Avoid"),
])
def test_verdict_thresholds(scanner, score, want):
    assert scanner.verdict(score, "Momentum")[0] == want


def test_broken_trend_overrides_every_score(scanner):
    v, note = scanner.verdict(99.0, "Broken Trend")
    assert v == "Avoid" and "Structure is broken" in note


def test_the_broken_trend_name_is_avoided_end_to_end(scanned):
    _, by = scanned
    assert by["XOM"]["setup"] == "Broken Trend"
    assert by["XOM"]["verdict"] == "Avoid"


# ------------------------------------------------------------------ the run as a whole
def test_a_row_with_no_usable_price_is_dropped_and_named(scanned):
    """Never render a row full of em-dashes that looks like a finding."""
    out, by = scanned
    assert "NOPX" not in by
    assert out["meta"]["dropped"] == ["NOPX"]
    assert any("No usable price" in w for w in out["meta"]["data_warnings"])


def test_rows_come_back_ranked(scanned):
    out, _ = scanned
    scores = [r["score"] for r in out["results"]]
    assert scores == sorted(scores, reverse=True)


def test_the_regime_multiplier_is_applied_to_every_row(scanned):
    out, by = scanned
    mult = out["regime"]["multiplier"]
    for r in out["results"]:
        expected = min(100.0, round(r["normalized_score"] * mult, 1))
        assert abs(r["score"] - expected) <= 0.15, r["ticker"]


def test_a_late_scan_banners_itself_and_is_marked_stale(scanner, scan_data):
    scan_data["meta"]["minutes_late"] = 95
    out = scanner.scan(scan_data)
    assert out["meta"]["stale"] is True
    assert any("LATE SCAN" in w for w in out["meta"]["data_warnings"])


def test_a_degraded_board_says_so(scanned):
    out, _ = scanned
    assert any(n.startswith("DEGRADED:") for n in out["notable"])


def test_every_row_carries_its_reasons(scanned):
    _, by = scanned
    for tk, r in by.items():
        assert set(r["reasons"]) == set(r["pillars"])
        assert r["setup_note"], f"{tk} has no setup note"


# ------------------------------------------------------------------ the golden file
def test_the_whole_run_is_byte_identical_to_the_golden_output(scanner, scan_data):
    """One frozen input, one frozen output. Any change to the model — a weight, a band, a
    threshold — shows up here as a diff that has to be justified, rather than as a board
    that quietly ranks differently one morning."""
    got = json.loads(json.dumps(scanner.scan(scan_data), sort_keys=True))
    want = json.loads((FIX / "scan_results_golden.json").read_text())
    assert got == want, (
        "the scoring model changed. If that was deliberate, regenerate the golden with:\n"
        "  python3 -c \"import json,scanner;"
        "json.dump(scanner.scan(json.load(open('tests/fixtures/scan_data.json'))),"
        "open('tests/fixtures/scan_results_golden.json','w'),indent=2,sort_keys=True)\"\n"
        "and say what moved and why in the commit message.")


# ------------------------------------------------------------------ found by this suite
def test_a_thin_row_is_not_sized_on_a_normalised_score(scanner, scan_data):
    """FOUND BY THIS SUITE, 2026-09-02.

    THIN carries trend data and nothing else. Its 24-of-25 trend score normalises against
    the 25 points of evidence that existed, times the regime multiplier, to 100 — so it
    ranked ABOVE a fully-evidenced NVDA at 76.7. scanner.py caps such a row's verdict
    (MIN_COVERAGE_FOR_STRONG) but nothing capped its SIZE, and conviction scales with
    score: the row with the least evidence behind it would have been sized at the maximum
    2% risk. `min_coverage_to_propose` closes it, and this test is why it exists."""
    import portfolio
    out = scanner.scan(scan_data)
    by = {r["ticker"]: r for r in out["results"]}
    assert by["THIN"]["score"] > by["NVDA"]["score"], \
        "the fixture must keep reproducing the condition, or this test proves nothing"

    empty = {"positions": [], "equity": 15000.0, "invested": 0.0, "cash": 15000.0}
    props, _ = portfolio.build_proposals(out["results"], empty)
    thin = next(p for p in props if p["ticker"] == "THIN")
    assert thin["blocked"] is True
    assert any("evidence base" in w for w in thin["warnings"])

    nvda = next(p for p in props if p["ticker"] == "NVDA")
    assert nvda["blocked"] is False, "a fully-evidenced row is unaffected"


# --- meta.scan_date and the macro banner (live defects, 2026-09-09) -----------------

def test_scan_date_accepts_the_documented_key(scanner, scan_data):
    # SCAN.md's contract names `date`; the module only ever read `scan_date`, so a caller
    # who followed the doc got KeyError on the first call.
    meta = {"date": "2026-09-09"}
    assert scanner.scan_date_of(meta) == "2026-09-09"
    assert meta["scan_date"] == "2026-09-09", "must normalise for everything downstream"


def test_scan_date_prefers_the_explicit_key(scanner):
    assert scanner.scan_date_of({"scan_date": "2026-09-09", "date": "2026-01-01"}) == "2026-09-09"


def test_a_scan_with_neither_key_is_refused(scanner):
    with pytest.raises(KeyError):
        scanner.scan_date_of({"slot": "midday"})


def test_a_scan_carrying_only_date_scores(scanner, scan_data):
    scan_data["meta"].pop("scan_date", None)
    scan_data["meta"]["date"] = "2026-09-09"
    out = scanner.scan(scan_data)
    assert out["results"], "the run must complete on the documented contract"


def _banner(scanner, scan_data, events):
    scan_data["meta"]["scan_date"] = "2026-09-09"
    scan_data["meta"]["macro_events"] = events
    return " | ".join(scanner.scan(scan_data)["notable"])


def test_a_high_impact_release_is_bannered_as_gating(scanner, scan_data):
    txt = _banner(scanner, scan_data,
                  [{"date": "2026-09-09", "name": "CPI (Aug)", "time_et": "08:30"}])
    assert "MACRO TODAY: CPI (Aug)" in txt
    assert "no new entries" in txt


def test_a_non_gating_release_is_reported_and_labelled_not_gating(scanner, scan_data):
    # This is the defect: the board claimed five releases froze entries on 2026-09-01 and
    # six on 2026-09-09, while the manager reported every one of them as not gating.
    txt = _banner(scanner, scan_data,
                  [{"date": "2026-09-09", "name": "ISM Manufacturing PMI", "time_et": "10:00"}])
    assert "NOT gating" in txt
    assert "no new entries" not in txt


def test_adp_is_not_read_as_payrolls(scanner, scan_data):
    txt = _banner(scanner, scan_data,
                  [{"date": "2026-09-09", "name": "ADP National Employment"}])
    assert "NOT gating" in txt


def test_the_board_and_the_manager_agree_on_every_release(scanner, scan_data):
    # One definition, imported from pm.py. If these ever disagree the board is lying
    # about the manager, which is the whole defect.
    import pm
    for name in ("FOMC Rate Decision", "CPI (Aug)", "Core PCE", "Nonfarm Payrolls",
                 "GDP (Q2 second estimate)", "Powell speaks",
                 "ISM Services PMI", "JOLTS Job Openings", "Beige Book",
                 "Initial Jobless Claims", "ADP National Employment"):
        txt = _banner(scanner, scan_data, [{"date": "2026-09-09", "name": name}])
        gated_on_board = "no new entries" in txt
        assert gated_on_board is pm._is_high_impact(name), name


def test_tomorrows_release_is_still_noted_for_the_overnight(scanner, scan_data):
    txt = _banner(scanner, scan_data,
                  [{"date": "2026-09-10", "name": "ISM Services PMI"}])
    assert "MACRO TOMORROW" in txt
