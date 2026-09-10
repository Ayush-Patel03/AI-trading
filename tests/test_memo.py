"""memo.py — the LLM-as-structured-extractor pattern (P-07).

The LLM writes a memo; the engine validates it against the news payload the session was
given, consumes only the numeric fields as logged features, and scores every probability
call against what the price did. These tests pin the contract: what a valid memo is, every
reason a memo is refused, that anonymisation round-trips, that the scanner attaches the
features without moving a score, and that the calibration loop closes.
"""
import json
import pathlib
import random

import pytest

FIX = pathlib.Path(__file__).parent / "fixtures"
AS_OF = "2026-09-10T12:30:00Z"
SALT = "run-2026-09-10-midday"

ARTICLE = ("NVIDIA Corp raised its full-year data-centre revenue guidance to $54.5 billion, "
           "12% above consensus, CEO Jensen Huang said on Tuesday. Shares of NVDA rose 3.1% "
           "after hours. Nvidia also named CFO Colette Kress to the board.")


def _payload():
    import memo
    return {"as_of": AS_OF, "salt": SALT, "symbols": {
        "NVDA": {"symbol_hash": memo.symbol_hash("NVDA", SALT),
                 "company_name": "NVIDIA Corp", "aliases": ["Nvidia", "NVIDIA"],
                 "executives": ["Jensen Huang", "Colette Kress"],
                 "items": [{"url": "https://example.com/nvda-guide",
                            "published_at": "2026-09-10T11:02:00Z", "source": "Example Wire",
                            "title": "NVIDIA raises guidance", "text": ARTICLE}]}}}


def _memo(**over):
    import memo
    m = {"schema": 1, "as_of": AS_OF, "symbol_hash": memo.symbol_hash("NVDA", SALT),
         "sources": [{"url": "https://example.com/nvda-guide",
                      "published_at": "2026-09-10T11:02:00Z", "source": "Example Wire"}],
         "event_type": "guidance", "direction": "positive", "magnitude_bucket": "medium",
         "confidence": 0.8, "p_up_5d": 0.62,
         "quote": "raised its full-year data-centre revenue guidance to $54.5 billion",
         "numerals": ["54.5", "12%", 3.1],
         "model": {"id": "claude-fable-5-1", "cutoff_date": "2026-06-01",
                   "prompt_hash": "abc123", "temperature": 0}}
    m.update(over)
    return m


@pytest.fixture
def memo():
    import memo as m
    return m


# ------------------------------------------------------------------ validation
def test_a_valid_memo_passes(memo):
    ok, errs = memo.validate(_memo(), memo.payload_for(_payload(), "NVDA"))
    assert ok, errs
    assert errs == []


def test_a_memo_may_quote_the_anonymised_text(memo):
    """The extractor saw COMPANY_A, not NVIDIA, so its verbatim quote contains the
    placeholder. The engine must accept that as the source text it was."""
    m = _memo(quote="COMPANY_A raised its full-year data-centre revenue guidance")
    ok, errs = memo.validate(m, memo.payload_for(_payload(), "NVDA"))
    assert ok, errs


@pytest.mark.parametrize("over,needle", [
    ({"sources": [{"url": "u", "published_at": "2026-09-10T12:28:00Z", "source": "s"}]},
     "later than as_of - 5 min"),                                 # inside the latency window
    ({"sources": [{"url": "u", "published_at": "2026-09-11T09:00:00Z", "source": "s"}]},
     "later than as_of - 5 min"),                                 # plainly in the future
    ({"numerals": ["54.5", "61"]}, "numeral '61' does not appear"),  # computed / hallucinated
    ({"quote": "raised guidance to $54.5 billion, a big beat"}, "not a verbatim substring"),
    ({"model": {"id": "x", "cutoff_date": "2026-06-01", "prompt_hash": "h", "temperature": 0.7}},
     "temperature 0.7 != 0"),
    ({"event_type": "rumour"}, "event_type 'rumour' not in"),
    ({"direction": "up"}, "direction 'up' not in"),
    ({"magnitude_bucket": "huge"}, "magnitude_bucket 'huge' not in"),
    ({"confidence": 1.4}, "confidence 1.4 not in [0, 1]"),
    ({"p_up_5d": -0.1}, "p_up_5d -0.1 not in [0, 1]"),
    ({"schema": 2}, "schema 2 != 1"),
    ({"symbol_hash": "NVDA"}, "does not match the payload"),
    ({"model": {"id": "x", "cutoff_date": "2026-12-01", "prompt_hash": "h", "temperature": 0}},
     "before model.cutoff_date"),
    ({"quote": "x" * 301}, "max 300"),
])
def test_each_rejection_reason_is_named(memo, over, needle):
    ok, errs = memo.validate(_memo(**over), memo.payload_for(_payload(), "NVDA"))
    assert not ok
    assert any(needle in e for e in errs), errs


