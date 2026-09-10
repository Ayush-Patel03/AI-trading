"""var.py — historical-simulation VaR and named stress windows for one desk (K-04, 2026-09-10).

K-02's drawdown ladder reacts to a loss after it has happened; K-03's exposure block says how
crowded the book is. Neither says how much the book stands to lose on an ordinary bad day, or
what it would have done through the four sessions this decade that broke the most books.
This module answers both, and nothing else.

Everything here is a PURE FUNCTION over one desk's weights and per-symbol daily returns —
the {SYMBOL: {date: r}} map house.daily_returns() derives from the staged bars.json. Nothing
reads a file, nothing mutates a book, nothing places or blocks an order by itself. pm.py
calls `hs_var()` and `stress()` once per run when bars are staged and `assess()` on the
pair; with `enforce` off (the default) the numbers go to the state, the journal and the
coverage row and gate nothing. With `enforce` on a breach refuses NEW ENTRIES on that desk.
Exits are never touched by anything in this file, in either mode.

DEFINITIONS
  w_i             notional_i / desk equity. Cash carries a return of 0, so a 40%-invested
                  book has a portfolio return of Σ w_i r_i with Σ w_i = 0.4 — the VaR is a
                  share of DESK EQUITY, not of the invested slice.
  HS VaR(α, h)    over the dates every held name shares (up to `max_days`, two years), the
                  h-day portfolio return series is sorted and the loss at the k-th worst
                  observation, k = ⌊(1−α)·n⌋ (at least 1), is the VaR. α = 0.99 on 500 days
                  is the 5th-worst day. Reported as a POSITIVE percentage of desk equity.
  CVaR            the mean loss of those k worst observations. Never below the VaR.
  stress window   the book's cumulative P&L, in percent of desk equity, had each holding
                  repeated its OWN return through the window. When the staged bars do not
                  reach back that far — a two-year file covers neither 2020 nor 2022 — the
                  holding's contribution is β_252 × the benchmark's return over the window
                  (the benchmark's own bars when they cover it, else the index return
                  recorded in BENCH_WINDOW_RET), labelled `coverage: "proxy"`.

The five windows, all inclusive of the return dated on each bound:
  2020-03-16      the COVID crash's worst single session (S&P 500 −11.98%)
  2022            2022-01-03 → 2022-12-30 cumulative, the rate-shock bear (−19.4%)
  2024-08-05      the yen-carry unwind session (−3.0%)
  2025-04-03/04   the two tariff sessions, cumulative (−4.8% then −6.0%: −10.5%)
  2025-04-09      the +9.5% tariff-pause squeeze. Reported as SHORT-side risk: a long book
                  gains, a short book (the options desk, later) takes the loss. It never
                  counts toward `worst_stress`, which is a long-book number.

Stdlib only. No numpy: the books hold a dozen names and the series are a few hundred days.
"""
import math

DEFAULT_RULES = {
    "alpha": 0.99,                      # VaR confidence
    "max_var_pct_of_desk": 3.0,         # 1-day VaR above this share of desk equity flags
    "max_stress_multiple_of_halt": 2.0, # worst stress loss above this × halt_pct flags
    "halt_pct": 8.0,                    # the ladder's rung-3 halt, restated here
    "enforce": False,                   # False: report only. True: a flag blocks NEW entries
}

MIN_DAYS = 120          # fewer overlapping daily returns than this and VaR is null
MAX_DAYS = 504          # two trading years of history at most

# Approximate S&P 500 index returns over each window, used ONLY when neither the holding's
# own bars nor the benchmark's staged bars cover it. A proxy is labelled as one everywhere.
BENCH_WINDOW_RET = {
    "2020-03-16": -0.1198,
    "2022": -0.1944,
    "2024-08-05": -0.0300,
    "2025-04-03/04": -0.1052,
    "2025-04-09": 0.0952,
}

