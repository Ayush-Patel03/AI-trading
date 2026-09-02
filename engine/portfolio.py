"""Portfolio and order-proposal layer for Scan Desk.

Mirrors the sizing and risk rules already in the trading-system repo
(src/execution/position_sizing.py, src/signals/profiles.py, src/ingestion/live.py)
so proposals obey the same limits the live executor would enforce.

This module PROPOSES. It never places an order. Execution is manual in Robinhood.

Paths resolve from SCAN_DIR, else from this file's own directory.
"""
import json, os, sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

# ---- rule config: mirrors src/signals/profiles.py + the risk audit ----
RULES = {
    "max_risk_per_trade_pct": 2.0,   # position_sizing default
    "min_risk_per_trade_pct": 0.5,   # conviction floor
    "max_positions": 10,
    "max_position_pct": 15.0,        # max single-name value
    "cumulative_risk_cap_pct": 20.0, # engine.py: sum of per-position risk
    "max_per_sector": 3,             # sectors.py MAX_PER_SECTOR
    "max_deployed_pct": 85.0,        # live.py
    "min_cash_reserve_pct": 15.0,
    "max_daily_loss_pct": 3.0,       # kill switch
    "min_score_to_propose": 60.0,
    # Exit / trim score thresholds. THE SINGLE SOURCE (M2): pm.py's exit pass reads these
    # rather than carrying its own copies of 45 and 55, which is how the two files drifted
    # apart in the first place. Scan Desk's advisory verdict and the manager's actual exit
    # must never disagree about when a thesis is gone.
    "exit_score_below": 45.0,     # below this the thesis that bought it is gone — close
    "trim_score_below": 55.0,     # between the two it is weakening — trim, do not close
    "atr_stop_multiple": 1.5,     # repo uses 1.2x; 1.5x for a multi-day swing hold
    "max_stop_pct": 12.0,         # never risk more than this per share, however wide the ATR
    "min_stop_pct": 3.0,          # never place a stop inside normal daily noise
    "fractional": True,          # Robinhood supports fractional shares
    "min_notional": 1.00,        # Robinhood's minimum fractional order
    "share_decimals": 6,
}

def capital_basis(book):
    """What was actually put in — the seed plus every later deposit (M4).

    `starting_equity` is set once when a book is seeded and never moves. Fund the account
    afterwards and the return is measured against a basis that no longer describes the
    capital at work: deposit $5,000 into a $5,000 book and it reports +100% having earned
    nothing. A book records funding as `deposits: [{date, amount}]`; withdrawals are the
    same list with a negative amount.

    Returns None when the book was never seeded, so a caller can report no return rather
    than a fabricated one."""
    seed = (book or {}).get("starting_equity")
    if not isinstance(seed, (int, float)) or isinstance(seed, bool) or seed <= 0:
        return None
    added = 0.0
    for d in (book.get("deposits") or []):
        if not isinstance(d, dict):
            continue
        amt = d.get("amount")
        if isinstance(amt, (int, float)) and not isinstance(amt, bool):
            added += float(amt)
    basis = float(seed) + added
    return basis if basis > 0 else None


# ---- sizing: verbatim logic from src/execution/position_sizing.py ----
def calculate_position_size(portfolio_value, entry_price, stop_loss_price, max_risk_pct=2.0,
                            fractional=False, decimals=6, cash_available=None):
    """Same formula as src/execution/position_sizing.py. The repo floors to whole
    shares because Alpaca trades whole shares; Robinhood supports fractional, so
    fractional=True keeps the remainder instead of discarding it.

    Affordability is capped by CASH, not by total equity. Equity includes money
    already tied up in open positions, so sizing against it proposes orders the
    account cannot actually fill."""
    if entry_price <= 0 or stop_loss_price <= 0:
        return 0.0
    if stop_loss_price >= entry_price:
        return 0.0
    risk_amount = portfolio_value * (max_risk_pct / 100)
    risk_per_share = entry_price - stop_loss_price
    if risk_per_share < 0.01:
        return 0.0
    raw = risk_amount / risk_per_share
    spendable = portfolio_value if cash_available is None else max(0.0, cash_available)
    max_affordable = spendable / entry_price
    shares = min(raw, max_affordable)
    return round(shares, decimals) if fractional else float(int(shares))

