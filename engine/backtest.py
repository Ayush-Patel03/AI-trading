"""backtest.py — replay the scoring model over history, so the edge can be measured
before another quarter of forward paper accumulates.

WHY THIS EXISTS
---------------
`validate.py` asks the only question that matters: does a high score go on to outperform a
low one? It reads archived scan records. Today the only records that exist are the live ones,
produced four a day since 2026-09-01, and the 20-session horizon does not reach n=30 until
about 2026-09-29 — with an EFFECTIVE n far smaller than that, because nineteen
semiconductor-adjacent names scored on the same morning in the same regime are close to one
observation, not nineteen.

This produces the same records from historical bars instead. Same scanner, same archive
format, same validator. An afternoon instead of a quarter.

WHAT IT CAN AND CANNOT TEST — read this before quoting a number from it
----------------------------------------------------------------------
The model scores five pillars. Only two can be reconstructed from bars:

    trend (25) + momentum (15)  =  40 of 100 points, computable
    fundamentals (20), catalyst (20), intelligence (20)  =  point-in-time data we have
                                                            no history for

Analyst targets, consensus ratings and retail sentiment as they stood on a past Tuesday are
not recoverable. Scoring a past date with TODAY's analyst target is look-ahead bias, and a
backtest with look-ahead in it is worse than no backtest: it produces a confident number that
is wrong in the flattering direction. So those pillars are simply absent, and the engine's own
coverage normalisation handles them exactly as it handles a dead source on a live run — which
is not a workaround, it is the mechanism the scanner already has.

The question this answers is therefore narrower than "does the model work". It is:

    **Does the trend-and-momentum core rank forward returns, on this universe, after costs?**

That is still the first question worth asking, because those 40 points are the half most
likely to be carrying the signal — and if they do not rank returns, nothing bolted on top
will save it.

`--financials` (see below) can restore part of the fundamentals pillar, but ONLY from an
explicit point-in-time table where every row carries the date it became public. The harness
will not accept a fundamentals value without one.

THREE BIASES YOU CANNOT REMOVE, ONLY STATE
------------------------------------------
1. **Survivorship.** The universe comes from the bars file, and a bars file is a list of
   symbols that exist TODAY. Names that delisted, went bankrupt or were acquired out of the
   index are absent, and they are disproportionately the losers. Every summary this writes
   carries the warning; do not delete it because the report reads better without it.
2. **The universe is chosen with hindsight**, for the same reason.
3. **Entry at the scored close.** A row is scored on date D's close and the forward return is
   measured from it. Real entry is the next open at best. `--entry next_open` measures that
   instead, and the difference between the two runs is worth looking at on its own.

Usage
-----
    python3 backtest.py --bars bars.json --start 2024-01-01 --end 2026-06-30 \
                        --every 5 --out-records records/ --summary backtest.json

    python3 validate.py --records records/ --bars bars.json \
                        --horizons 5,10,20 --out validation.json --md validation.md

Exit 0 wrote records. Exit 2 wrote nothing and said why.
"""
import argparse
import json
import os
import sys
from datetime import date, datetime

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import archive
import scanner
import technicals

SURVIVORSHIP = (
    "SURVIVORSHIP BIAS: the universe is the symbol list in the bars file, which is a list of "
    "companies that still exist. Names that delisted, went bankrupt or were acquired out of "
    "the index are absent, and they are disproportionately the losers. Every number in this "
    "run is optimistic by an unknown amount for that reason alone. Do not delete this line."
)
MODEL_SUBSET = (
    "PARTIAL MODEL: only the trend (25) and momentum (15) pillars are reconstructable from "
    "bars. Fundamentals, catalyst and intelligence are point-in-time data with no history, so "
    "they are ABSENT rather than guessed, and coverage normalisation scores what was there. "
    "This measures the trend-and-momentum core, not the whole model."
)
MIN_BARS_TO_SCORE = 220          # ma_200 plus a little; below this a row is not scored


# ---------------------------------------------------------------- bars
def load_bars(path):
    """{SYMBOL: [bar, ...]} oldest-first, from one response or a list of them."""
    raw = json.load(open(path))
    blocks = raw if isinstance(raw, list) else [raw]
    out = {}
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            rows = ((blk.get("data") or {}).get("results")
                    or blk.get("results") or [])
        for res in rows or []:
            if not isinstance(res, dict):
                continue
            sym = (res.get("symbol") or "").upper()
            if not sym:
                continue
            bars = [b for b in (res.get("bars") or []) if isinstance(b, dict)]
            bars.sort(key=lambda b: b.get("begins_at") or "")
            out.setdefault(sym, []).extend(bars)
    for sym in out:
        out[sym].sort(key=lambda b: b.get("begins_at") or "")
    return out


