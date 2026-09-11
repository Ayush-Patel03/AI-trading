"""render_review.py — the weekly review page (U-05).

The page is rendered from the same three-desk journal fixture test_report.py uses, plus
the real backfilled ledger and the real model card. The honesty budget is tested on the
OUTPUT: no win rate, no Sharpe, an n on every aggregate cell, the not-a-sample chip on
every aggregate under 30, and a renderer that refuses an input which would break any of
that rather than publishing it.
"""
import json
import pathlib
import re

import pytest

from conftest import ENGINE, FIX, ROOT
from test_report import _bars, _drift, JOURNALS, BOOKS

LEDGER = ROOT / "experiments" / "ledger.jsonl"
CARD = ROOT / "docs" / "model-card.md"
FAKE_MASK = "•" * 4 + "1234"           # a shape, never a literal — see test_config.py


@pytest.fixture
def mods(run_dir):
    import importlib
    import report, render_review, ledger
    for m in (report, render_review, ledger):
        importlib.reload(m)
    return report, render_review, ledger


@pytest.fixture
def review(mods, tmp_path):
    """report.build over the fixture, with the counterfactual, as --json would write it."""
    rp = mods[0]
    entries = rp.load_journals([str(JOURNALS)])
    series = rp.load_bars_oc(str(_bars(tmp_path, _drift())))
    return json.loads(json.dumps(rp.build(entries, rp.load_books(str(BOOKS)), series, (5, 10),
                                          with_cf=True)))


@pytest.fixture
def page(mods, review):
    _, rr, lg = mods
    return rr.render(review, lg.trials(str(LEDGER)), model_card=CARD.read_text(encoding="utf-8"),
                     generated_at="2026-09-11T20:35:00Z")


# ------------------------------------------------------------------ the page
def test_the_fixture_review_renders_every_section(page):
    for h in ("Weekly review &middot; 2026-W37", "Benchmark-relative, per desk", "Attribution",
              "Refusals", "E5 ·", "Shadow ledger", "House exposure", "Experiment ledger",
              "Model card"):
        assert h in page, h
    assert "Paper" in page and "no orders sent" in page
    assert "generated 2026-09-11T20:35:00Z" in page
    assert "engine unknown" in page              # the fixture journal carries no engine_sha
    assert "<title>Weekly review 2026-W37</title>" in page
    assert "@@" not in page


def test_the_page_reuses_the_trade_desk_palette_rather_than_inventing_one(page):
    import render_pm
    assert render_pm.CSS in page
    assert ':root[data-theme="dark"]' in page and "prefers-color-scheme: dark" in page


def test_not_a_sample_chip_appears_wherever_n_is_under_30(page):
    # every row with a sample column: the chip iff the row's observation n is under 30
    # (a bootstrap's "n=6 dates" is the interval's support, not the sample — report.py's
    # rule is observations on either side)
    rows = re.findall(r"<tr>(.*?)</tr>", page, re.S)
    checked = 0
    for r in rows:
        ns = [int(x) for x in re.findall(r'<span class="nn">n=(\d+)</span>', r)]
        if not ns or "chipcell" not in r:
            continue
        checked += 1
        if min(ns) < 30:
            assert "not a sample" in r, r
        else:
            assert "not a sample" not in r, r
    assert checked >= 20
    # the fixture has 24 closed trades: the attribution header itself says so
    assert "n=24 · not a sample" in page
    assert "n=6834" in page and 'sub2">ok</span>' in page      # the 6,834-row backtest is one


def test_no_win_rate_and_no_sharpe_anywhere(page):
    low = page.lower()
    for word in ("win rate", "win-rate", "winrate", "sharpe"):
        assert word not in low, word


def test_every_aggregate_cell_carries_its_n(page):
    cells = re.findall(r'<td class="n agg">(.*?)</td>', page, re.S)
    assert len(cells) >= 60
    for c in cells:
        assert re.search(r"n=(\d+|unknown)", c), c
    # and no bare percentage or dollar figure sits in a numeric cell without one
    for c in re.findall(r'<td class="n[^"]*">(.*?)</td>', page, re.S):
        if re.search(r"[\d]\.\d+%|\$[\d,]+\.\d\d", c):
            assert "n=" in c, c


def test_bars_are_inline_svg_with_counts_and_no_chart_library(page):
    assert page.count("<svg") == 2
    assert 'aria-label="Refusals by rule"' in page
    assert 'aria-label="Refusals by week and rule"' in page
    assert '<rect class="bar"' in page and '<rect class="seg s1"' in page
    assert "<title>sector_cap: 36</title>" in page
    assert "2026-W36" in page and "2026-W37" in page
    assert "<script src" not in page and "cdn" not in page.lower()