def conviction_from_score(score):
    """Scan Desk score -> the 0-2 conviction scale the sizing model expects.
    45 -> 0.0 (floor risk), 65 -> 1.0 (mid), 85+ -> 2.0 (full max_risk)."""
    return max(0.0, min(2.0, (score - 45.0) / 20.0))

def conviction_risk_pct(conviction, max_risk_pct, min_risk_pct):
    """Verbatim from calculate_conviction_position_size."""
    conviction = max(0.0, min(2.0, conviction))
    mid = (max_risk_pct + min_risk_pct) / 2
    if conviction <= 1.0:
        return min_risk_pct + (mid - min_risk_pct) * conviction
    return mid + (max_risk_pct - mid) * (conviction - 1.0)

def correlation_size_adjustment(gics, industry, open_positions):
    """PROXY for the repo's average_correlation_to_book, which needs price history
    we do not have here. Uses sector/industry overlap instead and says so.
    Same output shape: a multiplier in (0, 1]."""
    if not open_positions:
        return 1.0, "No open positions"
    same_sector = [p for p in open_positions if p.get("gics") == gics]
    same_industry = [p for p in same_sector if p.get("industry") == industry]
    if same_industry:
        est = 0.75 + 0.05 * min(len(same_industry), 3)
    elif same_sector:
        est = 0.45 + 0.08 * min(len(same_sector), 3)
    else:
        return 1.0, "No sector overlap with the book"
    clamped = max(0.3, min(0.9, est))
    factor = max(0.3, 1.0 - ((clamped - 0.3) / 0.6) * 0.7)
    note = (f"{len(same_industry)} position(s) in the same industry" if same_industry
            else f"{len(same_sector)} position(s) in the same sector")
    return factor, f"{note} — estimated correlation {est:.2f}, size x{factor:.2f} (PROXY, not computed from returns)"

# ---- stops: ATR when bars are available, structure when they are not ----
def derive_levels(r, rules=None):
    """Volatility-based stop and a 3R target.

    Until 2026-08-31 this data path had no OHLC bars, so stops were placed against
    structure and PORTFOLIO.md said to switch to ATR the moment bars became reachable.
    Robinhood's historicals made them reachable, so ATR is now the primary basis and
    structure is the documented fallback. A stop must always be reported with the basis
    that produced it — the two size positions very differently on a volatile name.

    Returns (stop, target, basis, risk_per_share, basis_kind)."""
    rules = rules or RULES
    p, ma50, ma200 = r["price"], r.get("ma_50"), r.get("ma_200")
    setup = r["setup"]
    atr = r.get("atr_14")

    if isinstance(atr, (int, float)) and atr > 0 and p > 0:
        mult = rules.get("atr_stop_multiple", 1.5)
        raw = p - mult * atr
        floor_ = p * (1 - rules.get("max_stop_pct", 12.0) / 100.0)
        ceil_ = p * (1 - rules.get("min_stop_pct", 3.0) / 100.0)
        stop = min(max(raw, floor_), ceil_)
        pct = (p - stop) / p * 100.0
        basis = (f"{mult:g}x ATR ({atr:.2f}) below ${p:,.2f} — {pct:.1f}% of price, "
                 f"sized to this name's actual daily range")
        if stop == floor_:
            basis += f"; widened stop capped at the {rules['max_stop_pct']:.0f}% limit"
        elif stop == ceil_:
            basis += f"; tightened stop floored at {rules['min_stop_pct']:.0f}% to stay outside noise"
        # A structural level just under the ATR stop is the better place to sit.
        for lvl, name in ((ma200, "200-day MA"), (ma50, "50-day MA")):
            if isinstance(lvl, (int, float)) and lvl < p and 0 < (stop - lvl * 0.985) / p < 0.02:
                stop = round(lvl * 0.985, 2)
                basis = (f"1.5% below the {name} (${lvl:,.2f}) — within a whisker of the "
                         f"{mult:g}x ATR stop, so the structural level is the better place to sit")
                break
        stop = round(stop, 2)
        risk = p - stop
        return stop, round(p + 3 * risk, 2), basis, risk, "atr"

    if setup == "Pullback in Uptrend" and ma200 and ma200 < p:
        stop = max(ma200 * 0.985, p * 0.90)
        basis = "1.5% below the 200-day MA — the level that defines the uptrend"
    elif setup == "Momentum" and ma50 and ma50 < p:
        stop = max(ma50 * 0.985, p * 0.92)
        basis = "1.5% below the 50-day MA — the trend line it is riding"
    elif setup == "Early Recovery" and ma50 and ma50 < p:
        stop = max(ma50 * 0.97, p * 0.90)
        basis = "3% below the 50-day MA — recovery is unconfirmed, wider stop"
    else:
        stop = p * 0.92
        basis = "8% fixed — no clean structural level"
    stop = round(stop, 2)
    risk = p - stop
    target = round(p + 3 * risk, 2)
    return stop, target, basis + " (no ATR available — structural fallback)", risk, "structure"

