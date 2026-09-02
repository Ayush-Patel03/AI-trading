"""fills.py — what a trade actually costs, instead of a flat 0.25%.

WIRED INTO NOTHING YET, deliberately. `pm.py` keeps its current fill model until someone
decides to switch, because every function here makes the paper record WORSE, and a silent
change to the cost model on a live book would make yesterday's equity curve and today's
incomparable for reasons nobody could see. Switching is a flag and a doctrine note; do it
knowingly, on a session boundary, and say so in the journal.

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
