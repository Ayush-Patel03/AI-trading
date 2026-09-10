"""guardrails.py — the live-trading guardrails, as DOCTRINE and pure validators (K-07).

This module does not trade and is never on a live path, because there is no live path:
docs/PM.md §1 says live mode is not implemented and this module does not change that.
What it does is write down, in code that can be tested, the controls that would have to
stand between the manager and a real order before anyone flips the flag — so the day that
conversation happens it is a review of a tested module and not a design session.

The reference frame is Knight Capital, 1 August 2012: a deployment error put a retired
test routine live and it sent ~4 million orders in 45 minutes, $460m lost, the firm gone
in a week. The regulatory response every broker-dealer already lives under is SEC Rule
15c3-5 (the Market Access Rule: pre-trade credit and capital thresholds, erroneous-order
checks, and a "regulatory risk management control" the firm itself must own) and FINRA
Regulatory Notice 15-09 (guidance on algorithmic trading: kill switches, testing, change
management, and the point that the controls belong to the FIRM, not the vendor). The
Agentic account is a retail account, not a broker-dealer, so none of this binds it — but
the reasoning is exactly right for a book that an LLM session drives, and the numbers
below are set to THIS book's size, not a firm's.

  two_key    live needs BOTH an environment flag AND a signed, dated config. Either alone
             does nothing. The config is an HMAC-SHA256 over its canonical body under a
             key that lives only in the environment; it carries `expires_at` and an
             `issued_at` no older than max_age_hours. Expired, unsigned, tampered, or
             missing → not allowed, and the reason says which.
  per_order  hard ceilings per ticket: notional, distance from the last print, quote age,
             spread. A single fat-fingered or stale order cannot exceed them.
  per_day    per desk and per symbol order counts, and total notional as a multiple of
             equity — the 15c3-5 credit threshold, scaled to a $5,000 book.
  deny       names the book must not trade live whatever the model says: sub-$5 stocks,
             thin ADV, leveraged ETFs, recent IPOs, a volume spike over 10× ADV, a 30%+
             move with no earnings event to explain it.
  circuit    tape-wide: VIX over 35 or SPY down 3% intraday and nothing new is placed.

Every validator returns (ok, reasons). `reasons` is a list of strings that say what would
be refused and why; an empty list means the check passed. A check whose input is MISSING
does not pass silently: with ctx["strict"] True (the live default) it is a refusal, and
without it the reason is reported under `unknown` in the result's meta, so the paper-mode
audit can say "would need X to decide" rather than pretending it decided.

pm.py uses exactly two of these on the paper path — check_order and check_symbol — to
journal, on every proposal, what the live guardrails WOULD have refused
(jrn["live_would_refuse"]). That changes no paper behaviour; it is the audit trail that
says how often the guardrails would bite, before they ever bite.

Pure and stdlib only: hmac, hashlib, json, datetime. Nothing here reads the clock — the
caller passes `now` — and the only file read is the signed config, on request.
"""
import datetime as dt
import hashlib
import hmac
import json
import os

LIVE_GUARDRAILS = {
    "two_key": {"env_flag": "AI_TRADING_LIVE", "signed_config": "live.signed.json",
                "max_age_hours": 24, "key_env": "AI_TRADING_LIVE_KEY"},
    "per_order": {"max_notional_usd": 750, "max_pct_from_last": 1.0, "max_quote_age_s": 60,
                  "max_spread_pct": 1.0},
    "per_day": {"max_orders_per_desk": 12, "max_orders_per_symbol": 2,
                "max_notional_multiple_of_equity": 2.0},
    "deny": {"min_price": 5.0, "min_adv_usd": 10e6, "leveraged_etf": True, "ipo_days": 90,
             "volume_spike_x_adv": 10, "move_pct_no_earnings": 30},
    "circuit": {"vix_max": 35, "spy_intraday_drop_pct": -3.0},
}

LEVERAGED_MARKERS = ("2x", "3x", "-2x", "-3x", "ultra", "ultrapro", "direxion", "proshares",
                     "leveraged", "bull 2", "bull 3", "bear 2", "bear 3", "daily 2", "daily 3")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rules(ctx, block):
    r = ((ctx or {}).get("rules") or LIVE_GUARDRAILS).get(block) or {}
    return dict(LIVE_GUARDRAILS[block], **r)


