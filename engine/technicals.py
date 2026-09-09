"""Turn raw Robinhood OHLCV bars into the technical fields the scanner scores.

Before 2026-08-31 the scan had no OHLC bars at all, so it had no true RSI, no ATR, and
only a half-usable relative volume scraped off a quote page. The Robinhood connector
returns a year of daily bars for up to ten symbols in ONE call, with no provenance gate
and no fetch cache, so every one of those gaps closes for the price of a single call.

Usage
-----
    # dump the raw `data.results` array from get_equity_historicals to bars.json
    python3 technicals.py --bars bars.json --out technicals.json
    # optionally fold in today's partial-session volume from get_equity_fundamentals:
    python3 technicals.py --bars bars.json --fundamentals fundamentals.json --out technicals.json
    # intraday slots: ALSO pass the raw get_equity_quotes response so gap_pct and the
    # 52-week fields describe TODAY'S session rather than the last completed bar:
    python3 technicals.py --bars bars.json --fundamentals fundamentals.json \
                          --quotes quotes.json --out technicals.json
    # pre-market slot: same, plus --premarket (no session open exists yet, so the gap
    # is the extended-hours print against the official prior close):
    python3 technicals.py --bars bars.json --fundamentals fundamentals.json \
                          --quotes quotes.json --premarket --out technicals.json

`technicals.json` is a {TICKER: {...}} map you merge straight into each candidate in
`scan_data.json`. Every value is computed from the bars — nothing is estimated, and any
field with too little history to be honest comes back null rather than approximated.

THE SESSION-DESCRIPTION FIX (README section 9.2, patched 2026-08-31)
--------------------------------------------------------------------
`get_equity_historicals` at interval=day returns bars only through the last CLOSED
session, so anything derived from the final bar describes the previous trading day.
MA50/MA200/RSI/ATR *should* be computed on completed bars and are untouched. But three
fields claim to describe today, and mid-session they were describing yesterday:
`gap_pct` (it published ESTC "+24.1% at the open" that was Friday's gap; the real
Monday gap was -2.51%), `week52_high`/`week52_low` (excluded today's range), and
`week52_change_pct` (measured to the prior close, not the live price).

Passing --fundamentals and --quotes now fixes all three inside this script:
  gap_pct            = today's open (fundamentals) vs official prior close (quotes)
  week52_high/low    = the exchange's own 52-week range (fundamentals), which includes today
  week52_change_pct  = bar-derived change rebased onto the live trade price (quotes)
Each symbol carries `gap_basis`: "session_open", "extended_hours" (--premarket), or
"prior_bar" when no live inputs were supplied — so the board can say which claim it is
making. Without --quotes the behaviour is byte-identical to the pre-fix script.

Indicator conventions: Wilder's smoothing for RSI and ATR (the standard, and what the
trading-system repo uses), simple moving averages for MA50/MA200.
"""
import argparse, json, sys

MIN_BARS = {"ma_50": 50, "ma_200": 200, "rsi_14": 15, "atr_14": 15, "avg_volume_20d": 20}


