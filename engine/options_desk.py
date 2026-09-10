"""options_desk.py — the paper options desk (E27, task D-02, 2026-09-10).

PAPER ONLY, BY CONSTRUCTION. This module reads files and returns dicts. It imports nothing
that can reach a broker, it writes no order file, and the book it manages is a paper
ledger of DEFINED-RISK structures. `pm.py --desk options` dispatches here and then applies
the same revision / --check / heartbeat / journal protocol every equity desk gets — the
protocol is not forked, only the decision engine is different. The desk ships INACTIVE in
desks.json and refuses to run without --allow-inactive until it is activated.

MANDATE (plan §6, Appendix H §2/§7)
  * XSP (fallback SPY) PUT CREDIT SPREADS, 30–45 DTE, short strike nearest 20Δ, $5 wide.
  * Premium at risk per structure ≤ 8% of desk equity ($400 on $5k): width×100 − credit×100.
    Total defined risk ≤ 40% of the desk. ≤ 5 concurrent structures, ≤ 2 per sector (an
    index is its own sector), never on an underlying a stock desk holds (SPY / XSP / SPX
    are one exposure and are treated as aliases of each other for that test).
  * Exit at 50% of max profit, or at 21 DTE, whichever comes first; and a DEFENSIVE CLOSE
    the moment spot trades under the short strike. A structure that reaches expiry anyway
    is cash-settled at intrinsic.
  * No new short vol when VIX > VIX3M, or VIX > 30, or SPY GEX < 0. GEX is dealer gamma
    from the chain snapshot (Σ gamma × OI × mult × spot² × 1%, calls +, puts −); when the
    chain carries no gamma / open interest the gate is SKIPPED and the skip is journaled.
    A missing vix.json fails the gate — an unmeasured regime is not a benign one.
  * Portfolio limits: β-weighted delta within ±0.5% of house NAV per 1% SPY move; net
    short vega ≤ 0.5% of desk equity per vol point; |theta| ≤ 0.3% of desk equity per day.
  * Paper fills: net credit = (mid of the spread) − $0.02 per leg; a leg with bid = 0 or
    (ask − bid) / mid > 10% is rejected at entry. Exits are never refused for width — a
    protective close fills at the mid ± $0.02/leg and the width is journaled.
  * Fees (RULES["fees"]): $0.04/contract regulatory pass-through on every leg, open and
    close; $0.35/contract index-option fee on XSP/SPX-style underlyings; $0 commission on
    equity options (SPY). Charged on the way in and on the way out.
  * Mark-to-market every slot from the chain snapshot mids; a leg the chain does not
    carry is marked by Black–Scholes at its last implied vol (source "model") and the
    journal says so.
  * Stress replay weekly: 2024-08-05 (SPX −3% at the open, VIX 23→65) and 2025-04-07/08
    (VIX 60 / 52) as INSTANTANEOUS shocks — spot shift + IV shift/floor — repriced by
    Black–Scholes leg by leg. The numbers are approximations of those sessions and live in
    RULES["stress"]["scenarios"] so they can be argued with.
  * Shadow fills (K-06) are NOT APPLICABLE: the paper fill above IS the conservative
    model (mid less slippage, plus fees, on both sides). The state says so.
  * Broker policy: a credit spread is defined-risk. Under intraday_margin (and legacy_pdt)
    the maintenance requirement IS the max loss (width − credit, × 100 × contracts); under
    cash_settled the full width is reserved. `margin_state()` reports requirement, equity
    and the projected deficit; an entry that would leave the requirement over equity is
    refused. Nothing here sells on a deficit — the total-risk cap is the tighter control.
  * Drawdown ladder (K-02) applies unchanged: rung 1 halves the per-structure risk, rung 2
    stops entries, rung 3 flattens every structure at its mark and cools off. The 3% daily
    kill switch stops entries for the day; exits stay live at every rung.

BLACK–SCHOLES, STDLIB
  bs_price(S, K, T, r, sigma, put=True) — European price; T in years. bs_greeks() returns
  delta, gamma, theta PER DAY and vega PER VOL POINT. implied_vol() is a bisection on
  [1e-4, 5.0] against bs_price; None when the price is outside the no-arbitrage band.
  XSP is European and cash-settled so the model is exact in kind; SPY is American and the
  early-exercise premium on a 20Δ put is ignored (it is small and the desk is paper).

INPUTS A SLOT NEEDS (staged in the run directory by the scheduled task)
  option_chains.json   any shape snapshots.py accepts (Robinhood rows, Cboe payloads, a
                       {SYMBOL: {...}} map) carrying puts for XSP and/or SPY at ≥ 3
                       expiries, with bid, ask, implied_volatility and — for the delta
                       pick and the GEX gate — delta, gamma and open_interest. Delta
                       missing is derived from the row's IV; IV missing is implied from
                       the mid; gamma/OI missing skips the GEX gate.
  vix.json             {"vix": 17.4, "vix3m": 19.2, "as_of": "..."} — the front and
                       3-month Cboe indices from the CSVs in docs/DATA.md. Any casing of
                       the keys; a `rows` list of {date, close} per index is accepted too.
  pm_quotes.json       optional; the SPY spot fallback when the chain carries no spot.
  the stock desks' books (paper_book*.json) — pm.load_peers stages them; held underlyings
                       are excluded and their equity is the house NAV for the delta limit.

FILES: book paper_book_options.json, journal pm_journal_current_options.json (project docs
claude/paper-book-options.json, claude/pm-journal-options.json), $5,000 starting equity.
"""
import argparse, datetime as dt, hashlib, json, math, os, sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import broker_policy
import house as house_mod
import ladder as ladder_mod
import snapshots

KIND = "options"
SENTINEL = "sentinel"
SLOT_ORDER = {"pre-market": 0, "opening-range": 1, "midday": 2, "power-hour": 3,
              "ad-hoc": 4, "sentinel": 5}
MULTIPLIER = 100
INDEX_ALIASES = {"XSP": {"XSP", "SPY", "SPX"}, "SPY": {"SPY", "XSP", "SPX"},
                 "SPX": {"SPX", "XSP", "SPY"}}

RULES = {
    "starting_equity": 5000.0,
    "underlyings": ["XSP", "SPY"],       # preference order; the first with a usable chain
    "multiplier": MULTIPLIER,
    "beta": {"XSP": 1.0, "SPY": 1.0, "SPX": 1.0},
    "dte_min": 30, "dte_max": 45, "dte_target": 40,
    "short_delta": 0.20,                 # |Δ| of the short put
    "delta_tol": 0.10,                   # accept the nearest strike within ±0.10 of it
    "width": {"XSP": 5.0, "SPY": 5.0},   # long strike = short strike − width (the MAXIMUM width)
    # A 20Δ short collects less than 20% of the width as credit — always — so a $5-wide
    # spread risks over $400 per contract at every IV, and the mandate's two numbers ($5
    # wide, ≤ 8% of $5k) cannot both hold. The risk cap is the risk rule; the width is
    # a structure parameter. With `width_fallback` the desk steps the width DOWN by
    # `width_step` (to no less than `width_min`) until one contract fits under the cap,
    # and journals the narrowing. Off, the desk simply does not trade under the cap.
    "width_fallback": True, "width_min": 2.0, "width_step": 1.0,
    "max_risk_per_structure_pct": 8.0,   # of desk equity: (width − credit) × mult × n
    "max_total_risk_pct": 40.0,          # Σ max loss of open structures
    "max_structures": 5,
    "max_per_sector": 2,                 # index underlyings are the "Index" sector
    "max_new_structures_per_run": 1,
    "profit_take_pct": 50.0,             # close when the debit to close ≤ 50% of the credit
    "exit_dte": 21,
    "defensive_close_on_breach": True,   # spot < short strike → close
    "vix_max": 30.0,
    "require_vix_below_vix3m": True,
    "require_gex_positive": True,
    "beta_delta_limit_pct_nav": 0.5,     # |Δ$ per 1% SPY move| ≤ 0.5% of house NAV
    "vega_limit_pct": 0.5,               # net short vega ≤ 0.5% of desk equity per vol point
    "theta_limit_pct": 0.3,              # |net theta| ≤ 0.3% of desk equity per day
    "max_daily_loss_pct": 3.0,           # the kill switch, same number as portfolio.RULES
    "risk_free": 0.04,
    "fill": {"slip_per_leg": 0.02, "max_spread_over_mid": 0.10, "min_bid": 0.01},
    "fees": {
        "regulatory_per_contract": 0.04,         # OCC / ORF / FINRA pass-through, every leg
        "index_per_contract": 0.35,              # Robinhood index-option fee (XSP, SPX ...)
        "equity_commission_per_contract": 0.0,   # Robinhood equity-option commission
        "index_underlyings": ["XSP", "SPX", "NDX", "RUT", "VIX", "DJX"],
    },
    "stress": {
        "every_days": 7,
        "scenarios": [
            {"name": "2024-08-05", "label": "SPX −3% at the open, VIX 23→65 intraday",
             "spot_shift_pct": -3.0, "iv_shift_pts": 42.0, "iv_floor": 0.65},
            {"name": "2025-04-07", "label": "VIX 60 intraday, the Apr-3/4 drawdown applied at once",
             "spot_shift_pct": -5.0, "iv_shift_pts": 30.0, "iv_floor": 0.60},
            {"name": "2025-04-08", "label": "VIX 52 close, SPX −1.6%",
             "spot_shift_pct": -1.6, "iv_shift_pts": 25.0, "iv_floor": 0.52},
        ],
    },
    "ladder": ladder_mod.DEFAULT,
    "equity_curve_max": 400,
    "closed_trades_max": 300,
}


