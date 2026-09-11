"""report.py — the refusals taxonomy, the E5 gate counterfactual, attribution, benchmark.

The taxonomy test does not trust a hand-written list of refusal strings: it harvests every
reason literal pm.py (and the modules pm.py quotes into `skipped`) can emit, by AST, and
fails the moment a new refusal is added without a rule for it.
"""
import ast
import datetime as dt
import json
import pathlib

import pytest

from conftest import ENGINE, FIX, ROOT

JOURNALS = FIX / "journals"
BOOKS = JOURNALS / "books"


@pytest.fixture
def rp(run_dir):
    import importlib
    import report as m
    importlib.reload(m)
    return m


# ------------------------------------------------------------------ harvesting pm.py
def _render(node):
    """A string literal, an f-string with every placeholder rendered as '1', a
    concatenation of those, or the first renderable branch of `a or "b"`. None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(v) if isinstance(v, ast.Constant) else "1" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        l, r = _render(node.left), _render(node.right)
        if l is None and r is None:
            return None
        return (l or "") + (r or "")
    if isinstance(node, ast.BoolOp):
        for v in node.values:
            s = _render(v)
            if s is not None:
                return s
    return None


def _harvest(path):
    """(direct strings, indirect expressions) that reach a journal `skipped` reason."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    direct, indirect = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            f = node.func
            # pm.py: jrn["skipped"].append({"symbol": ..., "reason": <expr>})
            if f.attr == "append" and isinstance(f.value, ast.Subscript) and \
                    isinstance(f.value.slice, ast.Constant) and f.value.slice.value == "skipped":
                d = node.args[0]
                if isinstance(d, ast.Dict):
                    for k, v in zip(d.keys, d.values):
                        if isinstance(k, ast.Constant) and k.value == "reason":
                            s = _render(v)
                            (direct if s is not None else indirect).append(
                                s if s is not None else ast.unparse(v))
            # portfolio.py: hard.append(...) / blocks.append(...) -> p["warnings"], blocks
            if f.attr == "append" and isinstance(f.value, ast.Name) and \
                    f.value.id in ("hard", "blocks"):
                s = _render(node.args[0])
                if s is not None:
                    direct.append(s)
            # broker_policy.py: self._note(...) -> pnote / the sellable note
            if f.attr == "_note" and node.args:
                s = _render(node.args[0])
                if s is not None:
                    direct.append(s)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                # ladder.py st["reason"], pm.py day["halt_reason"]
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) and \
                        t.slice.value in ("reason", "halt_reason"):
                    s = _render(node.value)
                    if s is not None:
                        direct.append(s)
        if isinstance(node, ast.FunctionDef) and node.name == "house_block":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    s = _render(sub.value)
                    if s is not None:
                        direct.append(s)
        if isinstance(node, ast.FunctionDef) and node.name == "ladder_pass":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "reason" for t in sub.targets):
                    s = _render(sub.value)
                    if s is not None:
                        direct.append(s)
    return direct, indirect


def test_every_refusal_string_pm_can_emit_today_has_a_rule(rp):
    direct, indirect = _harvest(ENGINE / "pm.py")
    assert len(direct) >= 29, "the harvest found fewer skipped reasons than pm.py carries"
    # every indirect reason is a string built in one of the modules harvested below
    assert set(indirect) <= {"book['day']['halt_reason']", "ladder['reason']", "pnote", "b",
                             "'; '.join(p['warnings'])", "hb"}, indirect
    for name in ("portfolio.py", "broker_policy.py", "ladder.py"):
        more, _ = _harvest(ENGINE / name)
        assert more, f"nothing harvested from {name}"
        direct += more
    unclassified = [s for s in direct if rp.classify(s)["rule"] == "other"]
    assert not unclassified, "add a rule to report.py for: " + "; ".join(unclassified)


