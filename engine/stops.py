"""stops.py — stop and exit policies, selectable per desk (K-07).

One stop rule for every desk was the original design, and it was the wrong design the
moment the desks stopped being the same trade. A 1.5×ATR stop clamped to 3–12% is a swing
stop: tight enough to matter on a multi-day hold, wide enough to sit outside a day's
noise. It is the wrong instrument for a momentum book (Han, Zhou & Zhu 2016: a stop cuts
momentum's worst month from ≈−50% to ≈−11%, and a WIDE trailing stop with a smaller size
dominates a tight fixed one) and it is actively harmful to a mean-reversion book, where
the whole edge is holding through the drawdown the stop would sell into (Kaminski & Lo
2014: stops add value under momentum and regime switching and subtract it on a random
walk or a mean-reverting series). docs/PM.md § "Stops by desk type" carries the argument;
this module carries the mechanics.

Three policies, chosen by desks.json `rules.stop_policy` with parameters in
`rules.stop_params`:

  fixed_atr          today's behaviour, unchanged: initial stop at N×ATR clamped to the
                     3–12% band, a 3R target, half off at the target and the stop to
                     breakeven. It DELEGATES to portfolio.derive_levels() and pm.exit_pass
                     — nothing is reimplemented, and the default for every existing desk.
  chandelier         momentum / swing-trend. Initial stop entry − k_init×ATR14 (2.5);
                     trailing stop highest close since entry − k_trail×ATR14 (3.0), which
                     RATCHETS UP and never down; no target by default (rank and trend
                     exits do that job); a time stop at max_sessions (40) closes a position
                     that has not made 1R by then. (Chande & Kroll's chandelier exit.)
  time_catastrophe   mean-reversion / rotation. No tight stop at all — the trade IS the
                     drawdown. A catastrophe stop at k_cat×ATR14 (3.5) only, a time stop at
                     max_sessions (6 for mean reversion, ~25 for a monthly rotation) that is
                     unconditional, and an optional profit target at target_pct (4.0%).

Every policy exposes
    initial(entry, atr, ctx)                     -> {stop, target, meta}
    update(pos, price, sessions_held, ctx)       -> {stop, target, exit_reason, meta}
with exit_reason in {"stop", "target", "time", "trail"} or None. pm.py maps them onto its
existing journal words ("trail" is a stop that had been ratcheted; "time" is new). A stop
returned by update() is never below the position's current stop — the ratchet is enforced
HERE, so no policy can lower a stop by accident.

Every position carries `stop_policy`, `initial_risk` (entry − initial stop, per share),
`initial_risk_usd`, `sessions_held`, `highest_close` and `trail_level`. The book observes
four prices a day, not closes; `highest_close` is the highest price the book has SEEN
since entry, and the docstring says so rather than pretending otherwise.

Pure: nothing here reads a file or the clock. Stdlib only.
"""
import datetime as dt

VALID = ("fixed_atr", "chandelier", "time_catastrophe")
DEFAULT_POLICY = "fixed_atr"

DEFAULTS = {
    "fixed_atr": {},
    "chandelier": {"k_init": 2.5, "k_trail": 3.0, "max_sessions": 40, "target_r": None,
                   "time_stop_below_r": 1.0},
    "time_catastrophe": {"k_cat": 3.5, "max_sessions": 6, "target_pct": 4.0},
}

EXIT_REASONS = ("stop", "target", "time", "trail")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pos(v):
    return _num(v) and v > 0


def _date(s):
    if isinstance(s, dt.date):
        return s
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def sessions_between(opened, today):
    """Weekday sessions strictly after `opened` up to and including `today`. The entry
    day itself is session 0; the next trading day is 1. Weekends only — holidays are not
    modelled here, exactly as broker_policy counts settlement."""
    o, t = _date(opened), _date(today)
    if o is None or t is None or t <= o:
        return 0
    n, d = 0, o
    while d < t:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _structural_fallback(entry, ctx):
    """No usable ATR: the stop portfolio.derive_levels would place (structure, else 8%).
    Imported lazily so this module stays importable on its own."""
    row = dict((ctx or {}).get("row") or {})
    row.setdefault("price", entry)
    row.setdefault("setup", row.get("setup") or "")
    try:
        import portfolio
        stop, target, basis, risk, kind = portfolio.derive_levels(row, (ctx or {}).get("rules"))
        return float(stop), basis, kind
    except Exception:                          # noqa: BLE001 — a stop must always exist
        stop = round(entry * 0.92, 2)
        return stop, "8% fixed — no ATR available (fallback)", "structure"


