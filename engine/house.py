"""house.py — house-level concentration and exposure metrics (K-03, 2026-09-10).

HOUSE-01 (pm.py) closed the per-symbol and per-sector gap across the three desks with two
caps. Those caps answer "is any one name or sector too big?" and nothing else. They cannot
tell a book of eight names that all move together from a book of eight independent bets,
they cannot see that every desk is long the same factor, and they cannot say how much of
the combined book is a single position dressed as seven. This module measures those things.

Everything here is a PURE FUNCTION over the combined book: a list of holdings
(symbol, sector, notional, desk) plus the combined equity, and — only when a `bars.json`
file was staged in the run directory — per-symbol daily return series. Nothing reads a
file, nothing mutates a book, nothing places or blocks an order by itself. pm.py calls
`metrics()` once per run and `assess()` on the result; with `enforce` off (the default)
the numbers and flags are reported in the state, the journal and the coverage row and
gate nothing. With `enforce` on a breach refuses NEW ENTRIES house-wide. Exits are never
touched by anything in this file, in either mode.

DEFINITIONS (all weights are fractions of COMBINED equity unless stated)
  w_i                  notional_i / combined_equity, summed per symbol across desks.
  N_eff (weights)      1 / Σ w̃_i²  — the Herfindahl "effective number of names", where
                       w̃ = w / Σw renormalises the invested book to sum to 1. Cash is not
                       a bet, so it is excluded from the count: a single 10% position is
                       one bet, not a hundred. Eight equal names → 8.
  N_eff (corr)         1 / (w̃ᵀ ρ w̃) — the same idea with a correlation matrix ρ. With
                       ρ = I it reduces to N_eff (weights) exactly, which is the
                       normalisation the two share. Eight equal names at ρ̄ = 0.6 → ≈ 1.5:
                       eight tickers, one and a half bets.
  N_eff (proxy)        N_eff (corr) with ρ = 1 inside a GICS sector and ρ_default across
                       sectors, used when no bars are staged. Harsher than a measured ρ
                       by construction — it treats six semiconductor names as one — and
                       labelled "proxy" everywhere it is shown.
  β·w                  Σ w_i β_i with w as a fraction of combined equity (cash carries β 0),
                       β from 252 daily returns against the benchmark in the bars file.
                       Null, never 0, without bars: an unmeasured beta is not a low one.
  momentum crowd       share of combined equity in names whose 12-1 month return
                       (close[-21] / close[-252] − 1) is over +50%. Null without bars.
  largest sector       biggest GICS sector as a share of combined equity (HOUSE-01's own
                       number, restated so one block carries the whole picture).
  top symbol           biggest single name as a share of combined equity.
  overlap              share of DISTINCT names held by two or more desks, and the share of
                       combined equity sitting in those names. The desk-overlap meter on
                       the mirror reads these two fields.

Stdlib only. No numpy: the books hold a dozen names and the matrices are tiny.
"""
import math

# Defaults. pm.PM_RULES["house_exposure"] is the live copy; this is the fallback when a
# caller passes nothing, and the single place the numbers are documented.
DEFAULT_RULES = {
    "min_n_eff": 3.0,               # fewer independent bets than this is a crowded house
    "max_beta_w": 0.8,              # combined beta-weighted exposure as a fraction of equity
    "max_momentum_crowd_pct": 60.0, # share of equity in names up >50% over 12-1 months
    "rho_default": 0.3,             # cross-sector correlation assumed by the sector proxy
    "enforce": False,               # False: report only. True: a breach blocks NEW entries
}

MOMENTUM_RET_12_1_MIN = 0.5         # ret_12_1 above this counts as a crowded momentum name
BETA_WINDOW = 252                   # daily returns in the beta and correlation window
MIN_RETURNS_FOR_BETA = 60           # fewer aligned returns than this and beta is null
MIN_RETURNS_FOR_CORR = 40           # fewer aligned returns than this and the pair falls
                                    # back to the sector proxy for that pair