# ---- portfolio marking ----
def mark_portfolio(pf, prices):
    """Mark holdings to the scan's prices. Broker is the source of truth for
    shares and cost basis; we only revalue."""
    positions = []
    for h in pf.get("positions", []):
        tk = h["symbol"]
        px = prices.get(tk, {}).get("price", h.get("last_price") or h["avg_cost"])
        mv = px * h["shares"]
        cost = h["avg_cost"] * h["shares"]
        positions.append({**h, "price": px, "market_value": round(mv, 2),
                          "cost_basis": round(cost, 2),
                          "unrealized": round(mv - cost, 2),
                          "unrealized_pct": round((mv - cost) / cost * 100, 2) if cost else 0.0,
                          "gics": prices.get(tk, {}).get("gics"),
                          "industry": prices.get(tk, {}).get("industry"),
                          "priced_from_scan": tk in prices})
    invested = sum(p["market_value"] for p in positions)
    cash = pf.get("cash", 0.0)
    equity = invested + cash
    return {"positions": positions, "invested": round(invested, 2), "cash": round(cash, 2),
            "equity": round(equity, 2),
            "deployed_pct": round(invested / equity * 100, 2) if equity else 0.0,
            "cash_pct": round(cash / equity * 100, 2) if equity else 0.0,
            "unrealized": round(sum(p["unrealized"] for p in positions), 2)}

def review_holdings(marked, results):
    """Does each holding still pass the scan that would have bought it?"""
    by_tk = {r["ticker"]: r for r in results}
    out = []
    for p in marked["positions"]:
        r = by_tk.get(p["symbol"])
        if not r:
            out.append({**p, "status": "unscanned", "note":
                        "Not in today's scan universe — no current read on the thesis"})
            continue
        if r["setup"] == "Broken Trend":
            st, note = "exit", f"Setup is now Broken Trend (below the 200-day) — score {r['score']:.0f}"
        elif r["score"] < RULES["exit_score_below"]:
            st, note = "exit", f"Score fell to {r['score']:.0f} — thesis no longer supported"
        elif r["score"] < RULES["trim_score_below"]:
            st, note = "trim", f"Score {r['score']:.0f} — weakening, consider reducing"
        elif r.get("upside_pct") is not None and r["upside_pct"] < 0:
            st, note = "trim", f"Trading above the analyst target ({r['upside_pct']:+.1f}%)"
        else:
            st, note = "hold", f"Thesis intact — score {r['score']:.0f}, {r['setup']}"
        out.append({**p, "status": st, "note": note, "score": r["score"],
                    "setup": r["setup"], "confidence": r.get("confidence")})
    return out

