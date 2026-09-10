"""orb.py — the stocks-in-play opening-range-breakout desk (E24, task D-03).

The mandate (plan §6; research Appendix E §1a — Zarattini, Barbon & Aziz 2024, "Beat the
market: an effective intraday momentum strategy for S&P500 ETF" / "stocks in play"):

  * each morning rank the scan universe (plus the held names) by OPENING RELATIVE VOLUME —
    the volume of the 09:30–09:35 bar divided by the 14-day average of that same 5-minute
    bucket; keep price > $5, 14-day ADV > 1M shares, ATR14 > $0.50; take the top 20;
  * direction from the first 5-minute candle: close > open → long; close < open → the
    paper goes short, THIS ACCOUNT CANNOT, so the name is skipped; doji → skip;
  * entry = a buy-stop just above the opening-range high (OR = the 09:30–09:35 bar), placed
    at the 09:35 sentinel, live until 10:30 and cancelled unfilled after that;
  * stop = entry − 0.10 × ATR14 (the paper's 10%-of-ATR stop);
  * size for ≤ 1% of desk equity at risk per trade, notional capped at 25% of desk equity
    (the cap is logged when it binds); at most 5 concurrent names;
  * exit at the close — the 15:45 power-hour flatten — never held overnight; the hourly
    sentinels manage the stop as a chandelier trail, k_trail 0.5 × ATR14 under the highest
    5-minute HIGH since entry (Chande & Kroll's chandelier hangs from the highest high; the
    stops.py variant for the multi-day desks hangs from the highest close the book has
    seen). k_trail 0.5 is a DESK-SPECIFIC parameter: stops.py's chandelier default is 3.0 ×
    ATR for a multi-day momentum hold; a trade that lives six hours trails six times tighter.

LONG-ONLY. The paper's edge was measured long AND short — the short leg carried a large
part of the return in a universe selected on volume, where the biggest movers are as often
down as up. This account cannot short, so the desk takes the long half only and the
expectation is that it is WEAKER than the paper's figure, not that it reproduces it. Every
red opening candle in the top 20 is recorded as a skip so the weekly review can count what
the missing leg would have traded.

Under `intraday_margin` (FINRA 4210 as amended by Notice 26-10) nothing counts day trades,
so a desk that is all day trades needs no PDT accounting; the desk's `broker_policy` says
so and the engine honours it.

WHAT THIS MODULE IS. Pure functions over 5-minute bars, the daily bars and a rules dict —
nothing here reads the clock or a file except the CLI at the bottom. pm.py wires them into
the sentinel: `opening_rvol` and `stocks_in_play` rank the universe, `stop_entries` writes
orders in the working_orders shape with type "stop-buy" (pm.simulate_fills fills that type
when a LATER 5-minute high reaches the trigger, at max(trigger, that bar's open) plus the
shadow haircut), `check_stop_buy` is that fill test, `manage` walks the bars since entry to
ratchet the trail on the 5-minute highs and detect a stop hit on the lows, and `replay` is the harness: the same functions
over a window of history, k = 1 half-spread on every fill, no Sharpe and no win rate.

INPUT. `bars_5m.json` — Robinhood `get_equity_historicals` at interval "5minute" for the
universe, today plus the 14-session lookback, in the same {symbol, data:{results:[…]}}
shape as bars.json (one response or a list of them). `begins_at` is RFC-3339 UTC and is
converted to America/New_York here; the 09:30 bucket is the opening range. If the file is
absent the desk logs "no intraday bars — no ORB today" and does nothing. The harness reads
Alpaca IEX 5-minute bars in the same shape (`runner/fetch_bars.py --timeframe 5Min` writes
it, stamping each result `interval "5minute"`): IEX-ONLY
VOLUME IS A FRACTION OF CONSOLIDATED VOLUME and not a constant one, so the RVOL rank it
produces is biased toward names whose IEX share is high that morning. The replay is a
DIRECTIONAL check of the mechanics, not a measurement of the edge; docs/BACKTEST.md says so
next to the E24 recipe.

Stdlib only.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                          # noqa: BLE001 — no tz database: fail loudly later
    ET = None

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

# ---- the desk's parameters. desks.json `rules.orb` overrides any key; the numbers are the
# paper's where the paper has one and this book's where it does not.
RULES = {
    "lookback_days": 14,          # sessions in the bucket-volume average
    "min_lookback_sessions": 5,   # fewer prior sessions with the bucket: no honest RVOL
    "top_n": 20,                  # stocks in play = the top N by opening RVOL
    "min_price": 5.0,             # $ — opening-bar close
    "min_adv_shares": 1_000_000,  # 14-day average daily volume, shares
    "min_atr": 0.50,              # $ ATR14
    "stop_atr_mult": 0.10,        # the paper's stop: 10% of ATR14 below entry
    "risk_pct": 1.0,              # % of desk equity at risk per trade
    "max_notional_pct": 25.0,     # % of desk equity per name — logged when it binds
    "max_concurrent": 5,          # open positions + working stop-buys
    "k_trail": 0.5,               # DESK-SPECIFIC chandelier: 0.5 × ATR under the highest 5-min high
    "breakout_tick": 0.01,        # buy-stop this far above the opening-range high
    "or_bucket": "09:30",         # the opening-range bar (ET, bar start)
    "entry_window_et": ["09:30", "10:00"],   # the 09:35 sentinel, and only that one
    "valid_from_et": "09:35",     # the first bar a stop-buy may fill on
    "cancel_after_et": "10:30",   # unfilled at this ET time: cancelled
    "flatten_at_et": "15:45",     # the power-hour flatten (replay: the bar at/after this)
    "doji_body_frac": 0.0,        # |close − open| ≤ this × range is a doji (0 = exact only)
    "fill_k": 1.0,                # half-spreads paid beyond the touch on every fill
    "fill_half_spread_bps": 5.0,  # the half-spread assumed when no two-sided quote exists
    "fractional": False,          # Robinhood does not take fractional STOP orders
    "bar_minutes": 5,
}

BUCKET_MIN = 5


# ------------------------------------------------------------------ helpers
def _f(v):
    try:
        if v is None or isinstance(v, bool):
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def rules_for(desk_rules=None):
    """RULES with a desk's `orb` block (or a bare override dict) applied."""
    r = dict(RULES)
    cfg = desk_rules or {}
    if isinstance(cfg.get("orb"), dict):
        cfg = cfg["orb"]
    for k, v in cfg.items():
        if k in r:
            r[k] = v
    return r