def test_a_missing_key_is_a_rejection(memo):
    m = _memo()
    del m["numerals"]
    ok, errs = memo.validate(m, memo.payload_for(_payload(), "NVDA"))
    assert not ok and errs == ["missing key: numerals"]


def test_the_latency_window_is_a_parameter(memo):
    m = _memo(sources=[{"url": "u", "published_at": "2026-09-10T12:20:00Z", "source": "s"}])
    blk = memo.payload_for(_payload(), "NVDA")
    assert memo.validate(m, blk, latency_min=5)[0]
    assert not memo.validate(m, blk, latency_min=15)[0]


def test_numerals_match_on_value_not_on_formatting(memo):
    """'$54.5 billion' in the article; the memo may cite 54.5 as a number or a string with
    the currency sign, and 12% with or without the sign — it is the same copied number."""
    m = _memo(numerals=[54.5, "$54.5", "12", "12%", "3.1%"])
    assert memo.validate(m, memo.payload_for(_payload(), "NVDA"))[0]


def test_a_no_news_memo_needs_no_sources_or_quote(memo):
    m = _memo(sources=[], event_type="none", direction="none", magnitude_bucket="none",
              quote="", numerals=[], p_up_5d=None)
    ok, errs = memo.validate(m, memo.payload_for(_payload(), "NVDA"))
    assert ok, errs


def test_an_event_memo_without_sources_is_refused(memo):
    ok, errs = memo.validate(_memo(sources=[]), memo.payload_for(_payload(), "NVDA"))
    assert not ok and any("no sources" in e for e in errs)


# ------------------------------------------------------------------ anonymisation
def test_anonymise_replaces_every_form_of_the_name_and_the_executives(memo):
    text, mapping = memo.anonymise(ARTICLE, "NVDA", "NVIDIA Corp", ["Nvidia", "NVIDIA"],
                                   ["Jensen Huang", "Colette Kress"])
    for leak in ("NVDA", "NVIDIA", "Nvidia", "Jensen", "Huang", "Kress"):
        assert leak not in text, text
    assert text.startswith("COMPANY_A raised")
    assert "Shares of COMPANY_A rose" in text
    assert "CEO EXEC_1 said" in text and "CFO EXEC_2 to the board" in text
    assert mapping["COMPANY_A"] == {"symbol": "NVDA", "company_name": "NVIDIA Corp",
                                    "aliases": ["Nvidia", "NVIDIA"]}
    assert mapping["EXEC_1"] == "Jensen Huang" and mapping["EXEC_2"] == "Colette Kress"


def test_anonymise_deanonymise_round_trip(memo):
    text = ("NVIDIA Corp guided above consensus; Jensen Huang and Colette Kress spoke. "
            "NVIDIA Corp's shares rose.")
    anon, mapping = memo.anonymise(text, "NVDA", "NVIDIA Corp", ["Nvidia"],
                                   ["Jensen Huang", "Colette Kress"])
    assert "NVIDIA" not in anon and "Huang" not in anon
    assert memo.deanonymise(anon, mapping) == text


def test_aliases_and_the_ticker_deanonymise_to_the_canonical_name(memo):
    anon, mapping = memo.anonymise("$NVDA and Nvidia are the same thing", "NVDA",
                                   "NVIDIA Corp", ["Nvidia"])
    assert anon == "COMPANY_A and COMPANY_A are the same thing"
    assert memo.deanonymise(anon, mapping) == "NVIDIA Corp and NVIDIA Corp are the same thing"