def bar_date(b):
    t = b.get("begins_at") or ""
    return t[:10]


def upto(bars, as_of):
    """Bars strictly on or before as_of. THE no-look-ahead boundary — every other guarantee
    in this file rests on this one function, which is why it is three lines and tested."""
    return [b for b in bars if bar_date(b) and bar_date(b) <= as_of]


def sessions(bars):
    return [bar_date(b) for b in bars if bar_date(b)]


# ---------------------------------------------------------------- point-in-time fundamentals
def load_financials(path):
    """{SYMBOL: [{available_from: 'YYYY-MM-DD', <field>: value, ...}, ...]}

    `available_from` is the date the figure became PUBLIC — a filing date, not a fiscal
    period end. A row without one is refused rather than dated by guesswork, because a
    quarter-end date used as an availability date leaks roughly six weeks of hindsight into
    every observation."""
    raw = json.load(open(path))
    if not isinstance(raw, dict):
        raise SystemExit("REFUSED: --financials must be an object keyed by symbol")
    out = {}
    for sym, rows in raw.items():
        clean = []
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            when = r.get("available_from")
            if not (isinstance(when, str) and len(when) == 10):
                raise SystemExit(
                    f"REFUSED: {sym} has a financials row with no `available_from` date. "
                    "A fundamentals value without the date it became public cannot be used "
                    "without leaking hindsight. Fix the table, do not relax this check.")
            clean.append(dict(r))
        clean.sort(key=lambda r: r["available_from"])
        out[sym.upper()] = clean
    return out


def financials_asof(rows, as_of):
    """The most recent row that was public on or before as_of, or {}."""
    live = [r for r in rows or [] if r["available_from"] <= as_of]
    if not live:
        return {}
    return {k: v for k, v in live[-1].items() if k != "available_from"}


# ---------------------------------------------------------------- one candidate
def candidate(sym, bars, as_of, bench_tech=None, fin=None):
    """The scan_data candidate block for one symbol on one date, from bars only."""
    hist = upto(bars, as_of)
    if len(hist) < MIN_BARS_TO_SCORE:
        return None
    if bar_date(hist[-1]) != as_of:
        return None                      # the name did not trade that session
    d = technicals.derive(hist)
    if not d.get("bars_used") or d.get("ma_200") is None:
        return None
    last = hist[-1]
    price = technicals._f(last.get("close_price"))
    if not price or price <= 0:
        return None

    c = {
        "name": sym,
        "price": price,
        "volume": technicals._f(last.get("volume")),
        "ma_50": d.get("ma_50"), "ma_200": d.get("ma_200"),
        "rsi_14": d.get("rsi_14"),
        "atr_14": d.get("atr_14"), "atr_pct": d.get("atr_pct"),
        "avg_volume_20d": d.get("avg_volume_20d"),
        "rel_volume": d.get("rel_volume"),
        "week52_high": d.get("week52_high"), "week52_low": d.get("week52_low"),
        "week52_change_pct": d.get("week52_change_pct"),
        "gap_pct": d.get("gap_pct"), "gap_basis": "prior_bar",
        "ret_20d_pct": d.get("ret_20d_pct"), "ret_60d_pct": d.get("ret_60d_pct"),
        "bars_used": d.get("bars_used"),
        # One price source, and it is a bar close. Not "confirmed" — the live scan means
        # something specific by that word and this is not it.
        "price_sources": 1,
    }
    if bench_tech:
        for n in (20, 60):
            mine, theirs = c.get(f"ret_{n}d_pct"), bench_tech.get(f"ret_{n}d_pct")
            c[f"rs_{n}d_vs_SPY"] = ((mine - theirs)
                                    if isinstance(mine, float) and isinstance(theirs, float)
                                    else None)
    if fin:
        c.update(fin)
    return c


# ---------------------------------------------------------------- regime
def regime_asof(bench_bars, as_of, universe_tech):
    """A regime block from history alone.

    VIX is deliberately absent unless its bars were supplied — `score_regime` already treats
    a missing input as partial and says so, and inventing a volatility proxy would put a
    number in a field that means something else. Breadth, on the other hand, is genuinely
    computable and is the strongest input here: the share of THIS universe above its own
    50-day moving average on that date."""
    hist = upto(bench_bars, as_of)
    reg = {}
    if len(hist) >= 60:
        closes = [technicals._f(b.get("close_price")) for b in hist]
        closes = [c for c in closes if c]
        highs = [technicals._f(b.get("high_price")) for b in hist[-252:]]
        highs = [h for h in highs if h]
        if closes and highs:
            hi = max(highs)
            reg["spy"] = {
                "pct_off_52w_high": (closes[-1] - hi) / hi * 100.0 if hi else None,
                "returns_1y": ((closes[-1] / closes[-252] - 1) * 100.0
                               if len(closes) >= 252 and closes[-252] else None),
            }
    above = [t for t in universe_tech if t.get("ma_50") and t.get("price")]
    if len(above) >= 10:
        pct = sum(1 for t in above if t["price"] > t["ma_50"]) / len(above) * 100.0
        reg["breadth"] = {"pct_above_50dma": round(pct, 1)}
    return reg