def _hhmm(t):
    """'HH:MM' from a string ('09:35', '09:35:00') or a time/datetime."""
    if isinstance(t, (dt.datetime, dt.time)):
        return t.strftime("%H:%M")
    return str(t)[:5]


def to_et(ts):
    """An aware ET datetime from an RFC-3339 string or a datetime. Naive datetimes are
    taken as ET already (the replay's synthetic bars). None when unparseable."""
    if isinstance(ts, dt.datetime):
        d = ts
    else:
        s = str(ts or "").strip()
        if not s:
            return None
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if ET is None:
        return d
    if d.tzinfo is None:
        return d.replace(tzinfo=ET)
    return d.astimezone(ET)


def _norm_bar(b):
    """One 5-minute bar in either the Robinhood shape (begins_at/open_price/…) or the plain
    one (t/o/h/l/c/v), to {t (aware ET), date, bucket, o, h, l, c, v}. Interpolated bars
    carry no information and are dropped; so is a bar with no close."""
    if not isinstance(b, dict) or b.get("interpolated"):
        return None
    if "t" in b and isinstance(b.get("t"), dt.datetime) and "bucket" in b:
        return b                                   # already normalised
    t = to_et(b.get("begins_at") or b.get("t") or b.get("time"))
    c = _f(b.get("close_price", b.get("c", b.get("close"))))
    if t is None or c is None:
        return None
    o = _f(b.get("open_price", b.get("o", b.get("open"))))
    h = _f(b.get("high_price", b.get("h", b.get("high"))))
    l = _f(b.get("low_price", b.get("l", b.get("low"))))
    v = _f(b.get("volume", b.get("v")))
    return {"t": t, "date": t.date().isoformat(), "bucket": t.strftime("%H:%M"),
            "o": o if o is not None else c, "h": h if h is not None else max(o or c, c),
            "l": l if l is not None else min(o or c, c), "c": c, "v": v or 0.0}


def bars_by_symbol(raw):
    """{SYMBOL: [normalised 5-minute bars, oldest first]} from a get_equity_historicals
    response, a list of them, or a {SYMBOL: [bars]} map. Returns (by_symbol, notes)."""
    out, notes = {}, []
    if raw is None:
        return out, notes
    if isinstance(raw, dict) and not ("data" in raw or "results" in raw):
        blocks = [{"results": [{"symbol": s, "bars": bars} for s, bars in raw.items()]}]
    else:
        blocks = raw if isinstance(raw, list) else [raw]
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            rows = ((blk.get("data") or {}).get("results") or blk.get("results") or [])
        for res in rows or []:
            if not isinstance(res, dict):
                continue
            sym = (res.get("symbol") or "").upper()
            if not sym:
                continue
            iv = res.get("interval")
            if iv and str(iv).lower() not in ("5minute", "5min", "5m"):
                notes.append(f"{sym}: interval {iv!r} is not 5minute — bars ignored")
                continue
            for b in res.get("bars") or []:
                nb = _norm_bar(b)
                if nb:
                    out.setdefault(sym, []).append(nb)
    for sym in out:
        out[sym].sort(key=lambda r: r["t"])
        dedup, seen = [], set()
        for r in out[sym]:
            if r["t"] in seen:
                continue
            seen.add(r["t"])
            dedup.append(r)
        out[sym] = dedup
    return out, notes


def _norm_rows(rows):
    """Normalised, sorted rows from raw or already-normalised bars."""
    rows = rows or []
    if rows and not (isinstance(rows[0], dict) and "bucket" in rows[0]):
        rows = [nb for nb in (_norm_bar(b) for b in rows) if nb]
        rows.sort(key=lambda r: r["t"])
    return rows


def _sym_bars(bars_5m, sym):
    return _norm_rows((bars_5m or {}).get(sym) or [])


def latest_date(bars_5m):
    d = None
    for sym in bars_5m or {}:
        for r in _sym_bars(bars_5m, sym):
            if d is None or r["date"] > d:
                d = r["date"]
    return d