# ------------------------------------------------------------------ weights
def weights(positions, combined_equity):
    """{symbol: w} with w = notional / combined_equity, summed per symbol across desks.

    `positions` is any iterable of dicts carrying `symbol` and `notional` (pm.py's
    house_exposure builds them from positions and working buy orders alike). Symbols with
    a non-positive notional are dropped: a short or an unpriced line is not a weight.
    """
    if not combined_equity or combined_equity <= 0:
        return {}
    out = {}
    for p in positions or []:
        sym = p.get("symbol")
        nv = p.get("notional")
        if not sym or not isinstance(nv, (int, float)) or nv <= 0:
            continue
        out[sym] = out.get(sym, 0.0) + float(nv) / float(combined_equity)
    return out


def _renorm(w):
    tot = sum(v for v in w.values() if v > 0)
    if tot <= 0:
        return {}
    return {k: v / tot for k, v in w.items() if v > 0}


def n_eff_weights(w):
    """Herfindahl effective number of names: 1 / Σ w̃², w̃ renormalised to sum to 1.

    None for an empty book — there is no honest number of bets in nothing."""
    wn = _renorm(w or {})
    if not wn:
        return None
    h = sum(v * v for v in wn.values())
    return 1.0 / h if h > 0 else None


def n_eff_corr(w, corr):
    """Correlation-aware effective number of bets: 1 / (w̃ᵀ ρ w̃).

    `corr` is {(sym_a, sym_b): rho} or a nested {sym_a: {sym_b: rho}}; a missing pair is
    read as 0 and the diagonal is always 1, so with an empty `corr` this equals
    n_eff_weights(w) exactly. That is the normalisation: the two agree at ρ = I.
    """
    wn = _renorm(w or {})
    if not wn:
        return None
    syms = sorted(wn)
    q = 0.0
    for a in syms:
        for b in syms:
            rho = 1.0 if a == b else _lookup(corr, a, b)
            q += wn[a] * wn[b] * rho
    return 1.0 / q if q > 0 else None


def _lookup(corr, a, b):
    if not corr:
        return 0.0
    if isinstance(corr, dict):
        v = corr.get((a, b))
        if v is None:
            v = corr.get((b, a))
        if v is None and isinstance(corr.get(a), dict):
            v = corr[a].get(b)
        if v is None and isinstance(corr.get(b), dict):
            v = corr[b].get(a)
        return float(v) if isinstance(v, (int, float)) else 0.0
    return 0.0


def sector_proxy_corr(sectors, rho_default=DEFAULT_RULES["rho_default"]):
    """ρ = 1 inside a GICS sector, rho_default across sectors. `sectors` is {symbol: gics}.

    'Unclassified' names are treated as their own sector each — an unknown sector is not
    evidence of independence, but it is not evidence of a shared one either, so they get
    the cross-sector default against everything."""
    syms = sorted(sectors or {})
    out = {}
    for i, a in enumerate(syms):
        for b in syms[i + 1:]:
            ga, gb = sectors.get(a), sectors.get(b)
            same = bool(ga) and ga == gb and ga != "Unclassified"
            out[(a, b)] = 1.0 if same else float(rho_default)
    return out


def n_eff_sector_proxy(w, sectors, rho_default=DEFAULT_RULES["rho_default"]):
    """n_eff_corr under the sector proxy. What the house reports when no bars are staged."""
    return n_eff_corr(w, sector_proxy_corr(sectors, rho_default))


# ------------------------------------------------------------------ factor exposures
def beta_exposure(w, betas):
    """Σ w_i β_i over names with a KNOWN beta, w as a fraction of combined equity.

    None when no beta is known for any held name. Names without a beta are left out of
    the sum and reported by the caller as uncovered — silently treating them as β = 0
    would make an unmeasured book look hedged.
    """
    if not w or not betas:
        return None
    tot, seen = 0.0, 0
    for sym, wi in w.items():
        b = betas.get(sym)
        if isinstance(b, (int, float)) and not isinstance(b, bool):
            tot += wi * float(b)
            seen += 1
    return tot if seen else None