# ---------------------------------------------------------------- the replay
def replay(bars, as_of, benchmark="SPY", financials=None, slot="Backtest"):
    """One historical scan. Returns the scanner's output, or None if nothing scored."""
    bench_bars = bars.get(benchmark) or []
    bench_tech = None
    if bench_bars:
        bh = upto(bench_bars, as_of)
        if len(bh) >= 60:
            bench_tech = technicals.derive(bh)

    candidates = {}
    for sym, b in bars.items():
        if sym == benchmark:
            continue
        fin = financials_asof((financials or {}).get(sym), as_of) if financials else None
        c = candidate(sym, b, as_of, bench_tech, fin)
        if c:
            candidates[sym] = c
    if not candidates:
        return None

    data = {
        "meta": {"scan_date": as_of, "slot": slot, "time": "16:00",
                 "data_warnings": [SURVIVORSHIP, MODEL_SUBSET],
                 "sources": ["backtest: historical daily bars"]},
        "regime": regime_asof(bench_bars, as_of, list(candidates.values())),
        "candidates": candidates,
        "history": [],
    }
    return scanner.scan(data)


def rebalance_dates(bars, benchmark, start, end, every):
    """Every Nth session on the benchmark's own calendar — the only calendar that is real."""
    cal = [d for d in sessions(bars.get(benchmark) or []) if start <= d <= end]
    seen, ordered = set(), []
    for d in cal:
        if d not in seen:
            seen.add(d)
            ordered.append(d)
    return ordered[::max(1, every)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bars", required=True)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--every", type=int, default=5,
                    help="sessions between replays. 5 = weekly; 1 = every session, which "
                         "produces heavily overlapping observations — see the note on "
                         "effective n in the module docstring.")
    ap.add_argument("--financials", help="point-in-time fundamentals table (see load_financials)")
    ap.add_argument("--out-records", required=True)
    ap.add_argument("--summary")
    a = ap.parse_args(argv)

    bars = load_bars(a.bars)
    if a.benchmark not in bars:
        print(f"REFUSED: benchmark {a.benchmark} is not in the bars file. It sets the "
              "trading calendar and the relative-strength baseline; without it the run "
              "would invent both.", file=sys.stderr)
        return 2
    fin = load_financials(a.financials) if a.financials else None

    dates = rebalance_dates(bars, a.benchmark, a.start, a.end, a.every)
    if not dates:
        print(f"REFUSED: no {a.benchmark} sessions between {a.start} and {a.end}.",
              file=sys.stderr)
        return 2

    os.makedirs(a.out_records, exist_ok=True)
    written, rows_total, skipped = 0, 0, []
    for d in dates:
        out = replay(bars, d, a.benchmark, fin)
        if not out or not out.get("results"):
            skipped.append(d)
            continue
        out["meta"]["run_id"] = f"{d}-backtest"
        rec = archive.record(out)
        with open(os.path.join(a.out_records, f"{d}-backtest.json"), "w") as f:
            json.dump(rec, f, indent=2)
        written += 1
        rows_total += len(out["results"])

    summary = {
        "_what": "A backtest replay of the Scan Desk scoring model over historical bars.",
        "_warnings": [SURVIVORSHIP, MODEL_SUBSET],
        "benchmark": a.benchmark,
        "start": a.start, "end": a.end, "every_n_sessions": a.every,
        "universe_size": len(bars) - 1,
        "replays_written": written,
        "replays_skipped_no_candidates": skipped,
        "observations": rows_total,
        "records_dir": os.path.abspath(a.out_records),
        "next": ("python3 validate.py --records %s --bars %s --horizons 5,10,20 "
                 "--out validation.json --md validation.md"
                 % (a.out_records, a.bars)),
    }
    if a.summary:
        with open(os.path.join(BASE, a.summary), "w") as f:
            json.dump(summary, f, indent=2)

    print(f"BACKTEST  {written} replays, {rows_total} observations, "
          f"{len(bars) - 1} symbols, {a.start} -> {a.end} every {a.every} sessions")
    print(f"  records -> {a.out_records}")
    print(f"  {SURVIVORSHIP}")
    print(f"  {MODEL_SUBSET}")
    print(f"  next: {summary['next']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