# ================================================================ Black–Scholes (stdlib)
def _norm_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1d2(S, K, T, r, sigma):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def bs_price(S, K, T, r, sigma, put=True):
    """European Black–Scholes price. T in years; sigma decimal (0.18 = 18%).

    At T <= 0 or sigma <= 0 the option is worth its intrinsic value."""
    S, K, T, r, sigma = float(S), float(K), float(T), float(r), float(sigma)
    if S <= 0 or K <= 0:
        raise ValueError("bs_price: S and K must be positive")
    if T <= 0 or sigma <= 0:
        return max(K - S, 0.0) if put else max(S - K, 0.0)
    d1, d2 = _d1d2(S, K, T, r, sigma)
    disc = math.exp(-r * T)
    if put:
        return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)


def bs_greeks(S, K, T, r, sigma, put=True):
    """{price, delta, gamma, theta (per DAY), vega (per VOL POINT)} for one option."""
    S, K, T, r, sigma = float(S), float(K), float(T), float(r), float(sigma)
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        itm = (K > S) if put else (S > K)
        return {"price": bs_price(S, K, max(T, 0.0), r, max(sigma, 0.0), put),
                "delta": (-1.0 if itm else 0.0) if put else (1.0 if itm else 0.0),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2 = _d1d2(S, K, T, r, sigma)
    disc = math.exp(-r * T)
    pdf = _norm_pdf(d1)
    sq = math.sqrt(T)
    gamma = pdf / (S * sigma * sq)
    vega = S * pdf * sq / 100.0
    if put:
        delta = _norm_cdf(d1) - 1.0
        theta_y = -S * pdf * sigma / (2.0 * sq) + r * K * disc * _norm_cdf(-d2)
    else:
        delta = _norm_cdf(d1)
        theta_y = -S * pdf * sigma / (2.0 * sq) - r * K * disc * _norm_cdf(d2)
    return {"price": bs_price(S, K, T, r, sigma, put), "delta": delta, "gamma": gamma,
            "theta": theta_y / 365.0, "vega": vega}


def implied_vol(price, S, K, T, r, put=True, lo=1e-4, hi=5.0, tol=1e-7, max_iter=200):
    """Bisection implied volatility, or None when `price` is outside [intrinsic, bound]
    or T <= 0. Monotone in sigma, so bisection cannot miss."""
    if price is None or T <= 0 or S <= 0 or K <= 0:
        return None
    price = float(price)
    p_lo, p_hi = bs_price(S, K, T, r, lo, put), bs_price(S, K, T, r, hi, put)
    if price < p_lo - 1e-12 or price > p_hi + 1e-12:
        return None
    a, b = lo, hi
    for _ in range(max_iter):
        m = 0.5 * (a + b)
        pm_ = bs_price(S, K, T, r, m, put)
        if abs(pm_ - price) < tol or (b - a) < tol:
            return m
        if pm_ > price:
            b = m
        else:
            a = m
    return 0.5 * (a + b)


# ================================================================ small helpers
def _num(v):
    return snapshots._num(v)


def _date(s):
    if isinstance(s, dt.date):
        return s
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _now(iso=None):
    if iso:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def merge_rules(overrides=None):
    """RULES with a desk's overrides laid over it (one level of dict merge for the blocks)."""
    out = json.loads(json.dumps(RULES))
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = dict(out[k], **v)
        else:
            out[k] = v
    return out


def _full(rules):
    """A complete rules dict: RULES with whatever partial overrides the caller passed."""
    if isinstance(rules, dict) and all(k in rules for k in RULES):
        return rules
    return merge_rules(rules)


def width_for(underlying, rules):
    w = rules.get("width")
    if isinstance(w, dict):
        return float(w.get(underlying) or w.get("default") or 5.0)
    return float(w or 5.0)


def is_index(underlying, rules):
    return str(underlying).upper() in set(rules.get("fees", {}).get("index_underlyings") or [])


def sector_of(underlying, rules):
    return "Index" if is_index(underlying, rules) else "Equity"


def fees_for(underlying, contracts, n_legs, rules):
    """Fees for one side (open OR close) of a structure: every leg, every contract."""
    f = rules.get("fees") or {}
    per = float(f.get("regulatory_per_contract") or 0.0)
    per += (float(f.get("index_per_contract") or 0.0) if is_index(underlying, rules)
            else float(f.get("equity_commission_per_contract") or 0.0))
    return round(per * n_legs * contracts, 2)


def new_book(starting_equity=None, rules=None):
    rules = merge_rules(rules)
    eq = float(starting_equity or rules["starting_equity"])
    return {"mode": "paper", "desk": "options", "kind": KIND, "revision": 0,
            "based_on_revision": 0, "last_run": None, "seeded": dt.date.today().isoformat(),
            "starting_equity": eq, "cash": eq, "realized_pnl": 0.0, "fees_paid": 0.0,
            "structures": [], "closed_trades": [], "equity_curve": [], "hwm": eq,
            "day": {}, "stress": {"last_date": None, "results": None}, "_next_id": 1}


def fingerprint(book):
    """What the book IS, not what it is worth (archive.book_fingerprint's rule, for
    structures): open structures, their size and credit, the halt flag, realised P&L."""
    st = sorted([[s.get("id"), s.get("underlying"), s.get("expiry"), s.get("short_strike"),
                  s.get("long_strike"), s.get("contracts"), round(float(s.get("credit") or 0), 4)]
                 for s in book.get("structures") or [] if s.get("status", "open") == "open"],
                key=lambda x: str(x[0]))
    payload = {"structures": st, "halted": bool((book.get("day") or {}).get("halted")),
               "realized_pnl": round(float(book.get("realized_pnl") or 0.0), 2),
               "closed": len(book.get("closed_trades") or []), "mode": book.get("mode")}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ================================================================ chain helpers
def puts_for(chain_rows, underlying):
    u = str(underlying).upper()
    return [c for c in chain_rows or [] if c.get("type") == "put"
            and str(c.get("symbol") or "").upper() == u]


def _dte(row, today):
    d = _date(row.get("expiry"))
    return (d - today).days if d else None


def _leg_iv(row, spot, dte, rules):
    """The row's IV, else the IV implied from its mid; None when neither is possible."""
    iv = _num(row.get("iv"))
    if iv and iv > 0:
        return iv
    mid = _num(row.get("mid"))
    if mid and spot and dte and dte > 0:
        return implied_vol(mid, spot, row["strike"], dte / 365.0, rules["risk_free"],
                           put=(row.get("type") == "put"))
    return None


def leg_greeks(row, spot, dte, rules, iv=None):
    """Greeks for one leg: the row's own where the chain carries them, Black–Scholes at the
    row's IV for what it does not. {delta, gamma, theta, vega, iv, source}."""
    iv = iv or _leg_iv(row, spot, dte, rules)
    bs = None
    if iv and spot and dte is not None and dte > 0:
        bs = bs_greeks(spot, row["strike"], dte / 365.0, rules["risk_free"], iv,
                       put=(row.get("type") == "put"))
    out = {"iv": iv, "source": "chain"}
    for g in ("delta", "gamma", "theta", "vega"):
        v = _num(row.get(g))
        if v is None and bs is not None:
            v = bs[g]
            out["source"] = "model"
        out[g] = v
    return out


def find_leg(chain_rows, underlying, expiry, strike, typ="put"):
    u = str(underlying).upper()
    for c in chain_rows or []:
        if (str(c.get("symbol") or "").upper() == u and c.get("type") == typ
                and c.get("expiry") == expiry and _num(c.get("strike")) is not None
                and abs(float(c["strike"]) - float(strike)) < 1e-6):
            return c
    return None


def gex_from_chain(chain_rows, spot, mult=MULTIPLIER):
    """Dealer gamma exposure in $ per 1% move: Σ gamma × OI × mult × spot² × 0.01, calls
    positive, puts negative (the usual dealers-long-calls / short-puts convention).
    None when no contract carries both gamma and open interest."""
    if not spot:
        return None
    tot, n = 0.0, 0
    for c in chain_rows or []:
        g, oi = _num(c.get("gamma")), _num(c.get("open_interest"))
        if g is None or oi is None:
            continue
        sign = 1.0 if c.get("type") == "call" else -1.0
        tot += sign * g * oi * mult * spot * spot * 0.01
        n += 1
    return round(tot, 2) if n else None


# ================================================================ selection
def select_structure(chain_rows, spot, rules, today, underlying=None, max_loss_per_contract=None):
    """Pick the put credit spread the mandate describes from one underlying's chain.

    Returns (structure_candidate, reason). The candidate carries the two legs (the chain
    rows), their greeks, dte and width; `reason` is None on success and says why not
    otherwise. Expiry: the one nearest rules["dte_target"] inside [dte_min, dte_max].
    Short strike: the put whose |Δ| is nearest rules["short_delta"] (chain delta, else
    Black–Scholes at the row's IV), within delta_tol; ties go to the LOWER strike. Long
    strike: short − width, which must exist in the chain at that expiry. With
    `max_loss_per_contract` (dollars) and rules["width_fallback"], the width is the WIDEST
    of rules["width"], width − step, ... ≥ width_min whose (width − credit) × mult fits
    the cap; the candidate carries `width_max` and `narrowed` so the journal can say so."""
    rules = _full(rules)
    today = _date(today)
    if not spot or spot <= 0:
        return None, "no spot price for the underlying"
    if underlying is None:
        syms = sorted({str(c.get("symbol")).upper() for c in chain_rows or [] if c.get("symbol")})
        underlying = syms[0] if syms else None
    if not underlying:
        return None, "no underlying in the chain"
    puts = puts_for(chain_rows, underlying)
    if not puts:
        return None, f"{underlying}: no puts in the chain"
    by_exp = {}
    for c in puts:
        d = _dte(c, today)
        if d is None or d < rules["dte_min"] or d > rules["dte_max"]:
            continue
        by_exp.setdefault(c["expiry"], {"dte": d, "rows": []})["rows"].append(c)
    if not by_exp:
        return None, (f"{underlying}: no expiry between {rules['dte_min']} and "
                      f"{rules['dte_max']} DTE in the chain")
    expiry, e = min(by_exp.items(), key=lambda kv: (abs(kv[1]["dte"] - rules["dte_target"]), kv[0]))
    dte = e["dte"]
    target, tol = float(rules["short_delta"]), float(rules["delta_tol"])
    best = None
    for c in e["rows"]:
        if _num(c.get("strike")) is None or float(c["strike"]) >= spot:
            continue                       # a credit spread is sold OUT of the money
        g = leg_greeks(c, spot, dte, rules)
        if g["delta"] is None:
            continue
        gap = abs(abs(g["delta"]) - target)
        if gap <= tol and (best is None or (gap, -float(c["strike"])) < (best[0], -float(best[1]["strike"]))):
            best = (gap, c, g)
    if best is None:
        return None, (f"{underlying} {expiry}: no put within ±{tol:.2f} of {target:.2f}Δ "
                      "(no deltas and no IV to derive them, or the chain is too sparse)")
    _, short_row, short_g = best
    width_max = width_for(underlying, rules)
    widths = [width_max]
    if rules.get("width_fallback") and max_loss_per_contract is not None:
        step, wmin = float(rules.get("width_step") or 1.0), float(rules.get("width_min") or 1.0)
        w = width_max - step
        while w >= wmin - 1e-9:
            widths.append(round(w, 4))
            w -= step
    mult = float(rules.get("multiplier", MULTIPLIER))
    slip = float(rules["fill"]["slip_per_leg"])
    tried = []
    for width in widths:
        long_strike = round(float(short_row["strike"]) - width, 4)
        long_row = find_leg(e["rows"], underlying, expiry, long_strike)
        if long_row is None:
            tried.append(f"${width:g} wide: no {long_strike:g} put in the chain")
            continue
        s_mid, l_mid = _num(short_row.get("mid")), _num(long_row.get("mid"))
        credit = (s_mid - l_mid - 2 * slip) if s_mid is not None and l_mid is not None else None
        if max_loss_per_contract is not None and credit is not None:
            loss = (width - credit) * mult
            if loss > max_loss_per_contract + 1e-9:
                tried.append(f"${width:g} wide: credit ${credit:.2f}, max loss ${loss:,.0f} > ${max_loss_per_contract:,.0f}")
                continue
        long_g = leg_greeks(long_row, spot, dte, rules)
        return {"underlying": underlying, "expiry": expiry, "dte": dte, "spot": spot,
                "short_strike": float(short_row["strike"]), "long_strike": long_strike,
                "width": width, "width_max": width_max, "narrowed": width < width_max,
                "short_row": short_row, "long_row": long_row,
                "short_greeks": short_g, "long_greeks": long_g,
                "sector": sector_of(underlying, rules)}, None
    return None, (f"{underlying} {expiry} {float(short_row['strike']):g} short: no width fits — "
                  + "; ".join(tried))


# ================================================================ gates and limits
def gates(vix, vix3m, gex, rules):
    """(ok, reasons). Every FAILING condition is a reason; a gate that cannot be judged is
    a failure for VIX (unmeasured is not benign) and a journaled SKIP for GEX (the chain
    snapshot does not always carry gamma and open interest)."""
    rules = _full(rules)
    reasons, ok = [], True
    vix, vix3m = _num(vix), _num(vix3m)
    if vix is None:
        ok = False
        reasons.append("VIX unavailable — vix.json not staged or unreadable; no new short vol")
    else:
        if vix > float(rules["vix_max"]):
            ok = False
            reasons.append(f"VIX {vix:.2f} > {float(rules['vix_max']):.0f}")
        if rules.get("require_vix_below_vix3m"):
            if vix3m is None:
                ok = False
                reasons.append("VIX3M unavailable — the term-structure gate cannot be judged")
            elif vix > vix3m:
                ok = False
                reasons.append(f"VIX {vix:.2f} > VIX3M {vix3m:.2f} (backwardation)")
    if rules.get("require_gex_positive"):
        gex = _num(gex)
        if gex is None:
            reasons.append("GEX gate SKIPPED — no gamma / open interest in the chain snapshot")
        elif gex < 0:
            ok = False
            reasons.append(f"SPY GEX {gex:,.0f} < 0 (dealers short gamma)")
    return ok, reasons


def structure_greeks(s, mult=MULTIPLIER):
    """Net per-share greeks of a put credit spread (short leg negative, long leg positive)
    and their dollar forms for the whole structure."""
    sg, lg = s.get("short_greeks") or {}, s.get("long_greeks") or {}
    n = float(s.get("contracts") or 1)
    out = {}
    for g in ("delta", "gamma", "theta", "vega"):
        a, b = sg.get(g), lg.get(g)
        out[g] = (None if a is None or b is None else (-float(a) + float(b)))
        out[g + "_usd"] = (None if out[g] is None else round(out[g] * mult * n, 4))
    spot = float(s.get("spot") or 0.0)
    beta = float((s.get("beta") if s.get("beta") is not None else 1.0))
    out["delta_usd_per_1pct"] = (None if out["delta"] is None or not spot
                                 else round(out["delta"] * mult * n * spot * 0.01 * beta, 4))
    return out


def open_structures(book):
    return [s for s in book.get("structures") or [] if s.get("status", "open") == "open"]


def total_risk(book):
    return round(sum(float(s.get("max_loss") or 0.0) for s in open_structures(book)), 2)


def book_greeks(book, extra=None, mult=MULTIPLIER):
    """Σ dollar greeks over the open structures (plus `extra`, a candidate)."""
    tot = {"delta_usd_per_1pct": 0.0, "vega_usd": 0.0, "theta_usd": 0.0, "gamma_usd": 0.0}
    for s in open_structures(book) + ([extra] if extra else []):
        g = structure_greeks(s, mult)
        for k in tot:
            if g.get(k) is not None:
                tot[k] += float(g[k])
    return {k: round(v, 4) for k, v in tot.items()}


def limits_ok(book, new_structure, rules, equity=None, house_nav=None):
    """(ok, reasons) for adding `new_structure` (contracts, max_loss, greeks, sector set)."""
    rules = _full(rules)
    reasons = []
    eq = float(equity if equity is not None else desk_equity(book))
    nav = float(house_nav if house_nav else eq)
    ml = float(new_structure.get("max_loss") or 0.0)
    cap = eq * float(rules["max_risk_per_structure_pct"]) / 100.0
    if ml > cap + 1e-9:
        reasons.append(f"risk per structure ${ml:,.2f} > {float(rules['max_risk_per_structure_pct']):.0f}% "
                       f"of ${eq:,.2f} (${cap:,.2f})")
    tot_cap = eq * float(rules["max_total_risk_pct"]) / 100.0
    tot = total_risk(book) + ml
    if tot > tot_cap + 1e-9:
        reasons.append(f"total defined risk ${tot:,.2f} > {float(rules['max_total_risk_pct']):.0f}% "
                       f"of ${eq:,.2f} (${tot_cap:,.2f})")
    opens = open_structures(book)
    if len(opens) + 1 > int(rules["max_structures"]):
        reasons.append(f"{len(opens)} structures open already — the cap is {int(rules['max_structures'])}")
    sec = new_structure.get("sector") or "Unclassified"
    same = sum(1 for s in opens if (s.get("sector") or "Unclassified") == sec)
    if same + 1 > int(rules["max_per_sector"]):
        reasons.append(f"{same} structure(s) already in sector {sec} — the cap is {int(rules['max_per_sector'])}")
    g = book_greeks(book, new_structure, rules.get("multiplier", MULTIPLIER))
    d_cap = nav * float(rules["beta_delta_limit_pct_nav"]) / 100.0
    if abs(g["delta_usd_per_1pct"]) > d_cap + 1e-9:
        reasons.append(f"β-weighted delta ${g['delta_usd_per_1pct']:,.2f} per 1% SPY move is outside "
                       f"±{float(rules['beta_delta_limit_pct_nav']):.1f}% of the ${nav:,.2f} house NAV (±${d_cap:,.2f})")
    v_cap = eq * float(rules["vega_limit_pct"]) / 100.0
    if -g["vega_usd"] > v_cap + 1e-9:
        reasons.append(f"net short vega ${-g['vega_usd']:,.2f} per vol point > "
                       f"{float(rules['vega_limit_pct']):.1f}% of ${eq:,.2f} (${v_cap:,.2f})")
    t_cap = eq * float(rules["theta_limit_pct"]) / 100.0
    if abs(g["theta_usd"]) > t_cap + 1e-9:
        reasons.append(f"net theta ${g['theta_usd']:,.2f}/day is over {float(rules['theta_limit_pct']):.1f}% "
                       f"of ${eq:,.2f} (${t_cap:,.2f}) — too much short gamma")
    return (not reasons), reasons


# ================================================================ paper fills
def paper_fill(legs, side, rules, strict=True):
    """The paper fill for a multi-leg order.

    legs: [{"row": chain contract, "side": "sell"|"buy"}, ...]; `side` is "open" or
    "close". Net = Σ(sell mids) − Σ(buy mids); slippage is $0.02 per leg AGAINST the desk
    (a credit shrinks, a debit grows). With `strict` (entries) a leg with bid < min_bid or
    (ask − bid) / mid > max_spread_over_mid rejects the fill; a close is never refused —
    the width is reported and the fill goes through (protection does not sit unfilled).
    Returns {ok, net, credit (bool), legs: [...], reasons}."""
    f = _full(rules)["fill"]
    slip, max_w, min_bid = float(f["slip_per_leg"]), float(f["max_spread_over_mid"]), float(f["min_bid"])
    reasons, out_legs, net = [], [], 0.0
    for leg in legs:
        row = leg["row"]
        bid, ask, mid = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("mid"))
        if mid is None and bid is not None and ask is not None and ask >= bid > 0:
            mid = (bid + ask) / 2.0
        label = f"{row.get('symbol')} {row.get('expiry')} {float(row.get('strike') or 0):g}{'P' if row.get('type') == 'put' else 'C'}"
        if mid is None or mid <= 0:
            reasons.append(f"{label}: no two-sided quote")
            out_legs.append({"label": label, "side": leg["side"], "bid": bid, "ask": ask, "mid": None})
            continue
        wide = None
        if bid is None or bid < min_bid:
            wide = f"{label}: bid {bid if bid is not None else 'none'} — no bid"
        elif ask is not None and (ask - bid) / mid > max_w:
            wide = f"{label}: spread {(ask - bid) / mid * 100:.1f}% of mid > {max_w * 100:.0f}%"
        if wide:
            reasons.append(wide)
        sign = 1.0 if leg["side"] == "sell" else -1.0
        net += sign * mid
        out_legs.append({"label": label, "side": leg["side"], "strike": row.get("strike"),
                         "expiry": row.get("expiry"), "type": row.get("type"),
                         "bid": bid, "ask": ask, "mid": round(mid, 4)})
    missing = any(l.get("mid") is None for l in out_legs)
    if missing:
        return {"ok": False, "net": None, "credit": None, "legs": out_legs, "reasons": reasons}
    n = len(legs)
    # $0.02 per leg against the desk: a credit (net > 0) shrinks, a debit (net < 0) deepens
    net_fill = net - slip * n
    ok = not (strict and reasons)
    return {"ok": ok, "net": round(net_fill, 4), "credit": net_fill > 0, "gross": round(net, 4),
            "legs": out_legs, "reasons": reasons}


