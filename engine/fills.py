"""fills.py — what a trade actually costs, instead of a flat 0.25%.

THE BOOKED FILL MODEL IS NOT SWITCHED, deliberately. `pm.py` keeps its current fill model
until someone decides to switch, because every function here makes the paper record WORSE,
and a silent change to the cost model on a live book would make yesterday's equity curve
and today's incomparable for reasons nobody could see. Switching is a flag and a doctrine
note; do it knowingly, on a session boundary, and say so in the journal. What pm.py DOES
read from here, since K-06, is the SHADOW model at the bottom of this file — it records a
second price next to every booked fill and changes nothing about the book's P&L.

WHAT IS WRONG WITH THE CURRENT MODEL
------------------------------------
PM.md section 11 calls it out: "Slippage is a flat 0.25% on exits. It is a placeholder, not a
measurement." Three specific ways that placeholder flatters the book:

1. **It ignores the spread**, which the engine now measures on every quote. On a thin name
   the bid/ask is a larger cost than anything the scoring model reasons about. A sale does
   not happen at the last print less a constant; it happens at the bid.
2. **A gap through a stop is recorded as a fill at the stop less 0.25%.** A stop is not a
   resting order here (PM.md section 5 — fractional positions cannot carry one at the
   broker), and even if it were, a gap-down opens THROUGH it. The honest fill is the open.
   This is the single largest overstatement in the paper record, and it is worst precisely
   on the days that matter.
3. **A gap through a buy limit is recorded as a fill at the limit.** That one runs the other
   way — a real order would have filled at the better open price — so correcting it costs
   the book nothing and is included for symmetry, because a cost model that only ever
   corrects in one direction is not a cost model, it is a haircut.

Everything here is a pure function of numbers you already have. No state, no I/O.

    fill = exit_fill(price=222.26, bid=222.10, ask=222.32)
    fill = stop_exit_fill(stop=205.49, session_open=198.00)   # gapped through

SHADOW FILLS (K-06, 2026-09-10) — the first thing here that pm.py DOES read
-----------------------------------------------------------------------------
The functions above stay unwired. What pm.py now calls is `shadow_fill()`: alongside every
booked fill it records what the same order would have cost against the quote — a buy at
the ask plus k half-spreads plus a size add-on, a sale at the bid less the same — and the
signed gap between the two. The booked price and the booked P&L do not move; the book
simply carries a second, honest number next to each of its own. `shadow_gap_usd` is
(booked − shadow) × shares, so a POSITIVE gap means the paper book flattered itself.
`implementation_shortfall_bps` is the shadow fill's distance from the decision-time mid,
which is what a real desk means by execution cost, and `cost_budget_status()` adds those
up against a per-year budget. The booked path is byte-identical with the model switched
off; the test suite pins that against a frozen pre-change output.
"""

