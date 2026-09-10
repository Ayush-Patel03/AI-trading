"""P-04 — the attention-fade features in engine/sentiment.py.

Two promises pinned in opposite directions:
  * every key scanner.py reads from the retail block is still emitted with the same
    meaning, so the golden scan_results are byte-identical after a merge (scores, pillars,
    verdicts, reasons) — the ONLY additions on a row are the new retail keys and `features`;
  * the new features are what the docstring says: a z-score against a rolling 20-scan
    history the module maintains, a spike flag, a percentile rank, a StockTwits bull share
    from a staged symbol stream, and nulls — never zeros — when the inputs are missing.
"""
import copy
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys

import pytest

import sentiment as sm
from conftest import ENGINE, FIX

NOW = dt.datetime(2026, 9, 10, 14, 30, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------ fixtures
def _apewisdom(rows):
    return {"results": [{"ticker": t, "mentions": m, "mentions_24h_ago": m0, "upvotes": u,
                         "rank": i + 1, "rank_24h_ago": i + 2}
                        for i, (t, m, m0, u) in enumerate(rows)]}


TODAY_AW = _apewisdom([("NVDA", 412, 300, 900), ("TSLA", 200, 210, 400), ("MU", 88, 60, 120),
                       ("HOOD", 40, 41, 50), ("AND", 30, 30, 10)])


def _history(counts_by_symbol, start=dt.datetime(2026, 8, 1, 14, 30, tzinfo=dt.timezone.utc)):
    """A synthetic 20-scan history: one entry per business-day scan per symbol."""
    syms = {}
    for sym, counts in counts_by_symbol.items():
        rows, d = [], start
        for c in counts:
            rows.append({"ts": d.isoformat(timespec="seconds"), "mentions": c, "upvotes": c * 2})
            d += dt.timedelta(days=1)
        syms[sym] = rows
    return {"_meta": {"window": 20, "updated": None}, "symbols": syms}


def _st_stream(sym, tags, newest=NOW, step_min=7):
    msgs, t = [], newest
    for i, tag in enumerate(tags):
        m = {"id": 1000 + i, "created_at": t.isoformat().replace("+00:00", "Z"),
             "body": f"${sym} msg {i}",
             "entities": {"sentiment": {"basic": tag} if tag else None}}
        msgs.append(m)
        t -= dt.timedelta(minutes=step_min)
    return {"symbol": {"id": 1, "symbol": sym}, "messages": msgs}


def _trends(sym, values, end="2026-09-10"):
    d = dt.date.fromisoformat(end) - dt.timedelta(days=len(values) - 1)
    return {sym: [{"date": (d + dt.timedelta(days=i)).isoformat(), "value": v}
                  for i, v in enumerate(values)]}


# ------------------------------------------------------------------ the z-score
def test_zscore_by_hand():
    prior = [10.0, 12.0, 8.0, 11.0, 9.0]          # mean 10, sample std sqrt(2.5)
    z, mean = sm.zscore(15.0, prior)
    assert mean == 10.0 and z == pytest.approx(5.0 / (2.5 ** 0.5), abs=1e-3)
    assert sm.zscore(15.0, prior[:4]) == (None, 10.25), "under min_history the z is null"
    assert sm.zscore(15.0, [7.0] * 6) == (None, 7.0), "a flat history has no z"


def test_attention_z_from_a_synthetic_20_scan_history():
    hist = _history({"NVDA": [100 + (i % 3) * 10 for i in range(20)],    # mean 110, spread 0/10/20
                     "TSLA": [200] * 19 + [201],
                     "MU": [90, 85, 95, 88, 92]})
    aw = sm.parse_apewisdom(TODAY_AW, [])
    feats, nxt = sm.attention_features(aw, hist, now=NOW)
    n = feats["NVDA"]
    prior = [100 + (i % 3) * 10 for i in range(20)]
    mean = sum(prior) / 20
    sd = (sum((x - mean) ** 2 for x in prior) / 19) ** 0.5
    assert n["attention_count"] == 412 and n["attention_upvotes"] == 900
    assert n["attention_history_n"] == 20 and n["attention_mean_20"] == round(mean, 2)
    assert n["attention_z"] == pytest.approx((412 - mean) / sd, abs=1e-3)
    assert n["attention_spike"] is True
    # MU: five prior scans, mean 90 — 88 today is inside a std, no spike
    m = feats["MU"]
    assert m["attention_history_n"] == 5 and m["attention_spike"] is False
    assert -1.0 < m["attention_z"] < 0.0
    # HOOD has no history at all: measured today, z null, spike null — not False
    h = feats["HOOD"]
    assert h["attention_count"] == 40 and h["attention_z"] is None and h["attention_spike"] is None
    assert h["attention_history_n"] == 0


def test_spike_flag_is_strictly_z_over_two():
    hist = _history({"A": [10, 12, 8, 11, 9] * 4})
    # mean 10, sample std sqrt(40/19) = 1.451: 12.5 is z 1.72, 13 is z 2.07
    aw = {"A": {"mentions": 12.5, "upvotes": 1}}
    z = sm.attention_features(aw, hist, now=NOW)[0]["A"]["attention_z"]
    assert 1.5 < z < 2.0
    assert sm.attention_features(aw, hist, now=NOW)[0]["A"]["attention_spike"] is False
    aw = {"A": {"mentions": 13, "upvotes": 1}}
    assert sm.attention_features(aw, hist, now=NOW)[0]["A"]["attention_z"] > 2
    assert sm.attention_features(aw, hist, now=NOW)[0]["A"]["attention_spike"] is True


def test_rank_pct_is_the_percentile_across_todays_feed():
    aw = sm.parse_apewisdom(TODAY_AW, [])
    feats, _ = sm.attention_features(aw, None, now=NOW)
    assert feats["NVDA"]["attention_rank_pct"] == 100.0       # 4 of 4 others below
    assert feats["TSLA"]["attention_rank_pct"] == 75.0
    assert feats["MU"]["attention_rank_pct"] == 50.0
    assert feats["AND"]["attention_rank_pct"] == 0.0
    one, _ = sm.attention_features({"X": {"mentions": 3}}, None, now=NOW)
    assert one["X"]["attention_rank_pct"] == 100.0


def test_history_rolls_records_a_faded_name_at_zero_and_replaces_a_rerun():
    hist = _history({"NVDA": [100] * 20, "GONE": [50] * 3})
    aw = sm.parse_apewisdom(TODAY_AW, [])
    _, nxt = sm.attention_features(aw, hist, now=NOW)
    assert len(nxt["symbols"]["NVDA"]) == 20, "the window is 20 scans, oldest dropped"
    assert nxt["symbols"]["NVDA"][-1] == {"ts": NOW.isoformat(timespec="seconds"),
                                          "mentions": 412, "upvotes": 900}
    assert nxt["symbols"]["GONE"][-1]["mentions"] == 0, "off page 1 today is recorded as 0"
    assert nxt["symbols"]["TSLA"] == [{"ts": NOW.isoformat(timespec="seconds"),
                                       "mentions": 200, "upvotes": 400}]
    assert nxt["_meta"]["updated"] == NOW.isoformat(timespec="seconds")
    # a re-run inside the same hour replaces its own row rather than doubling it
    _, again = sm.attention_features(aw, nxt, now=NOW + dt.timedelta(minutes=20))
    assert len(again["symbols"]["NVDA"]) == 20
    assert sum(1 for r in again["symbols"]["NVDA"] if r["mentions"] == 412) == 1
    # a symbol whose stored rows are all zero is dropped, so the file cannot grow forever
    dead = _history({"DEAD": [0] * 20})
    _, nxt = sm.attention_features(aw, dead, now=NOW)
    assert "DEAD" not in nxt["symbols"]


def test_no_apewisdom_means_no_attention_rows_and_an_untouched_history():
    hist = _history({"NVDA": [100] * 5})
    feats, nxt = sm.attention_features({}, hist, now=NOW)
    assert feats == {} and nxt["symbols"] == hist["symbols"]


# ------------------------------------------------------------------ StockTwits bull share
def test_st_bull_pct_from_a_fixture_message_list():
    warnings = []
    payload = _st_stream("NVDA", ["Bullish", "Bullish", "Bearish", None, "Bullish", None])
    row = sm.parse_stocktwits_symbol_stream(payload, warnings, now=NOW)
    assert row["st_bull_pct"] == 75.0 and row["st_bull_n"] == 3 and row["st_bear_n"] == 1
    assert row["st_msgs"] == 6 and row["st_stream_age_min"] == 0.0
    assert row["st_stream_stale"] is False and warnings == []
    assert row["st_newest_utc"] == NOW.isoformat(timespec="seconds")


def test_a_stale_symbol_stream_is_reported_with_its_age_and_warned():
    warnings = []
    old = NOW - dt.timedelta(hours=50)
    row = sm.parse_stocktwits_symbol_stream(_st_stream("NVDA", ["Bullish", "Bearish"], newest=old),
                                            warnings, now=NOW)
    assert row["st_bull_pct"] == 50.0 and row["st_stream_stale"] is True
    assert row["st_stream_age_min"] == 50 * 60
    assert warnings and "50h old" in warnings[0]


def test_untagged_messages_give_a_null_share_not_a_fifty():
    row = sm.parse_stocktwits_symbol_stream(_st_stream("X", [None, None]), [], now=NOW)
    assert row["st_bull_pct"] is None and row["st_msgs"] == 2
    assert sm.parse_stocktwits_symbol_stream({"messages": "nope"}, []) is None
    assert sm.parse_stocktwits_symbol_stream(None, []) is None


def test_the_symbol_stream_is_still_refused_as_a_trending_input():
    warnings = []
    assert sm.parse_stocktwits_trending(_st_stream("NVDA", ["Bullish"]), warnings) == {}
    assert warnings and "REFUSED" in warnings[0]


def test_symbol_streams_are_loaded_by_file_name(tmp_path, monkeypatch):
    (tmp_path / "st_symbol_NVDA.json").write_text(json.dumps(_st_stream("NVDA", ["Bullish"])),
                                                  encoding="utf-8")
    (tmp_path / "st_symbol_mu.json").write_text(json.dumps(_st_stream("MU", ["Bearish"])),
                                                encoding="utf-8")
    (tmp_path / "st_trending.json").write_text("{}", encoding="utf-8")
    got = sm.load_symbol_streams(base=str(tmp_path))
    assert set(got) == {"NVDA", "MU"}


# ------------------------------------------------------------------ Google Trends
def test_trends_z_and_nulls():
    tr = sm.parse_trends(_trends("NVDA", [50, 52, 48, 51, 49, 50, 90]), [])
    n = tr["NVDA"]
    assert n["trends_latest"] == 90 and n["trends_date"] == "2026-09-10" and n["trends_n"] == 6
    assert n["trends_z"] > 2
    short = sm.parse_trends(_trends("MU", [1, 2, 3]), [])["MU"]
    assert short["trends_z"] is None and short["trends_n"] == 2
    assert sm.parse_trends({"X": "bad"}, []) == {} and sm.parse_trends(None, []) == {}


# ------------------------------------------------------------------ build()
def test_build_carries_every_feature_and_nulls_when_files_are_missing():
    hist = _history({"NVDA": [100 + (i % 3) * 10 for i in range(20)]})
    res = sm.build(apewisdom=TODAY_AW, now=NOW, attention_history=hist,
                   st_symbol_streams={"NVDA": _st_stream("NVDA", ["Bullish", "Bearish", "Bullish"])},
                   trends=_trends("NVDA", [50, 52, 48, 51, 49, 50, 90]))
    n = res["tickers"]["NVDA"]
    assert n["attention_z"] > 2 and n["attention_spike"] is True and n["attention_rank_pct"] == 100.0
    assert n["st_bull_pct"] == pytest.approx(66.7) and n["trends_z"] > 2
    assert res["_meta"]["attention"]["spikes"] == ["NVDA"]
    assert any(w.startswith("ATTENTION SPIKE") for w in res["_meta"]["warnings"])
    assert res["_meta"]["sources_present"]["stocktwits_symbol_streams"] is True
    assert res["_meta"]["sources_present"]["trends"] is True
    assert "reddit_raw" not in res["_meta"]["sources_present"]
    assert res["_attention_history"]["symbols"]["NVDA"][-1]["mentions"] == 412
    # a name with no history, no stream and no trends: the attention keys are present
    # and null (unmeasured), the stream / trends keys are absent (no source at all)
    m = res["tickers"]["MU"]
    assert m["attention_count"] == 88 and m["attention_z"] is None and m["attention_spike"] is None
    assert "st_bull_pct" not in m and "trends_z" not in m
    # the single-source gate is unchanged from before P-04: only AMBIGUOUS_BUT_REAL names
    # need a second feed. "AND" is on the wider (text-path) stop-list and is still emitted
    # on one feed, exactly as it was; the scanner keys are untouched
    assert "AND" in res["tickers"] and res["tickers"]["AND"]["wsb_mentions"] == 30
    one_feed = sm.build(apewisdom=_apewisdom([("IT", 5, 4, 1)]), now=NOW)
    assert "IT" not in one_feed["tickers"]
    assert n["wsb_mentions"] == 412 and n["wsb_mentions_24h_ago"] == 300 and n["wsb_rank"] == 1


def test_build_without_any_source_has_no_attention_and_no_history_change():
    res = sm.build(now=NOW)
    assert res["tickers"] == {} and res["_meta"]["attention"]["symbols_with_z"] == 0
    assert res["_attention_history"]["symbols"] == {}


# ------------------------------------------------------------------ merge + the golden
@pytest.fixture
def scanner(run_dir):
    import importlib
    import scanner as s
    importlib.reload(s)
    return s


def _scan_data():
    return json.loads((FIX / "scan_data.json").read_text(encoding="utf-8"))


def _sentiment_for_fixture():
    """A sentiment map that reproduces the fixture's retail numbers exactly (so the
    scanner sees the same inputs) plus the attention features on top."""
    aw = _apewisdom([("NVDA", 412, None, 900), ("MU", 88, None, 120)])
    for r in aw["results"]:
        r["rank"] = {"NVDA": 1, "MU": 9}[r["ticker"]]
        r["rank_24h_ago"] = 0
    gauges = {"NVDA": {"score": 74, "label": "Bullish"}, "MU": {"score": 61, "label": "Bullish"}}
    hist = _history({"NVDA": [100 + (i % 3) * 10 for i in range(20)], "MU": [90, 85, 95, 88, 92]})
    return sm.build(apewisdom=aw, st_gauges=gauges, now=NOW, attention_history=hist,
                    st_symbol_streams={"NVDA": _st_stream("NVDA", ["Bullish", "Bearish", "Bullish"])},
                    trends=_trends("NVDA", [50, 52, 48, 51, 49, 50, 90]))


def test_merge_writes_retail_and_features_and_the_scan_golden_is_unchanged(scanner):
    data = _scan_data()
    before = copy.deepcopy(data)
    merged, rep = sm.merge_into_scan_data(data, _sentiment_for_fixture())
    assert set(rep["merged"]) == {"NVDA", "MU"}
    c = merged["candidates"]["NVDA"]
    # the scanner's keys carry the fixture values still
    for k in ("wsb_mentions", "wsb_rank", "stocktwits_score", "stocktwits_label"):
        assert c["retail"][k] == before["candidates"]["NVDA"]["retail"][k]
    assert "wsb_mentions_24h_ago" not in c["retail"], "a null is not written as a key"
    # the attention features are on the retail block AND the features dict
    assert c["retail"]["attention_spike"] is True and c["retail"]["st_bull_pct"] == pytest.approx(66.7)
    assert c["features"]["attention_spike"] == 1 and c["features"]["attention_z"] > 2
    assert c["features"]["attention_rank_pct"] == 100.0 and c["features"]["attention_count"] == 412
    assert c["features"]["trends_z"] > 2
    mu = merged["candidates"]["MU"]
    assert mu["features"]["st_bull_pct"] is None and mu["features"]["trends_z"] is None
    assert mu["features"]["attention_z"] is not None
    # XOM had no chatter: an explicit empty marker, no features written
    assert merged["candidates"]["XOM"]["retail"] == {"sentiment_checked": True, "sources_count": 0}
    assert "features" not in merged["candidates"]["XOM"]

    out = scanner.scan(merged)
    want = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    got = {r["ticker"]: r for r in out["results"]}
    assert [r["ticker"] for r in out["results"]] == [r["ticker"] for r in want["results"]]
    for w in want["results"]:
        g = got[w["ticker"]]
        for k in ("score", "raw_score", "normalized_score", "pillars", "verdict", "setup",
                  "coverage_pct", "missing_pillars", "reasons", "verdict_note"):
            assert g[k] == w[k], (w["ticker"], k)
        extra = set(g) - set(w)
        assert extra <= {"features"}, extra
        assert set(g["retail"]) - set(w["retail"]) <= (set(sm.REPORTED_KEYS) | set(sm.SCANNER_RETAIL_KEYS)
                                                       | {"sentiment_checked"})
    # the logged features ride into the row
    assert got["NVDA"]["features"]["attention_z"] > 2


def test_merge_keeps_technicals_features_alongside(scanner):
    data = _scan_data()
    data["candidates"]["NVDA"]["features"] = {"ret_12_1": 0.42, "beta_252": 1.7}
    merged, _ = sm.merge_into_scan_data(data, _sentiment_for_fixture())
    f = merged["candidates"]["NVDA"]["features"]
    assert f["ret_12_1"] == 0.42 and f["beta_252"] == 1.7 and f["attention_z"] > 2


def test_reported_keys_carry_no_reddit_field():
    assert not any(k.startswith("reddit") for k in sm.REPORTED_KEYS)
    assert not hasattr(sm, "parse_reddit_posts")
    src = (ENGINE / "sentiment.py").read_text(encoding="utf-8")
    assert "arctic" not in src.lower().replace("arctic shift route was", "")


# ------------------------------------------------------------------ the CLI on fixtures
def _cli(run_dir, *args):
    env = dict(os.environ)
    env["SCAN_DIR"] = str(run_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(ENGINE / "sentiment.py"), *args], cwd=run_dir,
                          capture_output=True, text=True, encoding="utf-8", env=env)