# ================================================================ marking
def desk_equity(book):
    """cash − Σ liability (the debit to close every open structure at its last mark)."""
    liab = sum(float(s.get("value") or 0.0) * float(s.get("contracts") or 0) *
               float(s.get("multiplier") or MULTIPLIER) for s in open_structures(book))
    return round(float(book.get("cash") or 0.0) - liab, 2)


def _mark_one(s, chain_rows, spot, today, rules):
    """Mark one structure. Chain mids where the legs are quoted, Black–Scholes at the
    leg's last IV otherwise. Returns the source label."""
    today = _date(today)
    dte = (_date(s["expiry"]) - today).days
    s["dte"] = dte
    spot = spot or s.get("spot")
    if spot:
        s["spot"] = spot
    src = "chain"
    legs = {}
    for name, strike in (("short", s["short_strike"]), ("long", s["long_strike"])):
        row = find_leg(chain_rows, s["underlying"], s["expiry"], strike)
        g = s.get(f"{name}_greeks") or {}
        if row is not None and _num(row.get("mid")) is not None:
            mid = float(row["mid"])
            g = leg_greeks(row, spot, dte, rules, iv=None)
            if g.get("iv") is None:
                g["iv"] = (s.get(f"{name}_greeks") or {}).get("iv")
            legs[name] = {"mid": mid, "bid": _num(row.get("bid")), "ask": _num(row.get("ask")),
                          "row": row}
        else:
            iv = g.get("iv")
            if spot and iv and dte > 0:
                bs = bs_greeks(spot, strike, dte / 365.0, rules["risk_free"], iv, put=True)
                g = dict(g, delta=bs["delta"], gamma=bs["gamma"], theta=bs["theta"],
                         vega=bs["vega"], source="model")
                legs[name] = {"mid": bs["price"], "bid": None, "ask": None, "row": None}
            elif dte <= 0 and spot:
                legs[name] = {"mid": max(float(strike) - float(spot), 0.0), "bid": None,
                              "ask": None, "row": None}
                g = dict(g, source="intrinsic")
            else:
                legs[name] = {"mid": None, "row": None}
                g = dict(g, source="unpriced")
            src = g["source"]
        s[f"{name}_greeks"] = g
        s[f"{name}_mid"] = legs[name].get("mid")
    if legs["short"].get("mid") is not None and legs["long"].get("mid") is not None:
        s["value"] = round(max(legs["short"]["mid"] - legs["long"]["mid"], 0.0), 4)
        s["value_source"] = src
    else:
        s["value_source"] = "unpriced (carried)"
        src = "unpriced"
    n, mult = float(s["contracts"]), float(s.get("multiplier") or MULTIPLIER)
    s["unrealized"] = round((float(s["credit"]) - float(s.get("value") or 0.0)) * mult * n
                            - float(s.get("fees") or 0.0), 2)
    s["greeks"] = structure_greeks(s, mult)
    s["_legs"] = legs
    return src