def _dt(v):
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ per order
def check_order(order, ctx=None):
    """One ticket against the per-order ceilings.

    order: {symbol, shares, limit_price|price, notional?}
    ctx:   {last: float, quote_age_s: float, spread_pct: float, strict: bool, rules: {...}}
    """
    ctx = ctx or {}
    r = _rules(ctx, "per_order")
    strict = bool(ctx.get("strict", True))
    reasons, unknown = [], []
    sym = (order or {}).get("symbol") or "?"
    px = (order or {}).get("limit_price")
    if not _num(px):
        px = (order or {}).get("price")
    shares = (order or {}).get("shares")
    notional = (order or {}).get("notional")
    if not _num(notional) and _num(px) and _num(shares):
        notional = px * shares
    if not _num(notional):
        reasons.append(f"{sym}: order carries no usable price or share count")
        return False, reasons
    if notional > r["max_notional_usd"]:
        reasons.append(f"{sym}: notional ${notional:,.2f} over the ${r['max_notional_usd']:,.0f} "
                       "per-order ceiling")
    last = ctx.get("last")
    if _num(last) and last > 0 and _num(px):
        dist = abs(px - last) / last * 100.0
        if dist > r["max_pct_from_last"]:
            reasons.append(f"{sym}: limit {px:,.2f} is {dist:.2f}% from the last print "
                           f"{last:,.2f}, over the {r['max_pct_from_last']:.1f}% limit")
    else:
        unknown.append("last print")
    age = ctx.get("quote_age_s")
    if _num(age):
        if age > r["max_quote_age_s"]:
            reasons.append(f"{sym}: quote is {age:.0f}s old, over the {r['max_quote_age_s']:.0f}s "
                           "limit — not a live price")
    else:
        unknown.append("quote age")
    sp = ctx.get("spread_pct")
    if _num(sp):
        if sp > r["max_spread_pct"]:
            reasons.append(f"{sym}: spread {sp:.2f}% of price, over the {r['max_spread_pct']:.1f}% "
                           "limit")
    else:
        unknown.append("spread")
    if unknown and strict:
        reasons.append(f"{sym}: cannot verify {', '.join(unknown)} — a live order needs every "
                       "check to pass, not merely none to fail")
    return not reasons, reasons


# ------------------------------------------------------------------ per day
def check_day(book, ctx=None):
    """The day's order flow for one desk against the per-day ceilings.

    ctx: {orders: [{symbol, notional}], equity: float, desk: str, rules: {...}}
    `orders` is every order placed today on this desk (working, filled, cancelled — an
    order counts when it is SENT, which is what 15c3-5 counts). When ctx carries no
    `orders`, today's working orders and closed trades on the book are counted instead.
    """
    ctx = ctx or {}
    r = _rules(ctx, "per_day")
    reasons = []
    desk = ctx.get("desk") or "this desk"
    orders = ctx.get("orders")
    if orders is None:
        today = str(((book or {}).get("day") or {}).get("date") or "")
        orders = []
        for o in (book or {}).get("working_orders", []):
            if str(o.get("placed") or "")[:10] == today:
                orders.append({"symbol": o.get("symbol"), "notional": o.get("notional")
                               or (o.get("limit_price", 0.0) * o.get("shares", 0.0))})
        for c in (book or {}).get("closed_trades", []):
            if str(c.get("closed") or "")[:10] == today:
                orders.append({"symbol": c.get("symbol"),
                               "notional": (c.get("exit") or 0.0) * (c.get("shares") or 0.0)})
    n = len(orders)
    if n > r["max_orders_per_desk"]:
        reasons.append(f"{desk}: {n} orders today, over the {r['max_orders_per_desk']} per-desk "
                       "daily limit")
    by_sym = {}
    for o in orders:
        by_sym[o.get("symbol")] = by_sym.get(o.get("symbol"), 0) + 1
    for s, k in sorted(by_sym.items(), key=lambda kv: str(kv[0])):
        if k > r["max_orders_per_symbol"]:
            reasons.append(f"{desk}: {k} orders in {s} today, over the "
                           f"{r['max_orders_per_symbol']} per-symbol daily limit")
    equity = ctx.get("equity")
    if not _num(equity):
        eq = (book or {}).get("cash", 0.0)
        for p in (book or {}).get("positions", []):
            eq += (p.get("last_price") or p.get("avg_cost") or 0.0) * p.get("shares", 0.0)
        equity = eq
    total = sum(float(o.get("notional") or 0.0) for o in orders)
    if _num(equity) and equity > 0 and total > r["max_notional_multiple_of_equity"] * equity:
        reasons.append(f"{desk}: ${total:,.2f} sent today, over "
                       f"{r['max_notional_multiple_of_equity']:.1f}x the ${equity:,.2f} equity")
    return not reasons, reasons