# Costs are per-side and in basis points of notional. Robinhood charges no commission on
# equities; the SEC/FINRA pass-through fees apply to SALES only and are tiny but real, and a
# model that pretends they are zero will be wrong in the same direction every single time.
DEFAULT_COSTS = {
    "sell_fee_bps": 0.3,        # SEC + FINRA TAF pass-through, sales only, order of magnitude
    "buy_fee_bps": 0.0,
    # Fallback when no quote is available. This is the OLD flat assumption, kept only as the
    # last resort and named so it can never be mistaken for a measurement.
    "assumed_half_spread_pct": 0.25,
    # A quoted spread wider than this is not a tradeable market for this book — the caller
    # should refuse the trade rather than model a fill inside it.
    "max_modelled_spread_pct": 5.0,
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def half_spread(price, bid=None, ask=None, costs=None):
    """Half the quoted spread as a fraction of price, or the fallback assumption.

    Returns (fraction, basis) where basis is 'quoted' or 'assumed', because a board that
    cannot say which one it used is making a claim it has not earned."""
    c = dict(DEFAULT_COSTS, **(costs or {}))
    if _num(bid) and _num(ask) and bid > 0 and ask >= bid and _num(price) and price > 0:
        spread_pct = (ask - bid) / price * 100.0
        if spread_pct <= c["max_modelled_spread_pct"]:
            return spread_pct / 200.0, "quoted"
    return c["assumed_half_spread_pct"] / 100.0, "assumed"


def exit_fill(price, bid=None, ask=None, costs=None):
    """A marketable sale. Fills at the bid when one is quoted, then pays the sale fee.

    Returns (fill_price, detail). The detail names the basis so the journal can say whether
    the number came from a quote or from an assumption."""
    c = dict(DEFAULT_COSTS, **(costs or {}))
    if _num(bid) and bid > 0 and _num(price) and price > 0 and (not _num(ask) or ask >= bid):
        gross, basis = float(bid), "bid"
    else:
        hs, hb = half_spread(price, bid, ask, c)
        gross, basis = float(price) * (1 - hs), f"{hb} half-spread"
    net = gross * (1 - c["sell_fee_bps"] / 10_000.0)
    return round(net, 6), f"sold at the {basis}"


def entry_fill(limit, session_open=None, ask=None, costs=None):
    """A resting buy limit worked against a session.

    A limit buy fills at the LIMIT when price trades down to it intraday, but at the OPEN
    when the session gaps below it — you do not pay more than you asked, and on a gap you
    pay less. The current engine always books the limit, which understates the book by the
    size of every favourable gap."""
    c = dict(DEFAULT_COSTS, **(costs or {}))
    if not (_num(limit) and limit > 0):
        return None, "no limit price"
    px, basis = float(limit), "at the resting limit"
    if _num(session_open) and 0 < session_open < limit:
        px, basis = float(session_open), "at the open — the session gapped through the limit"
    net = px * (1 + c["buy_fee_bps"] / 10_000.0)
    return round(net, 6), basis


def stop_exit_fill(stop, session_open=None, price=None, bid=None, ask=None, costs=None):
    """A stop that has been broken.

    THE important one. A stop here is not resting at the broker, and even a real stop order
    becomes a market order once touched. When the session opens BELOW the stop, the fill is
    the open — not the stop, and not the stop less a constant. On a gap-down that difference
    is the whole loss."""
    if _num(session_open) and _num(stop) and session_open < stop:
        return exit_fill(session_open, None, None, costs)[0], \
            "gapped through the stop — filled at the open, not at the stop"
    ref = price if _num(price) else stop
    px, detail = exit_fill(ref, bid, ask, costs)
    return px, detail


def round_trip_cost_pct(price, bid=None, ask=None, costs=None):
    """What one complete in-and-out costs, as a percentage of price, before any market move.

    The number to hold against an expected edge: a strategy whose edge is smaller than this
    is a way of paying the spread on a schedule."""
    c = dict(DEFAULT_COSTS, **(costs or {}))
    hs, basis = half_spread(price, bid, ask, c)
    fees = (c["buy_fee_bps"] + c["sell_fee_bps"]) / 10_000.0
    return round((hs * 2 + fees) * 100.0, 4), basis


def tradeable_spread(price, bid, ask, max_spread_pct=1.0):
    """(ok, spread_pct or None). Mirrors PM_RULES['max_spread_pct'] — an entry that pays a
    spread wider than the gate is a bad trade before the thesis is even tested."""
    if not (_num(bid) and _num(ask) and _num(price) and price > 0 and ask >= bid > 0):
        return False, None
    spread_pct = (ask - bid) / price * 100.0
    return spread_pct <= max_spread_pct, round(spread_pct, 4)


# ------------------------------------------------------------------ shadow fills (K-06)
# The defaults pm.PM_RULES["shadow"] ships with. k is the number of half-spreads paid
# BEYOND the touch: 1.0 at the open, at the close and pre-market, where the book crosses
# a spread that is wide and moving; 0.0 at midday, where a marketable order on a liquid
# name really does fill at the touch; 0.5 for the sentinel and ad-hoc runs. The add-on is
# the size/impact cost the spread does not carry, in bps of the mid, by liquidity tier.
DEFAULT_SHADOW = {
    "enabled": True,
    "k_by_slot": {"pre-market": 1.0, "opening-range": 1.0, "midday": 0.0, "power-hour": 1.0,
                  "sentinel": 0.5, "ad-hoc": 0.5},
    "add_on_bps_large_cap": 2.0,
    "add_on_bps_small_cap": 15.0,
    "small_cap_adv_usd": 10e6,       # dollar ADV under this is the small-cap tier
    "gap_through_stops": True,       # a stop exit's reference is min(last, stop), never the stop
}
DEFAULT_COST_BUDGET_BPS_PER_YEAR = 200.0


def adv_usd(row):
    """Dollar average daily volume from whatever a scan row or order meta carries: an
    explicit `adv_usd`, else `avg_volume_20d` × price, else `volume` × price, else None —
    and None means the caller treats the name as LARGE cap. The default errs toward the
    smaller add-on deliberately: the tier is a guess, and the guess is labelled."""
    if not isinstance(row, dict):
        return None
    v = row.get("adv_usd")
    if _num(v) and v > 0:
        return float(v)
    px = row.get("price")
    if not (_num(px) and px > 0):
        return None
    for k in ("avg_volume_20d", "volume"):
        vol = row.get(k)
        if _num(vol) and vol > 0:
            return float(vol) * float(px)
    return None


def shadow_fill(side, bid, ask, last, slot, rules=None, adv_usd=None, cap=None):
    """What a marketable order would really have paid, against the quote it saw.

    Buys fill at  ask + k × half_spread + add_on;  sells at  bid − k × half_spread − add_on.
    `k` comes from rules["k_by_slot"][slot]; the add-on is bps of the mid by liquidity tier
    (small cap when `adv_usd` is known and under rules["small_cap_adv_usd"], else large).
    `cap`, when given, bounds the touch: a sale's touch is min(bid, cap), a buy's is
    max(ask, cap). The exit path passes cap = min(last, stop) for a broken stop under
    rules["gap_through_stops"], so a stale bid above the tape is never the fill and the
    stop price itself never is either.

    Returns {"price", "half_spread", "k", "add_on_bps", "basis"}. With no usable bid/ask
    the price is `last` (the booked price the caller passes) and basis is "no-quote", so
    a missing quote records a zero gap rather than an invented one.
    """
    r = dict(DEFAULT_SHADOW, **(rules or {}))
    k_map = r.get("k_by_slot") or {}
    k = float(k_map.get(slot, k_map.get("ad-hoc", 0.5)))
    small = _num(adv_usd) and adv_usd < float(r.get("small_cap_adv_usd", 10e6))
    add_on_bps = float(r["add_on_bps_small_cap"] if small else r["add_on_bps_large_cap"])
    quoted = (_num(bid) and _num(ask) and bid > 0 and ask >= bid)
    if not quoted:
        px = float(last) if _num(last) else None
        return {"price": round(px, 6) if px is not None else None, "half_spread": None,
                "k": k, "add_on_bps": add_on_bps, "basis": "no-quote"}
    hs = (float(ask) - float(bid)) / 2.0
    mid = (float(ask) + float(bid)) / 2.0
    add_on = mid * add_on_bps / 10_000.0
    if side == "buy":
        touch = float(ask)
        if _num(cap) and cap > touch:
            touch = float(cap)
        px = touch + k * hs + add_on
    else:
        touch = float(bid)
        if _num(cap) and 0 < cap < touch:
            touch = float(cap)
        px = touch - k * hs - add_on
    return {"price": round(px, 6), "half_spread": round(hs, 6), "k": k,
            "add_on_bps": add_on_bps,
            "basis": f"{'ask' if side == 'buy' else 'bid'} {'+' if side == 'buy' else '−'} "
                     f"{k:g}×half-spread {'+' if side == 'buy' else '−'} {add_on_bps:g}bp "
                     f"({'small' if small else 'large'} cap)"}


def decision_mid(bid, ask):
    """(bid + ask) / 2 at the moment of the decision, or None without a two-sided quote."""
    if _num(bid) and _num(ask) and bid > 0 and ask >= bid:
        return round((float(bid) + float(ask)) / 2.0, 6)
    return None


def shortfall_bps(fill, mid):
    """Implementation shortfall: |fill − decision mid| / mid × 1e4. None without a mid."""
    if not (_num(fill) and _num(mid) and mid > 0):
        return None
    return round(abs(float(fill) - float(mid)) / float(mid) * 10_000.0, 4)


def record_shadow(book, side, shares, booked, shadow, mid, today):
    """Accumulate one fill into book["shadow"] and return the per-fill record.

    gap = (booked − shadow) × shares for a SALE (the book sold higher than it would have),
    and (shadow − booked) × shares for a BUY (the book bought cheaper) — so a positive gap
    always means the paper book flattered itself. The yearly cost accumulator is what
    cost_budget_status() reads: shortfall × notional, in dollars, keyed by year.
    """
    sh = book.setdefault("shadow", {"cum_gap_usd": 0.0, "n_fills": 0,
                                    "gap_share_of_realized_pct": None, "by_year": {}})
    sh.setdefault("by_year", {})
    spx = shadow.get("price")
    gap = None
    if _num(spx) and _num(booked):
        gap = (float(booked) - spx) * shares if side == "sell" else (spx - float(booked)) * shares
        gap = round(gap, 6)
    isf = shortfall_bps(spx, mid)
    notional = round(float(booked) * shares, 6)
    sh["cum_gap_usd"] = round(sh.get("cum_gap_usd", 0.0) + (gap or 0.0), 6)
    sh["n_fills"] = int(sh.get("n_fills", 0)) + 1
    yr = str(getattr(today, "year", str(today)[:4]))
    y = sh["by_year"].setdefault(yr, {"shortfall_usd": 0.0, "notional_usd": 0.0, "n_fills": 0,
                                      "n_quoted": 0})
    y["notional_usd"] = round(y["notional_usd"] + notional, 6)
    y["n_fills"] += 1
    if isf is not None:
        y["shortfall_usd"] = round(y["shortfall_usd"] + isf / 10_000.0 * notional, 6)
        y["n_quoted"] += 1
    realized = float(book.get("realized_pnl", 0.0) or 0.0)
    sh["gap_share_of_realized_pct"] = (round(sh["cum_gap_usd"] / abs(realized) * 100.0, 2)
                                       if realized else None)
    return {"shadow_price": round(spx, 4) if _num(spx) else None,
            "shadow_gap_usd": round(gap, 4) if gap is not None else None,
            "implementation_shortfall_bps": isf,
            "decision_mid": mid, "shadow_basis": shadow.get("basis")}


def shadow_summary(book):
    """The book-level block, as the journal and state carry it (no per-year detail)."""
    sh = (book or {}).get("shadow") or {}
    realized = float((book or {}).get("realized_pnl", 0.0) or 0.0)
    cum = round(float(sh.get("cum_gap_usd", 0.0) or 0.0), 2)
    return {"cum_gap_usd": cum, "n_fills": int(sh.get("n_fills", 0) or 0),
            "gap_share_of_realized_pct": (round(cum / abs(realized) * 100.0, 2)
                                          if realized else None)}


def cost_budget_status(book, today, rules=None):
    """Year-to-date execution cost against the desk's budget.

    ytd_cost_bps = Σ(shortfall × notional) over the year / average equity × 1e4, where the
    average equity is the mean of this year's equity-curve points (the book's current
    mark when the curve is empty). share_used_pct over 100 is the warning condition."""
    budget = float((rules or {}).get("cost_budget_bps_per_year",
                                     DEFAULT_COST_BUDGET_BPS_PER_YEAR))
    yr = str(getattr(today, "year", str(today)[:4]))
    y = (((book or {}).get("shadow") or {}).get("by_year") or {}).get(yr) or {}
    cost_usd = float(y.get("shortfall_usd", 0.0) or 0.0)
    pts = [c.get("equity") for c in ((book or {}).get("equity_curve") or [])
           if isinstance(c, dict) and str(c.get("date", ""))[:4] == yr and _num(c.get("equity"))]
    if pts:
        avg_eq = sum(pts) / len(pts)
    else:
        avg_eq = float((book or {}).get("cash", 0.0) or 0.0) + sum(
            (p.get("last_price") or p.get("avg_cost") or 0.0) * p.get("shares", 0.0)
            for p in ((book or {}).get("positions") or []))
    ytd_bps = round(cost_usd / avg_eq * 10_000.0, 4) if avg_eq > 0 else 0.0
    return {"ytd_cost_bps": ytd_bps, "budget_bps": budget,
            "share_used_pct": round(ytd_bps / budget * 100.0, 2) if budget > 0 else None,
            "ytd_cost_usd": round(cost_usd, 2), "avg_equity": round(avg_eq, 2),
            "n_fills": int(y.get("n_fills", 0) or 0), "year": yr}