def factor_exposure(positions, features, combined_equity=None, w=None):
    """Momentum crowding: share of combined equity in names with ret_12_1 > 0.5.

    `features` is {symbol: {"ret_12_1": float|None, ...}} — the shape technicals'
    per-symbol map has, or the one this module derives from bars. None when no features
    are available at all. A held name that has no ret_12_1 (short history) does not count
    as crowded; the returned dict says how much of the book was actually measured.
    """
    if not features:
        return None
    if w is None:
        w = weights(positions, combined_equity)
    crowd, covered, names = 0.0, 0.0, []
    for sym, wi in w.items():
        r = (features.get(sym) or {}).get("ret_12_1")
        if not isinstance(r, (int, float)) or isinstance(r, bool):
            continue
        covered += wi
        if r > MOMENTUM_RET_12_1_MIN:
            crowd += wi
            names.append(sym)
    return {"momentum_crowd_pct": round(crowd * 100.0, 2),
            "measured_pct": round(covered * 100.0, 2),
            "crowded_names": sorted(names)}


# ------------------------------------------------------------------ options desk (D-02)
OPTIONS_MULTIPLIER = 100


def options_equity(book):
    """An options book's equity: cash less the debit to close every open structure at its
    last mark (value × multiplier × contracts). Mirrors options_desk.desk_equity()."""
    liab = 0.0
    for s in (book or {}).get("structures") or []:
        if s.get("status", "open") != "open":
            continue
        liab += (float(s.get("value") or 0.0) * float(s.get("contracts") or 0)
                 * float(s.get("multiplier") or OPTIONS_MULTIPLIER))
    return round(float((book or {}).get("cash") or 0.0) - liab, 2)


def options_holdings(book, desk="options"):
    """The house-exposure lines for an options book's open structures.

    THE CONVERSION. A defined-risk put credit spread has a net delta (short put delta
    negated plus long put delta — positive, a hidden long). Its EQUITY EQUIVALENT is what
    a share position with the same first-order sensitivity to the underlying would be:

        notional = |net delta per share| × multiplier × contracts × spot × beta

    with beta the underlying's beta to SPY (1.0 for SPY / XSP / SPX). A structure the
    desk has not been able to mark (no delta, no spot) contributes nothing and is not
    reported as hedged — it is simply absent, exactly as an unpriced share position is
    from beta_exposure(). Sector is "Index" for an index underlying, else the structure's
    own `sector`. One line per structure, `kind: "options-delta"`."""
    out = []
    for s in (book or {}).get("structures") or []:
        if s.get("status", "open") != "open":
            continue
        g = s.get("greeks") or {}
        delta = g.get("delta")
        spot = s.get("spot")
        if not isinstance(delta, (int, float)) or not isinstance(spot, (int, float)) or spot <= 0:
            continue
        beta = s.get("beta") if isinstance(s.get("beta"), (int, float)) else 1.0
        notional = (abs(float(delta)) * float(s.get("multiplier") or OPTIONS_MULTIPLIER)
                    * float(s.get("contracts") or 0) * float(spot) * float(beta))
        if notional <= 0:
            continue
        out.append({"desk": desk, "symbol": str(s.get("underlying") or "?").upper(),
                    "sector": s.get("sector") or "Index", "notional": round(notional, 2),
                    "kind": "options-delta", "structure_id": s.get("id"),
                    "net_delta": round(float(delta), 4)})
    return out


# ------------------------------------------------------------------ concentration
def largest_sector_share(positions, combined_equity):
    """(sector, share of combined equity) for the biggest GICS sector, or (None, None)."""
    if not combined_equity or combined_equity <= 0:
        return None, None
    by = {}
    for p in positions or []:
        nv = p.get("notional")
        if not isinstance(nv, (int, float)) or nv <= 0:
            continue
        g = p.get("sector") or p.get("gics") or "Unclassified"
        by[g] = by.get(g, 0.0) + float(nv)
    if not by:
        return None, None
    g, nv = max(by.items(), key=lambda kv: kv[1])
    return g, nv / float(combined_equity)


