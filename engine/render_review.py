"""render_review.py — the weekly review as a page (U-05).

Renders `report.py --json` (plus the experiment ledger, an optional `ic.py --json`, the
model card and an optional `health.py --json`) into one self-contained HTML page for the
Artifact tool: inline CSS and SVG, no chart library, the Trade Desk palette and type
(`render_pm.CSS` is reused, not copied), light and dark.

THE HONESTY BUDGET, ENFORCED
----------------------------
`report.py` carries no win rate and no Sharpe and puts an n beside every number; this
page must not add either back. `render()` therefore checks its own output before returning
it: if the finished page contains any phrase in `BANNED` (case-insensitive) it raises
`HonestyError` and the CLI exits 2 — a ledger hypothesis or a model-card field that names
the metric is refused, not published. Every aggregate cell is emitted through `_agg()`,
which will not format a number without its n AND will not format an n without a number —
an absent value, or a count of zero, renders `_absent()` instead. Every aggregate under
`report.MIN_N` carries the "not a sample" chip, and on the attribution tables the chip
counts round trips rather than exit rows.

SECTIONS
--------
masthead · benchmark-relative per desk · attribution (exit reason / desk / entry-score
bucket / setup) · refusals histogram by rule and by week (inline SVG) · E5 counterfactual ·
COVER-01 expected-vs-actual runs · shadow ledger · house exposure · experiment ledger with
the DSR reminder · model card · footer.

Usage
-----
    python3 render_review.py --review review.json --ledger experiments/ledger.jsonl
                             [--ic ic.json] [--model-card docs/model-card.md]
                             [--health health.json] --out weekly-review.html
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import render_pm
import ledger as ledger_mod

MIN_N = 30
NOT_A_SAMPLE = "not a sample"
BANNED = ("win rate", "win-rate", "winrate", "sharpe")
DASH = "—"

esc = render_pm.esc


class HonestyError(ValueError):
    """The rendered page would carry a metric the review refuses."""


def honesty_violations(text):
    low = text.lower()
    return [b for b in BANNED if b in low]


# ---------------------------------------------------------------- formatting
def _fmt(v, dp=2, signed=True, unit="%"):
    """A bare number for the page — used ONLY inside _agg, which appends the n."""
    if v is None:
        return DASH
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DASH
    if unit == "$":
        return f"{'+' if f >= 0 else '−'}${abs(f):,.{dp}f}"
    s = f"{f:+.{dp}f}" if signed else f"{f:.{dp}f}"
    if s.startswith("-"):
        s = "−" + s[1:]
    return s + unit


def _sample_chip(n):
    return "" if n is None or n >= MIN_N else \
        f' <span class="chip warning"><span class="dot"></span>{NOT_A_SAMPLE}</span>'


def _absent(what="absent"):
    """A cell with nothing in it. NOT an aggregate: it carries no n, because a count of
    observations of a value that was never measured is a fabrication."""
    return f'<td class="n absent">{DASH} <span class="nn">{esc(what)}</span></td>'


def _agg(v, n, dp=2, signed=True, unit="%"):
    """An aggregate cell: the number and its n. There is no code path that formats an
    aggregate without its n, and none that prints an n without a number — an absent value
    or an n of zero renders `_absent()` instead.

    Both halves matter on the real report. `— n=25` (an empty SPY column beside the 25
    journal entries of the desk beside it) reads as twenty-five observations of nothing;
    `+$0.00 n=0` on a desk whose shadow model has priced no fill reads as a measured zero
    execution gap. Neither was measured. The not-a-sample chip sits once per row
    (`_chip_td`)."""
    n = 0 if n is None else int(n)
    if v is None or n <= 0:
        return _absent("not measured" if n <= 0 else "absent")
    return f'<td class="n agg">{_fmt(v, dp, signed, unit)} <span class="nn">n={n}</span></td>'


def _chip_td(n):
    """The row's sample verdict: the not-a-sample chip under MIN_N, else 'ok'."""
    n = 0 if n is None else int(n)
    inner = _sample_chip(n) if n < MIN_N else '<span class="sub2">ok</span>'
    return f'<td class="chipcell">{inner}</td>'


def _week_of(date_str):
    try:
        y, w, _ = dt.date.fromisoformat(str(date_str)[:10]).isocalendar()
        return f"{y}-W{w:02d}"
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- model card
def parse_model_card(text):
    """(fields, body). The front matter is the block between the first two `---` lines:
    plain `key: value` lines in order; a line that starts with whitespace continues the
    previous value. No YAML library — stdlib only, and the format is deliberately flat."""
    if text is None:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    fields, key = {}, None
    i = 1
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.strip() == "---":
            break
        if not line.strip():
            continue
        if line[0] in " \t" and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
            continue
        m = re.match(r"^([A-Za-z0-9_][A-Za-z0-9_\-. ]*):\s*(.*)$", line)
        if m:
            key = m.group(1).strip()
            fields[key] = m.group(2).strip()
        elif key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return fields, "\n".join(lines[i:])


