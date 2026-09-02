"""validate.py — does the Scan Desk score actually rank forward returns?

The cheapest possible test of the model, and the gate every later addition should pass
through. It never touches the book and never trades. It answers one question per horizon:
when the scanner said "this name scores 85 and that one scores 55", did the 85 go on to
outperform the 55 over the next 5 / 10 / 20 sessions?

Inputs
------
  --records DIR       a directory of compact scan records — the claude/scans/<run_id>.json
                      docs the scan archive writes (archive.py --record). Each carries
                      date, slot, time and rows with ticker, price, score, verdict, setup,
                      pillars and the numeric fields in archive.ROW_KEEP.
  --bars FILE         get_equity_historicals output for every ticker in the records — one
                      response, or a JSON list of responses concatenated (ten symbols per
                      call, so a month of scans is usually two or three calls).
  --horizons 5,10,20  forward windows in TRADING sessions.
  --out FILE          JSON results.   --md FILE  the human-readable report.

Method
------
  * One observation per (date, ticker): the LAST slot of the day, which carries the most
    information. Entry = the price the scan scored at; forward close = the bar N sessions
    after the scan date. Rows whose horizon has not fully elapsed are excluded, never
    padded.
  * Spearman rank correlation between score and forward return, with a t-approximation
    for the p-value. Rank correlation, because the claim is ordinal — "higher scores do
    better" — not that score is linear in return.
  * The same correlation for every pillar and for every reported-but-unscored field
    (relative strength, RSI, gap, relative volume...). This is how a data point earns its
    way into the score: it has to rank returns here first.
  * Mean return, median return and hit rate by verdict bucket and by setup, and the
    top-quintile minus bottom-quintile spread by score — the number a strategy actually
    monetises.
  * n under 30 per horizon is reported as NOT ENOUGH DATA. It will be, for the first few
    weeks. That is the honest answer, not a defect.

Everything here is a description of the paper record, not a forecast.
"""
import argparse, glob, json, math, os, sys
from datetime import date

FIELDS = ("rs_20d_vs_spy", "rs_60d_vs_spy", "ret_20d_pct", "rel_volume", "rsi_14", "gap_pct",
          "week52_change_pct", "upside_pct", "atr_pct", "coverage_pct", "vs_ma50_pct",
          "vs_ma200_pct", "implied_move_pct")
PILLARS = ("trend", "momentum", "fundamentals", "catalyst", "intelligence")
MIN_N = 30