def top_symbol_share(w):
    """(symbol, w) for the largest single name, or (None, None)."""
    if not w:
        return None, None
    s, v = max(w.items(), key=lambda kv: kv[1])
    return s, v


def pairwise_overlap(positions, combined_equity):
    """Names held by two or more desks.

    Returns {"names_pct": share of distinct names held by ≥ 2 desks,
             "equity_pct": share of combined equity sitting in those names,
             "names": [..], "distinct": n}.
    A desk holding a name AND a working buy on it counts once for that desk.
    """
    desks_by_sym, nv_by_sym = {}, {}
    for p in positions or []:
        sym, nv = p.get("symbol"), p.get("notional")
        if not sym or not isinstance(nv, (int, float)) or nv <= 0:
            continue
        desks_by_sym.setdefault(sym, set()).add(p.get("desk") or "?")
        nv_by_sym[sym] = nv_by_sym.get(sym, 0.0) + float(nv)
    if not desks_by_sym:
        return {"names_pct": None, "equity_pct": None, "names": [], "distinct": 0}
    shared = sorted(s for s, d in desks_by_sym.items() if len(d) >= 2)
    eq = float(combined_equity) if combined_equity and combined_equity > 0 else None
    return {
        "names_pct": round(len(shared) / len(desks_by_sym) * 100.0, 2),
        "equity_pct": (round(sum(nv_by_sym[s] for s in shared) / eq * 100.0, 2)
                       if eq else None),
        "names": shared, "distinct": len(desks_by_sym),
    }


# ------------------------------------------------------------------ bars → features
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def bars_rows(raw):
    """Accept the whole get_equity_historicals response, its data block, or the bare
    results array — the same three shapes technicals.py accepts."""
    if isinstance(raw, dict):
        d = raw.get("data")
        r = d.get("results") if isinstance(d, dict) else None
        if r is None:
            r = raw.get("results")
        return r if isinstance(r, list) else []
    return raw if isinstance(raw, list) else []


def close_series(raw):
    """{SYMBOL: [(date, close), ...]} oldest-first, interpolated bars dropped."""
    out = {}
    for res in bars_rows(raw):
        if not isinstance(res, dict):
            continue
        sym = str(res.get("symbol") or "").upper()
        if not sym:
            continue
        rows = []
        for b in res.get("bars") or []:
            if not isinstance(b, dict) or b.get("interpolated"):
                continue
            c = _f(b.get("close_price"))
            if c is None or c <= 0:
                continue
            rows.append((str(b.get("begins_at") or "")[:10], c))
        rows.sort(key=lambda r: r[0])
        if rows:
            out[sym] = rows
    return out


def daily_returns(series):
    """{SYMBOL: {date: simple return}} from close_series() output."""
    out = {}
    for sym, rows in series.items():
        rets = {}
        for (_, c0), (d1, c1) in zip(rows, rows[1:]):
            if c0 > 0:
                rets[d1] = c1 / c0 - 1.0
        out[sym] = rets
    return out


def _aligned(ra, rb, window):
    dates = sorted(set(ra) & set(rb))[-window:]
    return [ra[d] for d in dates], [rb[d] for d in dates]