def test_the_counterfactual_panel_carries_ci_n_and_the_reading(page):
    e5 = page.split("E5 ·")[1].split("Shadow ledger")[0]
    assert "sector_cap" in e5
    assert re.search(r"n=\d+ dates", e5)
    assert "refused names underperformed" in e5
    assert "not a sample" in e5
    assert "[" in e5 and "]" in e5             # a 90% interval is printed where one exists


def test_the_model_card_fields_are_parsed_and_rendered(mods, page):
    rr = mods[1]
    fields, body = rr.parse_model_card(CARD.read_text(encoding="utf-8"))
    for k in ("engine_sha", "purpose", "desks", "training_window", "universe", "n_observations",
              "ic_10d", "ic_20d", "quintile_spread", "slices", "trials_on_ledger", "dsr",
              "live_validation", "known_failure_regimes", "monitoring_rolling_ic",
              "monitoring_n_eff", "monitoring_shadow_gap", "rollback_rule", "owner", "date"):
        assert k in fields, k
    assert fields["n_observations"].startswith("6,834")
    assert "2024-08-21" in fields["training_window"] and "2026-08-28" in fields["training_window"]
    assert "−0.040" in fields["ic_10d"] and "−0.045" in fields["ic_20d"]
    assert "n = 0" in fields["live_validation"]
    assert "not yet computed" in fields["dsr"]
    assert "survivor" in fields["universe"].lower()
    assert "does not rank" in fields["verdict"]
    assert body.lstrip().startswith("# Model card")
    for v in (fields["ic_10d"], fields["live_validation"], fields["rollback_rule"]):
        assert rr.esc(v) in page
    # a card without front matter parses to no fields and renders the empty state
    assert rr.parse_model_card("# just prose\n") == ({}, "# just prose\n")
    assert "No model card staged" in rr.render({}, [], model_card="# just prose\n")


def test_the_ledger_table_lists_every_backfilled_trial_with_the_dsr_reminder(page):
    for tid in ("BT-2026-09-10", "BT-2026-09-10-ex-etf", "BT-2026-09-10-ex-semis",
                "BT-2026-09-10-per-horizon"):
        assert tid in page
    assert "4 trial(s)" in page and "the next is number 5" in page
    assert "DSR reminder" in page and "not measured" in page
    assert "n=unknown" in page               # the ex-ETF slice has n: null — not a zero


def test_shadow_and_house_render_their_absent_states_honestly(page, mods, review):
    assert "model off" in page                   # the fixture books carry no shadow block
    assert "was not measured" in page            # no house block on any fixture entry
    rr, lg = mods[1], mods[2]
    review["house"] = {"date": "2026-09-09", "slot": "power-hour", "desk": "swing",
                       "equity": 15000.0, "desks": 3,
                       "exposure": {"n_eff": 2.4, "n_eff_basis": "proxy", "beta_w": None,
                                    "largest_sector": "Information Technology",
                                    "largest_sector_pct": 34.2, "overlap_pct": 57.0,
                                    "overlap_equity_pct": 41.0, "flags": ["n_eff"],
                                    "enforce": True, "block_new_entries": True}}
    review["shadow"]["swing"] = {"model": "on", "cum_gap_usd": 12.4, "n_fills": 9,
                                 "gap_share_of_realized_pct": 3.1, "window_gap_usd": 4.2,
                                 "n_entries_with_shadow": 20}
    health = {"checks": [{"name": "shadow_gap", "status": "warn", "detail": "day gap $30.00",
                          "threshold": "day gap < $25"}]}
    doc = rr.render(review, lg.trials(str(LEDGER)), health=health)
    assert "2.40" in doc and "proxy" in doc and "Information Technology" in doc
    assert "blocking new entries house-wide" in doc and "n_eff" in doc
    assert "+$12.40 <span class=\"nn\">n=9</span>" in doc
    assert "WARN" in doc and "day gap $30.00" in doc


def test_an_ic_json_block_renders_with_n_dates(mods, review):
    rr, lg = mods[1], mods[2]
    ic = {"n": 210, "n_dates": 5, "horizons": [5],
          "score": {"5": {"n": 210, "n_dates": 5, "ic_mean": -0.02, "ic_tstat_nw": -0.4,
                          "pooled_spearman": -0.03, "pooled_p": 0.6,
                          "spread": {"quantile": 3, "n_dates": 5, "mean_pct": -0.3,
                                     "ci90_pct": [-1.2, 0.5], "block": 5}}}}
    doc = rr.render(review, lg.trials(str(LEDGER)), ic_json=ic)
    assert "This week&#x27;s IC" in doc or "This week's IC" in doc
    assert "n=5 dates" in doc and "n=210" in doc
    assert "[−1.20, +0.50]" in doc


