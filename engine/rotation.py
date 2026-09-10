"""rotation.py — the sector-rotation desk (E26): rules, proposals, and a purpose-built replay.

THE MANDATE (plan §6; Appendix E §10 — Faber's sector momentum, Antonacci's absolute momentum)
-------------------------------------------------------------------------------------------
Universe      the eleven SPDR sector ETFs (XLK XLF XLV XLY XLP XLE XLI XLB XLU XLRE XLC) plus
              VEU (ex-US equity, ranked alongside the sectors as the world's twelfth "sector"
              in Faber's GTAA spirit), SPY (the absolute-momentum reference — never held) and
              TLT (the bond leg — held only when the filter is off).
Signal        12-1 month total return: technicals.features()["ret_12_1"], close[t-21] /
              close[t-252] - 1 (Jegadeesh & Titman 1993; Faber 2010 sector rotation).
Rule          rank the risk universe on ret_12_1; when SPY's trailing 12-month return exceeds
              the 3-month T-bill return over the same window (Antonacci 2014, absolute
              momentum) hold the top `top_n` (3) equal-weight; otherwise hold TLT only.
Cadence       MONTHLY. Decisions only at the LAST power-hour (15:45 ET) slot of each calendar
              month — the last trading day on a weekday calendar with the NYSE holidays —
              plus `--force-rebalance`. A WEEKLY check (the last trading day of each week,
              same slot) acts only if a held ETF has dropped below rank `drop_below_rank` (6)
              or the absolute filter has flipped against what the book holds.
Stops         time_catastrophe, k_cat 3.5, max_sessions 25 (desks.json; the rank IS the exit,
              the catastrophe stop and the time stop are the safety net).
Sizing        equal sleeves of desk equity (3), scaled by the desk vol scalar
              (portfolio.desk_vol_scalar) when RULES["vol_target"] is on. The bond leg takes
              `bond_sleeves` (3 — the whole risk budget, Antonacci's rule; set 1 to keep two
              sleeves in cash instead).
House caps    the ETFs are sector-level by construction: exempt from HOUSE-01's per-name and
              per-sector caps (pm.house_exposure / house_block), INCLUDED in K-03's beta and
              N_eff (they stay in house["holdings"]).

WHAT THIS MODULE DOES AND DOES NOT DO
-------------------------------------
* `proposals()` turns a staged `bars_etf.json` (the get_equity_historicals payload for the
  14 symbols, >= 13 months of daily bars) and an optional `macro.json` ({"tbill_3m_pct": x})
  into candidate rows in the EXACT shape pm.entry_pass consumes (setup "Sector Rotation",
  verdict "Buy", coverage 100, `_source: "rotation"`), plus the exits (held ETFs that left
  the target set) and the sleeve map the entry pass sizes from. pm.py wires it in when the
  desk's filter says {"universe": "sector-etfs"} and nowhere else.
* `replay()` is the E26 harness: the same rule walked monthly over the bars, trading at the
  next session's open with a stated one-way cost. It reports monthly returns, the equity
  curve, max drawdown, the rebalance count, turnover and the correlation with SPY. No Sharpe,
  no win rate — the honesty budget (docs/BACKTEST.md).
* No NYSE holiday table existed anywhere in the engine (health.py and stops.py both say
  "exchange holidays are not known here"), so `nyse_holidays()` computes the exchange's
  rule-based calendar. It is the only calendar the desk uses; `is_rebalance_slot` also takes
  an explicit set of holiday dates for tests and for the day the rules change.

Pure except for `main()`; stdlib only.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import technicals

SECTOR_ETFS = ("XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC")
EX_US = "VEU"
BENCHMARK = "SPY"
BOND = "TLT"
RISK_UNIVERSE = SECTOR_ETFS + (EX_US,)          # what gets ranked
UNIVERSE = RISK_UNIVERSE + (BENCHMARK, BOND)    # what bars_etf.json must carry (14 symbols)

SECTOR_OF = {
    "XLK": "Information Technology", "XLF": "Financials", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLE": "Energy",
    "XLI": "Industrials", "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate",
    "XLC": "Communication Services", "VEU": "International Equity",
    "TLT": "Treasuries", "SPY": "Broad Market",
}
NAME_OF = {
    "XLK": "Technology Select Sector SPDR", "XLF": "Financial Select Sector SPDR",
    "XLV": "Health Care Select Sector SPDR", "XLY": "Consumer Discretionary Select Sector SPDR",
    "XLP": "Consumer Staples Select Sector SPDR", "XLE": "Energy Select Sector SPDR",
    "XLI": "Industrial Select Sector SPDR", "XLB": "Materials Select Sector SPDR",
    "XLU": "Utilities Select Sector SPDR", "XLRE": "Real Estate Select Sector SPDR",
    "XLC": "Communication Services Select Sector SPDR", "VEU": "Vanguard FTSE All-World ex-US",
    "TLT": "iShares 20+ Year Treasury Bond", "SPY": "SPDR S&P 500",
}

SETUP = "Sector Rotation"
VERDICT = "Buy"
SOURCE = "rotation"
SCORE = 85.0            # full conviction on the sizing scale; the RANK carries the signal
DEFAULTS = {"top_n": 3, "sleeves": 3, "bond_sleeves": 3, "drop_below_rank": 6, "bond": BOND,
            "benchmark": BENCHMARK}
YEAR = technicals.YEAR
TBILL_DEFAULT_NOTE = ("macro.json carries no tbill_3m_pct — the bill rate defaulted to 0.0%, "
                      "so the absolute filter compares SPY's 12-month return to zero")


def is_etf(symbol):
    """True for every symbol the desk may hold or rank. pm.py exempts these from the
    HOUSE-01 per-name / per-sector caps."""
    return str(symbol or "").upper() in UNIVERSE


def config(desk_rules=None):
    """The desk's rotation parameters: desks.json rules.rotation over DEFAULTS."""
    r = (desk_rules or {}).get("rotation") if isinstance(desk_rules, dict) else None
    out = dict(DEFAULTS)
    for k, v in (r or {}).items():
        if k in out:
            out[k] = v
    return out