def _cov(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (n - 1)


def beta_of(ra, rb, window=BETA_WINDOW, min_n=MIN_RETURNS_FOR_BETA):
    xs, ys = _aligned(ra, rb, window)
    if len(xs) < min_n:
        return None
    vb = _cov(ys, ys)
    if not vb:
        return None
    return _cov(xs, ys) / vb


def corr_of(ra, rb, window=BETA_WINDOW, min_n=MIN_RETURNS_FOR_CORR):
    xs, ys = _aligned(ra, rb, window)
    if len(xs) < min_n:
        return None
    va, vb = _cov(xs, xs), _cov(ys, ys)
    if not va or not vb:
        return None
    return _cov(xs, ys) / math.sqrt(va * vb)


def ret_12_1(rows):
    """12-1 month return: close 21 bars ago over close 252 bars ago, minus one. None on
    short history — the standard momentum definition skips the most recent month."""
    if len(rows) < BETA_WINDOW + 1:
        return None
    c_then, c_skip = rows[-1 - BETA_WINDOW][1], rows[-1 - 21][1]
    return c_skip / c_then - 1.0 if c_then > 0 else None


def features_from_bars(raw, symbols=None, benchmark="SPY"):
    """{SYMBOL: {"beta_252": float|None, "ret_12_1": float|None, "bars_used": n}} plus
    the pairwise correlation map, from a staged bars file.

    Prefers technicals.features() for beta_252 / ret_12_1 when a future technicals.py
    exposes one (imported lazily, any failure ignored), and fills whatever it did not
    supply from the bars directly. The benchmark must be in the same bars file; without
    it every beta is None and the caller reports β·w as n/a, never 0.

    Returns (features, corr, meta). `meta` records the benchmark used and the symbols
    with no bars so the report can say what was measured.
    """
    series = close_series(raw)
    if not series:
        return {}, {}, {"benchmark": None, "no_bars": sorted(symbols or [])}
    rets = daily_returns(series)
    bench = (benchmark or "").upper()
    rb = rets.get(bench)
    want = sorted(symbols) if symbols else sorted(series)
    feats = {}
    ext = _technicals_features(raw)
    for sym in want:
        rows = series.get(sym)
        f = {"beta_252": None, "ret_12_1": None, "bars_used": len(rows) if rows else 0}
        e = ext.get(sym) if isinstance(ext, dict) else None
        if isinstance(e, dict):
            for k in ("beta_252", "ret_12_1"):
                v = e.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    f[k] = float(v)
        if rows:
            if f["beta_252"] is None and rb is not None and sym != bench:
                f["beta_252"] = beta_of(rets[sym], rb)
            if f["ret_12_1"] is None:
                f["ret_12_1"] = ret_12_1(rows)
        feats[sym] = f
    corr = {}
    held = [s for s in want if s in rets]
    for i, a in enumerate(held):
        for b in held[i + 1:]:
            c = corr_of(rets[a], rets[b])
            if c is not None:
                corr[(a, b)] = c
    meta = {"benchmark": bench if rb is not None else None,
            "no_bars": [s for s in want if s not in series]}
    return feats, corr, meta


def _technicals_features(raw):
    """Lazy hook: technicals.features(rows) -> {SYM: {beta_252, ret_12_1}} if it exists.
    The module is imported here and not at the top so house.py has no import-time
    dependency on the scanner's toolchain, and any failure falls back to the local
    computation rather than failing the manager."""
    try:
        import technicals
        fn = getattr(technicals, "features", None)
        if fn is None:
            return {}
        out = fn(bars_rows(raw))
        return out if isinstance(out, dict) else {}
    except Exception:       # noqa: BLE001 — a helper that cannot fail the run
        return {}


# ------------------------------------------------------------------ the block
def metrics(positions, combined_equity, bars=None, rules=None, benchmark="SPY"):
    """The house exposure block. Pure: positions + equity (+ optional bars) in, dict out.

    positions: [{"symbol", "sector"|"gics", "notional", "desk"}, ...] across every desk,
               positions and working buy orders alike (pm.house_exposure builds it).
    bars:      the staged bars.json content, or None. Decides whether n_eff is measured
               or a sector proxy, and whether beta / momentum crowding exist at all.
    """
    rules = dict(DEFAULT_RULES, **(rules or {}))
    positions = [p for p in (positions or [])
                 if isinstance(p.get("notional"), (int, float)) and p["notional"] > 0]
    eq = float(combined_equity) if combined_equity and combined_equity > 0 else 0.0
    w = weights(positions, eq)
    sectors = {}
    for p in positions:
        sectors.setdefault(p["symbol"], p.get("sector") or p.get("gics") or "Unclassified")

    n_w = n_eff_weights(w)
    rho = float(rules.get("rho_default", DEFAULT_RULES["rho_default"]))
    proxy_corr = sector_proxy_corr(sectors, rho)
    n_proxy = n_eff_corr(w, proxy_corr)

    feats, corr, bmeta = ({}, {}, {"benchmark": None, "no_bars": sorted(w)})
    if bars is not None:
        feats, corr, bmeta = features_from_bars(bars, symbols=list(w), benchmark=benchmark)
    have_bars = bool(feats) and any(f.get("bars_used") for f in feats.values())

    if have_bars and corr:
        # Measured pairs win; a pair with too little shared history keeps the proxy value
        # so a thin bars file degrades to the proxy pair by pair, never to ρ = 0.
        merged = dict(proxy_corr)
        merged.update(corr)
        n_eff, basis = n_eff_corr(w, merged), "measured"
        measured_pairs = len(corr)
    else:
        n_eff, basis, measured_pairs = n_proxy, "proxy", 0

    betas = {s: f.get("beta_252") for s, f in feats.items()} if have_bars else {}
    beta_w = beta_exposure(w, betas)
    beta_cov = (round(sum(wi for s, wi in w.items()
                          if isinstance(betas.get(s), (int, float))) * 100.0, 2)
                if beta_w is not None else None)
    momentum = factor_exposure(positions, feats, w=w) if have_bars else None
    sector, sector_share = largest_sector_share(positions, eq)
    top_sym, top_w = top_symbol_share(w)
    overlap = pairwise_overlap(positions, eq)

    pairs_total = len(w) * (len(w) - 1) // 2
    out = {
        "equity": round(eq, 2),
        "names": len(w),
        "invested_pct": round(sum(w.values()) * 100.0, 2),
        "n_eff": round(n_eff, 2) if n_eff is not None else None,
        "n_eff_basis": basis,
        "n_eff_weights": round(n_w, 2) if n_w is not None else None,
        "n_eff_proxy": round(n_proxy, 2) if n_proxy is not None else None,
        "rho_default": rho,
        "corr_pairs_measured": measured_pairs,
        "corr_pairs_total": pairs_total,
        "beta_w": round(beta_w, 3) if beta_w is not None else None,
        "beta_coverage_pct": beta_cov,
        "beta_benchmark": bmeta.get("benchmark"),
        "momentum_crowd_pct": momentum["momentum_crowd_pct"] if momentum else None,
        "momentum_measured_pct": momentum["measured_pct"] if momentum else None,
        "momentum_names": momentum["crowded_names"] if momentum else [],
        "largest_sector": sector,
        "largest_sector_pct": round(sector_share * 100.0, 2) if sector_share is not None else None,
        "top_symbol": top_sym,
        "top_symbol_pct": round(top_w * 100.0, 2) if top_w is not None else None,
        "overlap_pct": overlap["names_pct"],
        "overlap_equity_pct": overlap["equity_pct"],
        "overlap_names": overlap["names"],
        "bars": ("staged" if bars is not None else "absent"),
        "bars_missing_for": bmeta.get("no_bars") or [],
        "weights_pct": {s: round(v * 100.0, 2) for s, v in sorted(w.items(), key=lambda kv: -kv[1])},
        "rules": {k: rules.get(k) for k in DEFAULT_RULES},
    }
    out.update(assess(out, rules))
    return out


def assess(m, rules=None):
    """{"flags": [...], "block_new_entries": bool, "reasons": [...], "enforce": bool}.

    A metric that is None (unmeasured) never flags — an absent beta is not a low one and
    is not a high one either; it is reported as n/a. With enforce off nothing blocks.
    """
    rules = dict(DEFAULT_RULES, **(rules or {}))
    flags, reasons = [], []
    n_eff = m.get("n_eff")
    if isinstance(n_eff, (int, float)) and n_eff < float(rules["min_n_eff"]):
        flags.append("n_eff")
        reasons.append(f"house N_eff {n_eff:.2f} ({m.get('n_eff_basis') or 'proxy'}) under the "
                       f"{float(rules['min_n_eff']):.1f} floor — {m.get('names', 0)} names, "
                       f"{n_eff:.1f} independent bet(s)")
    bw = m.get("beta_w")
    if isinstance(bw, (int, float)) and bw > float(rules["max_beta_w"]):
        flags.append("beta_w")
        reasons.append(f"house β·w {bw:.2f} over the {float(rules['max_beta_w']):.2f} cap "
                       f"(vs {m.get('beta_benchmark') or 'benchmark'})")
    mc = m.get("momentum_crowd_pct")
    if isinstance(mc, (int, float)) and mc > float(rules["max_momentum_crowd_pct"]):
        flags.append("momentum_crowd")
        reasons.append(f"momentum crowding {mc:.0f}% of combined equity in names up >50% over "
                       f"12-1 months, over the {float(rules['max_momentum_crowd_pct']):.0f}% cap"
                       + (f" ({', '.join(m.get('momentum_names') or [])})"
                          if m.get("momentum_names") else ""))
    enforce = bool(rules.get("enforce"))
    return {"flags": flags, "reasons": reasons, "enforce": enforce,
            "block_new_entries": bool(enforce and flags)}


def compact(m):
    """The coverage-row / journal summary: the handful of numbers the desk-overlap meter
    and the daily health check read. Additive — a consumer that ignores it loses nothing."""
    if not m:
        return None
    return {"n_eff": m.get("n_eff"), "n_eff_basis": m.get("n_eff_basis"),
            "beta_w": m.get("beta_w"),
            "momentum_crowd_pct": m.get("momentum_crowd_pct"),
            "largest_sector": m.get("largest_sector"),
            "largest_sector_pct": m.get("largest_sector_pct"),
            "top_symbol": m.get("top_symbol"), "top_symbol_pct": m.get("top_symbol_pct"),
            "overlap_pct": m.get("overlap_pct"), "overlap_equity_pct": m.get("overlap_equity_pct"),
            "flags": list(m.get("flags") or []),
            "enforce": bool(m.get("enforce")),
            "block_new_entries": bool(m.get("block_new_entries"))}


def console_line(m):
    """One line for the run log: `house: n_eff 1.3 (proxy) · β·w n/a · sector 34% IT · overlap 57%`."""
    if not m:
        return "house: exposure not measured (no peer book staged)"
    n = m.get("n_eff")
    bits = [f"n_eff {n:.1f} ({m.get('n_eff_basis') or 'proxy'})" if isinstance(n, (int, float))
            else "n_eff n/a"]
    bw = m.get("beta_w")
    bits.append(f"β·w {bw:.2f}" if isinstance(bw, (int, float)) else "β·w n/a")
    ls = m.get("largest_sector_pct")
    if isinstance(ls, (int, float)):
        bits.append(f"sector {ls:.0f}% {_abbr(m.get('largest_sector'))}")
    ov = m.get("overlap_pct")
    bits.append(f"overlap {ov:.0f}%" if isinstance(ov, (int, float)) else "overlap n/a")
    mc = m.get("momentum_crowd_pct")
    if isinstance(mc, (int, float)):
        bits.append(f"momentum crowd {mc:.0f}%")
    line = "house: " + " · ".join(bits)
    if m.get("flags"):
        line += "   FLAGS " + ",".join(m["flags"]) + (
            "  [ENFORCED — new entries blocked]" if m.get("block_new_entries") else "  [reported]")
    return line


_ABBR = {"Information Technology": "IT", "Financials": "Fin", "Health Care": "HC",
         "Consumer Discretionary": "CD", "Consumer Staples": "CS", "Communication Services": "Comm",
         "Industrials": "Ind", "Energy": "Energy", "Materials": "Mat", "Utilities": "Util",
         "Real Estate": "RE"}


def _abbr(sector):
    if not sector:
        return "?"
    return _ABBR.get(sector) or sector.split()[0]