def test_anonymise_walks_a_payload_and_drops_the_identity_keys(memo):
    blk = memo.payload_for(_payload(), "NVDA")
    anon, mapping = memo.anonymise(blk, blk["symbol"], blk["company_name"], blk["aliases"],
                                   blk["executives"])
    assert "company_name" not in anon and "aliases" not in anon and "symbol" not in anon
    assert "executives" not in anon
    assert anon["symbol_hash"] == blk["symbol_hash"]
    assert "NVIDIA" not in json.dumps(anon) and "Huang" not in json.dumps(anon)
    assert "COMPANY_A" in anon["items"][0]["text"]
    assert memo.deanonymise(anon, mapping)["items"][0]["text"] == \
        ARTICLE.replace("NVDA", "NVIDIA Corp").replace("Nvidia", "NVIDIA Corp")


def test_a_substring_of_another_word_is_not_replaced(memo):
    anon, _ = memo.anonymise("The ONE Corp rose; someone was there", "ONE", "ONE Corp", [])
    assert anon == "The COMPANY_A rose; someone was there"


def test_symbol_hash_is_opaque_and_salted(memo):
    h1, h2 = memo.symbol_hash("NVDA", "a"), memo.symbol_hash("NVDA", "b")
    assert h1 != h2 and "NVDA" not in h1 and len(h1) == 16
    assert memo.symbol_hash("nvda ", "a") == h1


# ------------------------------------------------------------------ features
def test_to_features_maps_enums_to_numbers(memo):
    f = memo.to_features(_memo())
    assert f == {"llm_event_type": "guidance", "llm_direction": 1, "llm_magnitude": 2,
                 "llm_confidence": 0.8, "llm_p_up_5d": 0.62}
    assert memo.to_features(_memo(direction="negative", magnitude_bucket="large",
                                  p_up_5d=None))["llm_direction"] == -1
    assert memo.to_features(_memo(direction="mixed"))["llm_direction"] == 0
    assert memo.to_features(_memo(magnitude_bucket="large"))["llm_magnitude"] == 3
    assert memo.to_features(_memo(p_up_5d=None))["llm_p_up_5d"] is None


def test_to_features_is_all_null_without_a_memo(memo):
    assert memo.to_features(None) == {k: None for k in memo.FEATURE_KEYS}