def _today_str(today, bars_5m=None):
    if today is None:
        return latest_date(bars_5m)
    if isinstance(today, (dt.date, dt.datetime)):
        return today.isoformat()[:10]
    return str(today)[:10]


def opening_bar(bars_5m, sym, today, bucket="09:30"):
    for r in _sym_bars(bars_5m, sym):
        if r["date"] == today and r["bucket"] == bucket:
            return r
    return None


# ------------------------------------------------------------------ 1. opening RVOL
def opening_rvol(bars_5m_by_symbol, today=None, lookback_days=14, bucket="09:30",
                 min_sessions=None):
    """{SYMBOL: {rvol, today_volume, avg_volume, n_sessions}} — today's volume in the
    opening bucket over the average of that SAME bucket across the last `lookback_days`
    prior sessions. rvol is None when today's bar is missing, when fewer than
    `min_sessions` prior sessions carry the bucket, or when the average is zero."""
    today = _today_str(today, bars_5m_by_symbol)
    need = RULES["min_lookback_sessions"] if min_sessions is None else min_sessions
    out = {}
    for sym in bars_5m_by_symbol or {}:
        rows = [r for r in _sym_bars(bars_5m_by_symbol, sym) if r["bucket"] == bucket]
        todays = next((r for r in rows if r["date"] == today), None)
        prior = [r for r in rows if r["date"] < today][-int(lookback_days):]
        avg = (sum(r["v"] for r in prior) / len(prior)) if prior else None
        rvol = None
        if todays is not None and avg and len(prior) >= need:
            rvol = round(todays["v"] / avg, 4)
        out[sym] = {"rvol": rvol, "today_volume": todays["v"] if todays else None,
                    "avg_volume": round(avg, 2) if avg is not None else None,
                    "n_sessions": len(prior)}
    return out


# ------------------------------------------------------------------ 2. daily stats
def _daily_rows(bars):
    rows = []
    for b in bars or []:
        if not isinstance(b, dict) or b.get("interpolated"):
            continue
        c = _f(b.get("close_price", b.get("close", b.get("c"))))
        if c is None:
            continue
        t = str(b.get("begins_at") or b.get("date") or b.get("t") or "")[:10]
        rows.append({"t": t, "o": _f(b.get("open_price", b.get("open", b.get("o")))),
                     "h": _f(b.get("high_price", b.get("high", b.get("h")))),
                     "l": _f(b.get("low_price", b.get("low", b.get("l")))), "c": c,
                     "v": _f(b.get("volume", b.get("v"))) or 0.0})
    rows.sort(key=lambda r: r["t"])
    return rows


def daily_stats(daily_bars, as_of, lookback=14):
    """{atr14, adv14, last_close, n_bars} from daily bars STRICTLY BEFORE `as_of` — today's
    bar is partial mid-session and must not enter either average."""
    as_of = _today_str(as_of)
    rows = [r for r in _daily_rows(daily_bars) if r["t"] and r["t"] < as_of]
    out = {"atr14": None, "adv14": None, "last_close": None, "n_bars": len(rows)}
    if not rows:
        return out
    out["last_close"] = rows[-1]["c"]
    vols = [r["v"] for r in rows[-int(lookback):]]
    out["adv14"] = round(sum(vols) / len(vols), 2) if vols else None
    try:
        import technicals
        atr = technicals.wilder_atr([r["h"] for r in rows], [r["l"] for r in rows],
                                    [r["c"] for r in rows], 14)
    except Exception:                      # noqa: BLE001 — technicals is optional here
        atr = None
    out["atr14"] = round(atr, 4) if atr else None
    return out


# ------------------------------------------------------------------ 3. stocks in play
def direction(bar, doji_body_frac=0.0):
    """'long' | 'short' | 'doji' from the opening candle. 'short' is what the paper would
    trade and what this account cannot; the caller records it as a skip."""
    if bar is None or bar.get("o") is None or bar.get("c") is None:
        return "doji"
    body = bar["c"] - bar["o"]
    rng = (bar.get("h") or 0.0) - (bar.get("l") or 0.0)
    if body == 0 or (doji_body_frac > 0 and rng > 0 and abs(body) <= doji_body_frac * rng):
        return "doji"
    return "long" if body > 0 else "short"