# ------------------------------------------------------------------ policies
class FixedATR:
    """Today's rule, by delegation. initial() is portfolio.derive_levels(); update() moves
    nothing — pm.exit_pass owns the stop, the 3R target, the scale-out and the breakeven
    move under this policy, exactly as it did before this module existed."""
    name = "fixed_atr"

    def __init__(self, params=None):
        self.params = dict(DEFAULTS["fixed_atr"], **(params or {}))

    def initial(self, entry, atr, ctx=None):
        ctx = ctx or {}
        row = dict(ctx.get("row") or {})
        row.setdefault("price", entry)
        row.setdefault("setup", row.get("setup") or "")
        if atr is not None and "atr_14" not in row:
            row["atr_14"] = atr
        import portfolio
        stop, target, basis, risk, kind = portfolio.derive_levels(row, ctx.get("rules"))
        return {"stop": stop, "target": target,
                "meta": {"stop_basis": basis, "stop_basis_kind": kind, "risk": risk}}

    def update(self, pos, price, sessions_held, ctx=None):
        return {"stop": pos.get("stop"), "target": pos.get("target"), "exit_reason": None,
                "meta": {"policy": self.name, "trail_level": None}}


class Chandelier:
    name = "chandelier"

    def __init__(self, params=None):
        self.params = dict(DEFAULTS["chandelier"], **(params or {}))

    def initial(self, entry, atr, ctx=None):
        p = self.params
        if _pos(atr) and _pos(entry):
            stop = round(entry - p["k_init"] * atr, 2)
            if stop <= 0:
                stop = round(entry * 0.5, 2)
            basis = (f"chandelier: {p['k_init']:g}x ATR ({atr:.2f}) below ${entry:,.2f} — "
                     f"{(entry - stop) / entry * 100:.1f}% of price; trails "
                     f"{p['k_trail']:g}x ATR under the highest close, up only")
            kind = "chandelier"
        else:
            stop, basis, kind = _structural_fallback(entry, ctx)
            basis += " (chandelier: no ATR — structural initial stop, no trail)"
        risk = round(entry - stop, 6)
        target = None
        if _pos(p.get("target_r")) and risk > 0:
            target = round(entry + p["target_r"] * risk, 2)
        return {"stop": stop, "target": target,
                "meta": {"stop_basis": basis, "stop_basis_kind": kind, "risk": risk}}

    def update(self, pos, price, sessions_held, ctx=None):
        p = self.params
        ctx = ctx or {}
        cur = pos.get("stop")
        atr = ctx.get("atr") if _pos(ctx.get("atr")) else pos.get("atr_14")
        hi = pos.get("highest_close")
        if not _pos(hi):
            hi = pos.get("high_water") or price
        hi = max(hi, price)
        trail = round(hi - p["k_trail"] * atr, 2) if _pos(atr) else None
        new_stop = cur
        raised = False
        if trail is not None and (cur is None or trail > cur):
            new_stop, raised = trail, True
        entry = pos.get("avg_cost") or 0.0
        risk = pos.get("initial_risk")
        if not _pos(risk):
            risk = (entry - (pos.get("stop") or entry)) if entry else None
        # The stop in force is a TRAIL once it has ever been ratcheted above the initial
        # level — a hit on it is a "trail" exit; a hit on the untouched initial stop is a
        # plain "stop". The distinction is for the journal: one is a trend that ended, the
        # other a trade that never worked.
        is_trail = raised or pos.get("stop_basis_kind") == "trail"
        reason = None
        target = pos.get("target")
        if new_stop is not None and price <= new_stop:
            reason = "trail" if is_trail else "stop"
        elif _pos(target) and price >= target:
            reason = "target"
        elif (_pos(p.get("max_sessions")) and sessions_held >= p["max_sessions"]
              and _pos(risk) and (price - entry) < p.get("time_stop_below_r", 1.0) * risk):
            reason = "time"
        meta = {"policy": self.name, "trail_level": trail, "highest_close": hi,
                # a raise that coincides with the exit is not journaled as a raise
                "raised": raised and reason is None,
                "k_trail": p["k_trail"], "atr": atr}
        if raised:
            meta["stop_basis"] = (f"chandelier trail: {p['k_trail']:g}x ATR ({atr:.2f}) under the "
                                  f"${hi:,.2f} high since entry — ratchets up only")
            meta["stop_basis_kind"] = "trail"
        return {"stop": new_stop, "target": target, "exit_reason": reason, "meta": meta}