def _f(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- statistics
def _ranks(xs):
    """Average ranks (ties share the mean rank)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        r = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = r
        i = j + 1
    return ranks


def spearman(xs, ys):
    """(rho, p_approx, n). Pearson on ranks; p from the t-approximation, two-sided."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 4:
        return None, None, n
    rx, ry = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None, None, n
    rho = sxy / math.sqrt(sxx * syy)
    if abs(rho) >= 1.0:
        return rho, 0.0, n
    t = rho * math.sqrt((n - 2) / (1 - rho * rho))
    # two-sided p from Student t with n-2 df, via the regularised incomplete beta
    p = _t_two_sided(t, n - 2)
    return rho, p, n


def _t_two_sided(t, df):
    x = df / (df + t * t)
    return _betainc(df / 2.0, 0.5, x)


def _betainc(a, b, x):
    """Regularised incomplete beta I_x(a, b) by continued fraction (Numerical Recipes)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b


def _betacf(a, b, x, itmax=200, eps=3e-12):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / (c if abs(c) > 1e-300 else 1e-300)
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / (c if abs(c) > 1e-300 else 1e-300)
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ---------------------------------------------------------------- data
def load_bars(path):
    """{TICKER: [(date, close), ...]} oldest first, from one response or a list of them."""
    raw = json.load(open(path))
    blocks = raw if isinstance(raw, list) and raw and isinstance(raw[0], dict) and "symbol" not in raw[0] \
        else [raw]
    series = {}
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            rows = (blk.get("data") or {}).get("results") or blk.get("results") or []
        for res in rows or []:
            sym = (res.get("symbol") or "").upper()
            if not sym:
                continue
            pts = []
            for b in res.get("bars") or []:
                if b.get("interpolated"):
                    continue
                c = _f(b.get("close_price"))
                d = str(b.get("begins_at") or "")[:10]
                if c and d:
                    pts.append((d, c))
            pts.sort()
            series.setdefault(sym, [])
            series[sym] = sorted({d: c for d, c in series[sym] + pts}.items())
    return series


def load_records(path):
    files = sorted(glob.glob(os.path.join(path, "*.json")))
    recs = []
    for fn in files:
        try:
            r = json.load(open(fn))
        except (ValueError, OSError):
            continue
        if isinstance(r, dict) and r.get("results") and r.get("date"):
            recs.append(r)
    return recs


def observations(records):
    """One row per (date, ticker): the last slot of the day."""
    best = {}
    for r in records:
        d, t = r.get("date"), str(r.get("time") or "")
        for row in r.get("results") or []:
            tk = row.get("ticker")
            if not tk or _f(row.get("score")) is None:
                continue
            key = (d, tk)
            if key not in best or t > best[key][0]:
                best[key] = (t, r.get("slot"), row)
    out = []
    for (d, tk), (t, slot, row) in sorted(best.items()):
        out.append({"date": d, "time": t, "slot": slot, "ticker": tk, "row": row})
    return out


def forward_returns(obs, series, horizons):
    for o in obs:
        s = series.get(o["ticker"].upper()) or []
        dates = [d for d, _ in s]
        o["fwd"] = {}
        if not s:
            continue
        # index of the scan day's bar (or the first bar after it)
        idx = next((i for i, d in enumerate(dates) if d >= o["date"]), None)
        if idx is None:
            continue
        entry = _f(o["row"].get("price")) or s[idx][1]
        for h in horizons:
            j = idx + h
            if j < len(s):
                o["fwd"][h] = (s[j][1] / entry - 1.0) * 100.0
    return obs


# ---------------------------------------------------------------- analysis
def bucket_stats(obs, key, h):
    groups = {}
    for o in obs:
        r = o["fwd"].get(h)
        if r is None:
            continue
        groups.setdefault(o["row"].get(key) or "—", []).append(r)
    return {g: {"n": len(v), "mean_pct": round(_mean(v), 2), "median_pct": round(_median(v), 2),
                "hit_rate": round(sum(1 for x in v if x > 0) / len(v), 3)}
            for g, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def quintile_spread(obs, h):
    pairs = [(_f(o["row"].get("score")), o["fwd"].get(h)) for o in obs]
    pairs = [p for p in pairs if p[0] is not None and p[1] is not None]
    if len(pairs) < 10:
        return None
    pairs.sort()
    q = max(1, len(pairs) // 5)
    bottom = [r for _, r in pairs[:q]]
    top = [r for _, r in pairs[-q:]]
    return {"top_mean_pct": round(_mean(top), 2), "bottom_mean_pct": round(_mean(bottom), 2),
            "spread_pct": round(_mean(top) - _mean(bottom), 2), "per_side_n": q}


def analyse(obs, horizons):
    out = {"horizons": {}, "n_observations": len(obs),
           "dates": sorted({o["date"] for o in obs}),
           "tickers": sorted({o["ticker"] for o in obs})}
    for h in horizons:
        ys = [o["fwd"].get(h) for o in obs]
        scores = [_f(o["row"].get("score")) for o in obs]
        rho, p, n = spearman(scores, ys)
        H = {"n": n, "enough_data": n >= MIN_N,
             "score_vs_return": {"spearman": round(rho, 3) if rho is not None else None,
                                 "p_value": round(p, 4) if p is not None else None},
             "quintile_spread": quintile_spread(obs, h),
             "by_verdict": bucket_stats(obs, "verdict", h),
             "by_setup": bucket_stats(obs, "setup", h),
             "pillars": {}, "fields": {}}
        for pl in PILLARS:
            xs = [_f((o["row"].get("pillars") or {}).get(pl)) for o in obs]
            r2, p2, n2 = spearman(xs, ys)
            H["pillars"][pl] = {"spearman": round(r2, 3) if r2 is not None else None,
                                "p_value": round(p2, 4) if p2 is not None else None, "n": n2}
        for fld in FIELDS:
            xs = [_f(o["row"].get(fld)) for o in obs]
            r3, p3, n3 = spearman(xs, ys)
            if n3 >= 4:
                H["fields"][fld] = {"spearman": round(r3, 3) if r3 is not None else None,
                                    "p_value": round(p3, 4) if p3 is not None else None, "n": n3}
        out["horizons"][str(h)] = H
    return out


def markdown(res, horizons):
    L = ["# Score validation — does the Scan Desk score rank forward returns?", ""]
    L.append(f"{res['n_observations']} observations across {len(res['dates'])} scan day(s) and "
             f"{len(res['tickers'])} tickers. One observation per (day, ticker): the last slot "
             "of the day. Entry = the price the scan scored at; forward return = the close N "
             "sessions later. Horizons that have not elapsed are excluded, never padded.")
    L.append("")
    for h in horizons:
        H = res["horizons"][str(h)]
        L.append(f"## {h}-session horizon — n = {H['n']}" +
                 ("" if H["enough_data"] else f"  ·  NOT ENOUGH DATA (under {MIN_N})"))
        s = H["score_vs_return"]
        L.append(f"- Spearman(score, forward return) = **{s['spearman']}** (p = {s['p_value']})"
                 if s["spearman"] is not None else "- Spearman: not computable yet")
        q = H["quintile_spread"]
        if q:
            L.append(f"- Top-quintile mean {q['top_mean_pct']:+.2f}% vs bottom-quintile "
                     f"{q['bottom_mean_pct']:+.2f}% → spread **{q['spread_pct']:+.2f}%** "
                     f"({q['per_side_n']} names a side)")
        if H["by_verdict"]:
            L.append("- By verdict: " + "; ".join(
                f"{k} n={v['n']} mean {v['mean_pct']:+.2f}% hit {v['hit_rate']:.0%}"
                for k, v in H["by_verdict"].items()))
        if H["by_setup"]:
            L.append("- By setup: " + "; ".join(
                f"{k} n={v['n']} mean {v['mean_pct']:+.2f}% hit {v['hit_rate']:.0%}"
                for k, v in H["by_setup"].items()))
        pl = ", ".join(f"{k} {v['spearman']}" for k, v in H["pillars"].items()
                       if v["spearman"] is not None)
        if pl:
            L.append(f"- Pillars (Spearman): {pl}")
        fl = sorted(((k, v) for k, v in H["fields"].items() if v["spearman"] is not None),
                    key=lambda kv: -abs(kv[1]["spearman"]))
        if fl:
            L.append("- Unscored fields, strongest first: " + ", ".join(
                f"{k} {v['spearman']} (p {v['p_value']})" for k, v in fl[:8]))
        L.append("")
    L.append("**Reading it.** A positive Spearman with a small p-value at a horizon means higher "
             "scores went on to earn more over that window; a field with a stronger, more "
             "significant correlation than the score itself is a candidate to be scored. "
             "Under 30 observations nothing here is evidence either way. This describes the "
             "paper record; it forecasts nothing.")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, help="directory of scan record JSONs")
    ap.add_argument("--bars", required=True, help="get_equity_historicals output(s)")
    ap.add_argument("--horizons", default="5,10,20")
    ap.add_argument("--out", default="validation.json")
    ap.add_argument("--md", default="validation.md")
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    recs = load_records(a.records)
    if not recs:
        sys.exit(f"REFUSED: no scan records with results in {a.records}")
    series = load_bars(a.bars)
    obs = forward_returns(observations(recs), series, horizons)
    missing = sorted({o["ticker"] for o in obs if not series.get(o["ticker"].upper())})
    res = analyse(obs, horizons)
    res["records_used"] = len(recs)
    res["tickers_without_bars"] = missing
    json.dump(res, open(a.out, "w"), indent=2)
    open(a.md, "w").write(markdown(res, horizons))
    for h in horizons:
        H = res["horizons"][str(h)]
        s = H["score_vs_return"]
        print(f"{h:>3}d  n={H['n']:<4} spearman={s['spearman']}  p={s['p_value']}"
              + ("" if H["enough_data"] else "   (not enough data)"))
    if missing:
        print("no bars for: " + ", ".join(missing), file=sys.stderr)
    print(f"-> {a.out}, {a.md}")


if __name__ == "__main__":
    main()