def _f(v):
    """Robinhood returns numbers as strings. Anything unparseable is a gap, not a zero."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def sma(values, period):
    vals = [v for v in values[-period:] if v is not None]
    if len(vals) < period:
        return None
    return sum(vals) / period


def wilder_rsi(closes, period=14):
    """Wilder's RSI. Returns None rather than a made-up number on short history."""
    closes = [c for c in closes if c is not None]
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes, closes[1:]):
        d = cur - prev
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def wilder_atr(highs, lows, closes, period=14):
    """Average True Range, Wilder-smoothed. The trading-system repo sizes stops at 1.2x ATR."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def derive(bars, today_volume=None):
    """One symbol's bars -> the technical block. Oldest-first order is assumed and enforced."""
    rows = []
    for b in bars or []:
        o, h, l, c = (_f(b.get("open_price")), _f(b.get("high_price")),
                      _f(b.get("low_price")), _f(b.get("close_price")))
        if c is None:
            continue
        rows.append({"t": b.get("begins_at"), "o": o, "h": h, "l": l, "c": c,
                     "v": _f(b.get("volume")), "interp": bool(b.get("interpolated"))})
    rows.sort(key=lambda r: r["t"] or "")
    # Interpolated bars are gap-fill and carry no information; they must not move an average.
    real = [r for r in rows if not r["interp"]]
    if not real:
        return {"bars_used": 0}

    closes = [r["c"] for r in real]
    highs = [r["h"] for r in real]
    lows = [r["l"] for r in real]
    vols = [r["v"] for r in real if r["v"] is not None]

    out = {"bars_used": len(real), "last_bar": real[-1]["t"]}
    out["ma_50"] = sma(closes, 50)
    out["ma_200"] = sma(closes, 200)
    out["rsi_14"] = wilder_rsi(closes, 14)
    atr = wilder_atr(highs, lows, closes, 14)
    out["atr_14"] = atr
    out["atr_pct"] = (atr / closes[-1] * 100.0) if atr and closes[-1] else None

    # 20-day average volume EXCLUDING the most recent bar, which is usually today's
    # partial session — including it drags the baseline toward the number being tested.
    hist_vols = vols[:-1] if len(vols) > 20 else vols
    out["avg_volume_20d"] = (sum(hist_vols[-20:]) / 20.0) if len(hist_vols) >= 20 else None

    live_vol = today_volume if today_volume is not None else (vols[-1] if vols else None)
    out["rel_volume"] = (live_vol / out["avg_volume_20d"]
                         if live_vol and out.get("avg_volume_20d") else None)

    win = closes[-252:] if len(closes) >= 60 else closes
    hi = [h for h in highs[-252:] if h is not None]
    lo = [l for l in lows[-252:] if l is not None]
    out["week52_high"] = max(hi) if hi else None
    out["week52_low"] = min(lo) if lo else None
    if len(win) >= 60 and win[0]:
        out["week52_change_pct"] = (closes[-1] - win[0]) / win[0] * 100.0
    else:
        out["week52_change_pct"] = None

    # Overnight gap: today's open against the prior completed close. The pre-market slot
    # exists to catch these, and the 2026-08-27 scan missed NVDA's 6% earnings gap entirely
    # because the bulk list pages it screened on served cached rows.
    # NOTE: mid-session the final bar is the LAST CLOSED session, so this bar-derived gap
    # describes yesterday. apply_live() overrides it when --quotes is supplied; gap_basis
    # says which claim survived.
    if len(real) >= 2 and real[-1]["o"] and real[-2]["c"]:
        out["gap_pct"] = (real[-1]["o"] - real[-2]["c"]) / real[-2]["c"] * 100.0
    else:
        out["gap_pct"] = None
    out["gap_basis"] = "prior_bar"

    out["prior_close"] = real[-2]["c"] if len(real) >= 2 else None
    out["last_close"] = real[-1]["c"]
    # Trailing returns over completed bars — the inputs to relative strength (see
    # --benchmark). Reported, not scored, until validate.py says they predict anything.
    for n in (20, 60):
        out[f"ret_{n}d_pct"] = ((closes[-1] / closes[-1 - n] - 1) * 100.0
                                if len(closes) > n and closes[-1 - n] else None)
    out["insufficient"] = sorted(k for k, need in MIN_BARS.items()
                                 if out.get(k) is None and len(real) < need)
    return out