class TimeCatastrophe:
    name = "time_catastrophe"

    def __init__(self, params=None):
        self.params = dict(DEFAULTS["time_catastrophe"], **(params or {}))

    def initial(self, entry, atr, ctx=None):
        p = self.params
        if _pos(atr) and _pos(entry):
            stop = round(entry - p["k_cat"] * atr, 2)
            if stop <= 0:
                stop = round(entry * 0.5, 2)
            basis = (f"catastrophe only: {p['k_cat']:g}x ATR ({atr:.2f}) below ${entry:,.2f} — "
                     f"{(entry - stop) / entry * 100:.1f}% of price; the trade is the "
                     f"drawdown, the time stop at {p['max_sessions']} sessions is the exit")
            kind = "catastrophe"
        else:
            stop, basis, kind = _structural_fallback(entry, ctx)
            basis += " (time_catastrophe: no ATR — structural catastrophe stop)"
        target = (round(entry * (1 + p["target_pct"] / 100.0), 2)
                  if _pos(p.get("target_pct")) else None)
        return {"stop": stop, "target": target,
                "meta": {"stop_basis": basis, "stop_basis_kind": kind,
                         "risk": round(entry - stop, 6)}}

    def update(self, pos, price, sessions_held, ctx=None):
        p = self.params
        cur, target = pos.get("stop"), pos.get("target")
        hi = pos.get("highest_close")
        if not _pos(hi):
            hi = pos.get("high_water") or price
        hi = max(hi, price)
        reason = None
        if _pos(cur) and price <= cur:
            reason = "stop"
        elif _pos(target) and price >= target:
            reason = "target"
        elif _pos(p.get("max_sessions")) and sessions_held >= p["max_sessions"]:
            reason = "time"
        return {"stop": cur, "target": target, "exit_reason": reason,
                "meta": {"policy": self.name, "trail_level": None, "highest_close": hi,
                         "raised": False}}


POLICIES = {"fixed_atr": FixedATR, "chandelier": Chandelier, "time_catastrophe": TimeCatastrophe}


def get_policy(name=None, params=None):
    """The policy object for a name. Unknown names raise, naming the valid ones."""
    name = name or DEFAULT_POLICY
    cls = POLICIES.get(str(name))
    if cls is None:
        raise ValueError(f"unknown stop policy {name!r}; valid names: {', '.join(VALID)}")
    return cls(params)


def resolve(desk_cfg):
    """(name, params) from a desk's config — `rules.stop_policy` / `rules.stop_params`,
    else the default. desk_cfg may be the whole desk entry or just its rules block."""
    cfg = desk_cfg or {}
    rules = cfg.get("rules") if isinstance(cfg.get("rules"), dict) else cfg
    name = rules.get("stop_policy") or DEFAULT_POLICY
    params = rules.get("stop_params") if isinstance(rules.get("stop_params"), dict) else {}
    return name, dict(params)


def apply_update(policy, pos, price, sessions_held, ctx=None):
    """Run policy.update() and enforce the invariant every caller relies on: the returned
    stop is never below the position's current stop. Returns the update dict with
    `raised` (bool) and `stop_from` (the previous stop) added to its meta."""
    res = policy.update(pos, price, sessions_held, ctx)
    cur = pos.get("stop")
    new = res.get("stop")
    if _num(cur) and (not _num(new) or new < cur):
        res["stop"] = cur
    res.setdefault("meta", {})
    res["meta"]["stop_from"] = cur
    res["meta"]["raised"] = bool(_num(res["stop"]) and (not _num(cur) or res["stop"] > cur))
    # A raise that coincides with the exit is applied (the sale is booked against the
    # level that fired) but not journaled as a raise — one decision, not two.
    res["meta"]["journal_raise"] = res["meta"]["raised"] and res.get("exit_reason") is None
    if res.get("exit_reason") not in (None,) + EXIT_REASONS:
        res["exit_reason"] = None
    return res
