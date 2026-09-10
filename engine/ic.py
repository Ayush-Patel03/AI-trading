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

BETA-RESIDUAL MODE (E2 — "was it beta?")
-----------------------------------------
`--beta-residual` answers the E2 question in docs/BACKTEST.md §6b by inspection. For every
observation that carries `features.beta_252` and has SPY's forward return on the same date
(computed from the bars exactly as the stock's is — `validate.forward_returns` on SPY's
closes), the forward return is regressed on beta × SPY forward return, pooled, **through the
origin** (`technicals.ols`, stdlib):

    fwd_h(i) = k · beta_252(i) · SPY_fwd_h(date_i) + e_h(i)

k is a free slope because beta_252 was estimated over a different window; with the
market-model assumption k ≈ 1. The residual e_h is the part of the forward return the name's
beta does not explain. The IC table is then computed twice for `atr_pct`, `rv_20d`,
`max_1m` (and `beta_252` itself, as the control): against the RAW forward return and
against the RESIDUAL, and printed side by side. A feature whose raw IC is real and whose
residual IC is near zero was a beta proxy in a directional sample; one that survives the
residual is a signal in its own right. Same Newey-West lag, same bootstrap block.

The SPY forward return is written into `--json` as `spy_fwd` per observation, so an
observations file produced with `--bars` is a valid `--records` input for this mode too.

Usage
-----
    python3 ic.py --records records/ --bars bars.json --horizons 5,10,20 \
                  [--by-feature] [--beta-residual] [--md ic.md] [--json ic.json]

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
import technicals

DEFAULT_HORIZONS = (5, 10, 20)
E2_FEATURES = ("atr_pct", "rv_20d", "max_1m", "beta_252")   # beta_252 is the control row
BENCHMARK = "SPY"
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
                    "fwd": _normalise_fwd(o.get("fwd")), "features": _features_of(row),
                    "spy_fwd": _normalise_fwd(o.get("spy_fwd"))})
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
                    "fwd": _normalise_fwd(r.get("fwd")), "features": feats,
                    "spy_fwd": _normalise_fwd(r.get("spy_fwd"))})
    return out


def spy_forward(series, dates, horizons, benchmark=BENCHMARK):
    """{date: {h: pct}} — the benchmark's forward return from each date's close, through
    validate.forward_returns so it is measured exactly as the stocks' returns are."""
    if not series.get(benchmark):
        return {}
    obs = [{"date": d, "ticker": benchmark, "row": {}} for d in sorted(set(dates))]
    validate.forward_returns(obs, series, [int(h) for h in horizons])
    return {o["date"]: _normalise_fwd(o["fwd"]) for o in obs}


def attach_spy_forward(obs, series, horizons):
    """Fill `spy_fwd` on every observation that lacks it, from the bars."""
    need = sorted({o["date"] for o in obs if not o.get("spy_fwd")})
    if not need:
        return obs
    sf = spy_forward(series, need, horizons)
    for o in obs:
        if not o.get("spy_fwd"):
            o["spy_fwd"] = dict(sf.get(o["date"], {}))
    return obs


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
            obs = _from_precomputed(raw["observations"])
            return attach_spy_forward(obs, validate.load_bars(bars), horizons) if bars else obs
        if raw.get("results") and raw.get("date"):
            return _with_forward([raw], bars, horizons)
        raise SystemExit(f"REFUSED: {records} is neither an archive record nor an observations file")
    if isinstance(raw, list):
        if raw and all(isinstance(r, dict) and r.get("results") for r in raw):
            return _with_forward(raw, bars, horizons)
        obs = _from_precomputed(raw)
        return attach_spy_forward(obs, validate.load_bars(bars), horizons) if bars else obs
    raise SystemExit(f"REFUSED: cannot read observations from {records}")


def _with_forward(recs, bars, horizons):
    if not bars:
        raise SystemExit("REFUSED: archive records carry no forward returns; pass --bars so "
                         "they can be measured from the closes, exactly as validate.py does.")
    series = validate.load_bars(bars)
    obs = validate.forward_returns(validate.observations(recs), series, list(horizons))
    return attach_spy_forward(from_validate_obs(obs), series, horizons)


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


# ---------------------------------------------------------------- E2: beta residual
def beta_residual(obs, h):
    """Pooled OLS through the origin of fwd_h on beta_252 × SPY_fwd_h.

    Returns (slope, residual observations, n): the observations are copies whose `fwd`
    carries ONLY {h: residual}, so `ic_table` runs on them unchanged. Rows without
    beta_252, a stock forward return or a SPY forward return at h are left out (they are
    not padded), and n counts what stayed. (None, [], 0) when fewer than 3 rows remain or
    the regressor is constant."""
    rows = []
    for o in obs:
        y = o["fwd"].get(h)
        b = o["features"].get("beta_252")
        m = (o.get("spy_fwd") or {}).get(h)
        if y is None or b is None or m is None:
            continue
        rows.append((o, y, b * m))
    if len(rows) < 3:
        return None, [], 0
    fit = technicals.ols([r[1] for r in rows], [[r[2] for r in rows]], intercept=False)
    if fit is None:
        return None, [], 0
    coef, _, resid = fit
    out = [dict(o, fwd={h: e}) for (o, _, _), e in zip(rows, resid)]
    return coef[0], out, len(rows)


