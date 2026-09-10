"""earnings_quality.py — the E22 / E23 earnings-quality features (P-06). Logged, never scored.

WHY. The catalyst pillar knows WHEN a name reports and how big a move the options price;
it knows nothing about what the last report SAID. Two literatures say that matters:

  * Post-earnings-announcement drift (Bernard & Thomas 1989; Chan, Jegadeesh & Lakonishok
    1996): names with a large standardised earnings surprise (SUE) keep drifting in the
    surprise's direction for weeks, and the drift is strongest when the earnings surprise
    and the announcement-window price reaction AGREE — high SUE with a high abnormal
    return (Chan, Jegadeesh & Lakonishok, "Momentum strategies", JF 1996). That is E23.
  * Ben-Rephael, Da & Israelsen and the "non-fundamental gap" line of work: the part of
    the announcement return the surprise does NOT explain — the residual of regressing the
    announcement return on SUE across the cross-section — reverses, about 1% over the
    following 21 sessions. A name that gapped far more than its surprise warranted is a
    mean-reversion candidate, not a momentum one. That is E22.

Neither is in the score. Each is a feature on the row for `ic.py --by-feature` and the
recipes in docs/BACKTEST.md §6d, under the same rule as every S-04 feature: a sign test in
sample, then hold-out, then a ledger row, before anything is promoted.

THE STAGED FILE — $SCAN_DIR/earnings_history.json, written by the scheduled task from
`get_earnings_results` (one symbol per call, trailing up to 8 quarters):

    {
      "NVDA": [
        {"fiscal_quarter": "2026Q2", "report_date": "2026-08-26", "timing": "pm",
         "eps_actual": 1.05, "eps_estimate": 1.01, "surprise_pct": 3.96,
         "revenue_actual": 46700000000, "revenue_estimate": 46000000000},
        ...
      ],
      ...
    }

THE MAPPING from the connector's rows (the Robinhood earnings record — `from_connector()`
implements it, and it also accepts rows already in the file's shape):

    connector field                    file field
    ---------------                    ----------
    year + quarter  (2026, 2)      ->  fiscal_quarter  "2026Q2"
    report.date                    ->  report_date     (YYYY-MM-DD)
    report.timing                  ->  timing          "am" | "pm"
    eps.actual                     ->  eps_actual
    eps.estimate                   ->  eps_estimate
    (actual − estimate) / |estimate| × 100
                                   ->  surprise_pct    (recomputed; a supplied value is kept)
    revenue.actual / .estimate     ->  revenue_actual / revenue_estimate (optional)

A quarter with no actual (an upcoming report) is kept in the file for `days_since_earnings`
to ignore and contributes nothing to SUE. `report_date` is the announcement date. Rows
dated after `as_of` are excluded from everything — the run cannot know them yet.

THE FEATURES (`FEATURE_KEYS`), every one null when its inputs are missing:

    sue                 latest (eps_actual − eps_estimate) / sample std of the last up to 8
                        surprises (eps_actual − eps_estimate, the latest included). Null with
                        fewer than 4 reported quarters or a zero std. Unitless.
    ear_3d              the announcement-window cumulative abnormal return: Σ over sessions
                        t−1, t, t+1 of (stock daily return − SPY daily return), where t is
                        the first session on or after report_date — or the session AFTER it
                        when timing is "pm" (an after-close print trades the next day). A
                        fraction. Null without SPY bars, or without bars through t+1 (a
                        report inside the last two sessions has no window yet).
    reg_residual        the "non-fundamental gap": ear_3d minus the pooled cross-sectional
                        OLS fit of ear_3d on sue (with intercept) across every symbol in the
                        run that has both. Null when fewer than MIN_CROSS_SECTION (5) names
                        have both, or the design is singular. Expected sign over 21 sessions:
                        NEGATIVE (it reverses).
    earnings_agreement  Chan, Jegadeesh & Lakonishok's same-sign gate: +1 when sue ≥
                        SUE_HIGH (1.0) and ear_3d ≥ EAR_HIGH (0.02); −1 when sue ≤ −1.0 and
                        ear_3d ≤ −0.02; 0 otherwise; null when either input is null.
    days_since_earnings sessions (weekdays) from the latest reported quarter's report_date
                        to as_of; null when no reported quarter is on or before as_of.

Usage
    python3 earnings_quality.py --history earnings_history.json --bars bars.json \\
                                --as-of 2026-09-10 --out earnings_quality.json
    # scanner.py picks up $SCAN_DIR/earnings_quality.json on its own and merges the five
    # keys into each row's `features` dict (null for a symbol the file does not cover).

`--bars` is the same get_equity_historicals payload technicals.py reads and MUST include
SPY; without SPY every ear_3d is null and so is every reg_residual. stdlib only.
"""
import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from technicals import _clean_bars, ols   # noqa: E402 — engine module, stdlib underneath

FEATURE_KEYS = ("sue", "ear_3d", "reg_residual", "earnings_agreement", "days_since_earnings")
OUT_FILE = "earnings_quality.json"