def screen(candidates, bars_5m, daily_bars, rules=None, today=None, held=()):
    """Rank the universe by opening RVOL and apply the filters.

    `candidates` — tickers, or scan rows ({ticker, atr_14, avg_volume_20d, gics, …}); the
    held names are added. ATR14 and ADV14 come from `daily_bars` ({SYMBOL: bars}) when a
    symbol has them and from the scan row otherwise (labelled `stats_source`). Returns
    (in_play, rejected): in_play is the top N by RVOL with every field the entry pass
    needs — rvol, or_high, or_low, direction, atr14, adv14, price — and rejected is one
    {symbol, reason} per name that did not make it, so the journal can say why."""
    r = rules_for(rules)
    today = _today_str(today, bars_5m)
    rows_by = {}
    order = []
    for c in candidates or []:
        row = c if isinstance(c, dict) else {"ticker": c}
        sym = (row.get("ticker") or row.get("symbol") or "").upper()
        if sym and sym not in rows_by:
            rows_by[sym] = row
            order.append(sym)
    for sym in held or ():
        sym = str(sym).upper()
        if sym and sym not in rows_by:
            rows_by[sym] = {"ticker": sym, "held": True}
            order.append(sym)
    rv = opening_rvol(bars_5m, today, r["lookback_days"], r["or_bucket"],
                      r["min_lookback_sessions"])
    scored, rejected = [], []
    for sym in order:
        row = rows_by[sym]
        ob = opening_bar(bars_5m, sym, today, r["or_bucket"])
        if ob is None:
            rejected.append({"symbol": sym, "reason": f"no {r['or_bucket']} bar for {today}"})
            continue
        stat = rv.get(sym) or {}
        if stat.get("rvol") is None:
            rejected.append({"symbol": sym, "reason":
                             f"no opening RVOL — {stat.get('n_sessions', 0)} prior session(s) "
                             f"with the {r['or_bucket']} bucket, need {r['min_lookback_sessions']}"})
            continue
        ds = daily_stats((daily_bars or {}).get(sym), today, r["lookback_days"]) \
            if (daily_bars or {}).get(sym) else {"atr14": None, "adv14": None}
        atr, adv, src = ds.get("atr14"), ds.get("adv14"), "daily_bars"
        if atr is None and _num(row.get("atr_14")):
            atr, src = float(row["atr_14"]), "scan_row"
        if adv is None and _num(row.get("avg_volume_20d")):
            adv = float(row["avg_volume_20d"])
            src = "scan_row" if src == "scan_row" else "mixed"
        price = ob["c"]
        if price is None or price <= r["min_price"]:
            rejected.append({"symbol": sym, "reason":
                             f"price {price} at or under the ${r['min_price']:.2f} floor"})
            continue
        if adv is None or adv <= r["min_adv_shares"]:
            rejected.append({"symbol": sym, "reason":
                             f"14-day ADV {adv} at or under {r['min_adv_shares']:,.0f} shares"
                             + ("" if adv is not None else " (no daily bars, no scan volume)")})
            continue
        if atr is None or atr <= r["min_atr"]:
            rejected.append({"symbol": sym, "reason":
                             f"ATR14 {atr} at or under ${r['min_atr']:.2f}"
                             + ("" if atr is not None else " (no daily bars, no scan ATR)")})
            continue
        scored.append({"symbol": sym, "rvol": stat["rvol"],
                       "today_volume": stat["today_volume"], "avg_volume": stat["avg_volume"],
                       "or_high": ob["h"], "or_low": ob["l"], "or_open": ob["o"],
                       "or_close": ob["c"], "price": price,
                       "direction": direction(ob, r["doji_body_frac"]),
                       "atr14": round(atr, 4), "adv14": round(adv, 2), "stats_source": src,
                       "gics": row.get("gics"), "industry": row.get("industry"),
                       "held": bool(row.get("held")), "date": today})
    scored.sort(key=lambda s: (-s["rvol"], s["symbol"]))
    in_play = scored[:int(r["top_n"])]
    for s in scored[int(r["top_n"]):]:
        rejected.append({"symbol": s["symbol"],
                         "reason": f"RVOL {s['rvol']:.2f}x outside the top {r['top_n']}"})
    for i, s in enumerate(in_play, 1):
        s["rank"] = i
    return in_play, rejected


def stocks_in_play(candidates, bars_5m, daily_bars, rules=None, today=None, held=()):
    """The ranked list — see screen() for the rejected names."""
    return screen(candidates, bars_5m, daily_bars, rules, today, held)[0]


# ------------------------------------------------------------------ 4. entries
def _round_shares(v, r):
    if r.get("fractional"):
        return round(v, int(r.get("share_decimals", 6)))
    return float(int(v))


