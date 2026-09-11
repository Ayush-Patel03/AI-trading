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
   `--universe-history <json>` (from `universe_history.py`) REDUCES it: each replay date
   then scores only the names that were index members on that date, removed names
   included — provided the bars file has their bars. It does not remove it, and the
   summary, every record and the ledger row say which of the two was run.
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

THE TRIAL LEDGER
----------------
    python3 backtest.py ... --ledger [experiments/ledger.jsonl] \\
                        --experiment-id E10 --hypothesis "..." [--config-diff '{...}']

With `--ledger`, the run computes the per-date rank IC of what it just wrote (`ic.py`) and
appends one row to the experiment ledger (`ledger.py`) with the in-sample metrics and the
trial counter, then prints "trial N of the ledger". Without it the run is NOT counted and
says so — every configuration tried on the same data spends evidence whether or not it was
written down, so write it down. docs/BACKTEST.md, "Trial ledger and IC".

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
import ic
import ledger
import scanner
import filings
import technicals
import universe_history

SURVIVORSHIP = (
    "SURVIVORSHIP BIAS: the universe is the symbol list in the bars file, which is a list of "
    "companies that still exist. Names that delisted, went bankrupt or were acquired out of "
    "the index are absent, and they are disproportionately the losers. Every number in this "
    "run is optimistic by an unknown amount for that reason alone. Do not delete this line."
)
SURVIVORSHIP_WITH_HISTORY = (
    "SURVIVORSHIP, REDUCED NOT REMOVED: each replay date scores only the names that were "
    "index members on that date (--universe-history), so names removed later are in for the "
    "dates they were members — IF the bars file has their bars. A member with no bars is "
    "silently absent, which is the old bias in miniature; the ledger row and the summary "
    "carry the file's own bias statement. Do not delete this line."
)
MODEL_SUBSET = (
    "PARTIAL MODEL: only the trend (25) and momentum (15) pillars are reconstructable from "
    "bars. Fundamentals, catalyst and intelligence are point-in-time data with no history, so "
    "they are ABSENT rather than guessed, and coverage normalisation scores what was there. "
    "This measures the trend-and-momentum core, not the whole model."
)
TURNOVER_STATIC = (
    "TURNOVER IS APPROXIMATE: --shares-outstanding is today's share count applied to every "
    "replay date, so turnover_20d carries a small look-ahead (buybacks and issuance since "
    "the date). Rank it, do not quote its level."
)
MIN_BARS_TO_SCORE = 220          # ma_200 plus a little; below this a row is not scored


