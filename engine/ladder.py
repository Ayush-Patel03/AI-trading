"""ladder.py — the drawdown ladder: graded de-risking under the kill switch (K-02).

The book had two controls against a losing streak and both were cliffs: the 3% daily
kill switch (pm.kill_switch) and nothing at all against a slow bleed across sessions.
A desk could give back 7% over two weeks without any rule ever noticing, then lose 3% on
the eighth day and halt for the afternoon. The ladder is the practitioner pattern —
halve at −5%, shut at −7.5% — set to this book's numbers, and it sits UNDER the kill
switch: it refuses earlier and never allows more.

Drawdown is measured from the book's HIGH-WATER MARK, the highest marked equity it has
ever reached (`book["hwm"]`). The equity curve is capped at PM_RULES["equity_curve_max"]
points, so the HWM is stored on the book itself and only ever rises.

    rung 0                   dd <  4%   entries sized normally
    rung 1  (−4%)            halve every new entry
    rung 2  (−6%)            no new entries; exits, trims and rebalancing still run
    rung 3  (−8%)            HALT — flatten, cancel working buys, cool off for 5 sessions
    soft daily (−2% on day)  no new entries for the rest of the session (the 3% kill is untouched)

After a rung-3 halt the book is, by construction, ~8% under its HWM and would sit at
rung 2 forever, so the ladder RE-ARMS from the post-halt equity: once the cool-off has
passed, entries are allowed again at `reentry_size_mult` until the old HWM is regained,
and the rungs are measured from that re-entry base — another 8% down from there halts
again. Regaining the HWM clears the whole record.

This module is pure. `state_for` reads the book and returns a dict; pm.py applies it —
the entry-size multiplier at the one place an order's share count is fixed, the entry
refusal at the entry gate, the halt through the same path the kill switch uses.
Everything a run needs to explain itself is in the returned dict, and the same dict is
written to the journal entry and pm_state["book"]["ladder"].
"""
import datetime as dt