SCENARIOS = {
    "2020-03-16": {"start": "2020-03-16", "end": "2020-03-16", "side": "long",
                   "label": "COVID crash, worst single session"},
    "2022": {"start": "2022-01-03", "end": "2022-12-30", "side": "long",
             "label": "2022 rate-shock bear, full year cumulative"},
    "2024-08-05": {"start": "2024-08-05", "end": "2024-08-05", "side": "long",
                   "label": "yen-carry unwind session"},
    "2025-04-03/04": {"start": "2025-04-03", "end": "2025-04-04", "side": "long",
                      "label": "tariff sessions, two days cumulative"},
    "2025-04-09": {"start": "2025-04-09", "end": "2025-04-09", "side": "short",
                   "label": "tariff-pause squeeze (+9.5%): the short-side risk"},
}


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _clean_weights(weights):
    out = {}
    for sym, w in (weights or {}).items():
        if sym and _isnum(w) and w > 0:
            out[str(sym).upper()] = float(w)
    return out


def _clean_returns(rets):
    out = {}
    for sym, series in (rets or {}).items():
        if not sym or not isinstance(series, dict):
            continue
        clean = {str(d)[:10]: float(r) for d, r in series.items() if _isnum(r)}
        if clean:
            out[str(sym).upper()] = clean
    return out


# ------------------------------------------------------------------ VaR
def portfolio_returns(weights, returns, max_days=MAX_DAYS):
    """The dated portfolio return series Σ w_i r_i over the dates every covered name
    shares, newest `max_days` only. Returns (dates, values, covered_symbols, uncovered)."""
    w = _clean_weights(weights)
    rets = _clean_returns(returns)
    covered = sorted(s for s in w if s in rets)
    uncovered = sorted(s for s in w if s not in rets)
    if not covered:
        return [], [], covered, uncovered
    dates = set(rets[covered[0]])
    for s in covered[1:]:
        dates &= set(rets[s])
    dates = sorted(dates)[-max_days:]
    vals = [sum(w[s] * rets[s][d] for s in covered) for d in dates]
    return dates, vals, covered, uncovered


def _horizon(vals, h):
    """Compound consecutive h-day windows (overlapping) of a daily return series."""
    if h <= 1:
        return list(vals)
    out = []
    for i in range(len(vals) - h + 1):
        g = 1.0
        for r in vals[i:i + h]:
            g *= (1.0 + r)
        out.append(g - 1.0)
    return out


def hs_var(weights_by_symbol, daily_returns_by_symbol, alpha=0.99, horizon_days=1,
           min_days=MIN_DAYS, max_days=MAX_DAYS):
    """Historical-simulation VaR and CVaR of a weighted book, as a % of desk equity.

    {"var_pct", "cvar_pct", "n_days", "window": {"start", "end"}, "alpha", "horizon_days",
     "coverage_pct", "uncovered"} — or the same keys with var_pct / cvar_pct / window None
    and a `reason` when fewer than `min_days` overlapping returns exist or no held name
    has bars at all. An empty book is null too: there is no loss distribution of nothing.
    """
    alpha = float(alpha)
    h = max(1, int(horizon_days or 1))
    base = {"var_pct": None, "cvar_pct": None, "n_days": 0, "window": None,
            "alpha": alpha, "horizon_days": h, "coverage_pct": None, "uncovered": [],
            "basis": "historical simulation"}
    w = _clean_weights(weights_by_symbol)
    if not w:
        return dict(base, reason="no positions to measure")
    if not (0.0 < alpha < 1.0):
        return dict(base, reason=f"alpha {alpha} is not in (0, 1)")
    dates, vals, covered, uncovered = portfolio_returns(w, daily_returns_by_symbol, max_days)
    tot = sum(w.values())
    cov_pct = round(sum(w[s] for s in covered) / tot * 100.0, 2) if tot else None
    base.update({"n_days": len(dates), "coverage_pct": cov_pct, "uncovered": uncovered})
    if not covered:
        return dict(base, reason="no held name has bars in the staged file")
    if len(dates) < min_days:
        return dict(base, reason=f"only {len(dates)} overlapping day(s) of history across "
                                 f"{', '.join(covered)} — {min_days} needed")
    series = _horizon(vals, h)
    if len(series) < 1:
        return dict(base, reason="horizon longer than the shared history")
    ordered = sorted(series)                      # worst (most negative) first
    k = max(1, int(math.floor((1.0 - alpha) * len(ordered))))
    tail = ordered[:k]
    var_pct = -ordered[k - 1] * 100.0
    cvar_pct = -(sum(tail) / len(tail)) * 100.0
    base.update({"var_pct": round(var_pct, 3), "cvar_pct": round(max(cvar_pct, var_pct), 3),
                 "window": {"start": dates[0], "end": dates[-1]}, "tail_n": k})
    return base