def load_sector_map(path):
    """{SYMBOL: SECTOR_ETF}, upper-cased; {} without a path."""
    if not path:
        return {}
    raw = json.load(open(path, encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("REFUSED: --sector-map must be a {SYMBOL: ETF} object")
    return {str(k).upper(): str(v).upper() for k, v in raw.items() if v}


def load_shares(path):
    """{SYMBOL: float shares outstanding}; {} without a path."""
    if not path:
        return {}
    raw = json.load(open(path, encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("REFUSED: --shares-outstanding must be a {SYMBOL: shares} object")
    out = {}
    for k, v in raw.items():
        fv = technicals._f(v)
        if fv and fv > 0:
            out[str(k).upper()] = fv
    return out


# ---------------------------------------------------------------- bars
def load_bars(path):
    """{SYMBOL: [bar, ...]} oldest-first, from one response or a list of them."""
    raw = json.load(open(path, encoding="utf-8"))
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
    raw = json.load(open(path, encoding="utf-8"))
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
def candidate(sym, bars, as_of, bench_tech=None, fin=None, bench_hist=None,
              sector_hist=None, shares_outstanding=None):
    """The scan_data candidate block for one symbol on one date, from bars only.

    `bench_hist` / `sector_hist` are the benchmark's and the sector ETF's bars ALREADY cut
    to as_of by the caller; the research features (technicals.features) are computed from
    `hist` and those, so the no-look-ahead boundary is the same `upto` as everything else."""
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
    feats = technicals.features(hist, spy_bars=bench_hist, sector_bars=sector_hist,
                                shares_outstanding=shares_outstanding)

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
        # S-04: logged on every record for ic.py --by-feature; scanner.py scores none of it.
        "features": feats,
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


# ---------------------------------------------------------------- point-in-time universe
def allowed_on(membership, as_of):
    """The bars-file keys that were index members on as_of: the raw Wikipedia ticker and
    its normalised form (BRK.B and BRK-B) both match, so the membership file does not have
    to know how the bars source spells a share class."""
    mem = universe_history.members_on(membership, as_of)
    out = set(mem)
    out.update(universe_history.normalize_ticker(t) for t in mem)
    return out


# ---------------------------------------------------------------- the replay
def replay(bars, as_of, benchmark="SPY", financials=None, slot="Backtest", allowed=None,
           universe_bias=None, sector_map=None, shares_outstanding=None, filings_signal=None):
    """One historical scan. Returns the scanner's output, or None if nothing scored.

    `allowed` is the set of symbols that were index members on as_of (see `allowed_on`);
    None means the whole bars file, which is the survivor universe and is labelled as such.
    `sector_map` is {SYMBOL: SECTOR_ETF}; the ETFs named in it are benchmarks for the
    industry features and are NOT scored as candidates. `shares_outstanding` is
    {SYMBOL: float} for turnover_20d — a static snapshot, so treat that one feature as
    approximate (see --shares-outstanding). `filings_signal` is the parsed filings file
    (E16; `filings.load_staged` shape, ideally the per-symbol HISTORY form): laid over the
    rows point-in-time — a filing dated after as_of is null on that date."""
    bench_bars = bars.get(benchmark) or []
    bench_tech, bench_hist = None, None
    if bench_bars:
        bh = upto(bench_bars, as_of)
        if len(bh) >= 60:
            bench_tech = technicals.derive(bh)
            bench_hist = bh
    sector_map = sector_map or {}
    etfs = set(sector_map.values())
    sector_hist = {}
    for etf in etfs:
        if bars.get(etf):
            sector_hist[etf] = upto(bars[etf], as_of)

    candidates = {}
    for sym, b in bars.items():
        if sym == benchmark or sym in etfs:
            continue
        if allowed is not None and sym not in allowed:
            continue                     # not a member that day: not scored that day
        fin = financials_asof((financials or {}).get(sym), as_of) if financials else None
        c = candidate(sym, b, as_of, bench_tech, fin, bench_hist=bench_hist,
                      sector_hist=sector_hist.get(sector_map.get(sym)),
                      shares_outstanding=(shares_outstanding or {}).get(sym))
        if c:
            candidates[sym] = c
    if not candidates:
        return None

    warnings = [SURVIVORSHIP if allowed is None else SURVIVORSHIP_WITH_HISTORY, MODEL_SUBSET]
    if universe_bias:
        warnings.append(universe_bias)
    data = {
        "meta": {"scan_date": as_of, "slot": slot, "time": "16:00",
                 "data_warnings": warnings,
                 "sources": ["backtest: historical daily bars"]
                            + (["universe: point-in-time index membership"]
                               if allowed is not None else [])},
        "regime": regime_asof(bench_bars, as_of, list(candidates.values())),
        "candidates": candidates,
        "history": [],
    }
    return scanner.scan(data, filings_signal=filings_signal)


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
    ap.add_argument("--universe-history", dest="universe_history", metavar="JSON",
                    help="a universe_history.py file: each replay date scores only the names "
                         "that were index members on that date. Absent = the whole bars file, "
                         "which is a survivor universe and is labelled as such.")
    ap.add_argument("--sector-map", dest="sector_map", metavar="JSON",
                    help="{SYMBOL: SECTOR_ETF} for the industry features (S-04). Each ETF "
                         "must have bars in the file; the ETFs are benchmarks, not "
                         "candidates, and are not scored. Absent = those features null.")
    ap.add_argument("--shares-outstanding", dest="shares_outstanding", metavar="JSON",
                    help="{SYMBOL: shares} for turnover_20d. A STATIC snapshot (today's "
                         "share count applied to every date), so the feature is approximate "
                         "and every record says so. Absent = turnover_20d null.")
    ap.add_argument("--filings", metavar="JSON",
                    help="filings_signal.json (E16): {SYMBOL: [rows...]} history preferred; laid "
                         "over every replay date point-in-time (a filing is null before its "
                         "filing date). Features only — the score does not read it")
    ap.add_argument("--out-records", required=True)
    ap.add_argument("--summary")
    # The trial ledger. `--ledger` alone uses the default path; omitted, the run is not
    # counted and says so out loud.
    ap.add_argument("--ledger", nargs="?", const=ledger.DEFAULT_PATH, default=None,
                    metavar="FILE",
                    help=f"append this run to the experiment ledger (default {ledger.DEFAULT_PATH})")
    ap.add_argument("--experiment-id", help="ledger id, e.g. E10; auto X-<date>-<n> if omitted")
    ap.add_argument("--hypothesis", help="one sentence: what this trial claims")
    ap.add_argument("--config-diff", help="what differs from the incumbent (JSON or free text)")
    ap.add_argument("--horizons", default="5,10,20",
                    help="forward horizons in sessions for the ledger's IC summary")
    a = ap.parse_args(argv)

    bars = load_bars(a.bars)
    if a.benchmark not in bars:
        print(f"REFUSED: benchmark {a.benchmark} is not in the bars file. It sets the "
              "trading calendar and the relative-strength baseline; without it the run "
              "would invent both.", file=sys.stderr)
        return 2
    fin = load_financials(a.financials) if a.financials else None
    sector_map, shares_out = load_sector_map(a.sector_map), load_shares(a.shares_outstanding)
    missing_etfs = sorted(e for e in set(sector_map.values()) if e not in bars)
    if missing_etfs:
        print(f"note: sector ETF(s) named in --sector-map but not in the bars file: "
              f"{', '.join(missing_etfs)} — their industry features will be null",
              file=sys.stderr)

    dates = rebalance_dates(bars, a.benchmark, a.start, a.end, a.every)
    if not dates:
        print(f"REFUSED: no {a.benchmark} sessions between {a.start} and {a.end}.",
              file=sys.stderr)
        return 2

    uni = universe_block(a.universe_history, bars, a.benchmark, a.start, a.end)
    membership = uni.pop("_membership", None)
    filings_signal = None
    if a.filings:
        filings_signal = filings.load_file(a.filings)
        if filings_signal is None:
            print(f"REFUSED: --filings {a.filings} is missing or unreadable.", file=sys.stderr)
            return 2

    os.makedirs(a.out_records, exist_ok=True)
    written, rows_total, skipped = 0, 0, []
    for d in dates:
        allowed = allowed_on(membership, d) if membership is not None else None
        out = replay(bars, d, a.benchmark, fin, allowed=allowed,
                     universe_bias=uni.get("bias"), sector_map=sector_map,
                     shares_outstanding=shares_out, filings_signal=filings_signal)
        if not out or not out.get("results"):
            skipped.append(d)
            continue
        out["meta"]["run_id"] = f"{d}-backtest"
        rec = archive.record(out)
        with open(os.path.join(a.out_records, f"{d}-backtest.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
        written += 1
        rows_total += len(out["results"])

    summary = {
        "_what": "A backtest replay of the Scan Desk scoring model over historical bars.",
        "_warnings": [SURVIVORSHIP if membership is None else SURVIVORSHIP_WITH_HISTORY,
                      MODEL_SUBSET] + ([uni["bias"]] if uni.get("bias") else [])
                     + ([TURNOVER_STATIC] if shares_out else []),
        "benchmark": a.benchmark,
        "features": {"keys": list(technicals.FEATURE_KEYS),
                     "sector_map": bool(sector_map), "n_sector_etfs": len(set(sector_map.values())),
                     "shares_outstanding": bool(shares_out),
                     "note": "logged on every record for ic.py --by-feature; scored by nothing"},
        "start": a.start, "end": a.end, "every_n_sessions": a.every,
        "universe_size": len(bars) - 1,
        "universe": uni,
        "replays_written": written,
        "replays_skipped_no_candidates": skipped,
        "observations": rows_total,
        "records_dir": os.path.abspath(a.out_records),
        "next": ("python3 validate.py --records %s --bars %s --horizons 5,10,20 "
                 "--out validation.json --md validation.md"
                 % (a.out_records, a.bars)),
    }
    if a.summary:
        with open(os.path.join(BASE, a.summary), "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    print(f"BACKTEST  {written} replays, {rows_total} observations, "
          f"{len(bars) - 1} symbols, {a.start} -> {a.end} every {a.every} sessions")
    print(f"  records -> {a.out_records}")
    if membership is not None:
        print(f"  universe {uni['name']}: {uni['n_symbols_on_start']} members on {a.start}, "
              f"{uni['n_symbols_on_end']} on {a.end}, {uni['n_ever']} ever; "
              f"{uni['n_members_without_bars']} member(s) have no bars in the file")
    for w in summary["_warnings"]:
        print(f"  {w}")
    print(f"  next: {summary['next']}")

    if a.ledger:
        row = ledger_row(a, argv, written, rows_total, len(bars) - 1, uni)
        row = ledger.append(a.ledger, row)
        print(f"  LEDGER  trial {row['n_trials_to_date']} of the ledger ({a.ledger}) "
              f"-> {row['id']}")
        for h, m in (row["in_sample"] or {}).items():
            print(f"    {h:>3}d  IC {m.get('ic_mean')}  t(NW) {m.get('ic_tstat_nw')}  "
                  f"pooled rho {m.get('pooled_spearman')}  spread {m.get('spread_pct')}%")
    else:
        print("  LEDGER  not counted: no --ledger given. This trial spent evidence anyway; "
              "re-run with --ledger to record it.")
    return 0


def universe_block(path, bars, benchmark, start, end):
    """The `universe` block for the summary and the ledger row. Without a history file it
    names the bars file, which is all the old row ever said. With one it carries the
    point-in-time counts, the file's bias statement, and — the number that decides whether
    the file did any good — how many members have no bars to be scored on."""
    if not path:
        return {"name": None, "n_symbols": len(bars) - 1, "point_in_time": False}
    doc = universe_history.load(path)
    mem = doc["membership"]
    on_start, on_end = allowed_on(mem, start), allowed_on(mem, end)
    ever = set(mem) | {universe_history.normalize_ticker(t) for t in mem}
    have = set(bars) - {benchmark}
    # a member spelled either way counts as covered
    covered = {t for t in mem if t in have or universe_history.normalize_ticker(t) in have}
    bias = universe_history.bias_statement(mem, start, end, doc.get("unparseable_rows") or 0,
                                           doc.get("index") or "index", doc.get("notes"))
    return {
        "name": doc.get("name") or f"{doc.get('index', 'index')}-history",
        "file": os.path.basename(path),
        "fetched_at": doc.get("fetched_at"),
        "point_in_time": True,
        "n_symbols": len(bars) - 1,
        "n_symbols_on_start": len(universe_history.members_on(mem, start)),
        "n_symbols_on_end": len(universe_history.members_on(mem, end)),
        "n_ever": len(mem),
        "n_members_with_bars": len(covered),
        "n_members_without_bars": len(mem) - len(covered),
        "n_bars_symbols_never_members": len(have - ever),
        "bias": bias,
        "_membership": mem,
    }


def ledger_row(a, argv, written, rows_total, n_symbols, uni=None):
    """The ledger row for a finished run: the IC summary of what was just written."""
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]
    obs = ic.load_observations(a.out_records, a.bars, horizons) if written else []
    ins = ic.in_sample_metrics(ic.summarise(obs, horizons)) if obs else {}
    diff = a.config_diff
    if isinstance(diff, str):
        try:
            diff = json.loads(diff)
        except ValueError:
            pass
    cmd = ["backtest.py"] + list(argv if argv is not None else sys.argv[1:])
    universe = {"name": os.path.basename(a.bars), "n_symbols": n_symbols}
    bias = None
    if uni and uni.get("point_in_time"):
        universe = {k: uni[k] for k in ("name", "file", "n_symbols", "n_symbols_on_start",
                                        "n_symbols_on_end", "n_ever", "n_members_with_bars",
                                        "n_members_without_bars")}
        bias = uni.get("bias")
    return {
        "id": a.experiment_id,
        "hypothesis": a.hypothesis,
        "config_diff": diff,
        "harness_cmd": cmd,
        "window": {"start": a.start, "end": a.end},
        "universe": universe,
        "universe_bias": bias,
        "n": rows_total,
        "horizons": horizons,
        "in_sample": ins,
        "out_of_sample": None,
        "decision": None,
        "replays": written,
    }


if __name__ == "__main__":
    sys.exit(main())