def test_classify_maps_the_strings_the_live_journal_carries(rp):
    cases = {
        "Sector limit: already 3 positions in Information Technology (max 3)": "sector_cap",
        "House cap: NVDA would be $2,400 across 3 desks, 16.0% of the $15,000 combined book, "
        "over the 15% single-name house limit (already $2,000 held elsewhere)": "house_symbol_cap",
        "House cap: Information Technology would be $6,100 across 3 desks, 40.7% of the "
        "$15,000 combined book, over the 40% sector house limit": "house_sector_cap",
        "bid/ask spread 1.19% of price, over the 1.0% entry limit — too expensive to trade "
        "at this size": "spread",
        "price drifted +12.0% since the scan — rescored at the next scan before it can be "
        "entered": "price_drift",
        "scan is 331 minutes old — over the 240-minute freshness limit — entries frozen, "
        "holdings still managed": "scan_stale",
        "scan is dated 2026-09-09, not today — entries frozen, holdings still managed": "scan_stale",
        "no scan results available this run": "scan_stale",
        "macro gate: no new entries ahead of CPI at 08:30 ET": "macro_gate",
        "legacy_pdt: PDT guard: 2/3 day trades used — no new entries, one is held back as an "
        "exit hatch": "broker_policy",
        "trim wanted to fire — PDT guard: selling would be day trade 1/3 and this is not a "
        "stop — held": "broker_policy",
        "ladder rung 2: drawdown 6.10% from the high-water mark ($5,200.00) is past the 6% "
        "level — no new entries; exits, trims and rebalancing still run": "ladder",
        "soft daily level: session P&L -2.10% is through −2.0% — no new entries for the rest "
        "of the session; exits stay live (the 3% kill switch is separate and untouched)": "ladder",
        "Daily loss -3.10% breached the 3% kill switch": "kill_switch",
        "HALT: daily loss -3.10% breached the 3% kill-switch limit — no new entries": "kill_switch",
        "HALT: at max positions (8/8) — no new entries": "max_entries",
        "run cap: 3 new entries per slot, queued for the next one": "max_entries",
        "house exposure (enforced): N_eff 2.1 under 4 — no new entries house-wide until the "
        "combined book is less crowded; exits unaffected": "house_exposure",
        "Order would be $4.10 — below the broker's $5.00 minimum": "min_notional",
        "Only 60% of the evidence base was available (catalyst) — under the 70% floor. The "
        "score is normalised to what was there": "coverage",
        "no fresh price — last price is scan and cannot be trusted to fire a stop": "coverage",
        "power-hour places no new entries — a day-limit entry placed at the last slot expires "
        "unfilled at the session roll before it can ever fill.": "slot",
        "desk 'pullback': 13 scan row(s) outside this desk's mandate (setups=[...])": "desk_mandate",
        "already has a working order": "working_order",
        "Score 47 — weakening, but it was already trimmed today — one trim per name per "
        "session": "once_per_session",
        "15.4% of equity, over the 15% cap but inside the 1.0pt rebalance deadband — a price "
        "wobble is not a breach": "deadband",
        "earnings in 2 sessions — no entry into the print": "earnings_gate",
        "chandelier: stop 101.2 is not below the 100.00 entry — cannot size a position": "stop_policy",
        # portfolio.py joins a blocked proposal's warnings: the FIRST hard reason decides
        "Only 40% of the evidence base was available (catalyst, fundamentals, intelligence) — "
        "under the 70% floor. The score is normalised to what was there, so a thin row can "
        "outrank a complete one; it is not sized on that basis; Sector limit: already 3 "
        "positions in None (max 3); Trimmed to the 15% max-position cap": "coverage",
        "Sector limit: already 3 positions in Information Technology (max 3); Trimmed to "
        "available cash ($412.10)": "sector_cap",
        "chandelier: $3.20 at a 9.1% stop distance is under the $5.00 broker minimum": "min_notional",
        "something nobody has seen before": "other",
        None: "other",
    }
    for text, rule in cases.items():
        got = rp.classify(text)
        assert got["rule"] == rule, (text, got)
        assert got["detail"] == ("" if text is None else text), "the raw text is preserved"
    assert set(rule for _, rule in cases.items()) <= set(rp.RULES)
    assert rp.RULES[-1] == "other"


def test_side_separates_book_wide_management_and_entry_refusals(rp):
    assert rp.side_of("*", "macro gate: no new entries ahead of CPI", "macro_gate") == "book"
    assert rp.side_of("MU", "trim wanted to fire — PDT guard: held", "broker_policy") == "manage"
    assert rp.side_of("MU", "Score 47 — weakening, but it was already trimmed today — one trim "
                      "per name per session", "once_per_session") == "manage"
    assert rp.side_of("MU", "no fresh price — no price at all this slot", "coverage") == "manage"
    assert rp.side_of("CRDO", "Sector limit: already 3 positions in IT (max 3)", "sector_cap") == "entry"