# ------------------------------------------------------------------ stress
def window_return(series, start, end):
    """Compounded return of a {date: r} series over [start, end] inclusive, or None when
    the series does not carry a return dated on BOTH bounds — a partial window is not
    the window."""
    if not series or start not in series or end not in series:
        return None
    g = 1.0
    for d in sorted(series):
        if start <= d <= end:
            g *= (1.0 + series[d])
    return g - 1.0


def stress(weights_by_symbol, daily_returns_by_symbol, scenarios=None, betas=None,
           benchmark="SPY", bench_window_ret=None):
    """{name: {"pnl_pct", "coverage", "side", "window", "measured_pct", "by_symbol"}}.

    coverage is "own" when every held name repeats its own bars through the window,
    "proxy" when at least one name fell back to β_252 × the benchmark's window return,
    "partial" when some names could not be measured either way (their weight is left out
    and `measured_pct` says how much of the book was), and "none" — with pnl_pct None —
    when nothing could be. `betas` is {SYMBOL: beta_252} from house.features_from_bars;
    a name with no beta and no bars over the window is unmeasured, never β = 1.
    """
    scen = scenarios if isinstance(scenarios, dict) and scenarios else SCENARIOS
    w = _clean_weights(weights_by_symbol)
    rets = _clean_returns(daily_returns_by_symbol)
    betas = {str(k).upper(): v for k, v in (betas or {}).items() if _isnum(v)}
    bench = (benchmark or "").upper()
    fallback = dict(BENCH_WINDOW_RET, **(bench_window_ret or {}))
    tot = sum(w.values())
    out = {}
    for name, sc in scen.items():
        start, end = str(sc.get("start")), str(sc.get("end") or sc.get("start"))
        side = sc.get("side") or "long"
        bench_ret = window_return(rets.get(bench), start, end)
        bench_basis = "bars" if bench_ret is not None else None
        if bench_ret is None and _isnum(fallback.get(name)):
            bench_ret, bench_basis = float(fallback[name]), "index"
        by_sym, pnl, measured, proxied = {}, 0.0, 0.0, False
        for sym, wi in sorted(w.items()):
            r = window_return(rets.get(sym), start, end)
            if r is not None:
                by_sym[sym] = {"ret_pct": round(r * 100.0, 3), "coverage": "own"}
            elif sym == bench and bench_ret is not None:
                r = bench_ret
                by_sym[sym] = {"ret_pct": round(r * 100.0, 3), "coverage": bench_basis}
            elif sym in betas and bench_ret is not None:
                r = betas[sym] * bench_ret
                by_sym[sym] = {"ret_pct": round(r * 100.0, 3), "coverage": "proxy",
                               "beta_252": round(betas[sym], 3), "bench_basis": bench_basis}
                proxied = True
            else:
                by_sym[sym] = {"ret_pct": None, "coverage": "none"}
                continue
            pnl += wi * r
            measured += wi
        if not w or measured <= 0:
            cov = "none"
        elif measured < tot - 1e-12:
            cov = "partial"
        else:
            cov = "proxy" if proxied else "own"
        row = {"pnl_pct": round(pnl * 100.0, 3) if cov != "none" else None,
               "coverage": cov, "side": side, "label": sc.get("label"),
               "window": {"start": start, "end": end},
               "measured_pct": round(measured / tot * 100.0, 2) if tot else None,
               "benchmark_ret_pct": round(bench_ret * 100.0, 3) if bench_ret is not None else None,
               "benchmark_basis": bench_basis,
               "by_symbol": by_sym}
        if side == "short" and row["pnl_pct"] is not None:
            row["short_pnl_pct"] = round(-row["pnl_pct"], 3)
        out[name] = row
    return out