SUE_WINDOW = 8            # quarters in the surprise std (the connector's trailing maximum)
SUE_MIN_QUARTERS = 4      # fewer reported quarters than this: sue is null
SUE_HIGH = 1.0            # |sue| at or above one std of its own surprises is "high"
EAR_HIGH = 0.02           # |ear_3d| at or above 2% is "high"
MIN_CROSS_SECTION = 5     # names with both sue and ear_3d before the pooled fit runs


# ---------------------------------------------------------------- small helpers
def _f(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _std(xs, ddof=1):
    n = len(xs)
    if n - ddof < 1:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - ddof))


def sessions_between(a, b):
    """Weekdays strictly after `a` up to and including `b` (negative when b < a)."""
    if b < a:
        return -sessions_between(b, a)
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


# ---------------------------------------------------------------- the history file
def from_connector(rows):
    """One symbol's quarters in the file's shape, from the connector's rows (or from rows
    already in the file's shape — both are accepted, field by field). Sorted oldest first.
    Rows with no report date are dropped."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rep = r.get("report") if isinstance(r.get("report"), dict) else {}
        eps = r.get("eps") if isinstance(r.get("eps"), dict) else {}
        rev = r.get("revenue") if isinstance(r.get("revenue"), dict) else {}
        rd = _date(r.get("report_date") or rep.get("date"))
        if rd is None:
            continue
        fq = r.get("fiscal_quarter")
        if not fq and r.get("year") is not None and r.get("quarter") is not None:
            fq = f"{r['year']}Q{r['quarter']}"
        actual = _f(r.get("eps_actual", eps.get("actual")))
        est = _f(r.get("eps_estimate", eps.get("estimate")))
        sp = _f(r.get("surprise_pct"))
        if sp is None and actual is not None and est not in (None, 0.0):
            sp = (actual - est) / abs(est) * 100.0
        out.append({
            "fiscal_quarter": fq, "report_date": rd.isoformat(),
            "timing": (r.get("timing") or rep.get("timing") or None),
            "eps_actual": actual, "eps_estimate": est, "surprise_pct": sp,
            "revenue_actual": _f(r.get("revenue_actual", rev.get("actual"))),
            "revenue_estimate": _f(r.get("revenue_estimate", rev.get("estimate"))),
        })
    out.sort(key=lambda q: q["report_date"])
    return out


def load_history(raw):
    """{SYMBOL: [quarters oldest first]} from the staged file's object, whatever mix of
    shapes its rows are in. Anything that is not a {symbol: list} map is empty."""
    if not isinstance(raw, dict):
        return {}
    return {str(k).upper(): from_connector(v) for k, v in raw.items() if isinstance(v, list)}


def reported_upto(history, as_of):
    """The quarters with an actual EPS and a report date on or before as_of."""
    d = _date(as_of)
    return [q for q in history or []
            if q.get("eps_actual") is not None and _date(q.get("report_date")) is not None
            and (d is None or _date(q["report_date"]) <= d)]


# ---------------------------------------------------------------- the features
def sue(history, as_of=None):
    """Standardised unexpected earnings: latest surprise over the sample std of the last
    up to SUE_WINDOW surprises (latest included). Null under SUE_MIN_QUARTERS reported
    quarters, with a missing estimate on the latest, or with a zero std."""
    qs = [q for q in reported_upto(history, as_of) if q.get("eps_estimate") is not None]
    if len(qs) < SUE_MIN_QUARTERS:
        return None
    surprises = [q["eps_actual"] - q["eps_estimate"] for q in qs[-SUE_WINDOW:]]
    sd = _std(surprises)
    if not sd:
        return None
    return surprises[-1] / sd


def _event_index(dates, report_date, timing=None):
    """Index of the event session t in `dates` (sorted ISO strings): the first session on
    or after report_date, plus one when the print was after the close. None when the
    bars do not reach it."""
    rd = _date(report_date)
    if rd is None:
        return None
    iso = rd.isoformat()
    i = next((k for k, d in enumerate(dates) if d >= iso), None)
    if i is None:
        return None
    if str(timing or "").lower() == "pm" and dates[i] == iso:
        i += 1
    return i if i < len(dates) else None


def ear(bars, report_date, spy_bars, timing=None):
    """Cumulative abnormal return over sessions t−1, t, t+1 against SPY, aligned by date.
    Null without both series covering close[t−2] .. close[t+1]."""
    rows = {r["t"]: r["c"] for r in _clean_bars(bars)}
    spy = {r["t"]: r["c"] for r in _clean_bars(spy_bars)} if spy_bars else {}
    dates = sorted(set(rows) & set(spy))
    if len(dates) < 4:
        return None
    t = _event_index(dates, report_date, timing)
    if t is None or t < 2 or t + 1 >= len(dates):
        return None
    car = 0.0
    for k in (t - 1, t, t + 1):
        a, b = dates[k - 1], dates[k]
        if not rows[a] or not spy[a]:
            return None
        car += (rows[b] / rows[a] - 1.0) - (spy[b] / spy[a] - 1.0)
    return car


def reg_residual(ear_value, sue_value, cross_section):
    """The non-fundamental gap: ear minus the pooled OLS fit of ear on sue (with
    intercept) across `cross_section` = [(sue, ear), ...] for the whole universe. The pair
    being measured should be IN the cross-section (it is, when build() calls this). Null
    with either input null, under MIN_CROSS_SECTION usable pairs, or a singular design."""
    if ear_value is None or sue_value is None:
        return None
    pairs = [(s, e) for s, e in (cross_section or []) if s is not None and e is not None]
    if len(pairs) < MIN_CROSS_SECTION:
        return None
    fit = ols([e for _, e in pairs], [[s for s, _ in pairs]], intercept=True)
    if fit is None:
        return None
    coef, a, _ = fit
    return ear_value - (a + coef[0] * sue_value)


def agreement(sue_value, ear_value):
    """+1 / −1 when the surprise and the announcement reaction are both high with the same
    sign, 0 when not, null when either is null."""
    if sue_value is None or ear_value is None:
        return None
    if sue_value >= SUE_HIGH and ear_value >= EAR_HIGH:
        return 1
    if sue_value <= -SUE_HIGH and ear_value <= -EAR_HIGH:
        return -1
    return 0


def days_since_earnings(history, as_of):
    qs = reported_upto(history, as_of)
    if not qs:
        return None
    return sessions_between(_date(qs[-1]["report_date"]), _date(as_of))


def features_for(history, bars, spy_bars, as_of):
    """The per-symbol features (reg_residual left null — it needs the cross-section)."""
    out = {k: None for k in FEATURE_KEYS}
    qs = reported_upto(history, as_of)
    if not qs:
        return out
    latest = qs[-1]
    out["sue"] = sue(history, as_of)
    out["ear_3d"] = ear(bars, latest["report_date"], spy_bars, latest.get("timing"))
    out["earnings_agreement"] = agreement(out["sue"], out["ear_3d"])
    out["days_since_earnings"] = days_since_earnings(history, as_of)
    return out


def build(history_map, bars_map, as_of, benchmark="SPY"):
    """{SYMBOL: features} for every symbol in history_map, then the pooled regression
    across them for reg_residual. bars_map is {SYMBOL: bars} and must carry the benchmark
    for ear_3d to exist. Symbols with no bars still get sue and days_since_earnings."""
    spy = (bars_map or {}).get(benchmark)
    out = {}
    for sym, hist in (history_map or {}).items():
        out[sym] = features_for(hist, (bars_map or {}).get(sym), spy, as_of)
    cross = [(f["sue"], f["ear_3d"]) for f in out.values()]
    for f in out.values():
        f["reg_residual"] = reg_residual(f["ear_3d"], f["sue"], cross)
    return out


def bars_map_of(raw):
    """{SYMBOL: bars} from the get_equity_historicals payload (or its results array, or a
    map already in that shape)."""
    if isinstance(raw, dict) and "data" in raw:
        raw = (raw.get("data") or {}).get("results")
    elif isinstance(raw, dict) and "results" in raw:
        raw = raw.get("results")
    if isinstance(raw, dict):
        return {str(k).upper(): v for k, v in raw.items() if isinstance(v, list)}
    out = {}
    for res in raw or []:
        if isinstance(res, dict) and res.get("symbol"):
            out[str(res["symbol"]).upper()] = res.get("bars") or []
    return out


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Earnings-quality features (E22/E23), logged not scored.")
    ap.add_argument("--history", required=True, help="earnings_history.json — see the docstring")
    ap.add_argument("--bars", help="get_equity_historicals payload; MUST include SPY for ear_3d")
    ap.add_argument("--as-of", dest="as_of", default=date.today().isoformat())
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--out", default=OUT_FILE)
    a = ap.parse_args(argv)

    hist = load_history(json.load(open(a.history, encoding="utf-8")))
    bars = bars_map_of(json.load(open(a.bars, encoding="utf-8"))) if a.bars else {}
    if a.bars and a.benchmark.upper() not in bars:
        print(f"note: {a.benchmark} not in the bars file — ear_3d and reg_residual will be "
              "null for every symbol", file=sys.stderr)
    out = build(hist, bars, a.as_of, benchmark=a.benchmark.upper())
    path = a.out if os.path.isabs(a.out) else os.path.join(BASE, a.out)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    n_sue = sum(1 for f in out.values() if f["sue"] is not None)
    n_ear = sum(1 for f in out.values() if f["ear_3d"] is not None)
    n_reg = sum(1 for f in out.values() if f["reg_residual"] is not None)
    print(f"{path}: {len(out)} symbol(s); sue on {n_sue}, ear_3d on {n_ear}, "
          f"reg_residual on {n_reg} (needs {MIN_CROSS_SECTION}+ names with both)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