# ---------------------------------------------------------------- calendar
def _date(d):
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(str(d)[:10])


def easter(year):
    """Gregorian Easter Sunday (the anonymous / Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(d):
    """NYSE observance: a Saturday holiday closes the Friday before, a Sunday the Monday
    after — except New Year's Day on a Saturday, which the exchange does not observe (NYSE
    Rule 7.2 keeps the prior year's last session open)."""
    if d.weekday() == 5:
        return None if (d.month, d.day) == (1, 1) else d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def _nth_weekday(year, month, weekday, n):
    first = dt.date(year, month, 1)
    off = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=off + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    nxt = dt.date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def nyse_holidays(year):
    """The NYSE's full-day closures for `year`, from the exchange's published rules.
    Special closures (a national day of mourning, 9/11) are not rule-based and are not here;
    `is_rebalance_slot` takes an explicit set for those."""
    out = set()
    for d in (_observed(dt.date(year, 1, 1)),
              _nth_weekday(year, 1, 0, 3),                    # MLK Day
              _nth_weekday(year, 2, 0, 3),                    # Presidents' Day
              easter(year) - dt.timedelta(days=2),            # Good Friday
              _last_weekday(year, 5, 0),                      # Memorial Day
              _observed(dt.date(year, 6, 19)) if year >= 2022 else None,   # Juneteenth
              _observed(dt.date(year, 7, 4)),                 # Independence Day
              _nth_weekday(year, 9, 0, 1),                    # Labor Day
              _nth_weekday(year, 11, 3, 4),                   # Thanksgiving
              _observed(dt.date(year, 12, 25))):              # Christmas
        if d is not None and d.year == year:
            out.add(d)
    return out


def _holidays(calendar, year):
    """`calendar` may be None (the NYSE rules), a set/list of dates or ISO strings, or a
    callable year -> set of dates."""
    if calendar is None:
        return nyse_holidays(year)
    if callable(calendar):
        return {_date(x) for x in (calendar(year) or ())}
    return {_date(x) for x in calendar}


def is_trading_day(d, calendar=None):
    d = _date(d)
    return d.weekday() < 5 and d not in _holidays(calendar, d.year)


def next_trading_day(d, calendar=None):
    d = _date(d) + dt.timedelta(days=1)
    while not is_trading_day(d, calendar):
        d += dt.timedelta(days=1)
    return d


def last_trading_day_of_month(d, calendar=None):
    d = _date(d)
    nxt = dt.date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
    x = nxt - dt.timedelta(days=1)
    while not is_trading_day(x, calendar):
        x -= dt.timedelta(days=1)
    return x