def mark(book, chain_rows, spot, today=None, rules=None):
    """Mark every open structure. Returns {equity, cash, liability, unpriced, sources}."""
    rules = _full(rules)
    today = _date(today) or dt.date.today()
    unpriced, sources = [], {}
    for s in open_structures(book):
        sp = spot.get(s["underlying"]) if isinstance(spot, dict) else spot
        src = _mark_one(s, chain_rows, sp, today, rules)
        sources[s["id"]] = src
        if src == "unpriced":
            unpriced.append(s["id"])
    eq = desk_equity(book)
    liab = round(float(book.get("cash") or 0.0) - eq, 2)
    return {"equity": eq, "cash": round(float(book.get("cash") or 0.0), 2),
            "liability": liab, "unpriced": unpriced, "sources": sources}


# ================================================================ open / close
def _apply_open(book, cand, fill, contracts, today, slot, rules, jrn):
    mult = float(rules.get("multiplier", MULTIPLIER))
    credit = float(fill["net"])
    fees = fees_for(cand["underlying"], contracts, 2, rules)
    max_loss = round((cand["width"] - credit) * mult * contracts, 2)
    sid = f"{cand['underlying']}-{cand['expiry']}-{cand['short_strike']:g}/{cand['long_strike']:g}-{book.get('_next_id', 1)}"
    book["_next_id"] = int(book.get("_next_id", 1)) + 1
    s = {"id": sid, "underlying": cand["underlying"], "expiry": cand["expiry"],
         "short_strike": cand["short_strike"], "long_strike": cand["long_strike"],
         "width": cand["width"], "contracts": contracts, "multiplier": mult,
         "credit": round(credit, 4), "credit_gross_mid": fill.get("gross"),
         "max_loss": max_loss, "max_profit": round(credit * mult * contracts, 2),
         "opened": today.isoformat(), "opened_slot": slot, "dte_at_open": cand["dte"],
         "spot_at_open": cand["spot"], "spot": cand["spot"], "dte": cand["dte"],
         "sector": cand["sector"], "beta": float((rules.get("beta") or {}).get(cand["underlying"], 1.0)),
         "greeks_at_open": {"short": cand["short_greeks"], "long": cand["long_greeks"]},
         "short_greeks": cand["short_greeks"], "long_greeks": cand["long_greeks"],
         "short_mid": (cand["short_row"] or {}).get("mid"), "long_mid": (cand["long_row"] or {}).get("mid"),
         "value": round(credit, 4), "value_source": "fill",
         "fees": fees, "fees_open": fees, "status": "open", "closed": None, "closed_slot": None,
         "exit_reason": None, "pnl": None, "unrealized": round(-fees, 2)}
    s["greeks"] = structure_greeks(s, mult)
    book["cash"] = round(float(book["cash"]) + credit * mult * contracts - fees, 2)
    book["fees_paid"] = round(float(book.get("fees_paid") or 0.0) + fees, 2)
    book.setdefault("structures", []).append(s)
    jrn["decisions"].append({
        "action": "open-spread", "symbol": s["underlying"], "shares": float(contracts),
        "price": s["credit"], "structure_id": sid,
        "reason": f"put credit spread {s['short_strike']:g}/{s['long_strike']:g} {s['expiry']} "
                  f"({s['dte']} DTE), short |Δ| {abs(cand['short_greeks'].get('delta') or 0):.2f}",
        "detail": (f"credit ${s['credit']:.2f} (mid ${fill.get('gross') or 0:.2f} − $0.02/leg), max loss "
                   f"${max_loss:,.2f}, max profit ${s['max_profit']:,.2f}, fees ${fees:.2f}; "
                   f"legs: {'; '.join(l['label'] + ' ' + l['side'] + ' @' + str(l['mid']) for l in fill['legs'])}"),
        "legs": fill["legs"], "fees": fees, "max_loss": max_loss})
    return s