# ------------------------------------------------------------------ the honesty budget in code
def test_the_renderer_refuses_an_input_that_names_a_banned_metric(mods, review):
    rr, lg = mods[1], mods[2]
    rows = lg.trials(str(LEDGER))
    bad = dict(rows[0], hypothesis="a higher win rate at 10 sessions")
    with pytest.raises(rr.HonestyError):
        rr.render(review, [bad] + rows[1:])
    with pytest.raises(rr.HonestyError):
        rr.render(review, rows, model_card={"dsr": "needs a per-trial Sharpe series"})


# ------------------------------------------------------------------ CLI
def test_the_cli_writes_the_page(mods, review, tmp_path, capsys):
    rr = mods[1]
    rj = tmp_path / "review.json"
    rj.write_text(json.dumps(review), encoding="utf-8")
    out = tmp_path / "weekly-review.html"
    rc = rr.main(["--review", str(rj), "--ledger", str(LEDGER), "--model-card", str(CARD),
                  "--out", str(out)])
    assert rc == 0
    doc = out.read_text(encoding="utf-8")
    assert "Weekly review" in doc and "BT-2026-09-10" in doc and "engine_sha" in doc.replace("engine sha", "engine_sha")
    assert "wrote" in capsys.readouterr().out
    # a missing input is a refusal, not a traceback
    assert rr.main(["--review", str(tmp_path / "nope.json"), "--ledger", str(LEDGER),
                    "--out", str(out)]) == 2
    # and so is a banned word arriving through the CLI
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({"id": "X", "hypothesis": "Sharpe of 1"}) + "\n", encoding="utf-8")
    assert rr.main(["--review", str(rj), "--ledger", str(bad), "--out", str(out)]) == 2
    assert "REFUSED" in capsys.readouterr().err


# ------------------------------------------------------------------ hygiene
def test_the_page_carries_no_mirrors_block_or_masked_account(mods, review, tmp_path):
    """Same guard as test_mirror.py: a book's `mirrors.display` is a masked account number
    and must not travel. The review reads books, so a book that carries one is the test."""
    rp, rr, lg = mods
    books = rp.load_books(str(BOOKS))
    books["swing"] = dict(books["swing"], mirrors={"broker": "Robinhood", "display": FAKE_MASK,
                                                   "nickname": "Agentic"})
    entries = rp.load_journals([str(JOURNALS)])
    res = rp.build(entries, books, None, (5,), with_cf=False)
    doc = rr.render(res, lg.trials(str(LEDGER)), model_card=CARD.read_text(encoding="utf-8"))
    assert "mirrors" not in doc
    assert "1234" not in doc and FAKE_MASK not in doc
    import mirror
    assert mirror.scan_text(doc) == []
    assert "claude.ai/code/" + "artifact" not in doc     # the shape, split so this file passes its own scan


def test_report_json_carries_shadow_house_and_engine_sha(mods, review):
    assert set(review["shadow"]) == {"momentum", "pullback", "swing"}
    assert all(v["model"] == "off" for v in review["shadow"].values())
    assert review["house"] is None and review["engine_sha"] is None
    assert review["generated_at"].endswith("Z")
    rp = mods[0]
    entries = rp.load_journals([str(JOURNALS)])
    entries[-1] = dict(entries[-1], engine_sha="abc1234", shadow={"cum_gap_usd": 2.5, "n_fills": 3},
                       house={"equity": 15000.0, "desks": 3, "exposure": {"n_eff": 2.0}})
    entries[0] = dict(entries[0], shadow={"cum_gap_usd": 0.5, "n_fills": 1})
    res = rp.build(entries, {}, None, (5,))
    assert res["engine_sha"] == "abc1234"
    assert res["house"]["exposure"] == {"n_eff": 2.0} and res["house"]["date"] == entries[-1]["date"]
    d = entries[-1]["desk"]
    assert res["shadow"][d]["model"] == "on" and res["shadow"][d]["cum_gap_usd"] == 2.5
    assert res["shadow"][d]["window_gap_usd"] == (2.0 if entries[0]["desk"] == d else None)


def test_render_review_is_on_the_manifest_and_the_ci_allowlist():
    assert "render_review.py" in (ENGINE / "MANIFEST.txt").read_text(encoding="utf-8").split()
    assert '"render_review"' in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