# ---------------------------------------------------------------- SVG bars
def bars_svg(items, label, max_label=22):
    """Horizontal bars: items = [(name, count), ...] sorted as given. Inline SVG, no
    library; every bar carries its count as text so the number is never colour-only."""
    items = [(str(k), int(v)) for k, v in items if v is not None]
    if not items:
        return f'<div class="empty">{esc(label)}: nothing to draw.</div>'
    W, RH, PL, PR = 720, 22, 190, 60
    H = RH * len(items) + 8
    mx = max(v for _, v in items) or 1
    iw = W - PL - PR
    rows = []
    for i, (k, v) in enumerate(items):
        y = 4 + i * RH
        w = iw * v / mx
        name = k if len(k) <= max_label else k[:max_label - 1] + "…"
        rows.append(
            f'<text class="axis" x="{PL - 8}" y="{y + 14.5:.1f}" text-anchor="end">{esc(name)}</text>'
            f'<rect class="bar" x="{PL}" y="{y + 3}" width="{w:.1f}" height="{RH - 7}" rx="2">'
            f'<title>{esc(k)}: {v}</title></rect>'
            f'<text class="axis val" x="{PL + w + 6:.1f}" y="{y + 14.5:.1f}">{v}</text>')
    return (f'<svg class="bars" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">'
            + "".join(rows) + "</svg>")


_SERIES = ("s1", "s2", "s3", "s4", "s5", "s6", "s7")


def stacked_weeks_svg(by_week, rules, label):
    """One stacked horizontal bar per week, segments per rule (top six by count; the rest
    fold into 'other rules'). Every segment carries a <title> with its count."""
    weeks = list(by_week or {})
    if not weeks:
        return f'<div class="empty">{esc(label)}: no refusals in the window.</div>'
    top = list(rules)[:6]
    keys = top + (["other rules"] if len(rules) > 6 else [])
    W, RH, PL, PR = 720, 26, 90, 60
    H = RH * len(weeks) + 8
    iw = W - PL - PR
    totals = {w: sum(int(v) for v in by_week[w].values()) for w in weeks}
    mx = max(totals.values()) or 1
    rows = []
    for i, wk in enumerate(weeks):
        y = 4 + i * RH
        x = PL
        rows.append(f'<text class="axis" x="{PL - 8}" y="{y + 16.5:.1f}" text-anchor="end">{esc(wk)}</text>')
        counts = dict(by_week[wk])
        folded = sum(int(v) for k, v in counts.items() if k not in top)
        for j, k in enumerate(keys):
            v = folded if k == "other rules" else int(counts.get(k, 0))
            if v <= 0:
                continue
            w = iw * v / mx
            rows.append(f'<rect class="seg {_SERIES[j % len(_SERIES)]}" x="{x:.1f}" y="{y + 4}" '
                        f'width="{w:.1f}" height="{RH - 9}"><title>{esc(wk)} {esc(k)}: {v}</title></rect>')
            x += w
        rows.append(f'<text class="axis val" x="{x + 6:.1f}" y="{y + 16.5:.1f}">n={totals[wk]}</text>')
    legend = "".join(f'<span class="lg"><span class="sw {_SERIES[j % len(_SERIES)]}"></span>{esc(k)}</span>'
                     for j, k in enumerate(keys))
    return (f'<svg class="bars" viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}">'
            + "".join(rows) + f'</svg><div class="legend">{legend}</div>')


# ---------------------------------------------------------------- sections
def _card(title, body, count=None, note=None):
    c = f' <span class="count">{esc(count)}</span>' if count is not None else ""
    n = f' <span class="note">{note}</span>' if note else ""
    return f'<div class="card"><h2>{esc(title)}{c}{n}</h2>{body}</div>'