def _apply_close(book, s, debit, today, slot, reason, detail, rules, jrn, fill=None):
    mult = float(s.get("multiplier") or MULTIPLIER)
    n = float(s["contracts"])
    fees = fees_for(s["underlying"], int(n), 2, rules)
    book["cash"] = round(float(book["cash"]) - debit * mult * n - fees, 2)
    book["fees_paid"] = round(float(book.get("fees_paid") or 0.0) + fees, 2)
    s["fees"] = round(float(s.get("fees") or 0.0) + fees, 2)
    s["fees_close"] = fees
    s["status"] = "closed"
    s["closed"] = today.isoformat()
    s["closed_slot"] = slot
    s["exit_reason"] = reason
    s["debit"] = round(debit, 4)
    s["value"] = round(debit, 4)
    s["pnl"] = round((float(s["credit"]) - debit) * mult * n - float(s["fees"]), 2)
    s["unrealized"] = 0.0
    s.pop("_legs", None)
    book["realized_pnl"] = round(float(book.get("realized_pnl") or 0.0) + s["pnl"], 2)
    book["structures"] = [x for x in book["structures"] if x is not s]
    book.setdefault("closed_trades", []).append(s)
    jrn["decisions"].append({
        "action": "close-spread", "symbol": s["underlying"], "shares": n, "price": s["debit"],
        "structure_id": s["id"], "reason": reason,
        "detail": detail + f"; debit ${debit:.2f} vs credit ${float(s['credit']):.2f}, "
                           f"P&L ${s['pnl']:+,.2f} after ${s['fees']:.2f} fees",
        "legs": (fill or {}).get("legs"), "fees": fees, "pnl": s["pnl"]})
    return s


def close_debit(s, chain_rows, rules):
    """(debit, fill, note) — the paper debit to close: chain legs through paper_fill
    (never refused), else the marked value plus the per-leg slippage."""
    legs = (s.get("_legs") or {})
    short_row, long_row = (legs.get("short") or {}).get("row"), (legs.get("long") or {}).get("row")
    if short_row is None or long_row is None:
        short_row = find_leg(chain_rows, s["underlying"], s["expiry"], s["short_strike"])
        long_row = find_leg(chain_rows, s["underlying"], s["expiry"], s["long_strike"])
    if short_row is not None and long_row is not None:
        f = paper_fill([{"row": short_row, "side": "buy"}, {"row": long_row, "side": "sell"}],
                       "close", rules, strict=False)
        if f.get("net") is not None:
            note = ("; ".join(f["reasons"]) + " — closed anyway, protection does not wait"
                    if f["reasons"] else None)
            return round(max(-f["net"], 0.0), 4), f, note
    slip = float(_full(rules)["fill"]["slip_per_leg"])
    v = float(s.get("value") or 0.0)
    return round(v + 2 * slip, 4), None, f"no chain legs — closed at the {s.get('value_source')} mark + $0.02/leg"