def test_cli_runs_on_the_fixtures_and_maintains_the_history(run_dir):
    (run_dir / "apewisdom.json").write_text(json.dumps(TODAY_AW), encoding="utf-8")
    (run_dir / "st_symbol_NVDA.json").write_text(
        json.dumps(_st_stream("NVDA", ["Bullish", "Bullish", "Bearish"])), encoding="utf-8")
    (run_dir / "trends.json").write_text(json.dumps(_trends("NVDA", [50, 52, 48, 51, 49, 50, 90])),
                                         encoding="utf-8")
    sd = _scan_data()
    (run_dir / "scan_data.json").write_text(json.dumps(sd), encoding="utf-8")
    hist_path = run_dir / "archive" / "attention_history.json"
    args = ["--apewisdom", "apewisdom.json", "--stocktwits-symbols", "--trends", "trends.json",
            "--merge-into", "scan_data.json", "--now", NOW.isoformat()]
    r = _cli(run_dir, *args)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "attention: 1 scan(s) of history, 0 name(s) with a z-score" in r.stdout
    assert hist_path.exists(), "the first scan seeds the rolling history"
    h = json.loads(hist_path.read_text(encoding="utf-8"))
    assert set(h["symbols"]) == {"NVDA", "TSLA", "MU", "HOOD", "AND"}
    assert h["symbols"]["NVDA"] == [{"ts": NOW.isoformat(timespec="seconds"),
                                     "mentions": 412, "upvotes": 900}]
    res = json.loads((run_dir / "sentiment.json").read_text(encoding="utf-8"))
    assert "_attention_history" not in res
    assert res["tickers"]["NVDA"]["st_bull_pct"] == pytest.approx(66.7)
    assert res["tickers"]["NVDA"]["attention_z"] is None
    merged = json.loads((run_dir / "scan_data.json").read_text(encoding="utf-8"))
    assert merged["candidates"]["NVDA"]["features"]["st_bull_pct"] == pytest.approx(66.7)
    assert merged["candidates"]["NVDA"]["features"]["attention_z"] is None
    assert merged["meta"]["sentiment_meta"]["attention"]["scans_in_history"] == 1

    # six more scans an hour apart: the z becomes measurable on the seventh
    for i in range(1, 7):
        t = NOW + dt.timedelta(hours=i)
        r = _cli(run_dir, "--apewisdom", "apewisdom.json", "--now", t.isoformat())
        assert r.returncode == 0, r.stdout + r.stderr
    h = json.loads(hist_path.read_text(encoding="utf-8"))
    assert len(h["symbols"]["NVDA"]) == 7
    res = json.loads((run_dir / "sentiment.json").read_text(encoding="utf-8"))
    assert res["tickers"]["NVDA"]["attention_history_n"] == 6
    assert res["tickers"]["NVDA"]["attention_z"] is None, "six identical scans: flat, no z"


