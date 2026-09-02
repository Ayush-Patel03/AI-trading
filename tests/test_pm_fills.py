"""The paper fill model, which is what makes the P&L readable at all (docs/PM.md §3)."""
import datetime as dt
import json

import pytest

from conftest import run_pm


def test_entry_never_fills_in_the_run_that_placed_it(pm, run_dir, quotes, scan):
    book, jrn, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    placed = [d for d in jrn["decisions"] if d["action"] == "place-buy"]
    assert placed, "expected an entry to be placed"
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-buy"], \
        "an entry may never fill in the run that placed it"
    assert len(book["working_orders"]) == len(placed)


def test_entry_fills_at_the_limit_on_a_later_run(pm, run_dir, quotes, scan):
    book, jrn1, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    order = book["working_orders"][0]
    limit = order["limit_price"]
    (run_dir / "paper_book.json").write_text(json.dumps(book))
    _, jrn2, _ = run_pm(pm, run_dir, slot="midday", with_scan=True)
    fills = [d for d in jrn2["decisions"] if d["action"] == "fill-buy"]
    assert fills, "a later slot must work the resting order"
    assert fills[0]["price"] == limit, "a resting limit fills AT the limit, not at the print"


def test_day_order_expires_at_the_session_roll(pm, run_dir, quotes, scan):
    book, _, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    assert book["working_orders"]
    jrn = {"decisions": [], "skipped": [], "warnings": [], "slot": "pre-market"}
    tomorrow = dt.date.fromisoformat(book["day"]["date"]) + dt.timedelta(days=1)
    pm.roll_day(book, tomorrow, jrn)
    assert book["working_orders"] == [], "every day order dies at the roll"
    assert [d for d in jrn["decisions"] if d["action"] == "expire"]


def test_lapsed_thesis_cancels_instead_of_filling(pm, run_dir, quotes, scan):
    book, _, _ = run_pm(pm, run_dir, slot="opening-range", with_scan=True)
    (run_dir / "paper_book.json").write_text(json.dumps(book))
    lapsed = json.loads((run_dir / "scan_results.json").read_text())
    lapsed["results"][0]["score"] = 40.0          # under min_score_to_propose
    (run_dir / "scan_results.json").write_text(json.dumps(lapsed))
    _, jrn, _ = run_pm(pm, run_dir, slot="midday", with_scan=True)
    cancels = [d for d in jrn["decisions"] if d["action"] == "cancel"]
    assert cancels, "a thesis that lapsed before the fill must cancel"
    assert not [d for d in jrn["decisions"] if d["action"] == "fill-buy"]