# ------------------------------------------------------------------ refusals on the fixtures
def test_refusals_counts_by_week_and_desk_on_the_three_desk_fixture(rp):
    entries = rp.load_journals([str(JOURNALS)])
    assert {e["desk"] for e in entries} == {"swing", "pullback", "momentum"}
    assert len(entries) == 90                       # 3 desks × 6 days × (4 slots + 1 sentinel)
    R = rp.refusals(entries)
    # opening-range on every desk-day: 2 sector refusals, 1 spread, 1 desk mandate
    assert R["by_rule"]["sector_cap"] == 3 * 6 * 2
    assert R["by_rule"]["spread"] == 3 * 6
    assert R["by_rule"]["desk_mandate"] == 3 * 6
    assert R["by_rule"]["slot"] == 3 * 6              # power-hour, every desk, every day
    assert R["by_rule"]["house_symbol_cap"] == 1 and R["by_rule"]["price_drift"] == 1
    assert R["by_rule"]["other"] == 1
    assert R["other"] == ["a brand new reason nobody has classified"]
    assert set(R["by_week"]) == {"2026-W36", "2026-W37"}
    assert R["by_week"]["2026-W36"]["sector_cap"] == 3 * 4 * 2   # four sessions in W36
    assert R["by_week"]["2026-W37"]["sector_cap"] == 3 * 2 * 2
    assert R["by_desk"]["swing"]["house_symbol_cap"] == 1
    assert "house_symbol_cap" not in R["by_desk"]["momentum"]
    assert R["by_desk"]["momentum"]["price_drift"] == 1
    assert R["by_side"] == {"book": 3 * 6 * 2 + 9 + 3, "manage": 9,
                            "entry": 3 * 6 * 3 + 1 + 1 + 1}
    assert len(R["list"]) == R["n"]
    first = R["list"][0]
    assert set(first) == {"date", "desk", "slot", "symbol", "rule", "side"}
    # --since drops the earlier sessions
    later = rp.load_journals([str(JOURNALS)], since="2026-09-08")
    assert {e["date"] for e in later} == {"2026-09-08", "2026-09-09"}


def test_journal_files_can_be_given_individually_and_deduplicate_by_run_key(rp):
    one = rp.load_journals([str(JOURNALS / "pm-journal-pullback.json")])
    twice = rp.load_journals([str(JOURNALS / "pm-journal-pullback.json"),
                              str(JOURNALS / "pm-journal-pullback.json")])
    assert len(one) == 30 and len(twice) == 30
    assert all(e["desk"] == "pullback" for e in one)


def test_desk_is_read_off_the_book_file_name(rp):
    assert rp.desk_of_book_path("/x/paper-book.json") == "swing"
    assert rp.desk_of_book_path("/x/paper_book.json") == "swing"
    assert rp.desk_of_book_path("/x/paper-book-pullback.json") == "pullback"
    assert rp.desk_of_book_path("/x/paper_book_momentum.json") == "momentum"
    assert rp.desk_of_book_path("/x/book_swing.json") == "swing"
    books = rp.load_books(str(BOOKS))
    assert set(books) == {"swing", "pullback", "momentum"}