def _table(head, rows):
    th = "".join(('<th class="n">' if r else "<th>") + esc(h) + "</th>" for h, r in head)
    return (f'<div class="scroll"><table><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def masthead(review, generated_at):
    w = review.get("window") or {}
    week = _week_of(w.get("last")) or DASH
    desks = ", ".join(w.get("desks") or []) or DASH
    sha = review.get("engine_sha") or "unknown"
    return f"""<div class="mast">
    <div>
      <h1>Weekly review &middot; {esc(week)}</h1>
      <div class="sub">{esc(w.get('first') or DASH)} &rarr; {esc(w.get('last') or DASH)} &middot;
        {int(w.get('n_entries') or 0)} journal entries &middot; desks: {esc(desks)}</div>
    </div>
    <div class="spacer"></div>
    <span class="badge paper">Paper &mdash; no orders sent</span>
    <span class="badge">engine {esc(sha)}</span>
    <span class="badge">generated {esc(generated_at)}</span>
  </div>"""


def benchmark_section(review):
    B = review.get("benchmark") or {}
    if not B:
        return _card("Benchmark-relative, per desk",
                     '<div class="empty">No journal entries with equity in the window.</div>')
    head = [("desk", False), ("window", False), ("return", True), ("avg invested", True),
            ("SPY", True), ("exposure × SPY", True), ("excess", True), ("sample", False)]
    rows = []
    for desk, b in B.items():
        n = b.get("n_entries") or 0
        inv = b.get("avg_invested_frac")
        rows.append(
            f'<tr><td class="sym">{esc(desk)}</td>'
            f'<td class="st sub2">{esc(b.get("start"))} &rarr; {esc(b.get("end"))}</td>'
            + _agg(b.get("return_pct"), n)
            + _agg(None if inv is None else inv * 100.0, n, dp=0, signed=False)
            + _agg(b.get("spy_return_pct"), n)
            + _agg(b.get("exposure_adjusted_spy_pct"), n)
            + _agg(b.get("excess_vs_exposure_adjusted_spy_pct"), n)
            + _chip_td(n) + "</tr>")
    note = ("n = decision-slot journal entries in the window; one return per desk is one number, "
            "not a distribution. SPY absent from the bars leaves its columns as a dash, never assumed.")
    excluded = sum(sum((b.get("excluded_entries") or {}).values()) for b in B.values())
    if excluded:
        note += (f" {excluded} journal entr(ies) are excluded from a desk's window: an entry on or "
                 "before the book's own <code>resized.date</code> is on a different capital base, "
                 "and an entry with no <code>desk</code> field predates the desk split. Neither is "
                 "that desk's track record, and a return measured across a capital change is not "
                 "a return.")
    return _card("Benchmark-relative, per desk", _table(head, rows) + f'<p class="pnote">{note}</p>')


def attribution_section(review):
    A = review.get("attribution") or {}
    n_closed = A.get("n_closed") or 0
    parts = []
    for title, key in (("By exit reason", "by_exit_reason"), ("By desk", "by_desk"),
                       ("By entry-score bucket", "by_score_bucket"), ("By setup", "by_setup")):
        tbl = A.get(key) or {}
        if not tbl:
            continue
        head = [(title.replace("By ", "").lower(), False), ("exits", True), ("round trips", True),
                ("P&L total", True), ("P&L mean", True), ("return mean", True),
                ("weighted", True), ("return median", True), ("sample", False)]
        rows = []
        for k, v in tbl.items():
            n = v.get("n") or 0
            rt = v.get("n_round_trips") or 0
            rows.append(f'<tr><td class="sym">{esc(k)}</td>'
                        + f'<td class="n">{n}</td><td class="n">{rt}</td>'
                        + _agg(v.get("pnl_total"), n, unit="$")
                        + _agg(v.get("pnl_mean"), n, unit="$")
                        + _agg(v.get("pnl_pct_mean"), n)
                        + _agg(v.get("pnl_pct_mean_notional_weighted"), n)
                        + _agg(v.get("pnl_pct_median"), n)
                        + _chip_td(rt) + "</tr>")
        parts.append(f'<div class="subh">{esc(title)}</div>' + _table(head, rows))
    if not parts:
        parts.append('<div class="empty">No closed trades in the window.</div>')
    else:
        rt = A.get("n_round_trips") or 0
        parts.append('<p class="pnote"><b>An exit row is a slice, not a trade.</b> '
                     f'{n_closed} row(s) cover {int(A.get("n_desk_positions") or 0)} '
                     f'desk-position(s) in {int(A.get("n_names") or 0)} name(s) — {rt} closed '
                     f'out, {int(A.get("n_positions_still_open") or 0)} still open; a '
                     'cap-rebalance shaving of a few dollars is one row and so is a finished '
                     'position. The sample chip counts the round trips that completed, not the '
                     'rows. <b>return mean</b> equal-weights the rows; <b>weighted</b> weights '
                     'each by the notional it put at risk, which is what the money did.</p>')
    unmatched = A.get("n_unmatched_to_a_decision") or 0
    note = (f"{unmatched} closed trade(s) not matched to a place-buy decision (score bucket unknown)"
            if unmatched else None)
    return _card("Attribution · closed trades", "".join(parts),
                 count=f"n={n_closed}{'' if n_closed >= MIN_N else ' · ' + NOT_A_SAMPLE}",
                 note=esc(note) if note else None)


def refusals_section(review):
    R = review.get("refusals") or {}
    by_rule = R.get("by_rule") or {}
    n = R.get("n") or 0
    sides = R.get("by_side") or {}
    body = [f'<div class="subh">By rule &middot; n={n}</div>',
            bars_svg(list(by_rule.items()), "Refusals by rule"),
            f'<div class="subh">By week &middot; n={n}</div>',
            stacked_weeks_svg(R.get("by_week") or {}, list(by_rule), "Refusals by week and rule"),
            f'<p class="pnote">Sides: book-wide gates n={int(sides.get("book", 0))} (no names — counted, '
            f'not measurable) · management-side n={int(sides.get("manage", 0))} · entry candidates '
            f'n={int(sides.get("entry", 0))} (the only ones the counterfactual can measure).</p>']
    if R.get("n_composite"):
        body.append(f'<p class="pnote">{int(R["n_composite"])} of those {n} items named more than '
                    f'one rule — portfolio.py joins a blocked proposal\'s reasons with "; ", so one '
                    f'skipped name can carry an evidence-coverage refusal AND a sector limit AND a '
                    f'cap trim. Each is counted, so the bars total {int(R.get("n_rules") or n)} '
                    f'rather than {n}: <b>n</b> is names turned away, the bars are how often each '
                    f'gate was a reason.</p>')
    if R.get("other"):
        body.append('<p class="pnote"><b>Unclassified (rule <code>other</code> — add a rule in report.py):</b> '
                    + " · ".join(esc(s) for s in R["other"]) + "</p>")
    return _card("Refusals", "".join(body), count=f"n={n}")


def counterfactual_section(review):
    C = review.get("counterfactual")
    if not C:
        return _card("E5 · did the names each gate refused underperform the names it admitted?",
                     '<div class="empty">Not run this week. <code>report.py --counterfactual --bars bars.json</code> measures it.</div>')
    if "refused" in C:
        return _card("E5 · counterfactual", f'<div class="empty">{esc(C["refused"])}</div>')
    parts = [f'<p class="pnote">Entry = {esc(C.get("entry"))}; forward return = the close h sessions after '
             f'the entry session over that entry; difference = per-date refused mean minus admitted mean, '
             f'block-bootstrap 90% interval (block = horizon). Admitted names in the window: '
             f'n={int(C.get("n_admitted") or 0)} ({int(C.get("n_admitted_with_bars") or 0)} with bars).</p>']
    rules = C.get("rules") or {}
    if not rules:
        parts.append('<div class="empty">No named entry refusals in the window — nothing to measure.</div>')
    for rule, tbl in rules.items():
        nb = tbl.get("n_refused_with_bars") or 0
        head = [("h", True), ("refused mean", True), ("admitted mean", True),
                ("diff mean [90% CI]", True), ("sample", False), ("reading", False)]
        rows = []
        for h in C.get("horizons") or []:
            t = tbl.get(str(h)) or {}
            r, a, d = t.get("refused") or {}, t.get("admitted") or {}, t.get("diff") or {}
            ci = d.get("ci90_pct")
            ci_s = (f' [{_fmt(ci[0], 2, True, "")}, {_fmt(ci[1], 2, True, "")}]' if ci else "")
            dm = _fmt(d.get("mean_pct"), 2, True, "%") if d.get("mean_pct") is not None else DASH
            nd = int(d.get("n_dates") or 0)
            least = min(int(r.get("n") or 0), int(a.get("n") or 0))
            rows.append(f'<tr><td class="n">{int(h)}</td>'
                        + _agg(r.get("mean_pct"), r.get("n"))
                        + _agg(a.get("mean_pct"), a.get("n"))
                        + f'<td class="n agg">{dm}{esc(ci_s)} <span class="nn">n={nd} dates</span></td>'
                        + _chip_td(least)
                        + f'<td class="sub2">{esc(t.get("verdict") or DASH)}</td></tr>')
        parts.append(f'<div class="subh">{esc(rule)} &middot; {int(tbl.get("n_refused") or 0)} refused '
                     f'name-dates, {nb} with bars{_sample_chip(nb)}</div>' + _table(head, rows))
    if C.get("unmeasured"):
        parts.append('<p class="pnote">Not measurable from the journal (no candidate names): '
                     + "; ".join(f'{esc(r)} book-wide n={int(v.get("book_wide", 0))}, management n={int(v.get("manage", 0))}'
                                 for r, v in sorted(C["unmeasured"].items())) + "</p>")
    if C.get("not_a_gate"):
        parts.append('<p class="pnote">Counted as reasons but not measurable as gates (they '
                     'refused nothing): ' + "; ".join(f'{esc(r)} n={int(v)}'
                                                      for r, v in sorted(C["not_a_gate"].items()))
                     + "</p>")
    if C.get("no_bars"):
        parts.append('<p class="pnote">No bars for: ' + esc(", ".join(C["no_bars"])) + "</p>")
    return _card("E5 · did the names each gate refused underperform the names it admitted?",
                 "".join(parts))


def shadow_section(review, health=None):
    S = review.get("shadow") or {}
    hrow = None
    for c in (health or {}).get("checks") or []:
        if c.get("name") == "shadow_gap":
            hrow = c
    if not S and not hrow:
        return _card("Shadow ledger · booked vs shadow fills",
                     '<div class="empty">No shadow block on any book or journal entry — the K-06 shadow fill model is off.</div>')
    head = [("desk", False), ("model", False), ("cumulative gap", True), ("gap this window", True),
            ("gap as share of realised", True), ("sample", False)]
    rows = []
    for desk, s in S.items():
        nf = int(s.get("n_fills") or 0)
        if s.get("model") == "on" and nf == 0:
            # On, but it has priced no fill yet. A $0.00 gap here is not a measured zero —
            # there is nothing to have measured — so every gap column says so.
            rows.append(f'<tr><td class="sym">{esc(desk)}</td>'
                        f'<td class="sub2">on · no fills priced</td>'
                        + _absent("no fills") + _absent("no fills") + _absent("no fills")
                        + f'<td class="chipcell">{_sample_chip(0)}</td></tr>')
            continue
        if s.get("model") == "off":
            rows.append(f'<tr><td class="sym">{esc(desk)}</td><td class="sub2">off</td>'
                        + _absent("model off") + _absent("model off") + _absent("model off")
                        + f'<td class="chipcell">{_sample_chip(0)} <span class="sub2">model off</span></td></tr>')
            continue
        rows.append(f'<tr><td class="sym">{esc(desk)}</td><td class="sub2">on</td>'
                    + _agg(s.get("cum_gap_usd"), nf, unit="$")
                    + _agg(s.get("window_gap_usd"), s.get("n_entries_with_shadow"), unit="$")
                    + _agg(s.get("gap_share_of_realized_pct"), nf)
                    + _chip_td(nf) + "</tr>")
    body = _table(head, rows) if rows else ""
    if hrow:
        body += (f'<p class="pnote">Health check <code>shadow_gap</code>: <b>{esc(str(hrow.get("status")).upper())}</b> '
                 f'— {esc(hrow.get("detail"))} (threshold {esc(hrow.get("threshold"))}).</p>')
    body += ('<p class="pnote">A positive gap means the paper book flattered itself: it sold higher or '
             'bought cheaper than the shadow model says a marketable order would have. n = shadow fills.</p>')
    return _card("Shadow ledger · booked vs shadow fills", body)


def coverage_section(review):
    """COVER-01 expected vs actual engine runs (report.py --coverage).

    The review's system-health section asks for this table and the runner prompt names
    pm-coverage.json as an input, but until 2026-09-11 neither script had a way to read it,
    so the table was transcribed by hand from the coverage doc's prose notes."""
    V = review.get("coverage")
    if not V:
        return _card("Coverage · expected vs actual engine runs",
                     '<div class="empty">No coverage doc staged '
                     '(<code>report.py --coverage claude/pm-coverage.json</code>). '
                     'Expected-versus-actual runs were not measured this week.</div>')
    T = V.get("totals") or {}
    days = V.get("days") or {}
    head = [("day", False), ("decision slots", True), ("sentinels", True), ("desk rows", True),
            ("missing", False), ("engine", False)]
    rows = []
    for date, d in days.items():
        miss = ", ".join(d.get("decision_slots_missing") or [])
        gap = int(d.get("sentinels_expected") or 0) - int(d.get("sentinels_present") or 0)
        if gap > 0:
            miss = (miss + "; " if miss else "") + f"{gap} sentinel(s)"
        for dk in d.get("desks_missing") or []:
            miss = (miss + "; " if miss else "") + f"no {dk} row"
        cls = "down" if (miss and not d.get("partial")) else ""
        rows.append(
            f'<tr><td class="sym">{esc(date)}'
            + ('<span class="sub2"> · partial</span>' if d.get("partial") else "")
            + "</td>"
            + f'<td class="n">{int(d.get("decision_slots_present") or 0)}/'
              f'{int(d.get("decision_slots_expected") or 0)}</td>'
            + f'<td class="n">{int(d.get("sentinels_present") or 0)}/'
              f'{int(d.get("sentinels_expected") or 0)}</td>'
            + f'<td class="n">{int(d.get("desk_rows") or 0)}</td>'
            + f'<td class="sub2 {cls}">{esc(miss) or DASH}</td>'
            + f'<td class="sub2">{esc(", ".join(d.get("engine_sha") or [])) or DASH}</td></tr>')
    body = _table(head, rows) if rows else '<div class="empty">The coverage doc records no days in the window.</div>'
    body += (f'<p class="pnote">Expected per trading day: '
             f'{len(V.get("decision_slots") or [])} decision slots '
             f'({esc(", ".join(V.get("decision_slots") or []))}) plus '
             f'{int(V.get("sentinels_per_day") or 0)} sentinels, each across every desk '
             f'(<code>runner/slots.json</code>). Over {int(T.get("days") or 0)} recorded day(s): '
             f'{int(T.get("present") or 0)}/{int(T.get("expected") or 0)} runs, '
             f'{int(T.get("missing") or 0)} missing, {int(T.get("aborted") or 0)} aborted, '
             f'{int(T.get("desk_rows") or 0)} desk rows written'
             + (f'; {int(T.get("partial_days") or 0)} day still being written is shown but left '
                'out of the totals' if T.get("partial_days") else "")
             + '. A missing row is a run that left no record — the desk was not looked at, or '
               'the run could not write. That is not the same as a quiet run, which writes a '
               'row saying nothing fired.</p>')
    return _card("Coverage · expected vs actual engine runs", body,
                 count=f'{int(T.get("present") or 0)}/{int(T.get("expected") or 0)}')


def _tile(label, value, sub=None, cls=""):
    return (f'<div class="tile"><div class="lab">{label}</div><div class="val {cls}">{value}</div>'
            + (f'<div class="delta">{sub}</div>' if sub else "") + "</div>")


def house_section(review):
    H = review.get("house")
    if not H:
        return _card("House exposure · latest state",
                     '<div class="empty">No journal entry in the window carries a <code>house</code> block '
                     '— the peer books were not staged, so combined exposure was not measured.</div>')
    X = H.get("exposure") or {}
    names_n = int(H.get("desks") or 0)
    ne = X.get("n_eff")
    basis = X.get("n_eff_basis") or "proxy"
    tiles = [
        _tile("Combined equity", render_pm.money(H.get("equity")), f'{names_n} desk(s) · n={names_n}'),
        _tile("N_eff", f'{ne:.2f}' if isinstance(ne, (int, float)) else DASH,
              f'{esc(basis)} · n={names_n} desks', "down" if isinstance(ne, (int, float)) and ne < 3 else ""),
        _tile("β·w", f'{X["beta_w"]:.2f}' if isinstance(X.get("beta_w"), (int, float)) else "n/a",
              "vs SPY when bars are staged"),
        _tile("Overlap", render_pm.pct(X.get("overlap_pct"), 0),
              f'{render_pm.pct(X.get("overlap_equity_pct"), 0)} of equity in shared names'),
        _tile("Largest sector", render_pm.pct(X.get("largest_sector_pct"), 0), esc(X.get("largest_sector") or DASH)),
    ]
    flags = X.get("flags") or []
    chips = "".join(render_pm.chip(f, "critical") for f in flags) or render_pm.chip("no flags", "good")
    blk = ("blocking new entries house-wide" if X.get("block_new_entries") else
           ("enforced" if X.get("enforce") else "reported, not enforced"))
    body = (f'<div class="rail">{"".join(tiles)}</div>'
            f'<p class="pnote">As of {esc(H.get("date"))} {esc(H.get("slot") or "")} ({esc(H.get("desk"))} run) '
            f'&middot; {chips} &middot; {esc(blk)}. N_eff is a sector proxy (ρ = 1 inside a sector) '
            f'unless labelled measured.</p>')
    return _card("House exposure · latest state", body)


def ledger_section(rows, ic_json=None):
    trials = [r for r in (rows or []) if r.get("kind", "trial") == "trial"]
    head = [("id", False), ("hypothesis", False), ("window", False), ("n", True),
            ("in-sample IC", True), ("out-of-sample", True), ("decision", False), ("trial #", True),
            ("sample", False)]
    out = []
    for t in trials:
        w = t.get("window") or {}
        ins = t.get("in_sample") or {}
        ic_s, ic_n = _in_sample_ic(ins, t.get("n"))
        oos = t.get("out_of_sample")
        oos_s = "not measured" if not oos else esc(json.dumps(oos)[:60])
        out.append(f'<tr><td class="sym">{esc(t.get("id"))}</td>'
                   f'<td class="sub2">{esc(t.get("hypothesis") or DASH)}</td>'
                   f'<td class="st sub2">{esc(w.get("start") or DASH)} &rarr; {esc(w.get("end") or DASH)}</td>'
                   f'<td class="n">{esc(t.get("n")) if t.get("n") is not None else DASH}</td>'
                   f'<td class="n agg">{ic_s} <span class="nn">n={ic_n if ic_n is not None else "unknown"}</span></td>'
                   f'<td class="n sub2">{oos_s}</td>'
                   f'<td class="sub2">{esc(t.get("decision") or "(none yet)")}</td>'
                   f'<td class="n">{esc(t.get("n_trials_to_date") if t.get("n_trials_to_date") is not None else DASH)}</td>'
                   + _chip_td(ic_n) + "</tr>")
    body = _table(head, out) if out else '<div class="empty">Empty ledger.</div>'
    k = len(trials)
    body += (f'<p class="pnote"><b>DSR reminder.</b> {k} trial(s) on the ledger; the next is number {k + 1}. '
             'The deflated-ratio correction (Bailey &amp; López de Prado 2014) needs this count and a '
             'per-trial series of risk-adjusted returns, and it has not been computed: with about two years '
             'of data, roughly seven tried configurations are enough for the best of them to look real '
             'from pure noise. Harvey, Liu &amp; Zhu (2016): t ≥ 3, on a hold-out. Nothing on this page '
             'has cleared either bar.</p>')
    if ic_json:
        body += _ic_block(ic_json)
    return _card("Experiment ledger", body, count=f"{k} trial(s)")


def _in_sample_ic(ins, n):
    """(html, n) for a ledger row's in_sample block: ic.py's horizon table when present,
    else the backfilled free-form spearman fields; longest horizon wins."""
    if not isinstance(ins, dict) or not ins:
        return DASH, None
    keys = sorted(ins, key=lambda k: -(int(re.sub(r"\D", "", str(k)) or 0)))
    for k in keys:
        v = ins[k]
        if isinstance(v, dict) and v.get("ic_mean") is not None:
            t = v.get("ic_tstat_nw")
            return (f'{esc(k)}d IC {_fmt(v["ic_mean"], 3, True, "")}'
                    + (f' t {_fmt(t, 2, True, "")}' if isinstance(t, (int, float)) else ""),
                    _n_or_none(v.get("n"), n))
        if isinstance(v, dict) and v.get("spearman") is not None:
            return f'{esc(k)} ρ {_fmt(v["spearman"], 3, True, "")}', _n_or_none(v.get("n"), n)
    for k in ("spearman_20d", "spearman_10d", "spearman_5d"):
        if isinstance(ins.get(k), (int, float)):
            return f'{esc(k[-3:])} ρ {_fmt(ins[k], 3, True, "")}', _n_or_none(n)
    return esc("; ".join(f"{k} {v}" for k, v in list(ins.items())[:2])), _n_or_none(n)


def _n_or_none(*cands):
    """The first usable n, else None — a backfilled slice with `n: null` says 'unknown',
    which is not a sample either, rather than inventing a zero."""
    for c in cands:
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            return int(c)
    return None


def _ic_block(ic_json):
    sc = ic_json.get("score") or {}
    if not sc:
        return ""
    head = [("h", True), ("IC mean", True), ("NW t", True), ("pooled ρ", True), ("spread", True),
            ("sample", False)]
    rows = []
    for h, t in sc.items():
        sp = t.get("spread") or {}
        ci = sp.get("ci90_pct")
        ci_s = f' [{_fmt(ci[0], 2, True, "")}, {_fmt(ci[1], 2, True, "")}]' if ci else ""
        nd = int(t.get("n_dates") or 0)
        rows.append(f'<tr><td class="n">{esc(h)}</td>'
                    + f'<td class="n agg">{_fmt(t.get("ic_mean"), 3, True, "")} <span class="nn">n={nd} dates</span></td>'
                    + f'<td class="n agg">{_fmt(t.get("ic_tstat_nw"), 2, True, "")} <span class="nn">n={nd} dates</span></td>'
                    + _agg(t.get("pooled_spearman"), t.get("n"), dp=3, unit="")
                    + f'<td class="n agg">{_fmt(sp.get("mean_pct"), 2, True, "%")}{esc(ci_s)} <span class="nn">n={int(sp.get("n_dates") or 0)} dates</span></td>'
                    + _chip_td(nd) + "</tr>")
    return (f'<div class="subh">This week\'s IC (ic.py) &middot; n={int(ic_json.get("n") or 0)} observations, '
            f'{int(ic_json.get("n_dates") or 0)} dates</div>' + _table(head, rows))


def model_card_section(fields):
    if not fields:
        return _card("Model card", '<div class="empty">No model card staged (<code>--model-card docs/model-card.md</code>).</div>')
    rows = "".join(f'<div class="kv"><dt>{esc(k.replace("_", " "))}</dt><dd>{esc(v)}</dd></div>'
                   for k, v in fields.items())
    return _card("Model card", f'<div class="kvgrid">{rows}</div>',
                 note=esc(f'{fields.get("card", "")} · {fields.get("date", "")}'.strip(" ·")))


EXTRA_CSS = """
.bars { display: block; width: 100%; height: auto; max-width: 760px; }
.bars .bar { fill: var(--accent); }
.bars .axis { fill: var(--ink-2); font-family: var(--mono); font-size: 11px; }
.bars .axis.val { fill: var(--ink-3); }
.seg.s1 { fill: var(--accent); } .seg.s2 { fill: #7a5bd6; } .seg.s3 { fill: #d0873b; }
.seg.s4 { fill: #2b9c8f; } .seg.s5 { fill: #c25d8a; } .seg.s6 { fill: #8a9a2e; } .seg.s7 { fill: var(--ink-3); }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.sw.s1 { background: var(--accent); } .sw.s2 { background: #7a5bd6; } .sw.s3 { background: #d0873b; }
.sw.s4 { background: #2b9c8f; } .sw.s5 { background: #c25d8a; } .sw.s6 { background: #8a9a2e; } .sw.s7 { background: var(--ink-3); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; padding: 6px 16px 12px; font-size: 11.5px; color: var(--ink-2); }
.subh { font-size: 11px; letter-spacing: .07em; text-transform: uppercase; color: var(--ink-2);
        font-weight: 600; padding: 14px 16px 6px; }
.pnote { margin: 0; padding: 10px 16px 14px; font-size: 12px; color: var(--ink-3); line-height: 1.6; max-width: 96ch; }
.pnote b { color: var(--ink-2); }
.pnote code, .empty code { font-family: var(--mono); font-size: 11px; background: var(--surface-2); padding: 1px 4px; border-radius: 3px; }
.nn { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); margin-left: 4px; white-space: nowrap; }
td.agg { white-space: nowrap; }
td.absent { white-space: nowrap; color: var(--ink-3); }
td.chipcell { white-space: nowrap; }
.mast { gap: 10px 16px; }
.kvgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1px;
          background: var(--rule-solid); }
.kv { background: var(--surface); padding: 10px 14px; display: flex; flex-direction: column; gap: 3px; margin: 0; }
.kv dt { font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-3); font-weight: 600; }
.kv dd { margin: 0; font-size: 12.5px; color: var(--ink); line-height: 1.5; }
.card .rail { margin: 0; border: 0; border-radius: 0; box-shadow: none; }
.honesty { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 14px; }
"""


# ---------------------------------------------------------------- the page
def render(review_json, ledger_rows, ic_json=None, model_card=None, health=None,
           generated_at=None):
    """The page. `model_card` is the text of docs/model-card.md (or an already-parsed
    fields dict). Raises HonestyError rather than publish a page that names a win rate or
    a Sharpe — the check runs on the finished HTML, inputs included."""
    review = review_json or {}
    if isinstance(model_card, dict):
        fields = model_card
    else:
        fields, _ = parse_model_card(model_card)
    gen = generated_at or review.get("generated_at") or \
        dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    w = review.get("window") or {}
    week = _week_of(w.get("last")) or "review"

    honesty = ('<div class="honesty">'
               + render_pm.chip("paper record", "warning")
               + render_pm.chip("n= on every aggregate", "accent")
               + render_pm.chip(f"under {MIN_N}: {NOT_A_SAMPLE}", "mute")
               + render_pm.chip("no hit-rate, no risk-adjusted ratio", "mute")
               + "</div>")

    sections = [
        benchmark_section(review),
        attribution_section(review),
        refusals_section(review),
        counterfactual_section(review),
        coverage_section(review),
        shadow_section(review, health),
        house_section(review),
        ledger_section(ledger_rows, ic_json),
        model_card_section(fields),
    ]
    foot = ('<div class="foot">'
            '<p><b>Reading it.</b> A gate whose refused names underperformed the admitted ones, with an '
            'interval clear of zero and n past 30 on both sides, is doing its job; one whose refused names '
            'outperformed is costing return and should be argued about, not loosened by reflex. Everything '
            'here describes the paper record and forecasts nothing.</p>'
            '<p><b>What this page refuses.</b> It prints no hit-rate and no risk-adjusted ratio at this size; '
            'every aggregate carries its n; anything under 30 is marked <i>not a sample</i>. The renderer '
            'checks its own output for those words and exits rather than publish them.</p>'
            '<p>Not investment advice. Paper, absolute: nothing on this page placed an order.</p></div>')

    doc = (f"<title>Weekly review {esc(week)}</title>\n<style>{render_pm.CSS}{EXTRA_CSS}</style>\n"
           f'<div class="wrap">{masthead(review, gen)}{honesty}{"".join(sections)}{foot}</div>')
    bad = honesty_violations(doc)
    if bad:
        raise HonestyError("REFUSED: the page would carry " + ", ".join(repr(b) for b in bad)
                           + " — the weekly review prints neither. Fix the input that names it.")
    return doc


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--review", required=True, help="report.py --json output")
    ap.add_argument("--ledger", required=True, help="experiments/ledger.jsonl")
    ap.add_argument("--ic", default=None, help="ic.py --json output for the week's archive")
    ap.add_argument("--model-card", default=None, help="docs/model-card.md")
    ap.add_argument("--health", default=None, help="health.py --json output")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        review = _load(a.review)
        rows = ledger_mod.trials(a.ledger)
        ic_json = _load(a.ic) if a.ic else None
        health = _load(a.health) if a.health else None
        card = None
        if a.model_card:
            with open(a.model_card, encoding="utf-8") as f:
                card = f.read()
    except (OSError, ValueError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    try:
        doc = render(review, rows, ic_json=ic_json, model_card=card, health=health)
    except HonestyError as e:
        print(str(e), file=sys.stderr)
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"wrote {len(doc):,} bytes -> {a.out}")
    print("PUBLISH: project_write claude/reviews/weekly-<date>.html from that file "
          "(Artifact(file_path=...) for a board).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