def test_cli_with_no_sources_exits_2_and_writes_no_history(run_dir):
    r = _cli(run_dir, "--apewisdom", "missing.json")
    assert r.returncode == 2
    assert "NO SOURCES" in r.stdout
    assert not (run_dir / "archive" / "attention_history.json").exists()


def test_cli_history_can_be_disabled(run_dir):
    (run_dir / "apewisdom.json").write_text(json.dumps(TODAY_AW), encoding="utf-8")
    r = _cli(run_dir, "--apewisdom", "apewisdom.json", "--attention-history", "")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "history disabled" in r.stdout
    assert not (run_dir / "archive" / "attention_history.json").exists()


def test_runner_round_trips_the_attention_history(tmp_path):
    sys.path.insert(0, str(pathlib.Path(ENGINE).parent / "runner"))
    import run as runner
    import selftest
    state = selftest.make_state_repo(tmp_path / "state")
    (state / "archive").mkdir()
    seed = {"_meta": {"window": 20}, "symbols": {"NVDA": [{"ts": "x", "mentions": 1, "upvotes": 1}]}}
    runner.write_json(state / "archive" / "attention_history.json", seed)
    now = selftest.synthetic_now("12:30")
    inputs = selftest.write_inputs(tmp_path / "inputs",
                                   {"scan_data.json": selftest.fresh_scan_data(now)}, as_of=now)
    run_dir = tmp_path / "run"
    staged = runner.stage_run(pathlib.Path(ENGINE), state, inputs, run_dir, [], None)
    assert "archive/attention_history.json" in staged["state"]
    assert runner.load_json(run_dir / "archive" / "attention_history.json") == seed
    runner.write_json(run_dir / "archive" / "attention_history.json", {"symbols": {}})
    assert runner.write_back_archive(state, run_dir) == ["archive/attention_history.json"]
    assert runner.load_json(state / "archive" / "attention_history.json") == {"symbols": {}}
    assert ("reddit_posts.json", "--reddit") not in runner.SENTIMENT_ARGS
    assert ("trends.json", "--trends") in runner.SENTIMENT_ARGS


def test_sentiment_module_is_stdlib_only():
    import ast
    src = (ENGINE / "sentiment.py").read_text(encoding="utf-8")
    names = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.add((node.module or "").split(".")[0])
    assert names <= set(sys.stdlib_module_names), names