def worst_stress(stress_result):
    """(name, pnl_pct) of the most negative LONG-side window, or (None, None)."""
    worst = None
    for name, row in (stress_result or {}).items():
        if row.get("side") == "short" or not _isnum(row.get("pnl_pct")):
            continue
        if worst is None or row["pnl_pct"] < worst[1]:
            worst = (name, row["pnl_pct"])
    return worst if worst else (None, None)


# ------------------------------------------------------------------ the verdict
def assess(var_result, stress_result, rules=None):
    """{"flags", "reasons", "enforce", "block_new_entries", "worst_stress",
    "worst_stress_window"}. A null VaR or an unmeasured stress never flags — an absent
    number is not a low one. With enforce off nothing blocks."""
    rules = dict(DEFAULT_RULES, **(rules or {}))
    flags, reasons = [], []
    v = (var_result or {}).get("var_pct")
    cap = float(rules["max_var_pct_of_desk"])
    if _isnum(v) and v > cap:
        flags.append("var")
        reasons.append(f"1-day {float(rules['alpha']) * 100:.0f}% VaR {v:.2f}% of desk equity "
                       f"over the {cap:.1f}% cap")
    name, pnl = worst_stress(stress_result)
    limit = float(rules["max_stress_multiple_of_halt"]) * float(rules["halt_pct"])
    if _isnum(pnl) and -pnl > limit:
        flags.append("stress")
        reasons.append(f"stress {name} would lose {-pnl:.1f}% of desk equity, over "
                       f"{float(rules['max_stress_multiple_of_halt']):.0f}× the "
                       f"{float(rules['halt_pct']):.0f}% halt ({limit:.0f}%)")
    enforce = bool(rules.get("enforce"))
    return {"flags": flags, "reasons": reasons, "enforce": enforce,
            "block_new_entries": bool(enforce and flags),
            "worst_stress": pnl, "worst_stress_window": name}


def compact(var_result, stress_result, verdict=None):
    """The journal / coverage-row summary: a handful of numbers, no per-symbol detail."""
    v = var_result or {}
    name, pnl = worst_stress(stress_result)
    a = verdict or {}
    return {"var_pct": v.get("var_pct"), "cvar_pct": v.get("cvar_pct"),
            "n_days": v.get("n_days"), "alpha": v.get("alpha"),
            "reason": v.get("reason"),
            "worst_stress": pnl, "worst_stress_window": name,
            "stress": {k: {"pnl_pct": r.get("pnl_pct"), "coverage": r.get("coverage")}
                       for k, r in (stress_result or {}).items()},
            "flags": list(a.get("flags") or []), "enforce": bool(a.get("enforce")),
            "block_new_entries": bool(a.get("block_new_entries"))}


def console_line(summary):
    """`risk: VaR99 1.8% (cvar 2.6%, 498d) · worst stress 2020-03-16 −9.4% (proxy)`."""
    if not summary:
        return "risk: VaR not measured"
    v = summary.get("var_pct")
    if not _isnum(v):
        return f"risk: VaR n/a ({summary.get('reason') or 'no bars staged'})"
    bits = [f"VaR{int(round((summary.get('alpha') or 0.99) * 100))} {v:.2f}%"
            f" (cvar {summary.get('cvar_pct'):.2f}%, {summary.get('n_days')}d)"]
    name, pnl = summary.get("worst_stress_window"), summary.get("worst_stress")
    if name and _isnum(pnl):
        cov = ((summary.get("stress") or {}).get(name) or {}).get("coverage")
        bits.append(f"worst stress {name} {pnl:+.1f}%" + (f" ({cov})" if cov else ""))
    line = "risk: " + " · ".join(bits)
    if summary.get("flags"):
        line += "   FLAGS " + ",".join(summary["flags"]) + (
            "  [ENFORCED — new entries blocked]" if summary.get("block_new_entries") else "  [reported]")
    return line
