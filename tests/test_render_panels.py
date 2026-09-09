"""RENDER-01 and RENDER-02 — both found by the live 2026-09-03 pre-market run.

`render.py` has no `__main__` guard: importing it RUNS it, against `scan_results.json` in
$SCAN_DIR. That makes it awkward to test and is exactly why it had no tests and why both of
these shipped. Importing it under a staged run directory is the test.
"""
import importlib
import json
import pathlib
import sys

import pytest

# The scanner's own golden output — a real, complete row rather than a hand-built one that
# is missing whichever field render.py happens to index directly.
FIX = pathlib.Path(__file__).parent / "fixtures"


def _scan():
    doc = json.loads((FIX / "scan_results_golden.json").read_text(encoding="utf-8"))
    doc["meta"] = dict(doc.get("meta") or {}, slot="Pre-market", time="08:00")
    doc["meta"].pop("live_board_url", None)
    return doc


def _render(run_dir, scan=None, live_url=None, extra=None):
    """Stage a scan and import render.py, which runs on import."""
    doc = scan or _scan()
    if live_url is not None:
        doc["meta"]["live_board_url"] = live_url
    if extra:
        doc.update(extra)
    (run_dir / "scan_results.json").write_text(json.dumps(doc), encoding="utf-8")
    sys.modules.pop("render", None)
    sys.modules.pop("archive", None)
    import archive           # noqa: F401  — reload it under this run dir too
    importlib.reload(archive)
    import render            # noqa: F401  — importing IS running
    importlib.reload(render)
    # Two boards per run: the rolling one that keeps the bookmarked URL current, and the
    # frozen per-run snapshot. Only the snapshot carries the "open the live board" link.
    snaps = [f for f in run_dir.glob("scan-desk-*.html")]
    return ((run_dir / "scan-desk.html").read_text(encoding="utf-8"),
            snaps[0].read_text(encoding="utf-8") if snaps else "")


# ------------------------------------------------------------------ RENDER-01
def test_a_board_renders_when_no_board_url_was_staged(run_dir):
    """The live failure: `archive.LIVE_BOARD_URL` was a bare None nothing resolved, so the
    run died in html.escape(None) AFTER scanner.py had succeeded and the hand-off was
    written. The scan survived; the board did not."""
    _, snap = _render(run_dir)                   # no engine-config.json, no live_board_url
    assert "Snapshot" in snap
    html = snap
    assert "Open the live Scan Desk" not in html, \
        "with no URL there is nothing to link to — omit the link, do not invent one"


def test_the_link_appears_when_a_url_is_staged(run_dir):
    _, html = _render(run_dir, live_url="https://example.invalid/board")
    assert "Open the live Scan Desk" in html
    assert "https://example.invalid/board" in html


def test_the_url_is_resolved_from_the_private_config(run_dir):
    """The identifier lives in engine-config.json, staged at run time — never in this repo."""
    (run_dir / "engine-config.json").write_text(json.dumps(
        {"boards": {"scan_desk": "https://example.invalid/from-config"}}), encoding="utf-8")
    _, html = _render(run_dir)
    assert "https://example.invalid/from-config" in html, \
        "archive.LIVE_BOARD_URL must resolve through config.board_url()"


# ------------------------------------------------------------------ RENDER-02
def test_an_uncollected_insider_panel_says_so_instead_of_rendering_empty_tables(run_dir):
    """'We looked and found no insider activity' and 'this slot does no insider work' are
    different claims. Three boards a day were making the first one."""
    html, _ = _render(run_dir)
    assert "Insider activity" in html
    assert "Not collected this slot" in html
    assert "Notable purchases" not in html, \
        "an empty table with headers reads as a finding"


def test_an_uncollected_ipo_panel_says_so_too(run_dir):
    html, _ = _render(run_dir)
    assert "IPO watch" in html
    assert "Recent pricings" not in html


def test_a_populated_insider_panel_still_renders_its_tables(run_dir):
    html, _ = _render(run_dir, extra={"insider_panel": {
        "clusters": [{"ticker": "AAA", "filers": 3, "side": "buy", "value": 1_000_000}],
        "purchases": [{"ticker": "BBB", "insider": "A Director", "value": 250_000}],
        "notes": ["CARRIED FROM Power hour 2026-09-02"]}})
    assert "Notable purchases" in html
    assert "AAA" in html and "BBB" in html
    assert "CARRIED FROM" in html
    assert "Not collected this slot" not in html.split("Insider activity")[1][:400]


def test_a_populated_ipo_panel_still_renders_its_tables(run_dir):
    html, _ = _render(run_dir, extra={"ipo": {
        "upcoming": [{"symbol": "NEWCO", "company": "New Co", "range": "$18-20",
                      "date": "2026-09-10", "underwriters": "Bank"}],
        "recent": [], "notes": []}})
    assert "Upcoming" in html and "NEWCO" in html


def test_the_board_never_contains_an_unsubstituted_placeholder(run_dir):
    html, snap = _render(run_dir)
    assert "@@" not in html and "@@" not in snap
    for doc in (html, snap):
        assert "{IPO_PANEL}" not in doc and "{INSIDER_PANEL}" not in doc
