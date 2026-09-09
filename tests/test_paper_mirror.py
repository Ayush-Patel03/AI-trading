"""STATE-01 — Scan Desk must see the book the manager actually holds.

`portfolio.json` was the live-account mirror: $50 and nothing held. Every risk gate the Scan
Desk panel applies was therefore computed against an empty book while the real one sat at
34% in one sector. These tests pin the projection that replaces it — and, more importantly,
pin the two cases where it must refuse to write at all, because an invented panel is worse
than the old one.
"""
import json

import pytest

from conftest import ENGINE  # noqa: F401  (staging contract lives there)


@pytest.fixture
def mirror(run_dir):
    import importlib
    import paper_mirror as pmir
    importlib.reload(pmir)
    return pmir


def test_the_house_mirror_carries_every_desks_positions(run_dir, mirror):
    out, note = mirror.build()
    assert out is not None, note
    assert sorted(out["desks"]) == ["momentum", "pullback", "swing"]
    symbols = {p["symbol"] for p in out["positions"]}
    # The three fixture books hold seven distinct names between them.
    assert symbols == {"DELL", "HOOD", "MDB", "MRVL", "MU", "NVDA", "SNDK"}
    assert out["mode"] == "paper"
    assert "PAPER" in out["account"]["label"]


def test_a_name_held_on_two_desks_merges_with_a_weighted_cost(run_dir, mirror):
    books = [json.loads((run_dir / f).read_text(encoding="utf-8"))
             for f in ("paper_book.json", "paper_book_momentum.json")]
    want_shares = want_cost = 0.0
    for b in books:
        for p in b["positions"]:
            if p["symbol"] == "NVDA":
                want_shares += p["shares"]
                want_cost += p["shares"] * p["avg_cost"]
    out, _ = mirror.build()
    nvda = next(p for p in out["positions"] if p["symbol"] == "NVDA")
    assert nvda["shares"] == pytest.approx(want_shares, rel=1e-9)
    assert nvda["avg_cost"] == pytest.approx(want_cost / want_shares, rel=1e-6), \
        "averaging the averages would be wrong — the basis is share-weighted"


def test_cash_is_summed_across_the_desks(run_dir, mirror):
    want = sum(json.loads((run_dir / f).read_text(encoding="utf-8"))["cash"]
               for f in ("paper_book.json", "paper_book_pullback.json",
                         "paper_book_momentum.json"))
    out, _ = mirror.build()
    assert out["cash"] == pytest.approx(want, abs=0.01)


def test_one_desk_can_be_mirrored_alone(run_dir, mirror):
    out, _ = mirror.build(only="pullback")
    assert out["desks"] == ["pullback"]
    book = json.loads((run_dir / "paper_book_pullback.json").read_text(encoding="utf-8"))
    assert {p["symbol"] for p in out["positions"]} == {p["symbol"] for p in book["positions"]}


def test_working_orders_are_not_positions(run_dir, mirror):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    b["working_orders"] = [{"symbol": "AVGO", "shares": 2.0, "limit_price": 400.0,
                            "status": "working"}]
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    out, _ = mirror.build()
    assert "AVGO" not in {p["symbol"] for p in out["positions"]}, \
        "a resting order is committed capital, but it is not a holding — the manager " \
        "already counts it itself (CAP-01)"


def test_it_refuses_rather_than_writing_an_empty_panel(run_dir, mirror):
    for f in ("paper_book.json", "paper_book_pullback.json", "paper_book_momentum.json"):
        (run_dir / f).unlink()
    out, reason = mirror.build()
    assert out is None
    assert "nothing to mirror" in reason


def test_it_refuses_to_mirror_a_book_that_is_not_paper(run_dir, mirror):
    b = json.loads((run_dir / "paper_book.json").read_text(encoding="utf-8"))
    b["mode"] = "live"
    (run_dir / "paper_book.json").write_text(json.dumps(b), encoding="utf-8")
    out, reason = mirror.build()
    assert out is None and "paper mode" in reason, \
        "the day a book goes live the broker is the truth and this file must not exist"


def test_a_missing_peer_book_is_reported_not_silently_dropped(run_dir, mirror):
    (run_dir / "paper_book_momentum.json").unlink()
    out, note = mirror.build()
    assert out is not None
    assert "momentum" in note


def test_the_cli_writes_a_file_portfolio_py_can_mark(run_dir, mirror):
    assert mirror.main(["--out", "portfolio.json"]) == 0
    pf = json.loads((run_dir / "portfolio.json").read_text(encoding="utf-8"))
    import portfolio
    marked = portfolio.mark_portfolio(pf, {})
    assert marked["equity"] > 0
    assert len(marked["positions"]) == len(pf["positions"])
    assert marked["cash"] == pytest.approx(pf["cash"], abs=0.01)


def test_the_cli_exits_2_and_writes_nothing_when_it_refuses(run_dir, mirror):
    for f in ("paper_book.json", "paper_book_pullback.json", "paper_book_momentum.json"):
        (run_dir / f).unlink()
    assert mirror.main(["--out", "portfolio.json"]) == 2
    assert not (run_dir / "portfolio.json").exists()


def test_the_sector_gate_now_sees_the_real_concentration(run_dir, mirror):
    """The point of the whole fix, stated as a test.

    Against the old $50 empty mirror, portfolio.build_proposals saw zero Information
    Technology positions and would propose a fourth. Against the paper mirror it sees the
    names that are really held."""
    import portfolio
    mirror.main(["--out", "portfolio.json"])
    pf = json.loads((run_dir / "portfolio.json").read_text(encoding="utf-8"))
    marked = portfolio.mark_portfolio(pf, {})
    it_held = [p for p in marked["positions"] if p.get("gics") == "Information Technology"]
    assert len(it_held) >= portfolio.RULES["max_per_sector"], \
        "the fixture books really are over the per-sector count when combined"


def test_a_holding_outside_todays_scan_keeps_its_sector_label(run_dir, mirror):
    """mark_portfolio used to take the sector from the SCAN row only, so a holding the
    scan dropped lost its label and disappeared from the sector count — the gate went
    blind on precisely the positions it exists to watch."""
    import portfolio
    mirror.main(["--out", "portfolio.json"])
    pf = json.loads((run_dir / "portfolio.json").read_text(encoding="utf-8"))
    marked = portfolio.mark_portfolio(pf, {})          # no scan rows at all
    labelled = [p for p in marked["positions"] if p.get("gics")]
    assert len(labelled) == len(pf["positions"])