def size_entries(in_play, desk_equity, rules=None, avail_cash=None, open_count=0,
                 open_symbols=(), today=None, ts=None, run_key=None, slot="sentinel",
                 max_new=None):
    """Buy-stop orders for the long names in play, and the notes for the rest.

    Sizing: shares = (risk_pct × equity) / (trigger − stop), whole shares; the notional is
    capped at max_notional_pct × equity (LOGGED when the cap binds, because at a 10%-of-ATR
    stop the cap binds on most names) and at the cash available. Returns (orders, notes)
    where orders are in pm.py's working_orders shape with type "stop-buy"."""
    r = rules_for(rules)
    eq = float(desk_equity or 0.0)
    risk_usd = eq * r["risk_pct"] / 100.0
    cap_usd = eq * r["max_notional_pct"] / 100.0
    room = int(r["max_concurrent"]) - int(open_count or 0)
    if max_new is not None:
        room = min(room, int(max_new))
    cash = eq if avail_cash is None else float(avail_cash)
    today = _today_str(today, None) or (in_play[0]["date"] if in_play else None)
    ts = ts or (dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"))
    orders, notes = [], []
    spent = 0.0
    held = {str(s).upper() for s in open_symbols or ()}
    for s in in_play or []:
        sym = s["symbol"]
        if s.get("direction") != "long":
            why = {"short": "red opening candle — the paper shorts it; this account is long-only",
                   "doji": "doji opening candle — no direction"}.get(s.get("direction"),
                                                                     "no direction")
            notes.append({"symbol": sym, "reason": f"ORB rank {s.get('rank')}: {why}"})
            continue
        if sym in held:
            notes.append({"symbol": sym, "reason": "ORB: already held or working — one entry per name"})
            continue
        if room <= 0:
            notes.append({"symbol": sym, "reason":
                          f"ORB: max {r['max_concurrent']} concurrent names — no room this run"})
            continue
        atr = s["atr14"]
        trigger = round(s["or_high"] + r["breakout_tick"], 2)
        stop = round(trigger - r["stop_atr_mult"] * atr, 2)
        risk_ps = round(trigger - stop, 6)
        if risk_ps <= 0 or trigger <= 0:
            notes.append({"symbol": sym, "reason": f"ORB: stop {stop} not below trigger {trigger}"})
            continue
        raw_shares = risk_usd / risk_ps
        shares = raw_shares
        cap_bound = False
        if shares * trigger > cap_usd:
            shares, cap_bound = cap_usd / trigger, True
        cash_bound = False
        if shares * trigger > max(0.0, cash - spent):
            shares, cash_bound = max(0.0, cash - spent) / trigger, True
        shares = _round_shares(shares, r)
        if shares <= 0:
            notes.append({"symbol": sym, "reason":
                          f"ORB: {'cash' if cash_bound else 'cap'} leaves under one share at "
                          f"${trigger:,.2f} (${max(0.0, cash - spent):,.2f} free)"})
            continue
        notional = round(shares * trigger, 2)
        dollar_risk = round(shares * risk_ps, 2)
        if cap_bound:
            notes.append({"symbol": sym, "reason":
                          f"ORB notional cap binds: 1% risk sized {raw_shares:,.0f} sh "
                          f"(${raw_shares * trigger:,.0f}); capped at {r['max_notional_pct']:g}% "
                          f"of equity = {shares:g} sh (${notional:,.2f}), risking "
                          f"${dollar_risk:,.2f} ({dollar_risk / eq * 100 if eq else 0:.2f}%)"})
        basis = (f"ORB: {r['stop_atr_mult']:g}x ATR14 ({atr:.2f}) below the ${trigger:,.2f} "
                 f"breakout — {risk_ps / trigger * 100:.2f}% of price; trails {r['k_trail']:g}x "
                 "ATR under the highest 5-min high, up only; flat at the close")
        order = {
            "id": f"{today}-{slot}-orb-{sym}",
            "symbol": sym, "side": "buy", "kind": "entry", "type": "stop-buy",
            "trigger_price": trigger, "limit_price": trigger, "shares": shares,
            "notional": notional, "placed": ts, "placed_slot": slot, "run_key": run_key,
            "expires": "day", "valid_from_et": r["valid_from_et"],
            "cancel_after_et": r["cancel_after_et"], "status": "working",
            "reason": (f"ORB rank {s.get('rank')}: opening RVOL {s['rvol']:.2f}x, green "
                       f"{r['or_bucket']} candle — buy-stop above the OR high {s['or_high']:,.2f}"),
            "meta": {"stop": stop, "target": None, "stop_basis": basis,
                     "stop_basis_kind": "chandelier", "atr_14": atr,
                     "atr_pct": round(atr / trigger * 100, 2) if trigger else None,
                     "stop_pct": round(risk_ps / trigger * 100, 2), "score": None,
                     "gics": s.get("gics"), "industry": s.get("industry"),
                     "thesis": "opening-range breakout on a stock in play (E24)",
                     "risk_pct": round(dollar_risk / eq * 100, 2) if eq else None,
                     "dollar_risk": dollar_risk, "stop_policy": "chandelier",
                     "initial_risk": risk_ps,
                     "stop_params": {"k_init": r["stop_atr_mult"], "k_trail": r["k_trail"],
                                     "max_sessions": 1, "target_r": None},
                     "adv_usd": round(s["adv14"] * s["price"], 2),
                     "orb": {"rank": s.get("rank"), "rvol": s["rvol"], "or_high": s["or_high"],
                             "or_low": s["or_low"], "direction": s["direction"],
                             "adv14": s["adv14"], "atr14": atr,
                             "stats_source": s.get("stats_source"),
                             "notional_cap_bound": cap_bound, "cash_bound": cash_bound,
                             "unscaled_shares": round(raw_shares, 2), "date": today}},
            "warnings": [],
        }
        orders.append(order)
        spent += notional
        room -= 1
    return orders, notes


def stop_entries(in_play, desk_equity, rules=None, **kw):
    """The orders only — see size_entries() for the notes."""
    return size_entries(in_play, desk_equity, rules, **kw)[0]


# ------------------------------------------------------------------ 5. fills
def haircut(base, bid=None, ask=None, rules=None):
    """The shadow haircut on a fill: k half-spreads beyond the touch, from the quote when
    there is a two-sided one and from `fill_half_spread_bps` of the price otherwise."""
    r = rules_for(rules)
    k = float(r["fill_k"])
    if _num(bid) and _num(ask) and bid > 0 and ask >= bid:
        hs = (ask - bid) / 2.0
    else:
        hs = float(base) * float(r["fill_half_spread_bps"]) / 10_000.0
    return round(k * hs, 6)


def check_stop_buy(order, bars_today, now_et, rules=None, bid=None, ask=None):
    """The fill test for a "stop-buy" order against the day's 5-minute bars.

    Bars from `valid_from_et` up to (not including) `cancel_after_et`, completed by
    `now_et`, are walked oldest first; the first whose HIGH reaches the trigger fills the
    order at max(trigger, that bar's open) + haircut — a gap above the trigger fills at the
    open, never at the trigger. No hit and the clock past `cancel_after_et`: cancelled.
    Returns {status: 'fill'|'cancel'|'working', price, bar, reason}."""
    r = rules_for(rules)
    trig = float(order.get("trigger_price") or order.get("limit_price"))
    vf = _hhmm(order.get("valid_from_et") or r["valid_from_et"])
    ca = _hhmm(order.get("cancel_after_et") or r["cancel_after_et"])
    date = (order.get("meta") or {}).get("orb", {}).get("date") or str(order.get("placed", ""))[:10]
    now_et = to_et(now_et)
    now_h = _hhmm(now_et) if now_et else "23:59"
    step = dt.timedelta(minutes=int(r["bar_minutes"]))
    rows = [b for b in _norm_rows(bars_today) if (not date or b["date"] == date)
            and vf <= b["bucket"] < ca]
    for b in rows:
        if now_et is not None and b["t"] + step > now_et:
            break                                   # the bar is still forming
        if b["h"] is not None and b["h"] >= trig:
            base = max(trig, b["o"] if b["o"] is not None else trig)
            px = round(base + haircut(base, bid, ask, r), 4)
            return {"status": "fill", "price": px, "bar": b["t"].isoformat(),
                    "base": round(base, 4),
                    "reason": (f"{b['bucket']} bar high {b['h']:,.2f} reached the "
                               f"{trig:,.2f} trigger — filled at "
                               f"{'the open' if base > trig else 'the trigger'} "
                               f"{base:,.2f} + haircut")}
    if now_h >= ca:
        return {"status": "cancel", "price": None, "bar": None,
                "reason": f"unfilled by {ca} ET — the opening-range breakout did not come"}
    return {"status": "working", "price": None, "bar": None, "reason": None}


# ------------------------------------------------------------------ 6. management
def manage_position(pos, bars_since_entry, rules=None, atr=None):
    """Walk the 5-minute bars since the last visit for ONE position: a bar whose low
    reaches the stop exits at min(stop, that bar's open) — a gap through the stop exits at
    the open — otherwise the highest 5-minute HIGH ratchets the chandelier trail
    (highest high − k_trail × ATR14) up, never down. Within one bar the low is tested
    BEFORE the high raises the trail: the order of prints inside a bar is unknown, so the
    stop is assumed to have been hit first. Returns
    {stop, highest_high, trail_level, raised, exit_reason, exit_price, exit_bar,
     managed_through}; nothing on `pos` is mutated. The anchor is seeded from
    `pos["highest_high"]`, else `highest_close` (what the book has seen), else the entry."""
    r = rules_for(rules)
    atr = atr if _num(atr) and atr > 0 else pos.get("atr_14")
    stop = pos.get("stop")
    stop = float(stop) if _num(stop) else None
    hh = pos.get("highest_high")
    if not _num(hh):
        hh = pos.get("highest_close")
    hh = float(hh) if _num(hh) else float(pos.get("avg_cost") or 0.0)
    trail = None
    raised = False
    was_trail = pos.get("stop_basis_kind") == "trail"
    out = {"stop": stop, "highest_high": hh, "trail_level": None, "raised": False,
           "exit_reason": None, "exit_price": None, "exit_bar": None,
           "managed_through": pos.get("orb_managed_through")}
    for b in _norm_rows(bars_since_entry):
        out["managed_through"] = b["t"].isoformat() if isinstance(b["t"], dt.datetime) else b["t"]
        if stop is not None and b["l"] is not None and b["l"] <= stop:
            o = b["o"] if b["o"] is not None else stop
            out.update({"exit_reason": "trail" if (raised or was_trail) else "stop",
                        "exit_price": round(min(stop, o), 4), "exit_bar": out["managed_through"],
                        "stop": stop, "highest_high": hh, "trail_level": trail, "raised": raised})
            return out
        bar_hi = b["h"] if b["h"] is not None else b["c"]
        if bar_hi is not None and bar_hi > hh:
            hh = bar_hi
        if _num(atr) and atr > 0:
            trail = round(hh - r["k_trail"] * atr, 2)
            if stop is None or trail > stop:
                stop, raised = trail, True
    out.update({"stop": stop, "highest_high": hh, "trail_level": trail, "raised": raised})
    return out


def manage(positions, bars_5m_since_entry, rules=None):
    """{SYMBOL: manage_position(...)} for a list of positions; `bars_5m_since_entry` is
    {SYMBOL: [bars after the entry bar / the last managed bar]}."""
    out = {}
    for pos in positions or []:
        sym = pos.get("symbol")
        out[sym] = manage_position(pos, (bars_5m_since_entry or {}).get(sym) or [], rules)
    return out


def bars_after(bars, after_iso, date=None):
    """The bars strictly after an ISO timestamp (or all of the day's when None)."""
    after = to_et(after_iso) if after_iso else None
    return [b for b in _norm_rows(bars) if (date is None or b["date"] == date)
            and (after is None or b["t"] > after)]


def in_entry_window(now, rules=None):
    """True when `now` (UTC or ET) falls in the 09:35 sentinel's window."""
    r = rules_for(rules)
    t = to_et(now)
    if t is None:
        return False
    lo, hi = (_hhmm(x) for x in r["entry_window_et"])
    return lo <= _hhmm(t) < hi


# ------------------------------------------------------------------ 7. the harness
def replay(bars_5m_by_symbol, daily_bars, start, end, rules=None, starting_equity=5000.0):
    """Replay the desk over [start, end] on 5-minute bars, the same functions the sentinel
    runs, with the k = 1 haircut on EVERY fill (entry, stop, flatten). Per session: rank,
    size against the running equity, walk the buckets chronologically — pending stop-buys
    fill on the first bar whose high reaches the trigger (max_concurrent enforced in RVOL
    rank order at fill time), open positions ratchet on closes and exit on a low through
    the stop (the trail ratchets on the 5-minute highs), everything still open exits at the
    open of the `flatten_at_et` bar (the last close when no such bar exists). Returns {daily_pnl, equity_curve, n_trades,
    n_days_traded, avg_r, max_dd, cost_bps_assumed, …}. No Sharpe, no win rate: at this
    trade count neither is evidence."""
    r = rules_for(rules)
    b5, _ = bars_by_symbol(bars_5m_by_symbol) if not _is_normalised(bars_5m_by_symbol) \
        else (bars_5m_by_symbol, [])
    start, end = _today_str(start), _today_str(end)
    dates = sorted({b["date"] for rows in b5.values() for b in rows if start <= b["date"] <= end})
    daily = daily_bars or {}
    equity = float(starting_equity)
    out = {"start": start, "end": end, "starting_equity": equity, "daily_pnl": {},
           "equity_curve": [], "trades": [], "n_trades": 0, "n_days_traded": 0,
           "n_sessions": len(dates), "n_symbols": len(b5), "avg_r": None, "max_dd": 0.0,
           "cost_bps_assumed": round(2 * r["fill_k"] * r["fill_half_spread_bps"], 2),
           "skips": {"short": 0, "doji": 0, "cap_bound": 0, "unfilled": 0, "max_concurrent": 0},
           "rules": r, "note": ("IEX-only 5-minute volume biases RVOL; directional check of "
                                "the mechanics, not a measurement of the edge. Long-only: the "
                                "paper's edge was long and short.")}
    peak = equity
    step = dt.timedelta(minutes=int(r["bar_minutes"]))
    for date in dates:
        in_play, _rej = screen(list(b5.keys()), b5, daily, r, today=date)
        orders, notes = size_entries(in_play, equity, r, avail_cash=equity, open_count=0,
                                     today=date, ts=f"{date}T09:35:00", slot="replay")
        out["skips"]["short"] += sum(1 for s in in_play if s["direction"] == "short")
        out["skips"]["doji"] += sum(1 for s in in_play if s["direction"] == "doji")
        out["skips"]["cap_bound"] += sum(1 for o in orders if o["meta"]["orb"]["notional_cap_bound"])
        day_bars = {o["symbol"]: {b["bucket"]: b for b in b5[o["symbol"]] if b["date"] == date}
                    for o in orders}
        buckets = sorted({bk for m in day_bars.values() for bk in m})
        pending = list(orders)             # rank order
        open_pos = {}
        day_pnl = 0.0
        fills_today = 0
        fa, vf, ca = _hhmm(r["flatten_at_et"]), _hhmm(r["valid_from_et"]), _hhmm(r["cancel_after_et"])

        def _close(sym, p, px, reason, bucket):
            nonlocal day_pnl
            px = round(px - haircut(px, rules=r), 4)
            pnl = round((px - p["entry"]) * p["shares"], 2)
            day_pnl += pnl
            out["trades"].append({"date": date, "symbol": sym, "entry": p["entry"],
                                  "exit": px, "shares": p["shares"], "pnl": pnl,
                                  "r": round(pnl / (p["risk"] * p["shares"]), 3)
                                  if p["risk"] > 0 else None, "reason": reason,
                                  "exit_bucket": bucket, "entry_bucket": p["entry_bucket"]})
            del open_pos[sym]

        for bk in buckets:
            # 1. manage what is open (bars after the entry bar)
            for sym in list(open_pos):
                p = open_pos[sym]
                b = day_bars[sym].get(bk)
                if b is None or bk <= p["entry_bucket"]:
                    continue
                if bk >= fa:
                    _close(sym, p, b["o"], "flatten", bk)
                    continue
                m = manage_position({"stop": p["stop"], "highest_high": p["hh"],
                                     "avg_cost": p["entry"], "atr_14": p["atr"],
                                     "stop_basis_kind": "trail" if p["raised"] else "chandelier"},
                                    [b], r)
                if m["exit_reason"]:
                    _close(sym, p, m["exit_price"], m["exit_reason"], bk)
                    continue
                p["stop"], p["hh"] = m["stop"], m["highest_high"]
                p["raised"] = p["raised"] or m["raised"]
            # 2. work the pending stop-buys
            for o in list(pending):
                sym = o["symbol"]
                if bk >= ca:
                    pending.remove(o)
                    out["skips"]["unfilled"] += 1
                    continue
                if bk < vf:
                    continue
                b = day_bars[sym].get(bk)
                if b is None or b["h"] is None or b["h"] < o["trigger_price"]:
                    continue
                pending.remove(o)
                if len(open_pos) >= int(r["max_concurrent"]):
                    out["skips"]["max_concurrent"] += 1
                    continue
                base = max(o["trigger_price"], b["o"] if b["o"] is not None else o["trigger_price"])
                px = round(base + haircut(base, rules=r), 4)
                open_pos[sym] = {"entry": px, "shares": o["shares"], "stop": o["meta"]["stop"],
                                 "hh": px, "atr": o["meta"]["atr_14"], "risk": px - o["meta"]["stop"],
                                 "raised": False, "entry_bucket": bk}
                fills_today += 1
        for sym in list(open_pos):        # no flatten bar: the last close
            p = open_pos[sym]
            last = sorted(day_bars[sym].values(), key=lambda b: b["bucket"])[-1]
            _close(sym, p, last["c"], "flatten-last-bar", last["bucket"])
        out["skips"]["unfilled"] += len(pending)
        equity = round(equity + day_pnl, 2)
        out["daily_pnl"][date] = round(day_pnl, 2)
        out["equity_curve"].append({"date": date, "equity": equity, "pnl": round(day_pnl, 2),
                                    "n_fills": fills_today})
        if fills_today:
            out["n_days_traded"] += 1
        peak = max(peak, equity)
        if peak > 0:
            out["max_dd"] = round(max(out["max_dd"], (peak - equity) / peak * 100.0), 3)
    out["n_trades"] = len(out["trades"])
    rs = [t["r"] for t in out["trades"] if t["r"] is not None]
    out["avg_r"] = round(sum(rs) / len(rs), 3) if rs else None
    out["ending_equity"] = equity
    out["total_pnl"] = round(equity - float(starting_equity), 2)
    return out


def _is_normalised(bars):
    if not isinstance(bars, dict) or not bars:
        return False
    for rows in bars.values():
        if rows:
            return isinstance(rows[0], dict) and "bucket" in rows[0]
    return True


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="E24 — opening-range-breakout replay harness")
    ap.add_argument("--bars-5m", required=True, help="5-minute bars (Robinhood/Alpaca shape)")
    ap.add_argument("--bars", default=None, help="daily bars for ATR14 / ADV14 (same shape)")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--equity", type=float, default=5000.0)
    ap.add_argument("--rules", default=None, help="JSON overrides for orb.RULES")
    ap.add_argument("--json", default=None, help="write the full result here")
    ap.add_argument("--ledger", default=None, help="append an E24 row to this ledger")
    ap.add_argument("--experiment-id", default="E24")
    ap.add_argument("--hypothesis", default=("long-only opening-range breakout on the top-20 "
                                             "opening-RVOL names, 10%-of-ATR stop, flat at the "
                                             "close, is positive after a 1-half-spread haircut"))
    args = ap.parse_args(argv)
    with open(args.bars_5m, encoding="utf-8") as f:
        raw5 = json.load(f)
    b5, notes = bars_by_symbol(raw5)
    daily = {}
    if args.bars:
        import backtest
        daily = backtest.load_bars(args.bars)
    rules = json.loads(args.rules) if args.rules else None
    res = replay(b5, daily, args.start, args.end, rules, args.equity)
    for n in notes:
        print("NOTE:", n, file=sys.stderr)
    print(f"E24 replay {res['start']}..{res['end']}  {res['n_symbols']} symbols  "
          f"{res['n_sessions']} sessions  {res['n_trades']} trades on {res['n_days_traded']} days  "
          f"avg R {res['avg_r']}  max DD {res['max_dd']:.2f}%  P&L ${res['total_pnl']:+,.2f}  "
          f"cost assumed {res['cost_bps_assumed']:g} bp/round trip")
    print(f"skips: {res['skips']}")
    print(res["note"])
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2, default=str)
    if args.ledger:
        import ledger
        row = ledger.append(args.ledger, {
            "id": args.experiment_id, "hypothesis": args.hypothesis,
            "config_diff": {"desk": "orb", "rules": res["rules"], "long_only": True},
            "harness_cmd": ["orb.py"] + list(argv if argv is not None else sys.argv[1:]),
            "window": {"start": res["start"], "end": res["end"]},
            "universe": {"name": f"{os.path.basename(args.bars_5m)} symbols",
                         "n_symbols": res["n_symbols"]},
            "n": res["n_trades"], "horizons": [],
            "in_sample": {"n_trades": res["n_trades"], "n_days_traded": res["n_days_traded"],
                          "n_sessions": res["n_sessions"], "avg_r": res["avg_r"],
                          "max_dd_pct": res["max_dd"], "total_pnl": res["total_pnl"],
                          "cost_bps_assumed": res["cost_bps_assumed"], "skips": res["skips"],
                          "data_caveat": "IEX-only 5-minute volume biases RVOL; directional only"},
            "out_of_sample": None, "decision": None})
        print(f"ledger: trial {row['n_trials_to_date']} of the ledger ({row['id']}) -> {args.ledger}")
    else:
        print("ledger: NOT counted (no --ledger)")
    return res


if __name__ == "__main__":
    main()
