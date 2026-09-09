"""Two guarantees that exist as prose and, until now, as nothing else.

PM.md 12c: `watch.py` has "no write path to a paper book, by construction". It is a separate
program precisely so that "the overnight run never mutates the book" is enforced by structure
rather than by a flag a future session can flip — but nothing checked the structure.

SCAN.md 5: `history.py` is a pure merge because a scheduled session that outlived its trading
day once wrote Friday's scores over Monday's file, five minutes after it had been reset.
"""
import ast

import json

import pytest

from conftest import ENGINE


# ------------------------------------------------------------------ watch.py cannot write
def test_watch_opens_exactly_one_file_for_writing(  # noqa: D401
):
    """Structural, not behavioural: a future edit that adds a second write path fails here
    before it can ever reach a book."""
    tree = ast.parse((ENGINE / "watch.py").read_text(encoding="utf-8"))
    writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
            mode = None
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if mode and any(m in mode for m in ("w", "a", "+", "x")):
                writes.append(ast.dump(node))
    assert len(writes) == 1, (
        "watch.py must have exactly one write path — its own journal. Found "
        f"{len(writes)}. PM.md 12c: it is a separate program so that 'the overnight run "
        "never mutates the book' is structural, not a flag.")


def test_watch_never_names_a_paper_book():
    src = (ENGINE / "watch.py").read_text(encoding="utf-8")
    body = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#") and '"""' not in l)
    # Reading a book is the job — the watch prices what is held. What it must never touch
    # is anything on the WRITE side of the book protocol.
    for forbidden in ("pm_book_next", "book_fingerprint", "based_on_revision", "_apply_sell"):
        assert forbidden not in body, (
            f"watch.py references {forbidden!r}. Outside 09:30-16:00 the manager could not "
            "sell most of what it holds even in live mode; a watch that booked an overnight "
            "exit would log a fill no real account could have gotten.")


def test_watch_imports_only_the_two_functions_it_is_allowed():
    """PM.md 12c: it imports pm.py's quote parser and the divergence check, and nothing
    else. Do not give it one."""
    tree = ast.parse((ENGINE / "watch.py").read_text(encoding="utf-8"))
    from_pm = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pm":
            from_pm += [a.name for a in node.names]
        if isinstance(node, ast.Import):
            assert "pm" not in [a.name for a in node.names], \
                "a bare `import pm` hands this program every mutating function pm.py has"
    assert sorted(from_pm) == ["broker_divergence", "quotes_to_prices"], from_pm


# ------------------------------------------------------------------ history.py is a merge
@pytest.fixture
def history(run_dir):
    import importlib
    import history as h
    importlib.reload(h)
    return h


def _entry(slot="Midday", time="12:30", scores=None):
    return {"slot": slot, "time": time, "scores": scores or {"NVDA": 78.0}}


def test_a_stale_session_cannot_write_yesterdays_scores(history):
    """The exact 2026-08-31 failure: a scan fired on Friday finished on Monday and wrote
    Friday-dated scores over Monday's file."""
    with pytest.raises(SystemExit) as e:
        history.merge({"date": "2026-09-02", "scans": []},
                      dict(_entry(), date="2026-08-28"), "2026-09-02")
    assert "REFUSED" in str(e.value)


def test_an_entry_missing_its_scores_is_refused(history):
    for bad in ({"slot": "Midday"}, {"scores": {"X": 1}}, "not a dict"):
        with pytest.raises((ValueError, SystemExit)):
            history.merge({}, bad, "2026-09-02")


def test_a_new_day_starts_a_new_file(history):
    out, fresh, _ = history.merge({"date": "2026-09-01",
                                   "scans": [_entry(slot="Pre-market")]},
                                  _entry(), "2026-09-02")
    assert fresh is True
    assert out["date"] == "2026-09-02"
    assert [s["slot"] for s in out["scans"]] == ["Midday"]


def test_slots_stay_in_time_order_however_they_arrive(history):
    doc = {"date": "2026-09-02", "scans": []}
    for slot, time in (("Power hour", "15:00"), ("Pre-market", "08:00"), ("Midday", "12:30")):
        doc, _, _ = history.merge(doc, _entry(slot, time), "2026-09-02")
    assert [s["time"] for s in doc["scans"]] == ["08:00", "12:30", "15:00"]


def test_a_rerun_replaces_its_own_slot_rather_than_duplicating_it(history):
    doc, _, _ = history.merge({"date": "2026-09-02", "scans": []}, _entry(), "2026-09-02")
    doc, _, _ = history.merge(doc, _entry(scores={"NVDA": 81.0}), "2026-09-02")
    midday = [s for s in doc["scans"] if s["slot"] == "Midday"]
    assert len(midday) == 1
    assert midday[0]["scores"]["NVDA"] == 81.0


def test_the_merge_does_not_mutate_its_input(history):
    doc = {"date": "2026-09-02", "scans": [_entry(slot="Pre-market", time="08:00")]}
    frozen = json.dumps(doc, sort_keys=True)
    history.merge(doc, _entry(), "2026-09-02")
    assert json.dumps(doc, sort_keys=True) == frozen, \
        "a pure function is the whole defence against four sessions writing one file"