def apply_live(d, fund=None, quote=None, premarket=False):
    """Re-point gap_pct and the 52-week fields at TODAY'S session (README section 9.2).

    fund:  {"open": float|None, "hi52": float|None, "lo52": float|None}
    quote: {"price": float|None, "prev_close": float|None, "ext_price": float|None}

    Completed-bar indicators (MA/RSI/ATR/relvol) are deliberately untouched. Every
    override degrades to the bar-derived value when its live input is missing, and
    `gap_basis` records which basis actually applied.
    """
    fund = fund or {}
    quote = quote or {}

    # Official SIP prior close outranks the bar-derived one when the broker supplies it.
    # The gap overrides below use ONLY the official prior close: falling back to the
    # bar-derived real[-2] close would mix today's open with the close before yesterday.
    if quote.get("prev_close"):
        d["prior_close"] = quote["prev_close"]

    # Gap: pre-market has no session open yet, so the extended-hours print is the honest
    # basis; intraday the session open from fundamentals is.
    if premarket:
        if quote.get("ext_price") and quote.get("prev_close"):
            d["gap_pct"] = ((quote["ext_price"] - quote["prev_close"])
                            / quote["prev_close"] * 100.0)
            d["gap_basis"] = "extended_hours"
    else:
        if fund.get("open") and quote.get("prev_close"):
            d["gap_pct"] = ((fund["open"] - quote["prev_close"])
                            / quote["prev_close"] * 100.0)
            d["gap_basis"] = "session_open"

    # 52-week range: the exchange's own figure includes today's session; bars exclude it.
    if fund.get("hi52"):
        d["week52_high"] = fund["hi52"]
    if fund.get("lo52"):
        d["week52_low"] = fund["lo52"]

    # 52-week change: rebase the bar-derived change from the last completed close onto
    # the live trade price.
    if (d.get("week52_change_pct") is not None and quote.get("price")
            and d.get("last_close")):
        d["week52_change_pct"] = ((1.0 + d["week52_change_pct"] / 100.0)
                                  * (quote["price"] / d["last_close"]) - 1.0) * 100.0
    return d


def _rows(raw):
    """Accept the whole tool response, its data block, or the bare results array."""
    if isinstance(raw, dict):
        r = raw.get("data", {}).get("results") if isinstance(raw.get("data"), dict) else None
        if r is None:
            r = raw.get("results")
        return r or []
    return raw or []