# ------------------------------------------------------------------ calibration
def _synthetic_rows(n, worse_than_base, seed=1):
    """Outcomes with a 55% base rate. A 'worse' extractor calls confidently the WRONG way
    half the time; a 'better' one leans the right way."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        y = 1 if rng.random() < 0.55 else 0
        if worse_than_base:
            p = 0.9 if (y == 0) == (i % 2 == 0) else 0.1
        else:
            p = 0.75 if y else 0.3
        rows.append({"as_of": f"2026-09-{1 + i % 28:02d}T12:30:00Z", "symbol": f"S{i}",
                     "p_up_5d": p, "realised_up_5d": y})
    return rows


def test_calibration_report_is_advisory_when_the_llm_is_worse_than_the_base_rate(memo):
    rep = memo.calibration_report(_synthetic_rows(200, worse_than_base=True))
    assert rep["n"] == 200
    assert rep["brier"] > rep["base_rate_brier"]
    assert rep["advisory"] is True
    assert "not below the base-rate" in rep["advisory_reason"]


def test_calibration_report_goes_live_only_when_better_over_100_calls(memo):
    good = _synthetic_rows(200, worse_than_base=False)
    rep = memo.calibration_report(good)
    assert rep["brier"] < rep["base_rate_brier"]
    assert rep["advisory"] is False
    few = memo.calibration_report(good[:60])
    assert few["brier"] < few["base_rate_brier"]
    assert few["advisory"] is True, "better, but not over enough calls yet"
    assert "60 of 100" in few["advisory_reason"]


def test_reliability_bins_group_by_stated_probability(memo):
    rep = memo.calibration_report(_synthetic_rows(200, worse_than_base=False))
    bins = {b["bin"]: b for b in rep["reliability_bins"]}
    assert set(bins) == {"0.3-0.4", "0.7-0.8"}
    assert bins["0.7-0.8"]["mean_outcome"] == 1.0 and bins["0.3-0.4"]["mean_outcome"] == 0.0
    assert sum(b["n"] for b in bins.values()) == 200


def test_an_empty_log_is_advisory(memo, tmp_path):
    rep = memo.calibration_report(str(tmp_path / "calibration.jsonl"))
    assert rep == {**rep, "n": 0, "advisory": True}


# ------------------------------------------------------------------ the scanner hook
def _stage(run_dir, memo_over=None, payload=True):
    (run_dir / "memos").mkdir(exist_ok=True)
    (run_dir / "memos" / "NVDA.json").write_text(json.dumps(_memo(**(memo_over or {}))),
                                                  encoding="utf-8")
    if payload:
        (run_dir / "news_payload.json").write_text(json.dumps(_payload()), encoding="utf-8")


@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


def _scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def test_the_golden_is_unchanged_without_memos(scanner, run_dir):
    """No memos/ directory: the run is byte-identical to the golden, run_dir or not."""
    want = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    got = json.loads(json.dumps(scanner.scan(_scan_data(), run_dir=str(run_dir)), sort_keys=True))
    assert got == want


def test_a_valid_memo_attaches_features_and_moves_no_score(scanner, run_dir):
    _stage(run_dir)
    plain = {r["ticker"]: r for r in scanner.scan(_scan_data())["results"]}
    out = scanner.scan(_scan_data(), run_dir=str(run_dir))
    by = {r["ticker"]: r for r in out["results"]}
    assert by["NVDA"]["features"]["llm_event_type"] == "guidance"
    assert by["NVDA"]["features"]["llm_direction"] == 1
    assert by["NVDA"]["features"]["llm_p_up_5d"] == 0.62
    # every other row carries the keys as null — rectangular for ic.py, never scored
    assert by["MU"]["features"]["llm_event_type"] is None
    assert by["MU"]["features"]["llm_p_up_5d"] is None
    for tk in by:
        for k in ("score", "pillars", "raw_score", "verdict", "setup", "coverage_pct"):
            assert by[tk][k] == plain[tk][k], f"{tk} {k} moved with a memo attached"
    assert out["meta"]["memo_rejections"] == {}
    assert out["meta"]["memos"]["accepted"] == ["NVDA"]
    assert out["meta"]["memos"]["advisory"] is True
    # the probability call is logged, pending, in the run's archive
    import memo
    rows = memo.read_calibration(str(run_dir / "archive" / "calibration.jsonl"))
    assert len(rows) == 1 and rows[0]["symbol"] == "NVDA" and rows[0]["realised_up_5d"] is None
    assert rows[0]["p_up_5d"] == 0.62


def test_a_memo_that_fails_validation_is_recorded_not_attached(scanner, run_dir):
    _stage(run_dir, {"numerals": ["999"]})
    out = scanner.scan(_scan_data(), run_dir=str(run_dir))
    by = {r["ticker"]: r for r in out["results"]}
    assert by["NVDA"]["features"]["llm_event_type"] is None
    assert "NVDA" in out["meta"]["memo_rejections"]
    assert any("999" in e for e in out["meta"]["memo_rejections"]["NVDA"])
    assert any(w.startswith("MEMO: rejected NVDA") for w in out["meta"]["data_warnings"])
    assert not (run_dir / "archive" / "calibration.jsonl").exists()


def test_a_memo_without_its_payload_is_rejected_not_fatal(scanner, run_dir):
    _stage(run_dir, payload=False)
    out = scanner.scan(_scan_data(), run_dir=str(run_dir))
    assert any("no news_payload.json" in e for e in out["meta"]["memo_rejections"]["NVDA"])


def test_an_unreadable_memo_never_crashes_the_scan(scanner, run_dir):
    (run_dir / "memos").mkdir()
    (run_dir / "memos" / "NVDA.json").write_text("{not json", encoding="utf-8")
    out = scanner.scan(_scan_data(), run_dir=str(run_dir))
    assert any("unreadable" in e for e in out["meta"]["memo_rejections"]["NVDA"])
    assert out["results"]


def test_the_same_call_is_not_logged_twice(scanner, run_dir):
    import memo
    _stage(run_dir)
    scanner.scan(_scan_data(), run_dir=str(run_dir))
    scanner.scan(_scan_data(), run_dir=str(run_dir))
    assert len(memo.read_calibration(str(run_dir / "archive" / "calibration.jsonl"))) == 1


# ------------------------------------------------------------------ resolve-memos
def _bars(sym, closes, start_day=8):
    return {"symbol": sym, "bars": [
        {"begins_at": f"2026-09-{start_day + i:02d}T00:00:00Z", "close_price": str(c)}
        for i, c in enumerate(closes)]}


def test_resolve_memos_fills_outcomes_from_synthetic_bars(run_dir, tmp_path):
    import history
    import memo
    arch = run_dir / "archive"
    arch.mkdir()
    memo.log_calibration(str(arch / "calibration.jsonl"), _memo(p_up_5d=0.7), "NVDA", "Midday", "r1")
    memo.log_calibration(str(arch / "calibration.jsonl"), _memo(p_up_5d=0.2), "MU", "Midday", "r1")
    memo.log_calibration(str(arch / "calibration.jsonl"), _memo(p_up_5d=0.5), "XOM", "Midday", "r1")
    # as_of 2026-09-10; NVDA base close 100 (Sep 10) -> 5 sessions later 110 (up);
    # MU 100 -> 95 (down); XOM has only three bars after as_of, so it stays pending.
    payload = {"data": {"results": [
        _bars("NVDA", [98, 99, 100, 101, 103, 105, 107, 110, 111]),
        _bars("MU", [98, 99, 100, 99, 98, 97, 96, 95, 94]),
        _bars("XOM", [98, 99, 100, 101, 102, 103]),
    ]}}
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps(payload), encoding="utf-8")
    rep, done, pending = history.resolve_memos(str(arch), str(bars), today="2026-09-20")
    assert (done, pending) == (2, 1)
    rows = {r["symbol"]: r for r in memo.read_calibration(str(arch / "calibration.jsonl"))}
    assert rows["NVDA"]["realised_up_5d"] == 1 and rows["NVDA"]["fwd_ret_5d"] == pytest.approx(0.10)
    assert rows["NVDA"]["brier"] == pytest.approx((0.7 - 1) ** 2)
    assert rows["MU"]["realised_up_5d"] == 0 and rows["MU"]["brier"] == pytest.approx(0.04)
    assert rows["XOM"]["realised_up_5d"] is None and rows["XOM"]["brier"] is None
    assert rows["NVDA"]["resolved_on"] == "2026-09-20"
    assert rep["n"] == 2 and rep["pending"] == 1 and rep["advisory"] is True
    # idempotent: a second pass resolves nothing new and rewrites nothing
    _, done2, pending2 = history.resolve_memos(str(arch), str(bars), today="2026-09-21")
    assert (done2, pending2) == (0, 1)


def test_resolve_memos_cli(run_dir, tmp_path, capsys):
    import history
    import memo
    arch = run_dir / "archive"
    arch.mkdir()
    memo.log_calibration(str(arch / "calibration.jsonl"), _memo(p_up_5d=0.7), "NVDA")
    bars = tmp_path / "bars.json"
    bars.write_text(json.dumps([_bars("NVDA", [100, 100, 100, 101, 102, 103, 104, 105])]),
                    encoding="utf-8")
    history.main(["--resolve-memos", "--archive", str(arch), "--bars", str(bars),
                  "--today", "2026-09-20"])
    out = capsys.readouterr().out
    assert "resolved 1 memo call(s) now" in out
    assert "ADVISORY: True" in out


def test_the_module_is_stdlib_only():
    import ast
    import sys
    src = (pathlib.Path(__file__).resolve().parents[1] / "engine" / "memo.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in sys.stdlib_module_names
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] in sys.stdlib_module_names