# ---- proposals ----
def build_proposals(results, marked, rules=RULES, daily_pnl_pct=0.0):
    held = {p["symbol"] for p in marked["positions"]}
    open_positions = [{"symbol": p["symbol"], "gics": p.get("gics"), "industry": p.get("industry")}
                      for p in marked["positions"]]
    equity = marked["equity"]
    blocks, proposals = [], []

    if daily_pnl_pct <= -rules["max_daily_loss_pct"]:
        blocks.append(f"HALT: daily loss {daily_pnl_pct:.2f}% breached the "
                      f"{rules['max_daily_loss_pct']}% kill-switch limit — no new entries")
        return [], blocks
    if len(held) >= rules["max_positions"]:
        blocks.append(f"HALT: at max positions ({len(held)}/{rules['max_positions']}) — no new entries")
        return [], blocks

    cumulative_risk = len(held) * rules["max_risk_per_trade_pct"]
    sector_counts = {}
    for p in open_positions:
        sector_counts[p["gics"]] = sector_counts.get(p["gics"], 0) + 1
    deployed = marked["invested"]
    cash_left = marked["cash"]
    slots = rules["max_positions"] - len(held)

    for r in results:
        if r["ticker"] in held:
            continue
        hard, trims = [], []
        if r["score"] < rules["min_score_to_propose"]:
            continue
        if r["verdict"] == "Avoid" or r["setup"] == "Broken Trend":
            continue
        if r.get("confidence") == "conflict":
            hard.append(f"Price sources disagree by {r.get('price_disagreement_pct')}% — "
                        "not tradeable until confirmed")
        g = r.get("gics")
        if sector_counts.get(g, 0) >= rules["max_per_sector"]:
            hard.append(f"Sector limit: already {sector_counts[g]} positions in {g} "
                        f"(max {rules['max_per_sector']})")
        if cumulative_risk + rules["max_risk_per_trade_pct"] > rules["cumulative_risk_cap_pct"]:
            hard.append(f"Cumulative risk cap: book already carries "
                        f"{cumulative_risk:.0f}% of the {rules['cumulative_risk_cap_pct']:.0f}% budget")

        stop, target, basis, risk_per_share, basis_kind = derive_levels(r, rules)
        conv = conviction_from_score(r["score"])
        risk_pct = conviction_risk_pct(conv, rules["max_risk_per_trade_pct"], rules["min_risk_per_trade_pct"])
        frac, dec = rules.get("fractional", False), rules.get("share_decimals", 6)
        shares = calculate_position_size(equity, r["price"], stop, risk_pct, frac, dec,
                                         cash_available=cash_left)
        corr_mult, corr_note = correlation_size_adjustment(g, r.get("industry"), open_positions)
        shares = round(shares * corr_mult, dec) if frac else float(int(shares * corr_mult))

        def _cap(v):
            return round(v, dec) if frac else float(int(v))
        max_val = equity * rules["max_position_pct"] / 100
        if shares * r["price"] > max_val:
            shares = _cap(max_val / r["price"])
            trims.append(f"Trimmed to the {rules['max_position_pct']:.0f}% max-position cap")
        room = equity * rules["max_deployed_pct"] / 100 - deployed
        if shares * r["price"] > room:
            shares = max(0.0, _cap(room / r["price"]))
            trims.append(f"Trimmed to the {rules['max_deployed_pct']:.0f}% max-deployed cap "
                         f"(${room:,.2f} of room left)")
        if shares * r["price"] > cash_left:
            shares = max(0.0, _cap(cash_left / r["price"]))
            trims.append(f"Trimmed to available cash (${cash_left:,.2f})")
        if shares <= 0:
            hard.append("Sized to zero shares under these limits")
        elif shares * r["price"] < rules.get("min_notional", 0):
            hard.append(f"Order would be ${shares * r['price']:.2f} — below the broker's "
                        f"${rules['min_notional']:.2f} minimum")

        notional = round(shares * r["price"], 2)
        proposals.append({
            "ticker": r["ticker"], "name": r["name"], "score": r["score"],
            "setup": r["setup"], "verdict": r["verdict"], "sector": r.get("sector"),
            "gics": g, "industry": r.get("industry"), "confidence": r.get("confidence"),
            "entry": r["price"], "stop": stop, "target": target, "stop_basis": basis,
            "stop_basis_kind": basis_kind, "atr_14": r.get("atr_14"),
            "atr_pct": r.get("atr_pct"), "rsi_14": r.get("rsi_14"),
            "stop_pct": round((r["price"] - stop) / r["price"] * 100, 2) if r["price"] else None,
            "risk_per_share": round(risk_per_share, 2),
            "reward_risk": 3.0,
            "conviction": round(conv, 2), "risk_pct_of_equity": round(risk_pct, 2),
            "shares": shares, "notional": notional,
            "pct_of_equity": round(notional / equity * 100, 2) if equity else 0.0,
            "dollar_risk": round(shares * risk_per_share, 2),
            "correlation_multiplier": round(corr_mult, 2), "correlation_note": corr_note,
            "analyst_target": r.get("analyst_target"), "upside_pct": r.get("upside_pct"),
            "next_earnings": r.get("next_earnings"),
            "blocked": bool(hard),
            "warnings": hard + trims,
            "thesis": r.get("setup_note"),
        })
        if not hard and shares > 0:
            slots -= 1
            cumulative_risk += risk_pct
            sector_counts[g] = sector_counts.get(g, 0) + 1
            deployed += notional
            cash_left = max(0.0, cash_left - notional)
            open_positions.append({"symbol": r["ticker"], "gics": g, "industry": r.get("industry")})
        if slots <= 0:
            blocks.append(f"Stopped proposing at {rules['max_positions']} total positions")
            break
    return proposals, blocks