def quote_extras(raw):
    """{TICKER: {price, prev_close, ext_price}} from a get_equity_quotes response.

    TWO ROW SHAPES, both accepted. The connector originally returned flat rows carrying
    `symbol` and `last_trade_price` at the top level. It now nests the live fields under
    `quote`, pairs them with an official `close` block, and renames the extended-hours
    print to `last_non_reg_trade_price`. The flat reader found no top-level `symbol`,
    skipped every row, and left quote_extra EMPTY — so the section-9.2 session overrides
    silently did nothing and gap_pct fell back to `prior_bar` on a live scan. It did not
    raise, which is why it went unnoticed until 2026-09-09.

    Reading both shapes is deliberate: the flat form is still what the archived fixtures
    and any older captured payload carry, and a reader that only understood the new shape
    would break replaying those.

    A row that has not traded, or whose state is not active, contributes nothing: the
    caller then keeps the honest bar-derived value instead of a price no venue printed.
    """
    out = {}
    for r in _rows(raw):
        if not isinstance(r, dict):
            continue
        q = r["quote"] if isinstance(r.get("quote"), dict) else r
        close = r["close"] if isinstance(r.get("close"), dict) else {}
        sym = q.get("symbol") or r.get("symbol") or close.get("symbol")
        if not sym:
            continue
        if q.get("has_traded") is False:
            continue
        if q.get("state") is not None and q.get("state") != "active":
            continue
        out[str(sym).upper()] = {
            "price": _f(q.get("last_trade_price")),
            "prev_close": (_f(q.get("adjusted_previous_close"))
                           or _f(q.get("previous_close"))
                           or _f(close.get("price"))),
            "ext_price": (_f(q.get("last_non_reg_trade_price"))
                          or _f(q.get("last_extended_hours_trade_price"))),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", required=True,
                    help="JSON: the data.results array from get_equity_historicals, "
                         "or the whole tool response")
    ap.add_argument("--fundamentals",
                    help="JSON: the data.results array from get_equity_fundamentals — "
                         "today's partial-session volume, session open, 52-week range")
    ap.add_argument("--quotes",
                    help="JSON: the RAW get_equity_quotes response. Enables the section-9.2 "
                         "session overrides: gap_pct against the official prior close, "
                         "52-week change rebased onto the live price")
    ap.add_argument("--premarket", action="store_true",
                    help="No session open exists yet: derive the gap from the extended-hours "
                         "print against the official prior close, never from fundamentals' "
                         "(previous session's) open")
    ap.add_argument("--benchmark", default="SPY",
                    help="Symbol in the SAME bars file to measure relative strength against "
                         "(include it in the get_equity_historicals call). Produces "
                         "rs_20d_vs_<bench> and rs_60d_vs_<bench> per symbol: the name's "
                         "trailing return minus the benchmark's. Skipped silently if absent.")
    ap.add_argument("--out")
    a = ap.parse_args()

    raw = json.load(open(a.bars, encoding="utf-8"))
    results = raw.get("data", {}).get("results") if isinstance(raw, dict) else raw
    if results is None:
        results = raw.get("results") if isinstance(raw, dict) else raw
    if not isinstance(results, list):
        sys.exit("REFUSED: --bars must be the results array (or the full tool response)")

    today_vol, fund_extra = {}, {}
    if a.fundamentals:
        for r in _rows(json.load(open(a.fundamentals, encoding="utf-8"))):
            if isinstance(r, dict) and r.get("symbol"):
                s = r["symbol"].upper()
                today_vol[s] = _f(r.get("volume"))
                fund_extra[s] = {"open": _f(r.get("open")),
                                 "hi52": _f(r.get("high_52_weeks")),
                                 "lo52": _f(r.get("low_52_weeks"))}

    quote_extra = {}
    if a.quotes:
        quote_extra = quote_extras(json.load(open(a.quotes, encoding="utf-8")))

    out = {}
    for res in results:
        sym = (res.get("symbol") or "").upper()
        if not sym:
            continue
        d = derive(res.get("bars"), today_vol.get(sym))
        if a.quotes and d.get("bars_used"):
            d = apply_live(d, fund_extra.get(sym), quote_extra.get(sym),
                           premarket=a.premarket)
        out[sym] = d

    # Relative strength against the benchmark, if its bars were in the same call.
    bench = (a.benchmark or "").upper()
    b = out.get(bench) if bench else None
    if b:
        for sym, d in out.items():
            if sym == bench:
                continue
            for n in (20, 60):
                mine, theirs = d.get(f"ret_{n}d_pct"), b.get(f"ret_{n}d_pct")
                d[f"rs_{n}d_vs_{bench}"] = ((mine - theirs) if isinstance(mine, float)
                                            and isinstance(theirs, float) else None)
    elif bench:
        print(f"note: benchmark {bench} not in the bars file — no relative-strength fields "
              "(add it to the get_equity_historicals call)", file=sys.stderr)

    text = json.dumps(out, indent=2) + "\n"
    if a.out:
        open(a.out, "w", encoding="utf-8").write(text)
    else:
        sys.stdout.write(text)

    for sym, d in sorted(out.items()):
        bits = [f"{sym}: {d.get('bars_used', 0)} bars"]
        for k, lbl in (("ma_50", "MA50"), ("ma_200", "MA200"), ("rsi_14", "RSI"),
                       ("atr_pct", "ATR%"), ("rel_volume", "relvol"), ("gap_pct", "gap%")):
            v = d.get(k)
            bits.append(f"{lbl} {v:.2f}" if isinstance(v, float) else f"{lbl} —")
        if d.get("gap_basis"):
            bits.append(f"gap basis: {d['gap_basis']}")
        if d.get("insufficient"):
            bits.append("short history for: " + ",".join(d["insufficient"]))
        print("  ".join(bits), file=sys.stderr)


if __name__ == "__main__":
    main()
