"""One source for the exit thresholds (M2), and a return measured against real capital (M4).

Both were audit findings that survived because nothing asserted them. The 45/55 exit and
trim thresholds were hardcoded in `pm.py` AND in `portfolio.py`: change one and Scan Desk's
advisory verdict and the manager's actual exit disagree about when a thesis is gone.
`starting_equity` was set once at seeding and never moved, so funding the account made the
reported return fiction.
"""
import json
import pathlib
import re

import pytest

from conftest import ENGINE, run_pm


# ---------------------------------------------------------------- M2: one source
def test_pm_carries_no_hardcoded_exit_thresholds():
    """The literal thresholds must appear in portfolio.RULES and nowhere else in pm.py.

    A comparison against a bare 45 or 55 in the exit pass is exactly the drift this
    finding was about, so it is caught statically rather than waited for."""
    src = (ENGINE / "pm.py").read_text()
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    offenders = re.findall(r'r\["score"\]\s*[<>=]+\s*(?:45|55)|(?:45|55)\s*<=\s*r\["score"\]', body)
    assert offenders == [], f"pm.py compares a score against a literal threshold: {offenders}"


def test_the_exit_threshold_is_honoured_from_rules(run_dir, quotes, pm, monkeypatch, scan):
    """Move the rule and the engine's behaviour must move with it."""
    monkeypatch.setitem(pm.RULES, "exit_score_below", 95.0)
    s = json.loads((run_dir / "scan_results.json").read_text())
    s["results"] = [{"ticker": "NVDA", "score": 90.0, "setup": "Momentum", "price": 222.255}]
    (run_dir / "scan_results.json").write_text(json.dumps(s))
    _, jrn, _ = run_pm(pm, run_dir, with_scan=True)
    exits = [d for d in jrn["decisions"]
             if d["symbol"] == "NVDA" and d.get("reason") == "thesis"]
    assert exits, "a score under RULES['exit_score_below'] must close the position"


def test_portfolio_and_pm_read_the_same_rule_object(pm):
    import portfolio
    assert pm.RULES is portfolio.RULES
    assert portfolio.RULES["exit_score_below"] < portfolio.RULES["trim_score_below"]


# ---------------------------------------------------------------- M4: capital basis
def test_capital_basis_is_the_seed_when_nothing_was_deposited():
    import portfolio
    assert portfolio.capital_basis({"starting_equity": 5000.0}) == 5000.0


def test_capital_basis_adds_every_deposit():
    import portfolio
    book = {"starting_equity": 5000.0,
            "deposits": [{"date": "2026-09-10", "amount": 2500.0},
                         {"date": "2026-09-20", "amount": 500.0}]}
    assert portfolio.capital_basis(book) == 8000.0


def test_capital_basis_handles_a_withdrawal_as_a_negative_deposit():
    import portfolio
    book = {"starting_equity": 5000.0, "deposits": [{"date": "x", "amount": -1000.0}]}
    assert portfolio.capital_basis(book) == 4000.0


def test_capital_basis_is_none_when_the_book_was_never_seeded():
    import portfolio
    for book in ({}, {"starting_equity": 0}, {"starting_equity": None},
                 {"starting_equity": "5000"}, {"starting_equity": True}):
        assert portfolio.capital_basis(book) is None, book


def test_junk_in_the_deposits_list_is_ignored_not_fatal():
    import portfolio
    book = {"starting_equity": 100.0,
            "deposits": [None, 7, {"amount": "50"}, {"nope": 1}, {"amount": 25.0}]}
    assert portfolio.capital_basis(book) == 125.0


def test_the_reported_return_is_measured_against_the_deposit_basis(run_dir, quotes, pm):
    """Deposit $5,000 into a $5,000 book that has earned nothing: the honest answer is 0%,
    not +100%."""
    b = json.loads((run_dir / "paper_book.json").read_text())
    equity = b["cash"] + sum(p["shares"] * p["avg_cost"] for p in b["positions"])
    b["starting_equity"] = round(equity / 2, 2)
    b["deposits"] = [{"date": "2026-09-02", "amount": round(equity / 2, 2)}]
    (run_dir / "paper_book.json").write_text(json.dumps(b))
    _, _, state = run_pm(pm, run_dir)
    assert state["book"]["starting_equity"] == pytest.approx(equity, abs=1.0)
    assert state["book"]["seed_equity"] == pytest.approx(equity / 2, abs=1.0)
    assert state["book"]["deposits_total"] == pytest.approx(equity / 2, abs=1.0)
    assert abs(state["book"]["total_return_pct"]) < 3.0, \
        "a deposit is not a return"
