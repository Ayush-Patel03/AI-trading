"""ic.py — rank information coefficient and quantile spreads, in pure Python.

WHY A SECOND STATISTIC NEXT TO validate.py
------------------------------------------
`validate.py` pools every (date, ticker) observation and asks one Spearman question of the
pile. That is the right first question and it stays. But a pooled correlation over 6,800
observations that came from 100 dates treats each observation as independent, and they are
not: nineteen names scored on the same morning share a regime, a sector and a tape. The
p-values it prints are therefore optimistic by an amount nobody can see.

This module asks the same question the way a factor desk does:

  * the **information coefficient** (IC) is the Spearman rank correlation between score and
    forward return computed WITHIN one date's cross-section. One number per date.
  * the IC **series** across dates is what carries the evidence. Its mean is the signal; its
    standard deviation is the noise; its t-statistic is the claim.
  * because forward returns over `h` sessions overlap for dates fewer than `h` sessions apart,
    consecutive ICs are autocorrelated and the ordinary t-statistic overstates itself. The
    t-statistic here uses a **Newey-West (Bartlett-kernel) HAC** variance with lag = horizon.
  * the **quantile spread** is the mean forward return of the top quantile minus the bottom
    quantile, again computed per date and averaged, with a **block-bootstrap** 90% interval
    (block = horizon) so the interval respects the same overlap.

NEWEY-WEST VARIANCE OF A SERIES MEAN (Bartlett kernel)
-------------------------------------------------------
For a series x_1..x_T with mean m and lag L:

    gamma_k = (1/T) * sum_{t=k+1..T} (x_t - m)(x_{t-k} - m)       k = 0..L
    S       = gamma_0 + 2 * sum_{k=1..L} (1 - k/(L+1)) * gamma_k    long-run variance
    Var(m)  = S / T
    t       = m / sqrt(S / T)

With L = 0 the weights vanish and S is the plain (population, 1/T) variance of the series,
which is the ordinary t-statistic. The Bartlett weights (1 - k/(L+1)) guarantee S >= 0.
Newey & West (1987), "A simple, positive semi-definite, heteroskedasticity and
autocorrelation consistent covariance matrix", Econometrica 55(3).

The lag is `horizon` in units of OBSERVATION DATES, as the harness plan specifies. With
`--every 5` replays, dates are already five sessions apart, so lag = horizon is conservative
(it allows for more overlap than exists). That errs in the honest direction.

INPUTS
------
The same records `backtest.py` writes and `validate.py` reads — the compact archive record
format (`archive.record`) — plus the bars file that supplies the forward closes. The loaders
are `validate.py`'s own; this file invents no format. `--records` may also point at a JSON
file of already-computed observations, each `{date, symbol, score, fwd: {h: pct}, features}`,
which is what `--json` writes under `observations` when asked.

PER-FEATURE MODE
----------------
`--by-feature` computes the same table for every numeric feature a row carries: the `features`
dict if the record has one, else the unscored fields `validate.FIELDS` already tracks
(`rs_20d_vs_spy`, `atr_pct`, ...). This is where a window-split experiment (E10) lives: which
input ranks returns in-sample, and does it still on the hold-out.

Usage
-----
    python3 ic.py --records records/ --bars bars.json --horizons 5,10,20 \
                  [--by-feature] [--md ic.md] [--json ic.json]

Everything here describes a replay. It forecasts nothing.
"""
import argparse
import json
import math
import os
import random
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import validate

DEFAULT_HORIZONS = (5, 10, 20)
MIN_CROSS_SECTION = 5        # fewer names on a date than this and that date has no IC
QUINTILE_MIN_N = 10          # median cross-section below this -> terciles
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 20260910    # fixed: the same input must give the same interval
CI_LEVEL = 0.90


def _f(v):
    return validate._f(v)


# ---------------------------------------------------------------- statistics
def spearman(xs, ys):
    """Spearman rho over paired values, None entries dropped. Delegates to validate.py so
    there is exactly one rank-correlation implementation in the engine."""
    rho, p, n = validate.spearman(xs, ys)
    return rho, p, n