# ------------------------------------------------------------------ E5 counterfactual
def _sessions(n, start=dt.date(2026, 8, 25)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _bars(tmp_path, drift, n=45, with_open=True, spy=0.002):
    """A bars file where each symbol compounds at its own daily drift. Opens sit 0.3%
    above the previous close so 'next open' is distinguishable from 'close'."""
    days = _sessions(n)
    results = []
    for sym, g in list(drift.items()) + [("SPY", spy)]:
        bars, px = [], 100.0
        for i, d in enumerate(days):
            close = px * (1 + g) ** i
            prev = px * (1 + g) ** (i - 1) if i else close
            b = {"begins_at": f"{d}T00:00:00Z", "close_price": f"{close:.6f}",
                 "interpolated": False}
            if with_open:
                b["open_price"] = f"{prev * 1.003:.6f}"
            bars.append(b)
        results.append({"symbol": sym, "bars": bars})
    p = tmp_path / "bars.json"
    p.write_text(json.dumps({"data": {"results": results}}), encoding="utf-8")
    return p


def _drift():
    d = {f"A{i}": 0.010 for i in range(8)}         # admitted names rise 1% a session
    d.update({f"R{i}": -0.010 for i in range(6)})  # sector-refused names fall 1%
    d.update({f"W{i}": 0.010 for i in range(4)})   # spread-refused names match the admitted
    return d


def test_next_open_entry_prefers_the_open_and_falls_back_to_the_close(rp, tmp_path):
    series = rp.load_bars_oc(str(_bars(tmp_path, {"A0": 0.01})))
    d, px = rp.next_open_entry(series, "A0", "2026-09-01")
    assert d == "2026-09-02"
    closes = dict((x, c) for x, _, c in series["A0"])
    assert px == pytest.approx(closes["2026-09-01"] * 1.003)   # the open, not the close
    series2 = rp.load_bars_oc(str(_bars(tmp_path, {"A0": 0.01}, with_open=False)))
    d2, px2 = rp.next_open_entry(series2, "A0", "2026-09-01")
    assert d2 == "2026-09-02" and px2 == pytest.approx(closes["2026-09-02"])
    assert rp.next_open_entry(series, "A0", "2099-01-01") is None
    assert rp.next_open_entry(series, "ZZZ", "2026-09-01") is None


def test_counterfactual_finds_the_refused_names_underperformed(rp, tmp_path):
    entries = rp.load_journals([str(JOURNALS)])
    series = rp.load_bars_oc(str(_bars(tmp_path, _drift())))
    C = rp.counterfactual(entries, series, horizons=(5, 10))
    assert C["horizons"] == [5, 10]
    sc = C["rules"]["sector_cap"]
    # 6 sessions × up to 6 distinct R names, de-duplicated across desks by (date, symbol)
    assert 12 <= sc["n_refused"] <= 36 and sc["n_refused_with_bars"] == sc["n_refused"]
    for h in ("5", "10"):
        t = sc[h]
        assert t["refused"]["n"] == sc["n_refused"]
        assert t["admitted"]["n"] > 0
        assert t["refused"]["mean_pct"] < 0 < t["admitted"]["mean_pct"]
        assert t["diff"]["mean_pct"] < 0
        assert t["diff"]["n_dates"] == 6 and t["diff"]["block"] == int(h)
        lo, hi = t["diff"]["ci90_pct"]
        assert lo <= t["diff"]["mean_pct"] <= hi and hi < 0
        assert t["verdict"].startswith("refused names underperformed")
        assert t["sample"] == "ok" and rp.NOT_A_SAMPLE not in t["verdict"]
    # one refusal is not a sample, whatever its interval says
    pd = C["rules"]["price_drift"]
    assert pd["n_refused"] == 1
    assert pd["5"]["sample"] == rp.NOT_A_SAMPLE and rp.NOT_A_SAMPLE in pd["5"]["verdict"]
    assert pd["5"]["diff"]["ci90_pct"] is None
    # the spread gate refused names that did exactly as well as the admitted ones
    sp = C["rules"]["spread"]["10"]
    assert abs(sp["diff"]["mean_pct"]) < 1e-6
    assert "no evidence either way" in sp["verdict"]
    # a longer horizon compounds a bigger gap
    assert sc["10"]["diff"]["mean_pct"] < sc["5"]["diff"]["mean_pct"]
    # book-wide and management-side refusals are counted as unmeasurable, not measured
    assert C["unmeasured"]["macro_gate"] == {"book_wide": 9, "manage": 0}
    assert C["unmeasured"]["broker_policy"] == {"book_wide": 0, "manage": 9}
    assert "macro_gate" not in C["rules"] and "broker_policy" not in C["rules"]
    assert C["no_bars"] == ["Q9"]
    assert C["n_admitted"] == C["n_admitted_with_bars"] > 0


def test_counterfactual_with_no_bars_for_the_names_measures_nothing(rp, tmp_path):
    entries = rp.load_journals([str(JOURNALS)])
    series = rp.load_bars_oc(str(_bars(tmp_path, {"ZZZ": 0.0})))
    C = rp.counterfactual(entries, series, horizons=(5,))
    t = C["rules"]["sector_cap"]["5"]
    assert t["refused"]["n"] == 0 and t["admitted"]["n"] == 0
    assert t["diff"]["ci90_pct"] is None
    assert "fewer than two dates" in t["verdict"]


# ------------------------------------------------------------------ attribution & benchmark
def test_attribution_tables_join_the_entry_decision(rp):
    entries = rp.load_journals([str(JOURNALS)])
    A = rp.attribution(entries, rp.load_books(str(BOOKS)))
    assert A["n_closed"] == 24 and A["all"]["n"] == 24
    assert A["n_unmatched_to_a_decision"] == 0
    assert set(A["by_exit_reason"]) == {"stop", "target", "trim", "thesis", "rebalance"}
    assert sum(v["n"] for v in A["by_exit_reason"].values()) == 24
    assert set(A["by_desk"]) == {"swing", "pullback", "momentum"}
    assert all(v["n"] == 8 for v in A["by_desk"].values())
    assert set(A["by_setup"]) == {"Pullback in Uptrend", "Momentum Breakout"}
    assert set(A["by_verdict"]) <= {"Buy", "Strong Buy"}
    assert all(k in ("60-69", "70-79", "80-89", "unknown") for k in A["by_score_bucket"])
    assert "unknown" not in A["by_score_bucket"]
    stat = A["by_desk"]["swing"]
    # `n` is exit rows; the round-trip fields and the notional-weighted return came in with
    # the 2026-09-11 fix (a book row is a slice of an exit, not a trade).
    assert set(stat) == {"n", "n_round_trips", "n_positions_still_open", "pnl_total",
                         "pnl_mean", "pnl_pct_mean", "pnl_pct_mean_notional_weighted",
                         "pnl_pct_median", "sample"}
    assert stat["sample"] == rp.NOT_A_SAMPLE
    assert "win_rate" not in json.dumps(A) and "sharpe" not in json.dumps(A).lower()
    # a trade with no matching decision lands in `unknown`
    A2 = rp.attribution(entries, {"swing": {"positions": [], "closed_trades": [
        {"symbol": "NOPE", "opened": "2026-09-01", "closed": "2026-09-02", "pnl": 1.0,
         "pnl_pct": 1.0, "reason": "stop"}]}})
    assert A2["n_unmatched_to_a_decision"] == 1 and A2["by_score_bucket"] == {"unknown": A2["all"]}


# ------------------------------------------------------------------ the coverage schedule
def test_the_coverage_schedule_agrees_with_runner_slots_json(rp):
    """`runner/slots.json` is the schedule; report.py grades COVER-01 against a COPY of it
    (`DECISION_SLOTS`, `SENTINELS_PER_DAY`), because engine/MANIFEST.txt stages the engine
    modules and nothing from runner/ — a staged report.py has no slots.json to read, and
    the coverage table has to work there. A copy is only safe while something fails when
    the two drift, which is this test: change the schedule in slots.json and the coverage
    table stops grading against last week's shape in silence."""
    slots = json.loads((ROOT / "runner" / "slots.json").read_text(encoding="utf-8"))["slots"]
    decision = tuple(name for name, _ in sorted(
        ((n, s.get("nominal_et") or "") for n, s in slots.items()
         if "pm" in (s.get("steps") or [])), key=lambda kv: kv[1]))
    assert rp.DECISION_SLOTS == decision, (
        f"runner/slots.json carries the PM slots {decision} and report.py grades against "
        f"{rp.DECISION_SLOTS} — update DECISION_SLOTS (and the comment beside it)")
    times = slots["sentinel"]["times_et"]
    assert rp.SENTINELS_PER_DAY == len(times), (
        f"runner/slots.json schedules {len(times)} sentinels a day ({times[0]}–{times[-1]} "
        f"ET) and report.py expects {rp.SENTINELS_PER_DAY} — update SENTINELS_PER_DAY")
    # the constant's comment quotes the cadence; it has to still be the cadence
    assert (times[0], times[-1]) == ("09:35", "15:35"), (
        "report.py's comment says hourly at :35 from 09:35 to 15:35 ET; slots.json now "
        f"says {times[0]}–{times[-1]}")
    # and the reason it is a copy at all: nothing from runner/ is staged with the engine
    manifest = (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert "report.py" in manifest
    assert not [n for n in manifest if "/" in n or n.endswith("slots.json")], (
        "the engine stages flat, from engine/ only — if runner/slots.json is staged now, "
        "parse it instead of copying it")


def test_benchmark_is_exposure_adjusted_against_spy(rp, tmp_path):
    entries = rp.load_journals([str(JOURNALS)])
    series = rp.load_bars_oc(str(_bars(tmp_path, _drift(), spy=0.01)))
    B = rp.benchmark(entries, series)
    assert set(B) == {"swing", "pullback", "momentum"}
    b = B["swing"]
    assert b["start"] == "2026-09-01" and b["end"] == "2026-09-09"
    assert b["n_entries"] == 24                       # sentinels excluded
    assert 0 < b["avg_invested_frac"] < 1
    assert b["spy_window"] == ["2026-09-01", "2026-09-09"]
    assert b["spy_return_pct"] == pytest.approx((1.01 ** 6 - 1) * 100, abs=1e-3)
    assert b["exposure_adjusted_spy_pct"] == pytest.approx(
        b["avg_invested_frac"] * b["spy_return_pct"], abs=1e-3)
    assert b["excess_vs_exposure_adjusted_spy_pct"] == pytest.approx(
        b["return_pct"] - b["exposure_adjusted_spy_pct"], abs=1e-3)
    assert b["sample"] == rp.NOT_A_SAMPLE
    # without SPY in the bars the columns are empty, not assumed
    B0 = rp.benchmark(entries, None)
    assert B0["swing"]["spy_return_pct"] is None
    assert B0["swing"]["excess_vs_exposure_adjusted_spy_pct"] is None
    assert B0["swing"]["return_pct"] == b["return_pct"]


# ------------------------------------------------------------------ markdown and CLI
def test_markdown_carries_n_beside_every_number_and_says_not_a_sample(rp, tmp_path):
    entries = rp.load_journals([str(JOURNALS)])
    series = rp.load_bars_oc(str(_bars(tmp_path, _drift())))
    res = rp.build(entries, rp.load_books(str(BOOKS)), series, (5, 10), with_cf=True)
    md = rp.markdown(res)
    assert rp.NOT_A_SAMPLE in md
    assert "(n=" in md
    lower = md.lower()
    assert "win rate" not in lower.replace("no win rate", "")
    assert "sharpe" not in lower.replace("no sharpe", "")
    assert "## E5" in md and "### sector_cap" in md
    assert "refused names underperformed" in md
    assert "a brand new reason nobody has classified" in md
    assert "| swing |" in md and "SPY" in md
    # every percentage or dollar figure in a table row is followed by its n
    import re
    for line in md.splitlines():
        if not line.startswith("| "):
            continue
        for m in re.finditer(r"[+-]?\$?[\d,]*\d\.\d+%?", line):
            rest = line[m.end():]
            # a number is followed by its n, or sits inside a [lo, hi] interval whose n follows
            assert (rest.startswith(" (n=") or rest.startswith(" [") or rest.startswith(",")
                    or rest.startswith("] (n=")), (m.group(0), line)


def test_cli_json_round_trip_and_read_only(rp, tmp_path, capsys):
    bars = _bars(tmp_path, _drift())
    before = {p: p.read_bytes() for p in list(JOURNALS.glob("*.json")) + list(BOOKS.glob("*.json"))}
    out_json, out_md = tmp_path / "r.json", tmp_path / "r.md"
    rc = rp.main(["--journals", str(JOURNALS), "--books", str(BOOKS), "--bars", str(bars),
                  "--horizons", "5,10", "--counterfactual", "--json", str(out_json),
                  "--md", str(out_md)])
    assert rc == 0
    assert {p: p.read_bytes() for p in before} == before, "the report must not touch its inputs"
    res = json.loads(out_json.read_text(encoding="utf-8"))
    assert res["window"] == {"since": None, "first": "2026-09-01", "last": "2026-09-09",
                             "n_entries": 90, "desks": ["momentum", "pullback", "swing"]}
    assert res["refusals"]["by_rule"]["sector_cap"] == 36
    assert res["counterfactual"]["rules"]["sector_cap"]["10"]["diff"]["mean_pct"] < 0
    assert res["attribution"]["n_closed"] == 24
    assert res["benchmark"]["swing"]["spy_return_pct"] is not None
    assert "_rows" not in res["refusals"]
    assert rp.markdown(res) == out_md.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "114 refusals" in out and "E5 sector_cap" in out
    # --since, and the counterfactual refused without bars
    rc2 = rp.main(["--journals", str(JOURNALS), "--since", "2026-09-08", "--json", str(out_json)])
    assert rc2 == 0
    assert json.loads(out_json.read_text(encoding="utf-8"))["window"]["first"] == "2026-09-08"
    assert rp.main(["--journals", str(JOURNALS), "--counterfactual"]) == 2
    assert rp.main(["--journals", str(tmp_path / "nowhere")]) == 2