# ------------------------------------------------------------------ deny list
def is_leveraged_etf(row):
    if not isinstance(row, dict):
        return False
    if row.get("leveraged") is True or _num(row.get("leverage")) and abs(row["leverage"]) > 1:
        return True
    name = str(row.get("name") or "").lower()
    return any(m in name for m in LEVERAGED_MARKERS)


def check_symbol(row, ctx=None):
    """A scan row (or order meta) against the deny list.

    Reads: price, adv_usd | avg_volume_20d×price | volume×price, name/leveraged, ipo_date |
    days_listed, volume vs ADV, change_pct | price_move_pct with earnings_today |
    has_earnings. A field that is absent is `unknown`: refused under strict, reported
    otherwise. Price is never optional.
    """
    ctx = ctx or {}
    r = _rules(ctx, "deny")
    strict = bool(ctx.get("strict", True))
    reasons, unknown = [], []
    row = row or {}
    sym = row.get("ticker") or row.get("symbol") or "?"
    px = row.get("price")
    if not (_num(px) and px > 0):
        return False, [f"{sym}: no price on the row"]
    if px < r["min_price"]:
        reasons.append(f"{sym}: ${px:,.2f} is under the ${r['min_price']:.2f} minimum price")
    adv = row.get("adv_usd")
    if not _num(adv):
        for k in ("avg_volume_20d", "volume"):
            v = row.get(k)
            if _num(v) and v > 0:
                adv = v * px
                break
    if _num(adv):
        if adv < r["min_adv_usd"]:
            reasons.append(f"{sym}: ${adv / 1e6:,.1f}m dollar ADV, under the "
                           f"${r['min_adv_usd'] / 1e6:,.0f}m minimum")
    else:
        unknown.append("dollar ADV")
    if r.get("leveraged_etf") and is_leveraged_etf(row):
        reasons.append(f"{sym}: leveraged ETF — never traded live")
    days = row.get("days_listed")
    ipo = row.get("ipo_date")
    now = _dt(ctx.get("now")) if ctx.get("now") else None
    if not _num(days) and ipo and now:
        d = _dt(ipo)
        if d:
            days = (now - d).days
    if _num(days):
        if days < r["ipo_days"]:
            reasons.append(f"{sym}: listed {days:.0f} days, under the {r['ipo_days']}-day "
                           "IPO seasoning")
    elif ipo is None and days is None and not row.get("seasoned"):
        unknown.append("listing age")
    vol = row.get("volume")
    avg = row.get("avg_volume_20d")
    if _num(vol) and _num(avg) and avg > 0:
        if vol > r["volume_spike_x_adv"] * avg:
            reasons.append(f"{sym}: volume {vol / avg:.1f}x the 20-day average, over the "
                           f"{r['volume_spike_x_adv']}x spike limit — something is happening "
                           "that the model does not know about")
    move = row.get("change_pct")
    if not _num(move):
        move = row.get("price_move_pct")
    if _num(move):
        earn = row.get("earnings_today") or row.get("has_earnings")
        if abs(move) > r["move_pct_no_earnings"] and not earn:
            reasons.append(f"{sym}: moved {move:+.1f}% with no earnings event — over the "
                           f"{r['move_pct_no_earnings']}% unexplained-move limit")
    if unknown and strict:
        reasons.append(f"{sym}: cannot verify {', '.join(unknown)} — refused until known")
    return not reasons, reasons