def newey_west_variance(xs, lag):
    """Long-run variance S of a series (see module docstring). S/len(xs) is Var(mean).

    lag=0 returns the plain population variance. The lag is capped at len(xs)-1."""
    T = len(xs)
    if T == 0:
        return None
    m = sum(xs) / T
    d = [x - m for x in xs]
    L = max(0, min(int(lag), T - 1))
    s = sum(v * v for v in d) / T
    for k in range(1, L + 1):
        gamma_k = sum(d[t] * d[t - k] for t in range(k, T)) / T
        s += 2.0 * (1.0 - k / (L + 1.0)) * gamma_k
    return max(s, 0.0)


def nw_tstat(xs, lag):
    """t-statistic of the mean of xs against zero, with a Newey-West variance."""
    T = len(xs)
    if T < 2:
        return None
    s = newey_west_variance(xs, lag)
    if s is None or s <= 0:
        return None
    return (sum(xs) / T) / math.sqrt(s / T)


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def block_bootstrap_ci(series, block, draws=BOOTSTRAP_DRAWS, level=CI_LEVEL, seed=BOOTSTRAP_SEED):
    """Circular moving-block bootstrap of the MEAN of `series`.

    Blocks of `block` consecutive values (wrapping at the end) are drawn with replacement
    until the resample is as long as the original; the mean of each resample is one draw.
    Returns (lo, hi) at the requested level, or None when the series is too short."""
    T = len(series)
    if T < 2:
        return None
    b = max(1, min(int(block), T))
    rng = random.Random(seed)
    n_blocks = -(-T // b)          # ceil
    means = []
    for _ in range(draws):
        acc, cnt = 0.0, 0
        for _ in range(n_blocks):
            start = rng.randrange(T)
            for j in range(b):
                if cnt >= T:
                    break
                acc += series[(start + j) % T]
                cnt += 1
        means.append(acc / T)
    means.sort()
    alpha = (1.0 - level) / 2.0
    lo = means[min(draws - 1, max(0, int(alpha * draws)))]
    hi = means[min(draws - 1, max(0, int((1.0 - alpha) * draws) - 1))]
    return lo, hi


# ---------------------------------------------------------------- observations
def _features_of(row):
    """A row's feature dict: its own `features` block first, then validate.FIELDS."""
    feats = {}
    raw = row.get("features")
    if isinstance(raw, dict):
        for k, v in raw.items():
            fv = _f(v)
            if fv is not None:
                feats[str(k)] = fv
    for k in validate.FIELDS:
        if k in feats:
            continue
        fv = _f(row.get(k))
        if fv is not None:
            feats[k] = fv
    return feats


def _normalise_fwd(fwd):
    out = {}
    for k, v in (fwd or {}).items():
        try:
            h = int(k)
        except (TypeError, ValueError):
            continue
        fv = _f(v)
        if fv is not None:
            out[h] = fv
    return out


def from_validate_obs(obs):
    """validate.observations() rows (with `fwd` filled) -> this module's observations."""
    out = []
    for o in obs:
        row = o.get("row") or {}
        score = _f(row.get("score"))
        if score is None:
            continue
        out.append({"date": o["date"], "symbol": o["ticker"], "score": score,
                    "fwd": _normalise_fwd(o.get("fwd")), "features": _features_of(row)})
    return out


def _from_precomputed(rows):
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        score = _f(r.get("score"))
        sym = r.get("symbol") or r.get("ticker")
        if score is None or not sym or not r.get("date"):
            continue
        feats = {}
        for k, v in (r.get("features") or {}).items():
            fv = _f(v)
            if fv is not None:
                feats[str(k)] = fv
        out.append({"date": r["date"], "symbol": sym, "score": score,
                    "fwd": _normalise_fwd(r.get("fwd")), "features": feats})
    return out


def load_observations(records, bars=None, horizons=DEFAULT_HORIZONS):
    """Observations from a records directory / archive-record file (needs `bars`), or from
    a JSON file of already-computed observations (no bars needed).

    Raises SystemExit with a REFUSED message rather than guessing."""
    if os.path.isdir(records):
        recs = validate.load_records(records)
        if not recs:
            raise SystemExit(f"REFUSED: no scan records with results in {records}")
        return _with_forward(recs, bars, horizons)
    raw = json.load(open(records, encoding="utf-8"))
    if isinstance(raw, dict):
        if isinstance(raw.get("observations"), list):
            return _from_precomputed(raw["observations"])
        if raw.get("results") and raw.get("date"):
            return _with_forward([raw], bars, horizons)
        raise SystemExit(f"REFUSED: {records} is neither an archive record nor an observations file")
    if isinstance(raw, list):
        if raw and all(isinstance(r, dict) and r.get("results") for r in raw):
            return _with_forward(raw, bars, horizons)
        return _from_precomputed(raw)
    raise SystemExit(f"REFUSED: cannot read observations from {records}")


def _with_forward(recs, bars, horizons):
    if not bars:
        raise SystemExit("REFUSED: archive records carry no forward returns; pass --bars so "
                         "they can be measured from the closes, exactly as validate.py does.")
    series = validate.load_bars(bars)
    obs = validate.forward_returns(validate.observations(recs), series, list(horizons))
    return from_validate_obs(obs)


# ---------------------------------------------------------------- the table
def _by_date(obs, key, h):
    """{date: [(x, fwd), ...]} for one predictor and one horizon, None entries dropped."""
    groups = {}
    for o in obs:
        y = o["fwd"].get(h)
        x = o["score"] if key is None else o["features"].get(key)
        if x is None or y is None:
            continue
        groups.setdefault(o["date"], []).append((x, y))
    return groups


def quantile_of(groups):
    """5 when the median cross-section is at least QUINTILE_MIN_N names, else 3."""
    sizes = sorted(len(v) for v in groups.values())
    if not sizes:
        return 5
    med = sizes[len(sizes) // 2]
    return 5 if med >= QUINTILE_MIN_N else 3


def ic_table(obs, h, key=None, lag=None):
    """The IC row for one predictor (None = the score) at one horizon."""
    groups = _by_date(obs, key, h)
    lag = h if lag is None else lag
    # A date contributes an IC only when it has a real cross-section: enough names, and a
    # predictor that actually varies (a constant ranks nothing, and validate.spearman says
    # so by returning None). The spread is computed on exactly the same dates.
    ics, dates = [], []
    for d in sorted(groups):
        pairs = groups[d]
        if len(pairs) < MIN_CROSS_SECTION:
            continue
        rho, _, _ = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        if rho is not None:
            ics.append(rho)
            dates.append(d)

    xs = [x for pairs in groups.values() for x, _ in pairs]
    ys = [y for pairs in groups.values() for _, y in pairs]
    pooled_rho, pooled_p, n = spearman(xs, ys)

    Q = quantile_of({d: groups[d] for d in dates})
    spreads = []
    for d in dates:
        # Sort on the predictor ONLY. Sorting on (x, y) would order ties by their outcome
        # and manufacture a spread out of nothing.
        pairs = sorted(groups[d], key=lambda p: p[0])
        q = len(pairs) // Q
        if q < 1:
            continue
        top = _mean([y for _, y in pairs[-q:]])
        bottom = _mean([y for _, y in pairs[:q]])
        spreads.append(top - bottom)
    ci = block_bootstrap_ci(spreads, block=max(1, h)) if len(spreads) >= 2 else None

    r3 = lambda v: round(v, 4) if v is not None else None
    return {
        "n": n,
        "n_dates": len(ics),
        "ic_mean": r3(_mean(ics)),
        "ic_std": r3(_std(ics)),
        "ic_tstat_nw": r3(nw_tstat(ics, lag)) if len(ics) >= 2 else None,
        "nw_lag": min(lag, max(0, len(ics) - 1)),
        "pooled_spearman": r3(pooled_rho),
        "pooled_p": r3(pooled_p),
        "spread": {
            "quantile": Q,
            "label": "quintile" if Q == 5 else "tercile",
            "n_dates": len(spreads),
            "mean_pct": r3(_mean(spreads)),
            "ci90_pct": [r3(ci[0]), r3(ci[1])] if ci else None,
            "block": max(1, h),
        },
    }


def summarise(obs, horizons=DEFAULT_HORIZONS, by_feature=False):
    horizons = [int(h) for h in horizons]
    out = {
        "_what": ("Per-date rank IC of score vs forward return, its Newey-West t-statistic, "
                  "the pooled Spearman, and the top-minus-bottom quantile spread with a "
                  "block-bootstrap 90% interval. Describes a replay; forecasts nothing."),
        "n": len(obs),
        "n_dates": len({o["date"] for o in obs}),
        "horizons": horizons,
        "score": {str(h): ic_table(obs, h) for h in horizons},
    }
    if by_feature:
        names = sorted({k for o in obs for k in o["features"]})
        out["features"] = {name: {str(h): ic_table(obs, h, key=name) for h in horizons}
                           for name in names}
    return out


def in_sample_metrics(summary):
    """The compact dict a ledger row records for the score. Horizon-keyed."""
    out = {}
    for h, t in (summary.get("score") or {}).items():
        sp = t.get("spread") or {}
        out[str(h)] = {"n": t.get("n"), "n_dates": t.get("n_dates"),
                       "ic_mean": t.get("ic_mean"), "ic_tstat_nw": t.get("ic_tstat_nw"),
                       "pooled_spearman": t.get("pooled_spearman"), "pooled_p": t.get("pooled_p"),
                       "spread_pct": sp.get("mean_pct"), "spread_ci90_pct": sp.get("ci90_pct"),
                       "spread_quantile": sp.get("quantile")}
    return out


# ---------------------------------------------------------------- report
def _fmt(v, dp=3, signed=True):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"
    return str(v)


def _row_line(name, t):
    sp = t.get("spread") or {}
    ci = sp.get("ci90_pct")
    ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "—"
    return (f"| {name} | {t['n']} | {t['n_dates']} | {_fmt(t['ic_mean'])} | "
            f"{_fmt(t['ic_std'])} | {_fmt(t['ic_tstat_nw'], 2)} | {_fmt(t['pooled_spearman'])} | "
            f"{_fmt(t['pooled_p'], 4, signed=False)} | {_fmt(sp.get('mean_pct'), 2)} {ci_s} "
            f"({sp.get('label')}) |")


def markdown(summary):
    L = ["# Rank IC — does the score rank forward returns within each date?", ""]
    L.append(f"{summary['n']} observations across {summary['n_dates']} date(s). IC = Spearman "
             "within one date's cross-section; t-stat uses a Newey-West (Bartlett) variance "
             "with lag = horizon; the spread is top minus bottom quantile per date, averaged, "
             "with a block-bootstrap 90% interval (block = horizon).")
    L.append("")
    header = ("| predictor | n | dates | IC mean | IC std | t (NW) | pooled rho | p | "
              "spread % [90% CI] |")
    sep = "|---|---:|---:|---:|---:|---:|---:|---:|---|"
    for h in summary["horizons"]:
        L.append(f"## {h}-session horizon")
        L.append(header)
        L.append(sep)
        L.append(_row_line("score", summary["score"][str(h)]))
        feats = summary.get("features") or {}
        ranked = sorted(feats.items(),
                        key=lambda kv: -abs(kv[1][str(h)]["ic_mean"] or 0.0))
        for name, tbl in ranked:
            L.append(_row_line(name, tbl[str(h)]))
        L.append("")
    L.append("**Reading it.** The IC mean is the signal, its t-statistic the claim. Harvey, "
             "Liu & Zhu (2016) argue a hold-out t of at least 3 given how many factors have "
             "been tried; an in-sample t of 2 after several trials is what noise looks like. "
             "A spread whose 90% interval straddles zero is not a spread. This describes a "
             "replay; it forecasts nothing.")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--records", required=True,
                    help="directory of archive records, one record file, or an observations file")
    ap.add_argument("--bars", help="get_equity_historicals output(s); required for archive records")
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    ap.add_argument("--by-feature", action="store_true")
    ap.add_argument("--md")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    obs = load_observations(a.records, a.bars, horizons)
    if not obs:
        print(f"REFUSED: no scored observations in {a.records}", file=sys.stderr)
        return 2
    res = summarise(obs, horizons, by_feature=a.by_feature)
    if a.json:
        payload = dict(res)
        payload["observations"] = [{"date": o["date"], "symbol": o["symbol"], "score": o["score"],
                                    "fwd": {str(k): v for k, v in o["fwd"].items()},
                                    "features": o["features"]} for o in obs]
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(markdown(res))
    for h in horizons:
        t = res["score"][str(h)]
        sp = t["spread"]
        print(f"{h:>3}d  n={t['n']:<5} dates={t['n_dates']:<4} IC={_fmt(t['ic_mean'])} "
              f"t(NW)={_fmt(t['ic_tstat_nw'], 2)}  pooled rho={_fmt(t['pooled_spearman'])} "
              f"spread={_fmt(sp['mean_pct'], 2)}% ({sp['label']})")
    if a.by_feature:
        print(f"  {len(res.get('features') or {})} feature(s) tabulated")
    if a.json or a.md:
        print("-> " + ", ".join(p for p in (a.json, a.md) if p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