def manage(book, chain_rows, spot, today, rules, slot="ad-hoc", jrn=None, force_reason=None):
    """The exit pass. Order per structure: expiry settlement, short-strike breach
    (defensive close), 50% of max profit, 21 DTE. Returns the list of exits taken."""
    rules = _full(rules)
    today = _date(today)
    jrn = jrn if jrn is not None else {"decisions": [], "skipped": [], "warnings": []}
    exits = []
    for s in list(open_structures(book)):
        sp = spot.get(s["underlying"]) if isinstance(spot, dict) else spot
        sp = sp or s.get("spot")
        dte = (_date(s["expiry"]) - today).days
        reason = detail = None
        if force_reason:
            reason, detail = "flatten", force_reason
        elif dte <= 0:
            reason, detail = "expired", f"expiry {s['expiry']} reached — cash-settled at intrinsic"
        elif rules.get("defensive_close_on_breach") and sp and sp < float(s["short_strike"]):
            reason, detail = "defensive-close", (f"spot {sp:,.2f} under the {float(s['short_strike']):g} "
                                                 "short strike — the structure is in the money")
        elif s.get("value") is not None and float(s["value"]) <= float(s["credit"]) * (1 - float(rules["profit_take_pct"]) / 100.0):
            reason, detail = "profit-take", (f"debit to close ${float(s['value']):.2f} ≤ "
                                             f"{100 - float(rules['profit_take_pct']):.0f}% of the "
                                             f"${float(s['credit']):.2f} credit — {float(rules['profit_take_pct']):.0f}% of max profit")
        elif dte <= int(rules["exit_dte"]):
            reason, detail = "dte-exit", f"{dte} DTE ≤ the {int(rules['exit_dte'])}-DTE exit"
        if reason is None:
            continue
        if reason == "expired":
            debit = round(max(float(s["short_strike"]) - float(sp or 0), 0.0)
                          - max(float(s["long_strike"]) - float(sp or 0), 0.0), 4) if sp else float(s.get("value") or 0.0)
            debit = max(min(debit, float(s["width"])), 0.0)
            fill, note = None, None
        else:
            debit, fill, note = close_debit(s, chain_rows, rules)
        if note:
            jrn["warnings"].append(f"{s['id']}: {note}")
        _apply_close(book, s, debit, today, slot, reason, detail, rules, jrn, fill)
        exits.append({"id": s["id"], "reason": reason, "debit": debit, "pnl": s["pnl"]})
    return exits


# ================================================================ stress
def stress(book, scenarios=None, rules=None, today=None):
    """Instantaneous shocks repriced by Black–Scholes leg by leg.

    Each scenario: {name, spot_shift_pct, iv_shift_pts, iv_floor}. Per leg
    S' = S × (1 + shift), σ' = max(σ + shift_pts/100, iv_floor), T unchanged. The loss is
    (value' − value) × mult × contracts summed over the open structures. Returns
    {"scenarios": [{name, label, pnl, pct_equity, structures: [...]}], "equity": eq}."""
    rules = _full(rules)
    scenarios = scenarios if scenarios is not None else (rules.get("stress") or {}).get("scenarios") or []
    today = _date(today) or dt.date.today()
    eq = desk_equity(book)
    out = []
    for sc in scenarios:
        pnl, rows = 0.0, []
        for s in open_structures(book):
            spot = float(s.get("spot") or 0.0)
            dte = (_date(s["expiry"]) - today).days
            if not spot or dte < 0:
                rows.append({"id": s["id"], "skipped": "no spot or expired"})
                continue
            T = max(dte, 0) / 365.0
            sp2 = spot * (1.0 + float(sc.get("spot_shift_pct") or 0.0) / 100.0)
            vals = {}
            for name, strike in (("short", s["short_strike"]), ("long", s["long_strike"])):
                iv = (s.get(f"{name}_greeks") or {}).get("iv") or 0.2
                iv2 = max(iv + float(sc.get("iv_shift_pts") or 0.0) / 100.0, float(sc.get("iv_floor") or 0.0))
                vals[name] = bs_price(sp2, strike, T, rules["risk_free"], min(iv2, 5.0), put=True)
            v2 = max(vals["short"] - vals["long"], 0.0)
            v1 = float(s.get("value") or 0.0)
            mult, n = float(s.get("multiplier") or MULTIPLIER), float(s["contracts"])
            d = round((v1 - v2) * mult * n, 2)
            pnl += d
            rows.append({"id": s["id"], "value": round(v1, 4), "value_shocked": round(v2, 4),
                         "pnl": d, "spot_shocked": round(sp2, 2), "max_loss": s.get("max_loss")})
        out.append({"name": sc.get("name"), "label": sc.get("label"), "pnl": round(pnl, 2),
                    "pct_equity": round(pnl / eq * 100.0, 2) if eq else None,
                    "spot_shift_pct": sc.get("spot_shift_pct"), "iv_shift_pts": sc.get("iv_shift_pts"),
                    "iv_floor": sc.get("iv_floor"), "structures": rows})
    return {"as_of": today.isoformat(), "equity": eq, "scenarios": out,
            "worst": (min(out, key=lambda x: x["pnl"])["name"] if out else None)}