# ------------------------------------------------------------------ circuit
def check_circuit(ctx=None):
    """Tape-wide: {vix, spy_intraday_pct}. Missing inputs are reported, and under strict
    they refuse — a circuit breaker that cannot read the tape is open, not closed."""
    ctx = ctx or {}
    r = _rules(ctx, "circuit")
    strict = bool(ctx.get("strict", True))
    reasons, unknown = [], []
    vix = ctx.get("vix")
    if _num(vix):
        if vix >= r["vix_max"]:
            reasons.append(f"VIX {vix:.1f} at or over the {r['vix_max']:.0f} circuit level")
    else:
        unknown.append("VIX")
    spy = ctx.get("spy_intraday_pct")
    if _num(spy):
        if spy <= r["spy_intraday_drop_pct"]:
            reasons.append(f"SPY {spy:+.2f}% intraday, through the "
                           f"{r['spy_intraday_drop_pct']:+.1f}% circuit level")
    else:
        unknown.append("SPY intraday move")
    if unknown and strict:
        reasons.append(f"cannot read {', '.join(unknown)} — the circuit is open until it can")
    return not reasons, reasons


# ------------------------------------------------------------------ two-key
def canonical(body):
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sign(body, key):
    """HMAC-SHA256 (hex) over the canonical body under `key` (str or bytes)."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, canonical(body).encode("utf-8"), hashlib.sha256).hexdigest()


def signed_config(body, key):
    """A config document {body, sig} ready to write to live.signed.json."""
    return {"body": body, "sig": sign(body, key)}


def live_mode_allowed(env, signed_config_path, now=None, rules=None):
    """(allowed, reason). Both keys must turn:

      1. env[env_flag] is one of 1/true/yes/on;
      2. the signed config at `signed_config_path` parses as {body, sig}, the HMAC over
         the canonical body under env[key_env] matches (constant-time compare), body
         .expires_at is after `now`, and body.issued_at (if present) is within
         max_age_hours of `now`.

    The reason names the FIRST thing that failed, so the operator fixes one thing at a
    time rather than guessing.
    """
    r = dict(LIVE_GUARDRAILS["two_key"], **((rules or {}).get("two_key") or {}))
    env = env or {}
    now = _dt(now) if now is not None else dt.datetime.now(dt.timezone.utc)
    flag = str(env.get(r["env_flag"], "")).strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False, f"{r['env_flag']} is not set — key 1 of 2 missing"
    key = env.get(r["key_env"])
    if not key:
        return False, f"{r['key_env']} is not set — no key to verify the signed config"
    path = signed_config_path or r["signed_config"]
    if not os.path.exists(path):
        return False, f"signed config {path} not found — key 2 of 2 missing"
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as exc:
        return False, f"signed config {path} unreadable: {exc}"
    body, sig = (doc or {}).get("body"), (doc or {}).get("sig")
    if not isinstance(body, dict) or not isinstance(sig, str):
        return False, "signed config is not {body, sig}"
    if not hmac.compare_digest(sign(body, key), sig):
        return False, "signed config signature does not verify — tampered or wrong key"
    exp = _dt(body.get("expires_at"))
    if exp is None:
        return False, "signed config carries no parseable expires_at"
    if now >= exp:
        return False, f"signed config expired at {body.get('expires_at')}"
    issued = _dt(body.get("issued_at")) if body.get("issued_at") else None
    if issued is not None:
        age_h = (now - issued).total_seconds() / 3600.0
        if age_h > r["max_age_hours"]:
            return False, (f"signed config issued {age_h:.1f}h ago, over the "
                           f"{r['max_age_hours']}h maximum — re-sign it")
    if body.get("mode") not in (None, "live"):
        return False, f"signed config mode is {body.get('mode')!r}, not 'live'"
    return True, (f"both keys present: {r['env_flag']} set and the signed config verifies "
                  f"(expires {body.get('expires_at')})")