DEFAULT = {
    "soft_daily_pct": 2.0,
    "rungs": [
        {"dd_pct": 4.0, "entry_size_mult": 0.5},
        {"dd_pct": 6.0, "entry_size_mult": 0.0},
        {"dd_pct": 8.0, "flatten": True, "cool_sessions": 5},
    ],
    "reentry_size_mult": 0.5,
}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _date(s):
    if isinstance(s, dt.date):
        return s
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def add_business_days(d, n):
    """The n-th business day strictly after `d` (weekends only — the book already
    counts settlement the same way in broker_policy.next_business_day)."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def seed_hwm(book):
    """The high-water mark the book already implies: the stored `hwm` if any, else the
    highest equity on the curve, else the seed capital. Never lower than any of them —
    a book that carried an HWM and a curve that has since been capped keeps the HWM."""
    cands = []
    if _num((book or {}).get("hwm")):
        cands.append(float(book["hwm"]))
    for c in (book or {}).get("equity_curve") or []:
        if isinstance(c, dict) and _num(c.get("equity")):
            cands.append(float(c["equity"]))
    if _num((book or {}).get("starting_equity")):
        cands.append(float(book["starting_equity"]))
    return max(cands) if cands else None


def _rung_for(dd_pct, rungs):
    """Highest rung whose threshold the drawdown has reached (1-based); 0 for none."""
    hit = 0
    for i, r in enumerate(rungs, start=1):
        if dd_pct >= float(r.get("dd_pct", 0)):
            hit = i
    return hit


def state_for(book, equity, today, rules=None, day_pnl_pct=None):
    """Evaluate the ladder for one run. Pure: nothing on `book` is written.

    book         the paper book (reads hwm, equity_curve, starting_equity, cool_until,
                 ladder_halt, day.ladder_soft_hit)
    equity       the marked equity this run
    today        datetime.date of the run
    rules        the PM_RULES["ladder"] dict (DEFAULT when None)
    day_pnl_pct  the session P&L the kill switch computed (None → soft level not judged)

    Returns a dict with: dd_pct, hwm, rung, entry_size_mult, entries_blocked, reason,
    soft_daily_hit, cool_until, reentry_active, plus halt/flatten (True only on the run
    that trips rung 3), cool_active, reentry_base, base_dd_pct, rules.
    """
    rules = dict(DEFAULT, **(rules or {}))
    rungs = sorted((rules.get("rungs") or DEFAULT["rungs"]),
                   key=lambda r: float(r.get("dd_pct", 0)))
    today = _date(today)
    equity = float(equity or 0.0)

    hwm = seed_hwm(book)
    hwm = max(hwm, equity) if hwm is not None else equity
    dd_pct = round((hwm - equity) / hwm * 100.0, 2) if hwm > 0 else 0.0

    halt_rec = (book or {}).get("ladder_halt") if isinstance((book or {}).get("ladder_halt"), dict) else None
    cool_until = _date((book or {}).get("cool_until"))
    regained = halt_rec is not None and equity >= hwm  # hwm == the HWM the halt was measured from
    if regained:
        halt_rec, cool_until = None, None

    st = {"dd_pct": dd_pct, "hwm": round(hwm, 2), "rung": 0, "entry_size_mult": 1.0,
          "entries_blocked": False, "reason": None, "soft_daily_hit": False,
          "cool_until": cool_until.isoformat() if cool_until else None,
          "cool_active": False, "reentry_active": False, "reentry_base": None,
          "base_dd_pct": None, "halt": False, "flatten": False, "regained": regained,
          "rules": rules}

    # --- which reference the rungs are measured from ------------------------------
    # Fresh book: the HWM. After a halt: the post-halt equity, sized at the re-entry
    # multiplier until the HWM is back.
    if halt_rec is not None:
        base = float(halt_rec.get("equity_after") or equity)
        base_dd = round((base - equity) / base * 100.0, 2) if base > 0 else 0.0
        st["reentry_base"] = round(base, 2)
        st["base_dd_pct"] = base_dd
        rung = _rung_for(base_dd, rungs)
        st["reentry_active"] = True
        st["entry_size_mult"] = float(rules.get("reentry_size_mult", 0.5))
    else:
        rung = _rung_for(dd_pct, rungs)
    st["rung"] = rung
    if rung > 0:
        r = rungs[rung - 1]
        if r.get("flatten"):
            # Rung 3. Fires once per reference: a halt already on record for this
            # reference is not re-tripped every run while the book sits under it.
            st["halt"] = st["flatten"] = True
            st["entry_size_mult"] = 0.0
        else:
            mult = float(r.get("entry_size_mult", 0.0))
            st["entry_size_mult"] = min(st["entry_size_mult"], mult)

    # --- gates, most restrictive first ------------------------------------------------
    if st["halt"]:
        st["entries_blocked"] = True
        ref = "the re-entry base" if halt_rec is not None else "the high-water mark"
        shown = st["base_dd_pct"] if halt_rec is not None else dd_pct
        st["reason"] = (f"ladder rung {rung}: drawdown {shown:.2f}% from {ref} "
                        f"(${st['reentry_base'] or st['hwm']:,.2f}) reached the "
                        f"{float(rungs[rung - 1]['dd_pct']):.0f}% halt — flatten, cancel working "
                        f"buys, no new entries for {int(rungs[rung - 1].get('cool_sessions', 0))} "
                        "sessions")
    elif cool_until and today and today <= cool_until:
        st["cool_active"] = True
        st["entries_blocked"] = True
        st["entry_size_mult"] = 0.0
        st["reason"] = (f"ladder cool-off: no new entries through {cool_until.isoformat()} after "
                        f"the {halt_rec.get('date') if halt_rec else '?'} drawdown halt "
                        f"(equity ${equity:,.2f} is {dd_pct:.2f}% under the "
                        f"${st['hwm']:,.2f} high-water mark)")
    elif st["entry_size_mult"] <= 0:
        st["entries_blocked"] = True
        ref = "re-entry base" if halt_rec is not None else "high-water mark"
        shown = st["base_dd_pct"] if halt_rec is not None else dd_pct
        st["reason"] = (f"ladder rung {rung}: drawdown {shown:.2f}% from the {ref} "
                        f"(${st['reentry_base'] or st['hwm']:,.2f}) is past the "
                        f"{float(rungs[rung - 1]['dd_pct']):.0f}% level — no new entries; "
                        "exits, trims and rebalancing still run")

    # --- soft daily level ---------------------------------------------------------------
    soft = float(rules.get("soft_daily_pct", 0) or 0)
    day = (book or {}).get("day") or {}
    hit_before = bool(day.get("ladder_soft_hit")) and day.get("date") == (today.isoformat() if today else None)
    hit_now = soft > 0 and day_pnl_pct is not None and day_pnl_pct <= -soft
    if hit_before or hit_now:
        st["soft_daily_hit"] = True
        if not st["entries_blocked"]:
            st["entries_blocked"] = True
            st["entry_size_mult"] = 0.0
            shown = day_pnl_pct if day_pnl_pct is not None else 0.0
            st["reason"] = (f"soft daily level: session P&L {shown:+.2f}% is through "
                            f"−{soft:.1f}% — no new entries for the rest of the session; "
                            "exits stay live (the 3% kill switch is separate and untouched)")

    if not st["entries_blocked"] and st["reentry_active"]:
        st["reason"] = (f"re-entry after the {halt_rec.get('date') if halt_rec else '?'} drawdown "
                        f"halt: entries sized ×{st['entry_size_mult']:.2f} until the "
                        f"${st['hwm']:,.2f} high-water mark is regained "
                        f"(equity ${equity:,.2f}, {dd_pct:.2f}% under)")
    elif not st["entries_blocked"] and rung > 0:
        st["reason"] = (f"ladder rung {rung}: drawdown {dd_pct:.2f}% from the "
                        f"${st['hwm']:,.2f} high-water mark — new entries sized "
                        f"×{st['entry_size_mult']:.2f}")
    return st


def halt_record(st, today, equity_after, rules=None):
    """The record pm.py stores on the book when rung 3 fires."""
    rules = dict(DEFAULT, **(rules or {}))
    rungs = sorted((rules.get("rungs") or DEFAULT["rungs"]),
                   key=lambda r: float(r.get("dd_pct", 0)))
    top = rungs[-1] if rungs else {}
    cool = int(top.get("cool_sessions", 0) or 0)
    today = _date(today)
    return {"date": today.isoformat(), "hwm": st["hwm"], "dd_pct": st["dd_pct"],
            "equity_after": round(float(equity_after), 2),
            "cool_until": add_business_days(today, cool).isoformat()}