# ================================================================ inputs
def load_vix(run_dir):
    """{vix, vix3m, as_of, source} from vix.json, or None."""
    for name in ("vix.json", "vix_term.json"):
        p = os.path.join(run_dir, name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        return parse_vix(d, source=name)
    return None


def parse_vix(d, source=None):
    if not isinstance(d, dict):
        return None
    low = {str(k).lower().replace(" ", "").replace("_", ""): v for k, v in d.items()}
    vix, vix3m = _num(low.get("vix")), _num(low.get("vix3m"))
    as_of = low.get("asof") or low.get("date")
    for key, target in (("vix", "vix"), ("vix3m", "vix3m")):
        v = low.get(key)
        if isinstance(v, dict):           # {"vix": {"close": 17.4, "date": ...}}
            val = _num(v.get("close") or v.get("last") or v.get("value"))
            if key == "vix":
                vix = val
            else:
                vix3m = val
            as_of = as_of or v.get("date")
        elif isinstance(v, list) and v:   # rows [{date, close}] — the last row
            last = v[-1] if isinstance(v[-1], dict) else {}
            val = _num(last.get("close") or last.get("value"))
            if key == "vix":
                vix = val
            else:
                vix3m = val
            as_of = as_of or last.get("date")
    if vix is None and vix3m is None:
        return None
    return {"vix": vix, "vix3m": vix3m, "as_of": as_of, "source": source}


def held_underlyings(peers):
    """Symbols the stock desks hold or have working buys on, expanded through the index
    aliases so a SPY holding blocks XSP and SPX too."""
    held = set()
    for bk in ((peers or {}).get("books") or {}).values():
        if not isinstance(bk, dict) or bk.get("kind") == KIND:
            continue
        for p in bk.get("positions") or []:
            if p.get("symbol"):
                held.add(str(p["symbol"]).upper())
        for o in bk.get("working_orders") or []:
            if o.get("side") == "buy" and o.get("status") == "working" and o.get("symbol"):
                held.add(str(o["symbol"]).upper())
    out = set(held)
    for s in held:
        out |= INDEX_ALIASES.get(s, set())
    return out


def house_nav(book, peers):
    """This desk's equity plus every peer stock desk's (cash + last-marked positions)."""
    nav = desk_equity(book)
    for bk in ((peers or {}).get("books") or {}).values():
        if not isinstance(bk, dict):
            continue
        if bk.get("kind") == KIND:
            nav += desk_equity(bk)
            continue
        inv = sum(float(p.get("last_price") or p.get("avg_cost") or 0.0) * float(p.get("shares") or 0.0)
                  for p in bk.get("positions") or [])
        nav += float(bk.get("cash") or 0.0) + inv
    return round(nav, 2)


def margin_state(book, equity, policy_name):
    """The broker policy's view of a defined-risk book. A credit spread's maintenance
    requirement is its max loss (width − credit) under a margin regime; a cash account
    reserves the full width. Reported; the total-risk cap is what actually binds."""
    opens = open_structures(book)
    if policy_name == "cash_settled":
        req = sum(float(s["width"]) * float(s.get("multiplier") or MULTIPLIER) * float(s["contracts"]) for s in opens)
        basis = "full width reserved (cash account)"
    else:
        req = sum(float(s.get("max_loss") or 0.0) for s in opens)
        basis = "max loss = (width − credit) × 100 × contracts (defined risk)"
    return {"broker_policy": policy_name, "maintenance_requirement": round(req, 2),
            "requirement_basis": basis, "projected_deficit": round(max(req - equity, 0.0), 2),
            "day_trades_used": 0, "day_trade_limit": None, "pdt_applies": False}


# ================================================================ the slot
def run_slot(run_dir, book, slot, now_iso=None, rules=None, peers=None, mode="paper",
             prices=None, desk_name="options", policy_name=None):
    """One slot of the options desk. Reads option_chains.json (any snapshots.py shape),
    vix.json and the peer books passed in; mutates and returns `book`; returns
    (book, journal_entry, state). pm.py applies the write protocol to the result."""
    rules = merge_rules(rules)
    now = _now(now_iso)
    today = now.date()
    sentinel = (slot == SENTINEL)
    run_key = (f"{today.isoformat()}#{slot}-{now.strftime('%H%M')}" if sentinel
               else f"{today.isoformat()}#{slot}")
    pol_name = broker_policy.resolve_name(policy_name, book, {"rules": rules})
    if pol_name not in broker_policy.VALID:
        raise ValueError(f"unknown broker policy {pol_name!r}; valid names: {', '.join(broker_policy.VALID)}")
    jrn = {"ts": now.isoformat().replace("+00:00", "Z"), "date": today.isoformat(),
           "slot": slot, "run_key": run_key, "mode": mode, "desk": desk_name, "kind": KIND,
           "broker_policy": pol_name, "sentinel": sentinel,
           "decisions": [], "skipped": [], "warnings": [], "daily_pnl_pct": 0.0,
           "stop_policy": "options-mandate", "live_would_refuse": [],
           "shadow": {"applicable": False,
                      "note": "shadow fills n/a — the paper fill (mid − $0.02/leg + fees) IS the conservative model"},
           "inputs": {}, "gates": None, "limits": None, "exits": [], "stress": None}

    # ---- book defaults and the day roll
    for k, v in (("structures", []), ("closed_trades", []), ("equity_curve", []),
                 ("realized_pnl", 0.0), ("fees_paid", 0.0), ("kind", KIND)):
        book.setdefault(k, v)
    book.setdefault("starting_equity", rules["starting_equity"])
    book.setdefault("cash", book["starting_equity"])
    if (book.get("day") or {}).get("date") != today.isoformat():
        book["day"] = {"date": today.isoformat(), "open_equity": None, "halted": False,
                       "halt_reason": None}

    # ---- inputs
    chains = snapshots.load_chains(run_dir)
    chain_files = snapshots.chain_files(run_dir)
    rows = []
    spots = {}
    for sym, ch in chains.items():
        rows.extend(ch.get("contracts") or [])
        if ch.get("spot"):
            spots[sym] = float(ch["spot"])
    for sym in rules["underlyings"]:
        if sym not in spots and isinstance((prices or {}).get(sym), dict) and (prices[sym].get("price") or 0) > 0:
            spots[sym] = float(prices[sym]["price"])
    vix = load_vix(run_dir)
    jrn["inputs"] = {"chain_files": chain_files, "chain_symbols": sorted(chains),
                     "n_contracts": len(rows), "spots": spots,
                     "vix": vix, "peers": sorted((peers or {}).get("books") or {})}
    if not chain_files:
        jrn["warnings"].append("No option chain staged (option_chains.json) — structures are "
                               "marked by model at their last IV and no entry is possible.")
    if vix is None:
        jrn["warnings"].append("vix.json not staged — the regime gates cannot be judged; "
                               "no new short vol this run.")

    # ---- mark, kill switch, ladder
    marked = mark(book, rows, spots, today, rules)
    if marked["unpriced"]:
        jrn["warnings"].append("Unpriced structures this run (carried at the last mark): "
                               + ", ".join(marked["unpriced"]))
    day = book["day"]
    if day.get("open_equity") is None:
        day["open_equity"] = marked["equity"]
    open_eq = day["open_equity"] or marked["equity"]
    day_pnl = ((marked["equity"] - open_eq) / open_eq * 100.0) if open_eq else 0.0
    jrn["daily_pnl_pct"] = round(day_pnl, 2)
    if not day.get("halted") and day_pnl <= -float(rules["max_daily_loss_pct"]):
        day["halted"] = True
        day["halt_reason"] = (f"Daily loss {day_pnl:.2f}% breached the "
                              f"{float(rules['max_daily_loss_pct']):.0f}% kill switch")
        jrn["warnings"].append("KILL SWITCH TRIPPED — " + day["halt_reason"] +
                               ". No new structures for the rest of the session; exits stay live.")
    lad = ladder_mod.state_for(book, marked["equity"], today, rules.get("ladder"), day_pnl)
    book["hwm"] = round(max(float(book.get("hwm") or 0.0), lad["hwm"]), 2)
    if lad["soft_daily_hit"] and not day.get("ladder_soft_hit"):
        day["ladder_soft_hit"] = True
        jrn["warnings"].append("SOFT DAILY LEVEL — " + lad["reason"] + ".")
    if lad["regained"]:
        book["cool_until"] = None
        book["ladder_halt"] = None
    if lad["halt"]:
        if not day.get("halted"):
            day["halted"] = True
            day["halt_reason"] = "Drawdown ladder halt — " + lad["reason"]
        jrn["warnings"].append("DRAWDOWN LADDER HALT — " + lad["reason"] +
                               ". Every structure is closed at its mark; the desk cools off.")
        manage(book, rows, spots, today, rules, slot, jrn, force_reason="Ladder halt: " + lad["reason"])
        after = mark(book, rows, spots, today, rules)
        rec = ladder_mod.halt_record(lad, today, after["equity"], rules.get("ladder"))
        book["ladder_halt"], book["cool_until"] = rec, rec["cool_until"]
        lad["cool_until"], lad["reentry_base"] = rec["cool_until"], rec["equity_after"]

    # ---- exits
    jrn["exits"] = manage(book, rows, spots, today, rules, slot, jrn)
    marked = mark(book, rows, spots, today, rules)

    # ---- entries (decision slots only)
    gate_ok, gate_reasons, gex = None, [], None
    if not sentinel:
        gex_sym = next((s for s in ("SPY", "XSP", "SPX") if s in chains), None)
        if gex_sym:
            gex = gex_from_chain(chains[gex_sym].get("contracts") or [], spots.get(gex_sym))
        gate_ok, gate_reasons = gates((vix or {}).get("vix"), (vix or {}).get("vix3m"), gex, rules)
        jrn["gates"] = {"ok": gate_ok, "reasons": gate_reasons, "vix": (vix or {}).get("vix"),
                        "vix3m": (vix or {}).get("vix3m"), "gex": gex, "gex_symbol": gex_sym}
        blocked = None
        if day.get("halted"):
            blocked = day.get("halt_reason")
        elif lad["entries_blocked"]:
            blocked = lad["reason"]
        elif not gate_ok:
            blocked = "regime gate: " + "; ".join(r for r in gate_reasons if "SKIPPED" not in r)
        if blocked:
            jrn["skipped"].append({"symbol": "*", "reason": blocked})
        else:
            _entry_pass(book, chains, spots, today, slot, rules, peers, marked, lad, pol_name, jrn)
    jrn["ladder"] = dict(lad, hwm=book["hwm"])

    # ---- weekly stress
    if not sentinel:
        last = _date((book.get("stress") or {}).get("last_date"))
        due = last is None or (today - last).days >= int(rules["stress"].get("every_days", 7))
        if due:
            res = stress(book, None, rules, today)
            book["stress"] = {"last_date": today.isoformat(), "results": res}
            jrn["stress"] = res
            worst = min(res["scenarios"], key=lambda x: x["pnl"]) if res["scenarios"] else None
            if worst and res["equity"] and worst["pnl"] < -0.5 * float(rules["max_total_risk_pct"]) / 100.0 * res["equity"]:
                jrn["warnings"].append(f"STRESS: {worst['name']} ({worst['label']}) would cost "
                                       f"${-worst['pnl']:,.2f} ({-worst['pct_equity']:.1f}% of the desk)")

    # ---- close the run
    marked = mark(book, rows, spots, today, rules)
    open_eq = day.get("open_equity") or marked["equity"]
    jrn["daily_pnl_pct"] = round(((marked["equity"] - open_eq) / open_eq * 100.0) if open_eq else 0.0, 2)
    book["hwm"] = round(max(float(book.get("hwm") or 0.0), marked["equity"]), 2)
    for s in book["structures"]:
        s["last_priced"] = jrn["ts"]
        s.pop("_legs", None)
    if not sentinel:
        book["equity_curve"] = [c for c in book["equity_curve"]
                                if not (c.get("date") == jrn["date"] and c.get("slot") == slot)]
        book["equity_curve"].append({"ts": jrn["ts"], "date": jrn["date"], "slot": slot,
                                     "equity": marked["equity"], "cash": marked["cash"],
                                     "invested": marked["liability"],
                                     "realized": round(book["realized_pnl"], 2)})
        book["equity_curve"].sort(key=lambda c: (c.get("date", ""), SLOT_ORDER.get(c.get("slot"), 9), c.get("ts", "")))
    book["equity_curve"] = book["equity_curve"][-int(rules["equity_curve_max"]):]
    book["closed_trades"] = book["closed_trades"][-int(rules["closed_trades_max"]):]
    book["based_on_revision"] = book.get("revision", 0)
    book["revision"] = book.get("revision", 0) + 1
    book["last_run"] = jrn["ts"]
    book["mode"] = mode
    book["cash"] = round(float(book["cash"]), 2)

    import archive, config
    jrn["run_id"] = archive.pm_run_id(jrn["date"], slot, jrn["ts"]) + f"-{desk_name}"
    jrn["book_revision"] = book["revision"]
    jrn["book_fingerprint"] = fingerprint(book)
    jrn["engine_sha"] = config.engine_sha()
    jrn["house"] = None
    greeks = book_greeks(book, None, rules["multiplier"])
    mstate = margin_state(book, marked["equity"], pol_name)
    jrn.update({"equity": marked["equity"], "cash": marked["cash"], "invested": marked["liability"],
                "realized_pnl": round(book["realized_pnl"], 2), "fees_paid": round(book["fees_paid"], 2),
                "halted": bool(day.get("halted")), "day_trades_used": 0,
                "scan_stale": None, "positions": len(open_structures(book)),
                "structures": len(open_structures(book)), "working_orders": 0,
                "total_risk": total_risk(book), "greeks": greeks, "margin": mstate})

    basis = float(book.get("starting_equity") or 0.0)
    state = {
        "generated": jrn["ts"], "mode": mode, "slot": slot, "date": jrn["date"],
        "engine_sha": jrn["engine_sha"], "desk": desk_name, "kind": KIND, "sentinel": sentinel,
        "account": book.get("mirrors", {}),
        "book": {"equity": marked["equity"], "cash": marked["cash"], "invested": marked["liability"],
                 "deployed_pct": round(total_risk(book) / marked["equity"] * 100.0, 2) if marked["equity"] else 0.0,
                 "total_risk": total_risk(book), "realized_pnl": round(book["realized_pnl"], 2),
                 "fees_paid": round(book["fees_paid"], 2),
                 "starting_equity": basis, "seed_equity": basis, "deposits_total": 0.0,
                 "total_return_pct": round((marked["equity"] / basis - 1) * 100.0, 2) if basis else 0.0,
                 "open_equity": day.get("open_equity"), "daily_pnl_pct": jrn["daily_pnl_pct"],
                 "halted": bool(day.get("halted")), "halt_reason": day.get("halt_reason"),
                 "hwm": book["hwm"], "cool_until": book.get("cool_until"), "ladder": jrn["ladder"],
                 "greeks": greeks, "structures": len(open_structures(book)),
                 **mstate},
        "structures": [dict(s) for s in book["structures"]],
        "positions": [], "working_orders": [],
        "closed_trades": book["closed_trades"][-40:], "equity_curve": book["equity_curve"],
        "journal": jrn, "rules": rules, "pm_rules": {},
        "broker_policy": {"name": pol_name,
                          "describe": broker_policy.POLICIES[pol_name]().describe()
                          + " — applied to a defined-risk book: maintenance = max loss per structure"},
        "house": None, "gates": jrn["gates"], "stress": jrn["stress"],
        "shadow": jrn["shadow"],
        "scan_as_of": None, "scan_stale": None, "orders_to_place": [],
    }
    return book, jrn, state


def _entry_pass(book, chains, spots, today, slot, rules, peers, marked, lad, pol_name, jrn):
    held = held_underlyings(peers)
    nav = house_nav(book, peers)
    placed = 0
    for sym in rules["underlyings"]:
        if placed >= int(rules["max_new_structures_per_run"]):
            break
        if sym in held:
            jrn["skipped"].append({"symbol": sym, "reason": "a stock desk holds this underlying "
                                                             "(or an alias of it) — no structure on it"})
            continue
        ch = chains.get(sym)
        if not ch:
            jrn["skipped"].append({"symbol": sym, "reason": "no chain staged for this underlying"})
            continue
        eq = marked["equity"]
        risk_cap = eq * float(rules["max_risk_per_structure_pct"]) / 100.0 * float(lad.get("entry_size_mult") or 1.0)
        cand, why = select_structure(ch.get("contracts") or [], spots.get(sym), rules, today, sym,
                                     max_loss_per_contract=risk_cap)
        if cand is None:
            jrn["skipped"].append({"symbol": sym, "reason": why})
            continue
        if cand.get("narrowed"):
            jrn["warnings"].append(
                f"{sym}: the ${cand['width_max']:g}-wide spread would risk more than the "
                f"${risk_cap:,.0f} per-structure cap at a {float(rules['short_delta']):.2f}Δ short; "
                f"narrowed to ${cand['width']:g} wide ({cand['short_strike']:g}/{cand['long_strike']:g}).")
        fill = paper_fill([{"row": cand["short_row"], "side": "sell"},
                           {"row": cand["long_row"], "side": "buy"}], "open", rules, strict=True)
        if not fill["ok"]:
            jrn["skipped"].append({"symbol": sym, "reason": "paper fill refused: " + "; ".join(fill["reasons"])})
            continue
        credit = float(fill["net"])
        if credit <= 0:
            jrn["skipped"].append({"symbol": sym, "reason": f"net credit ${credit:.2f} after slippage — not a credit"})
            continue
        mult = float(rules["multiplier"])
        loss_per = (cand["width"] - credit) * mult
        n = int(risk_cap // loss_per) if loss_per > 0 else 0
        if n < 1:
            jrn["skipped"].append({"symbol": sym, "reason":
                                   f"max loss ${loss_per:,.2f} per contract (${cand['width']:g} wide, "
                                   f"${credit:.2f} credit) exceeds the ${risk_cap:,.2f} per-structure risk cap"})
            continue
        chosen, reasons = None, []
        while n >= 1:
            trial = dict(cand, contracts=n, max_loss=round(loss_per * n, 2),
                         beta=float((rules.get("beta") or {}).get(sym, 1.0)))
            ok, reasons = limits_ok(book, trial, rules, equity=eq, house_nav=nav)
            if ok:
                req = margin_state(book, eq, pol_name)["maintenance_requirement"] + trial["max_loss"]
                if req > eq:
                    reasons = [f"{pol_name}: maintenance requirement ${req:,.2f} would exceed equity ${eq:,.2f}"]
                    ok = False
            if ok:
                chosen = trial
                break
            n -= 1
        jrn["limits"] = {"ok": chosen is not None, "reasons": reasons, "house_nav": nav,
                         "held_underlyings": sorted(held)}
        if chosen is None:
            jrn["skipped"].append({"symbol": sym, "reason": "limits: " + "; ".join(reasons)})
            continue
        _apply_open(book, chosen, fill, chosen["contracts"], today, slot, rules, jrn)
        placed += 1
    return placed


# ================================================================ CLI (offline helpers)
def main(argv=None):
    ap = argparse.ArgumentParser(description="options desk offline helpers (paper only)")
    ap.add_argument("--init-book", default=None, help="write a fresh $5,000 options book to PATH")
    ap.add_argument("--stress", default=None, help="run the stress scenarios over BOOK and print them")
    ap.add_argument("--iv", nargs=5, metavar=("PRICE", "S", "K", "DTE", "R"), default=None,
                    help="implied vol of a put from a price")
    a = ap.parse_args(argv)
    if a.init_book:
        if os.path.exists(a.init_book):
            print(f"refusing to overwrite {a.init_book}", file=sys.stderr)
            return 2
        with open(a.init_book, "w", encoding="utf-8") as f:
            json.dump(new_book(), f, indent=2)
        print(f"wrote {a.init_book}")
        return 0
    if a.stress:
        with open(a.stress, encoding="utf-8") as f:
            book = json.load(f)
        print(json.dumps(stress(book), indent=2))
        return 0
    if a.iv:
        price, S, K, dte, r = map(float, a.iv)
        print(implied_vol(price, S, K, dte / 365.0, r, put=True))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