def summarise_beta_residual(obs, horizons=DEFAULT_HORIZONS, features=E2_FEATURES):
    """Per horizon: the slope k, n, and for each feature the IC table against the raw
    forward return and against the residual — on the SAME rows, so the two columns differ
    only in what beta explains."""
    horizons = [int(h) for h in horizons]
    out = {"_what": ("E2: IC of each feature against the raw forward return and against the "
                     "residual of fwd on beta_252 × SPY_fwd (pooled OLS through the origin). "
                     "Raw IC real and residual IC near zero means the feature was beta. "
                     "Describes a replay; forecasts nothing."),
           "features": list(features), "horizons": horizons, "by_horizon": {}}
    for h in horizons:
        k, resid_obs, n = beta_residual(obs, h)
        H = {"n": n, "slope": round(k, 4) if k is not None else None,
             "n_without_beta": sum(1 for o in obs if o["features"].get("beta_252") is None),
             "n_without_spy": sum(1 for o in obs if (o.get("spy_fwd") or {}).get(h) is None),
             "features": {}}
        keep = {(o["date"], o["symbol"]) for o in resid_obs}
        raw_obs = [o for o in obs if (o["date"], o["symbol"]) in keep]
        for name in features:
            H["features"][name] = {"raw": ic_table(raw_obs, h, key=name),
                                   "residual": ic_table(resid_obs, h, key=name)}
        out["by_horizon"][str(h)] = H
    return out


def _side_line(name, raw, res):
    rs, ss = raw.get("spread") or {}, res.get("spread") or {}
    return (f"| {name} | {raw['n']} | {raw['n_dates']} | {_fmt(raw['ic_mean'])} | "
            f"{_fmt(raw['ic_tstat_nw'], 2)} | {_fmt(rs.get('mean_pct'), 2)} | "
            f"{_fmt(res['ic_mean'])} | {_fmt(res['ic_tstat_nw'], 2)} | "
            f"{_fmt(ss.get('mean_pct'), 2)} |")


def markdown_beta_residual(summary):
    L = ["# E2 — was it beta? IC against raw forward returns and against the beta residual", ""]
    L.append("For each horizon the forward return is regressed, pooled, through the origin, on "
             "beta_252 × SPY forward return; the residual is what beta does not explain. Both "
             "columns are computed on the same rows. A feature whose raw IC is real and whose "
             "residual IC is near zero was a beta proxy; one that survives is a signal in its "
             "own right. beta_252 itself is the control: its residual IC should be near zero.")
    L.append("")
    for h in summary["horizons"]:
        H = summary["by_horizon"][str(h)]
        L.append(f"## {h}-session horizon — n = {H['n']}, slope k = {_fmt(H['slope'], 3)}"
                 + ("" if H["n"] >= 30 else "  ·  NOT ENOUGH DATA (under 30)"))
        if H["n_without_beta"] or H["n_without_spy"]:
            L.append(f"({H['n_without_beta']} row(s) without beta_252 and {H['n_without_spy']} "
                     "without a SPY forward return were left out)")
        L.append("")
        L.append("| feature | n | dates | raw IC | raw t (NW) | raw spread % | "
                 "resid IC | resid t (NW) | resid spread % |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for name, t in H["features"].items():
            L.append(_side_line(name, t["raw"], t["residual"]))
        L.append("")
    L.append("**Reading it.** Compare the two IC columns row by row. Under 30 rows nothing "
             "here is evidence either way. This describes a replay; it forecasts nothing.")
    return "\n".join(L) + "\n"


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
    ap.add_argument("--beta-residual", action="store_true",
                    help="E2: IC of atr_pct / rv_20d / max_1m against raw and beta-residual "
                         "forward returns, side by side (needs beta_252 and SPY bars)")
    ap.add_argument("--md")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    obs = load_observations(a.records, a.bars, horizons)
    if not obs:
        print(f"REFUSED: no scored observations in {a.records}", file=sys.stderr)
        return 2
    res = summarise(obs, horizons, by_feature=a.by_feature)
    beta = summarise_beta_residual(obs, horizons) if a.beta_residual else None
    if beta is not None:
        res["beta_residual"] = beta
    if a.json:
        payload = dict(res)
        payload["observations"] = [{"date": o["date"], "symbol": o["symbol"], "score": o["score"],
                                    "fwd": {str(k): v for k, v in o["fwd"].items()},
                                    "features": o["features"],
                                    "spy_fwd": {str(k): v for k, v in (o.get("spy_fwd") or {}).items()}}
                                   for o in obs]
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(markdown(res))
            if beta is not None:
                f.write("\n" + markdown_beta_residual(beta))
    for h in horizons:
        t = res["score"][str(h)]
        sp = t["spread"]
        print(f"{h:>3}d  n={t['n']:<5} dates={t['n_dates']:<4} IC={_fmt(t['ic_mean'])} "
              f"t(NW)={_fmt(t['ic_tstat_nw'], 2)}  pooled rho={_fmt(t['pooled_spearman'])} "
              f"spread={_fmt(sp['mean_pct'], 2)}% ({sp['label']})")
    if a.by_feature:
        print(f"  {len(res.get('features') or {})} feature(s) tabulated")
    if beta is not None:
        for h in horizons:
            H = beta["by_horizon"][str(h)]
            print(f"E2 {h:>3}d  n={H['n']:<5} k={_fmt(H['slope'], 3)}   "
                  f"{'feature':<10} {'raw IC':>8} {'raw t':>7} | {'resid IC':>8} {'resid t':>7}")
            for name, t in H["features"].items():
                print(f"{'':>26}{name:<10} {_fmt(t['raw']['ic_mean']):>8} "
                      f"{_fmt(t['raw']['ic_tstat_nw'], 2):>7} | "
                      f"{_fmt(t['residual']['ic_mean']):>8} "
                      f"{_fmt(t['residual']['ic_tstat_nw'], 2):>7}")
    if a.json or a.md:
        print("-> " + ", ".join(p for p in (a.json, a.md) if p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