def last_trading_day_of_week(d, calendar=None):
    d = _date(d)
    x = d + dt.timedelta(days=4 - d.weekday())           # the Friday of this Mon-Fri week
    while x.weekday() >= 5 or not is_trading_day(x, calendar):
        x -= dt.timedelta(days=1)
    return x


def is_rebalance_slot(date, slot, calendar=None):
    """The monthly decision slot: the power-hour run on the last trading day of the month."""
    if slot != "power-hour":
        return False
    d = _date(date)
    return is_trading_day(d, calendar) and d == last_trading_day_of_month(d, calendar)


def is_weekly_slot(date, slot, calendar=None):
    """The weekly check: the power-hour run on the last trading day of the week."""
    if slot != "power-hour":
        return False
    d = _date(date)
    return is_trading_day(d, calendar) and d == last_trading_day_of_week(d, calendar)


def next_rebalance_date(date, calendar=None):
    d = _date(date)
    m = last_trading_day_of_month(d, calendar)
    if m > d:
        return m
    return last_trading_day_of_month(next_trading_day(m, calendar), calendar)


# ---------------------------------------------------------------- bars
def load_bars(raw):
    """{SYMBOL: [bar, ...]} oldest-first from a get_equity_historicals response (or its
    data block, or the bare results array, or a list of responses)."""
    blocks = raw if isinstance(raw, list) and raw and isinstance(raw[0], dict) \
        and "bars" not in raw[0] else [raw]
    out = {}
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            d = blk.get("data")
            rows = (d.get("results") if isinstance(d, dict) else None) or blk.get("results") or []
        for res in rows or []:
            if not isinstance(res, dict):
                continue
            sym = str(res.get("symbol") or "").upper()
            if not sym:
                continue
            bars = [b for b in (res.get("bars") or []) if isinstance(b, dict)]
            out.setdefault(sym, []).extend(bars)
    for sym in out:
        out[sym].sort(key=lambda b: str(b.get("begins_at") or b.get("date") or ""))
    return out


def _bar_date(b):
    return str(b.get("begins_at") or b.get("date") or "")[:10]


def upto(bars, as_of):
    """Bars on or before as_of — the no-look-ahead boundary, same as backtest.upto."""
    a = _date(as_of).isoformat()
    return [b for b in bars or [] if _bar_date(b) and _bar_date(b) <= a]


def _closes(bars):
    return [r["c"] for r in technicals._clean_bars(bars)]


def _last_close(bars):
    rows = technicals._clean_bars(bars)
    return rows[-1]["c"] if rows else None