if __name__ == "__main__":
    R = json.load(open(os.path.join(BASE, "scan_results.json")))
    pf = json.load(open(os.path.join(BASE, "portfolio.json")))
    prices = {r["ticker"]: r for r in R["results"]}
    marked = mark_portfolio(pf, prices)
    reviewed = review_holdings(marked, R["results"])
    props, blocks = build_proposals(R["results"], marked,
                                    daily_pnl_pct=pf.get("daily_pnl_pct", 0.0))
    out = {"account": pf.get("account", {}), "marked": marked, "holdings": reviewed,
           "proposals": props, "blocks": blocks, "rules": RULES,
           "as_of": R["meta"]["scan_date"] + " " + str(R["meta"].get("time", ""))}
    json.dump(out, open(os.path.join(BASE, "portfolio_state.json"), "w"), indent=2)

    print(f"EQUITY ${marked['equity']:,.2f}  |  invested ${marked['invested']:,.2f} "
          f"({marked['deployed_pct']:.1f}%)  |  cash ${marked['cash']:,.2f} ({marked['cash_pct']:.1f}%)")
    print(f"Unrealized ${marked['unrealized']:,.2f}\n")
    if reviewed:
        print(f"{'HOLDING':<8}{'SHRS':>6}{'PRICE':>10}{'VALUE':>12}{'P&L':>12}{'P&L%':>8}  ACTION")
        print("-"*78)
        for h in reviewed:
            print(f"{h['symbol']:<8}{h['shares']:>10,.4f}{h['price']:>10,.2f}{h['market_value']:>12,.2f}"
                  f"{h['unrealized']:>12,.2f}{h['unrealized_pct']:>7.1f}%  {h['status'].upper()}")
    print(f"\n{'PROPOSAL':<8}{'SHARES':>10}{'ENTRY':>10}{'STOP':>9}{'TARGET':>10}{'NOTIONAL':>12}{'RISK$':>9}  STATUS")
    print("-"*84)
    for p in props:
        st = "BLOCKED" if p["blocked"] else "READY"
        print(f"{p['ticker']:<8}{p['shares']:>10,.4f}{p['entry']:>10,.2f}{p['stop']:>9,.2f}"
              f"{p['target']:>10,.2f}{p['notional']:>12,.2f}{p['dollar_risk']:>9,.2f}  {st}")
        for w in p["warnings"]:
            print(f"         - {w}")
    for b in blocks:
        print(f"\n! {b}")