# ---------------------------------------------------------------- the rule
def rank(bars_by_symbol, as_of, symbols=None):
    """[{symbol, ret_12_1, rank}] for the risk universe, best first (rank 1). A symbol whose
    history does not cover the window carries ret_12_1 None and rank None, after the ranked
    ones — never a 0 that would rank it."""
    syms = [s.upper() for s in (symbols or RISK_UNIVERSE)]
    rows = []
    for s in syms:
        hist = upto(bars_by_symbol.get(s) or [], as_of)
        r = technicals.features(hist)["ret_12_1"] if hist else None
        rows.append({"symbol": s, "ret_12_1": r, "rank": None})
    ranked = sorted([r for r in rows if isinstance(r["ret_12_1"], (int, float))],
                    key=lambda r: (-r["ret_12_1"], r["symbol"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    unranked = [r for r in rows if r["rank"] is None]
    return ranked + unranked


def spy_return_12m(spy_bars, as_of):
    """SPY's trailing 12-month total return: close[t] / close[t-252] - 1 (no skip — Antonacci's
    absolute momentum uses the full window). None when the history is short."""
    closes = _closes(upto(spy_bars or [], as_of))
    return technicals._ret(closes, YEAR)


def filter_detail(spy_bars, tbill_3m_pct, as_of):
    """{on, spy_ret_12m, tbill_ret_12m, tbill_3m_pct, note}. `tbill_3m_pct` is the annualised
    3-month bill yield in percent; over a 12-month window its return is the yield itself.
    None / missing means 0 and the note says so. An unknown SPY return switches the filter
    OFF (bonds) — an unmeasured market is not a bull market."""
    default = not isinstance(tbill_3m_pct, (int, float)) or isinstance(tbill_3m_pct, bool)
    tb = 0.0 if default else float(tbill_3m_pct)
    spy = spy_return_12m(spy_bars, as_of)
    note = TBILL_DEFAULT_NOTE if default else None
    if spy is None:
        return {"on": False, "spy_ret_12m": None, "tbill_ret_12m": tb / 100.0,
                "tbill_3m_pct": tb, "tbill_default_used": default,
                "note": "SPY has fewer than 253 bars to as_of — the absolute filter cannot be "
                        "measured and is treated as OFF (bond leg)"}
    return {"on": spy > tb / 100.0, "spy_ret_12m": spy, "tbill_ret_12m": tb / 100.0,
            "tbill_3m_pct": tb, "tbill_default_used": default, "note": note}


def absolute_filter(spy_bars, tbill_3m_pct, as_of):
    """True when SPY's 12-month return beats the bill return over the same window."""
    return bool(filter_detail(spy_bars, tbill_3m_pct, as_of)["on"])


def target_book(ranks, filter_on, top_n=3, bond=BOND):
    """The symbols the desk should hold: the top `top_n` ranked names when the filter is on,
    else the bond leg alone."""
    if not filter_on:
        return [bond]
    ranked = [r["symbol"] for r in ranks if r.get("rank") is not None]
    return ranked[:max(0, int(top_n))]


def weekly_check(held, ranks, filter_on, drop_below_rank=6, bond=BOND):
    """Does the weekly check need to act? Only when a HELD sector ETF has dropped below
    `drop_below_rank` (or out of the ranking), or the absolute filter has flipped against
    what the book holds: bonds held while the filter is on, sectors held while it is off.
    An empty book never triggers it — the monthly slot (or --force-rebalance) seeds a book."""
    held = [str(h).upper() for h in held or []]
    if not held:
        return False
    sectors = [h for h in held if h != bond]
    if filter_on and not sectors:
        return True                      # holding the bond leg in a bull filter
    if not filter_on and sectors:
        return True                      # holding sectors in a bear filter
    by_sym = {r["symbol"]: r.get("rank") for r in ranks}
    for h in sectors:
        rk = by_sym.get(h)
        if rk is None or rk > drop_below_rank:
            return True
    return False


def tbill_from_macro(macro):
    """(tbill_3m_pct or None, note). macro.json is {"tbill_3m_pct": 4.1, ...}; absent or
    malformed means the default and the note says so."""
    if not isinstance(macro, dict):
        return None, TBILL_DEFAULT_NOTE
    v = macro.get("tbill_3m_pct")
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None, TBILL_DEFAULT_NOTE
    return float(v), None


# ---------------------------------------------------------------- candidate rows
def candidate_row(symbol, bars, as_of, rank_row=None):
    """One scan-shaped row for pm.entry_pass. Every field the entry pass, build_proposals,
    the stop policy and the journal read is present; `_source` marks the provenance."""
    hist = upto(bars or [], as_of)
    d = technicals.derive(hist)
    price = d.get("last_close")
    if not price:
        return None
    feats = technicals.features(hist)
    rk = rank_row or {}
    return {
        "ticker": symbol, "name": NAME_OF.get(symbol, symbol),
        "sector": SECTOR_OF.get(symbol), "gics": SECTOR_OF.get(symbol),
        "industry": "Sector ETF" if symbol in SECTOR_ETFS else "ETF",
        "price": float(price), "score": SCORE, "coverage_pct": 100.0, "missing_pillars": [],
        "setup": SETUP, "verdict": VERDICT,
        "setup_note": (f"12-1 month return {rk['ret_12_1'] * 100:+.1f}%, rank {rk['rank']} "
                       f"of {len(RISK_UNIVERSE)} in the sector universe"
                       if isinstance(rk.get("ret_12_1"), (int, float)) and rk.get("rank")
                       else "bond leg — the absolute-momentum filter is off"),
        "confidence": "ok", "upside_pct": None, "analyst_target": None, "next_earnings": None,
        "rsi_14": d.get("rsi_14"), "atr_14": d.get("atr_14"), "atr_pct": d.get("atr_pct"),
        "ma_50": d.get("ma_50"), "ma_200": d.get("ma_200"), "bars_used": d.get("bars_used"),
        "features": {"ret_12_1": feats.get("ret_12_1"), "rv_20d": feats.get("rv_20d"),
                     "atr_pct": feats.get("atr_pct")},
        "rotation_rank": rk.get("rank"), "rotation_ret_12_1": rk.get("ret_12_1"),
        "_source": SOURCE,
    }


def proposals(bars_by_symbol, as_of, held, slot, macro=None, force=False, cfg=None,
              calendar=None, vol_rules=None):
    """The desk's decision for one run. Returns a dict the PM carries as scan["rotation"]:

        acts            whether this run trades (monthly slot, a triggered weekly check, or
                        --force-rebalance)
        mode            "monthly" | "weekly" | "forced" | None
        why             one sentence for the journal
        filter          filter_detail(): on/off, SPY 12m, bill, the default note
        ranks           rank() output
        target          target_book() output
        held / exits / affirmed / entries
        sleeves, sleeves_by_symbol, vol_scalar, spy_rv_20d
        rows            candidate rows for every TARGET symbol (held or not — build_proposals
                        skips the held ones itself); [] when not acting
        next_rebalance  the next monthly decision date
        notes           anything the journal should say (the bill default, short histories)
    """
    cfg = dict(DEFAULTS, **(cfg or {}))
    as_of = _date(as_of)
    held = [str(h).upper() for h in held or []]
    bond, bench = cfg["bond"], cfg["benchmark"]
    notes = []
    tb, tb_note = tbill_from_macro(macro)
    if tb_note:
        notes.append(tb_note)
    missing = [s for s in UNIVERSE if not bars_by_symbol.get(s)]
    if missing:
        notes.append(f"bars_etf.json is missing {', '.join(missing)} — the universe is "
                     f"{len(UNIVERSE)} symbols and a missing one cannot be ranked or held")
    ranks = rank(bars_by_symbol, as_of)
    short = [r["symbol"] for r in ranks if r["rank"] is None and r["symbol"] not in missing]
    if short:
        notes.append(f"{', '.join(short)}: fewer than 253 bars to {as_of} — not ranked "
                     "(ret_12_1 needs 12 months plus the skipped month)")
    fd = filter_detail(bars_by_symbol.get(bench), tb, as_of)
    if fd.get("note") and fd["note"] not in notes:
        notes.append(fd["note"])
    filter_on = bool(fd["on"])
    target = target_book(ranks, filter_on, cfg["top_n"], bond)

    monthly = is_rebalance_slot(as_of, slot, calendar)
    weekly = is_weekly_slot(as_of, slot, calendar)
    weekly_hit = weekly and weekly_check(held, ranks, filter_on, cfg["drop_below_rank"], bond)
    if force:
        mode, why = "forced", "--force-rebalance: the rule is applied at this slot"
    elif monthly:
        mode, why = "monthly", f"{as_of} is the last trading day of the month at power-hour"
    elif weekly_hit:
        mode, why = "weekly", ("weekly check: a held ETF fell below rank "
                               f"{cfg['drop_below_rank']} or the absolute filter flipped")
    else:
        mode = None
        nxt = next_rebalance_date(as_of, calendar)
        why = (f"not a decision slot — next monthly decision {nxt} at power-hour"
               + ("; weekly check found nothing to do" if weekly else ""))
    acts = mode is not None

    # sleeves: equal shares of desk equity; the bond leg takes bond_sleeves of them
    sleeves = int(cfg["sleeves"])
    by_symbol = {}
    for s in target:
        by_symbol[s] = int(cfg["bond_sleeves"]) if s == bond else 1
    spy_rv = None
    if bars_by_symbol.get(bench):
        spy_rv = technicals.features(upto(bars_by_symbol[bench], as_of)).get("rv_20d")
    vt = (vol_rules or {}).get("vol_target") if isinstance(vol_rules, dict) else None
    scalar = 1.0
    if isinstance(vt, dict) and vt.get("enabled"):
        import portfolio
        scalar = portfolio.desk_vol_scalar(spy_rv, vt.get("target_vol_pct", 12.0),
                                           vt.get("lo", 0.5), vt.get("hi", 1.5))

    rows = []
    if acts:
        by_rank = {r["symbol"]: r for r in ranks}
        for s in target:
            row = candidate_row(s, bars_by_symbol.get(s), as_of, by_rank.get(s))
            if row is None:
                notes.append(f"{s}: no usable bars to {as_of} — cannot be proposed")
                continue
            rows.append(row)
    exits = [h for h in held if h not in target] if acts else []
    affirmed = [h for h in held if h in target] if acts else []
    entries = [s for s in target if s not in held] if acts else []
    return {
        "acts": acts, "mode": mode, "why": why, "as_of": as_of.isoformat(), "slot": slot,
        "filter_on": filter_on, "filter": fd, "tbill_3m_pct": fd["tbill_3m_pct"],
        "tbill_default_used": fd["tbill_default_used"],
        "ranks": ranks, "target": target, "held": held,
        "exits": exits, "affirmed": affirmed, "entries": entries,
        "sleeves": sleeves, "sleeves_by_symbol": by_symbol,
        "vol_scalar": round(float(scalar), 4), "spy_rv_20d": spy_rv,
        "rows": rows, "next_rebalance": next_rebalance_date(as_of, calendar).isoformat(),
        "top_n": cfg["top_n"], "drop_below_rank": cfg["drop_below_rank"], "bond": bond,
        "notes": notes, "universe": list(UNIVERSE),
    }


# ---------------------------------------------------------------- the harness
def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def _tbill_asof(series, as_of):
    """A constant, or {date: pct} looked up as the latest date <= as_of; None when absent."""
    if series is None:
        return None
    if isinstance(series, (int, float)) and not isinstance(series, bool):
        return float(series)
    if isinstance(series, dict):
        best = None
        a = _date(as_of).isoformat()
        for k, v in series.items():
            if str(k)[:10] <= a and (best is None or str(k)[:10] > best[0]):
                best = (str(k)[:10], v)
        return float(best[1]) if best is not None else None
    return None


def replay(bars_by_symbol, start, end, tbill_series=None, cfg=None, cost_bps=5.0,
           weekly=True):
    """Walk the rule over the bars, monthly (plus the weekly check), and report the honest
    numbers: monthly returns, the equity curve, max drawdown, rebalances, turnover and the
    correlation with SPY. No Sharpe, no win rate.

    Calendar: SPY's own sessions in the bars file (backtest.py's "the only calendar that is
    real"); a decision falls on the last session of each calendar month and the trade is
    booked at the NEXT session's open (its close when the bar has no open) — the paper desk
    decides at 15:45 and fills at the next slot. `cost_bps` is the one-way cost applied to
    every buy and sell (default 5 bp: a liquid ETF's spread plus fees; the engine's fallback
    assumption of a 25 bp half-spread is for names with no quote and would be an order of
    magnitude too pessimistic here). `tbill_series` is a constant percent or {date: pct};
    None means 0 and the summary says so.
    """
    cfg = dict(DEFAULTS, **(cfg or {}))
    bond, bench = cfg["bond"], cfg["benchmark"]
    spy = bars_by_symbol.get(bench) or []
    if not spy:
        raise ValueError(f"{bench} is not in the bars — it sets the calendar and the filter")
    start_s, end_s = _date(start).isoformat(), _date(end).isoformat()
    cal = [_bar_date(b) for b in spy if start_s <= _bar_date(b) <= end_s]
    cal = sorted(set(cal))
    if not cal:
        raise ValueError(f"no {bench} sessions between {start_s} and {end_s}")
    all_dates = sorted(set(_bar_date(b) for b in spy))
    idx_of = {d: i for i, d in enumerate(all_dates)}
    closes, opens = {}, {}
    for s, bars in bars_by_symbol.items():
        for r in technicals._clean_bars(bars):
            closes.setdefault(s, {})[r["t"]] = r["c"]
            if r["o"]:
                opens.setdefault(s, {})[r["t"]] = r["o"]

    def px_open(sym, d):
        return opens.get(sym, {}).get(d) or closes.get(sym, {}).get(d)

    c = float(cost_bps) / 10_000.0
    equity = 1.0
    cash = 1.0
    units = {}                   # symbol -> units held
    curve = []
    decisions, rebalances, turnover = 0, 0, []
    bond_months, month_ends = 0, 0
    pending = None               # (target list, sleeves_by_symbol, mode) to trade at the next open
    tbill_default = tbill_series is None
    log = []

    for i, d in enumerate(cal):
        # 1. execute a pending decision at this session's open
        if pending is not None:
            target, sleeves_by, mode = pending
            pending = None
            val = cash + sum(u * px_open(s, d) for s, u in units.items() if px_open(s, d))
            w_target = {s: n / cfg["sleeves"] for s, n in sleeves_by.items()}
            # The live desk sells what left the target set and buys what entered it, each
            # entry sized to its sleeve of CURRENT equity; a re-affirmed name is not
            # re-trimmed (rebalance_pass only trims over the cap). Same here.
            sold = bought = 0.0
            for s in [s for s in units if s not in w_target]:
                p = px_open(s, d)
                if not p:
                    continue
                sold += units[s] * p
                cash += units[s] * p * (1 - c)
                del units[s]
            for s, w in w_target.items():
                if s in units:
                    continue
                p = px_open(s, d)
                if not p:
                    continue
                spend = min(w * val, max(0.0, cash))
                if spend <= 0:
                    continue
                units[s] = spend * (1 - c) / p
                cash -= spend
                bought += spend
            if sold > 0 or bought > 0:
                one_way = (sold + bought) / 2.0 / val if val > 0 else 0.0
                turnover.append(one_way)
                rebalances += 1
                log.append({"date": d, "mode": mode, "target": target,
                            "turnover": round(one_way, 4)})
        # 2. mark at the close
        val = cash + sum(u * closes[s][d] for s, u in units.items() if d in closes.get(s, {}))
        equity = val
        curve.append({"date": d, "equity": round(equity, 6)})
        # 3. decide at the close: last session of the month, or the weekly check
        nxt = all_dates[idx_of[d] + 1] if idx_of[d] + 1 < len(all_dates) else None
        is_month_end = nxt is None or nxt[:7] != d[:7]
        dd = _date(d)
        is_week_end = nxt is None or _date(nxt).isocalendar()[:2] != dd.isocalendar()[:2]
        if not (is_month_end or (weekly and is_week_end)):
            continue
        ranks = rank(bars_by_symbol, d)
        fd = filter_detail(spy, _tbill_asof(tbill_series, d), d)
        filt = bool(fd["on"])
        held = list(units)
        act = is_month_end or (weekly and is_week_end
                               and weekly_check(held, ranks, filt, cfg["drop_below_rank"], bond))
        if is_month_end:
            month_ends += 1
            if not filt:
                bond_months += 1
        if not act:
            continue
        if fd["spy_ret_12m"] is None and not any(r["rank"] for r in ranks):
            continue                                  # not enough history yet: stay in cash
        target = target_book(ranks, filt, cfg["top_n"], bond)
        sleeves_by = {s: (int(cfg["bond_sleeves"]) if s == bond else 1) for s in target}
        decisions += 1
        pending = (target, sleeves_by, "monthly" if is_month_end else "weekly")

    # ---- summary numbers
    eq = [p["equity"] for p in curve]
    dates = [p["date"] for p in curve]
    daily = [b / a - 1.0 for a, b in zip(eq, eq[1:])]
    spy_daily = []
    for a, b in zip(dates, dates[1:]):
        ca, cb = closes[bench].get(a), closes[bench].get(b)
        spy_daily.append(cb / ca - 1.0 if ca and cb else 0.0)
    peak, max_dd = eq[0] if eq else 1.0, 0.0
    for e in eq:
        peak = max(peak, e)
        max_dd = min(max_dd, e / peak - 1.0)
    monthly = {}
    last_month, last_eq = None, None
    for dte, e in zip(dates, eq):
        m = dte[:7]
        if last_month is None:
            last_month, last_eq = m, e
            month_start_eq = eq[0]
            continue
        if m != last_month:
            monthly[last_month] = round(last_eq / month_start_eq - 1.0, 6)
            month_start_eq = last_eq
            last_month = m
        last_eq = e
    if last_month is not None and last_month not in monthly:
        monthly[last_month] = round(last_eq / month_start_eq - 1.0, 6)
    mvals = list(monthly.values())
    return {
        "_what": "E26 sector-rotation replay: 12-1 momentum rank, Antonacci absolute filter "
                 "vs the 3m bill, top-3 equal sleeves or TLT, decided at month-end, weekly "
                 "check, traded at the next open.",
        "_warnings": [
            "No Sharpe and no win rate by design (the honesty budget).",
            "Survivorship is small here (the ETFs all exist) but the START of the window is "
            "chosen with hindsight and the rule's parameters (12-1, top 3, rank 6) are the "
            "literature's, not fitted — every re-run with other parameters is a trial.",
            "Fills are the next session's open plus the stated one-way cost; the paper desk "
            "fills at the next slot's quote plus slippage, which is not the same number.",
        ] + ([TBILL_DEFAULT_NOTE] if tbill_default else []),
        "start": dates[0] if dates else start_s, "end": dates[-1] if dates else end_s,
        "sessions": len(curve), "cost_bps_one_way": float(cost_bps),
        "tbill_default_used": tbill_default,
        "config": {k: cfg[k] for k in ("top_n", "sleeves", "bond_sleeves", "drop_below_rank",
                                       "bond", "benchmark")},
        "monthly_returns": monthly,
        "n_months": len(mvals),
        "monthly_mean_pct": round(sum(mvals) / len(mvals) * 100.0, 4) if mvals else None,
        "monthly_median_pct": (round(sorted(mvals)[len(mvals) // 2] * 100.0, 4)
                               if mvals else None),
        "total_return_pct": round((eq[-1] / eq[0] - 1.0) * 100.0, 4) if eq else None,
        "equity_curve": curve,
        "max_dd": round(max_dd, 6),
        "max_dd_pct": round(max_dd * 100.0, 4),
        "n_decisions": decisions,
        "n_rebalances": rebalances,
        "n_month_ends": month_ends,
        "bond_months": bond_months,
        "corr_with_spy": (round(_pearson(daily, spy_daily), 4)
                          if _pearson(daily, spy_daily) is not None else None),
        "turnover": {"mean_one_way": round(sum(turnover) / len(turnover), 4) if turnover else None,
                     "total_one_way": round(sum(turnover), 4), "n": len(turnover)},
        "log": log,
    }


# ---------------------------------------------------------------- correlation with another book
def load_equity_curve(path):
    """[{date, equity}] from a paper book (equity_curve with slots — the last point per date
    wins), a replay summary, or a bare list of {date, equity}."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    pts = raw.get("equity_curve") if isinstance(raw, dict) else raw
    if not isinstance(pts, list):
        return []
    by_date = {}
    for p in pts:
        if not isinstance(p, dict):
            continue
        d, e = str(p.get("date") or p.get("ts") or "")[:10], p.get("equity")
        if d and isinstance(e, (int, float)) and e > 0:
            by_date[d] = float(e)                     # later slots overwrite earlier ones
    return [{"date": d, "equity": by_date[d]} for d in sorted(by_date)]


def corr_with(curve_a, curve_b):
    """{corr, n, dates} — Pearson correlation of the two curves' daily returns on their
    common dates. n under 30 is reported as such: it is not a sample."""
    a = {p["date"]: p["equity"] for p in curve_a}
    b = {p["date"]: p["equity"] for p in curve_b}
    common = sorted(set(a) & set(b))
    ra, rb = [], []
    for x, y in zip(common, common[1:]):
        if a[x] > 0 and b[x] > 0:
            ra.append(a[y] / a[x] - 1.0)
            rb.append(b[y] / b[x] - 1.0)
    r = _pearson(ra, rb)
    return {"corr": round(r, 4) if r is not None else None, "n": len(ra),
            "first": common[0] if common else None, "last": common[-1] if common else None,
            "not_a_sample": len(ra) < 30}


# ---------------------------------------------------------------- CLI
def seed_book(starting_equity=5000.0, today=None):
    """An empty paper book for the desk, the same $5,000 seed every desk starts from —
    what activation writes to the state repo as books/rotation.json."""
    d = (today or dt.date.today()).isoformat()
    return {"mode": "paper", "desk": "rotation", "revision": 0, "based_on_revision": None,
            "last_run": None, "seeded": d, "starting_equity": float(starting_equity),
            "cash": float(starting_equity), "realized_pnl": 0.0, "positions": [],
            "working_orders": [], "closed_trades": [], "day_trades": [], "equity_curve": [],
            "day": {"date": None, "open_equity": None, "halted": False, "halt_reason": None}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bars", default="bars_etf.json")
    ap.add_argument("--as-of", dest="as_of", default=None, help="YYYY-MM-DD (default: last bar)")
    ap.add_argument("--slot", default="power-hour")
    ap.add_argument("--macro", default="macro.json")
    ap.add_argument("--held", default="", help="comma-separated symbols the book holds")
    ap.add_argument("--force-rebalance", dest="force", action="store_true")
    ap.add_argument("--seed-book", dest="seed", action="store_true",
                    help="print an empty $5,000 paper book for the desk and exit")
    a = ap.parse_args(argv)
    if a.seed:
        print(json.dumps(seed_book(), indent=2))
        return 0
    with open(a.bars, encoding="utf-8") as f:
        bars = load_bars(json.load(f))
    macro = None
    if os.path.exists(a.macro):
        with open(a.macro, encoding="utf-8") as f:
            macro = json.load(f)
    as_of = a.as_of or max(_bar_date(b) for b in bars.get(BENCHMARK) or [] if _bar_date(b))
    out = proposals(bars, as_of, [s for s in a.held.upper().split(",") if s], a.slot,
                    macro=macro, force=a.force)
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
