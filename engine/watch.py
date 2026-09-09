"""watch.py — the EXTENDED-SESSION RISK WATCH. Read-only, by construction.

The book is protected only when something looks at it (PM.md section 5: no resting
stop can sit at the broker, because every position is fractional). Until now the
looking stopped at 15:45 ET and did not resume until 08:45 the next morning — a
seventeen-hour hole that spans three sessions Robinhood actually trades: after-hours
16:00-20:00, the overnight session 20:00-04:00 on eligible names, and pre-market
04:00-09:30.

This module closes the hole with the only thing that is honest at those hours: an
ALERT, not a trade.

WHY IT CANNOT TRADE, AND WHY THAT IS NOT A LIMITATION OF THIS FILE
    get_equity_tradability carries `extended_hours_fractional_tradability` per symbol,
    and the connector's own guidance is blunt: "Fractional and dollar-based orders
    place only in regular_hours regardless of the flags." Every position in every desk
    book is fractional. So outside 09:30-16:00 the manager could not sell most of what
    it holds even in live mode. A watch that booked a simulated overnight exit would be
    logging a fill that no real account could have gotten — precisely the flattery
    PM.md section 3 exists to prevent, in the one session where liquidity is thinnest
    and the flat 0.25% slippage assumption is most wrong.

    So this file has NO write path to the book. It is a separate module rather than a
    slot inside pm.py for exactly that reason: "the overnight run never mutates the
    book" is enforced by structure, not by a flag someone can flip. It imports pm.py
    for the quote parser (so overnight prices are read by the same audited code as
    every other run) and touches nothing else.

WHAT IT WRITES
    claude/pm-watch-journal.json — its own journal, all desks, keyed
    `<date>#<session>#<desk>`. Deliberately NOT claude/pm-journal.json: that file is
    the decision-slot index of PM.md section 8b, pm.py re-sorts it by SLOT_ORDER on
    every run, and an unknown slot would be sorted to the end of its day. Separate
    file, no collision, no risk to the archive index.

QUIET IS THE NORMAL OUTCOME
    No alert and no warning means nothing is written at all — same discipline as the
    sentinel. A holding with no overnight print is NOT an alert and NOT `UNPROTECTED`:
    "no print in this session" and "the manager cannot see this position" are different
    statements, and conflating them would fire a red banner every night on every
    illiquid name until nobody reads them.

USAGE
    python3 watch.py --session after-hours --desk swing \
        --book paper_book.json --quotes pm_quotes.json \
        --tradability pm_tradability.json --journal watch_journal_current.json \
        [--broker pm_broker.json] [--now ISO]

Paths resolve from SCAN_DIR, like every other file in the engine.
"""
import argparse, json, os, sys, datetime as dt

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# ONLY these two names, deliberately (PM.md 12c). A bare `import pm` was here and
# was never used — it handed this program every mutating function pm.py has, which
# is exactly the surface a separate program exists to not have. Do not add one.
from pm import quotes_to_prices, broker_divergence

SESSIONS = {
    "after-hours": {
        "label": "After-hours",
        "window": "16:00-20:00 ET",
        "what": ("the closing print, the after-hours reaction, and anything released "
                 "after the bell"),
    },
    "pre-open": {
        "label": "Pre-open",
        "window": "04:00-09:30 ET",
        "what": "the overnight gap, with lead time before the 08:45 decision slot",
    },
}

WATCH_RULES = {
    # Overnight prints are sparse: a name can go an hour between trades in after-hours
    # and all night in the overnight session. pm.py's 30-minute regular-hours limit
    # would refuse most of them, so the watch reads a wider window and reports the age
    # of every price it used. A 3-hour-old extended print is still evidence about where
    # the name is; it is simply not something to trade on, and this module trades on
    # nothing.
    "quote_max_age_min": 240,
    "alert_move_pct": 3.0,        # per-position move against the last decision mark
    "book_alert_pct": 3.0,        # book-level move: the kill-switch threshold
    "near_stop_pct": 1.5,         # within this much of the stop: worth saying, not an alert
    "journal_max": 400,
}


# ------------------------------------------------------------------ small helpers
def _now(iso=None):
    if iso:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _et(ts):
    try:
        from zoneinfo import ZoneInfo
        return ts.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return ts


def _load(path, default=None):
    if not path:
        return default
    p = path if os.path.isabs(path) else os.path.join(BASE, path)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as fh:
        text = fh.read().strip()
    return json.loads(text) if text else default


def _money(v):
    return f"${v:,.2f}"


# ------------------------------------------------------------------ tradability
def parse_tradability(payload):
    """symbol -> what it can actually do outside regular hours.

    This is the fact that decides whether an overnight alert is ACTIONABLE or merely
    informational, so it is fetched rather than assumed. Three real cases today:
    NVDA trades all day AND allows fractional extended-hours orders; MU trades all day
    but NOT fractional; XOM does not trade outside regular hours at all.
    """
    rows = (((payload or {}).get("data") or {}).get("results")
            or (payload or {}).get("results") or [])
    out = {}
    for r in rows:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        out[r["symbol"]] = {
            "all_day": str(r.get("all_day_tradability") or "").endswith("tradable")
            and "untradable" not in str(r.get("all_day_tradability") or ""),
            "ext_fractional": bool(r.get("extended_hours_fractional_tradability")),
            "tradeable": bool(r.get("tradeable")),
            "state": r.get("state"),
            "raw_all_day": r.get("all_day_tradability"),
        }
    return out


def exitable(sym, shares, trad):
    """(can_exit_now, one sentence saying why not).

    A whole-share position escapes the fractional restriction; a fractional one does
    not, whatever the name's own overnight flag says.
    """
    t = trad.get(sym)
    whole = abs(shares - round(shares)) < 1e-9 and shares >= 1
    if t is None:
        return None, ("overnight tradability for this name is unknown — no tradability "
                      "payload covered it.")
    if not t["all_day"]:
        return False, ("this name does not trade outside regular hours at all, so the "
                       "move is a gap you cannot act on until 09:30.")
    if whole:
        return True, ("whole-share position in a 24-hour-eligible name — it CAN be "
                      "exited by hand in the extended session.")
    if t["ext_fractional"]:
        return True, ("24-hour eligible and fractional orders are accepted in extended "
                      "hours — it CAN be exited by hand now.")
    return False, ("the name trades 24 hours but fractional orders do not: Robinhood "
                   "places fractional and dollar-based orders in regular hours only, so "
                   "the earliest exit is the 09:30 open.")


# ------------------------------------------------------------------ the watch
def watch(book, prices, trad, session, now, desk):
    et = _et(now)
    date = et.date().isoformat()
    positions, alerts, notes = [], [], []

    ref_equity = float(book.get("cash", 0.0))
    live_equity = float(book.get("cash", 0.0))

    for p in book.get("positions", []):
        sym = p["symbol"]
        shares = float(p.get("shares") or 0)
        ref = float(p.get("last_price") or p.get("avg_cost") or 0)
        q = prices.get(sym)
        ref_equity += ref * shares
        row = {
            "symbol": sym, "shares": round(shares, 6), "avg_cost": p.get("avg_cost"),
            "stop": p.get("stop"), "target": p.get("target"),
            "stop_basis_short": p.get("stop_basis_short"),
            "ref_price": round(ref, 4), "ref_as_of": p.get("last_priced"),
            "price": None, "as_of": None, "age_min": None, "move_pct": None,
            "status": "no-print",
        }
        can_exit, why = exitable(sym, shares, trad)
        row["can_exit_now"], row["tradability_note"] = can_exit, why

        if not q:
            live_equity += ref * shares
            notes.append(f"{sym}: no print in the {SESSIONS[session]['label'].lower()} "
                         f"session — carried at its {p.get('last_priced', 'last')} mark "
                         f"of {_money(ref)}, and " + (why or "no tradability data."))
            positions.append(row)
            continue

        px = float(q["price"])
        live_equity += px * shares
        move = ((px / ref) - 1) * 100 if ref else 0.0
        row.update({"price": round(px, 4), "as_of": q.get("as_of"),
                    "age_min": q.get("age_min"), "move_pct": round(move, 2),
                    "status": "priced"})

        stop, target = p.get("stop"), p.get("target")
        if stop and px <= stop:
            row["status"] = "stop-breached"
            alerts.append({
                "severity": "critical", "symbol": sym, "kind": "stop",
                "push": (f"PAPER {desk}: {sym} STOP BROKEN {session} — {_money(px)} vs "
                         f"{_money(stop)} stop, {move:+.1f}%. "
                         + ("Exitable by hand now." if can_exit
                            else "No exit until 09:30 (fractional).")),
                "message": (f"STOP BREACHED ({session}, PAPER): {sym} printed "
                            f"{_money(px)}, at or through its {_money(stop)} stop "
                            f"({p.get('stop_basis_short') or 'stop'}). {move:+.2f}% "
                            f"against the {_money(ref)} mark. "
                            + ("Exitable now — " if can_exit else "Not exitable now — ")
                            + (why or "")
                            + " The book is NOT changed by this run; the next decision "
                              "slot fires the stop at its own price."),
            })
        elif target and px >= target:
            row["status"] = "target-reached"
            alerts.append({
                "severity": "warn", "symbol": sym, "kind": "target",
                "push": (f"PAPER {desk}: {sym} hit its {_money(target)} target {session} "
                         f"at {_money(px)} ({move:+.1f}%). Scale-out waits for the next slot."),
                "message": (f"TARGET REACHED ({session}, PAPER): {sym} printed "
                            f"{_money(px)} against its {_money(target)} 3R target, "
                            f"{move:+.2f}% on the session. The scale-out is a decision-slot "
                            "action and has NOT been taken."),
            })
        elif abs(move) >= WATCH_RULES["alert_move_pct"]:
            row["status"] = "moved"
            alerts.append({
                "severity": "warn", "symbol": sym, "kind": "move",
                "push": (f"PAPER {desk}: {sym} {move:+.1f}% {session} to {_money(px)}"
                         + (f", {(px / stop - 1) * 100:.1f}% above its stop."
                            if stop else ".")),
                "message": (f"{sym} moved {move:+.2f}% in the {session} session, "
                            f"{_money(ref)} -> {_money(px)}"
                            + (f", now {(px / stop - 1) * 100:.1f}% above its "
                               f"{_money(stop)} stop" if stop else "")
                            + "."),
            })
        elif stop and px > stop and (px / stop - 1) * 100 <= WATCH_RULES["near_stop_pct"]:
            row["status"] = "near-stop"
            notes.append(f"{sym} is {(px / stop - 1) * 100:.1f}% above its "
                         f"{_money(stop)} stop at {_money(px)} — inside the "
                         f"{WATCH_RULES['near_stop_pct']:.1f}% band, not through it.")
        positions.append(row)

    # Working orders. They cannot fill here: a resting day limit is dead outside the
    # session, and roll_day() expires every one of them at the next session's start
    # anyway. What is worth saying is that the price the order was waiting for HAPPENED,
    # because the next decision slot will re-decide at a different price.
    orders = []
    for o in book.get("working_orders", []):
        if o.get("status") != "working":
            continue
        q = prices.get(o["symbol"])
        row = {"symbol": o["symbol"], "side": o.get("side"),
               "limit_price": o.get("limit_price"), "shares": o.get("shares"),
               "price": (round(float(q["price"]), 4) if q else None), "through": False}
        if q and o.get("side") == "buy" and float(q["price"]) <= float(o["limit_price"]):
            row["through"] = True
            notes.append(f"{o['symbol']} traded through the {_money(o['limit_price'])} "
                         f"buy limit at {_money(q['price'])} in this session. Day orders "
                         "do not work extended hours and expire at the session roll — "
                         "the next decision slot re-decides at the price it finds.")
        orders.append(row)

    book_move = ((live_equity / ref_equity) - 1) * 100 if ref_equity else 0.0
    if book_move <= -WATCH_RULES["book_alert_pct"]:
        alerts.append({
            "severity": "critical", "symbol": "*", "kind": "book",
            "push": (f"PAPER {desk} book {book_move:+.1f}% {session} vs the last decision "
                     f"mark — at the {WATCH_RULES['book_alert_pct']:.0f}% kill-switch level."),
            "message": (f"BOOK {book_move:+.2f}% ({desk} desk, PAPER) against the last "
                        f"decision-slot mark, {_money(ref_equity)} -> {_money(live_equity)}. "
                        f"That is at or through the {WATCH_RULES['book_alert_pct']:.0f}% "
                        "kill-switch threshold. The kill switch itself is a session control "
                        "and has not tripped; it will be evaluated at the next decision "
                        "slot against that session's opening equity."),
        })

    return {
        "ts": now.isoformat().replace("+00:00", "Z"),
        "et": et.isoformat(),
        "date": date,
        "session": session,
        "session_window": SESSIONS[session]["window"],
        "desk": desk,
        "run_key": f"{date}#{session}#{desk}",
        "mode": book.get("mode", "paper"),
        "book_revision": book.get("revision"),
        "read_only": True,
        "equity": round(live_equity, 2),
        "ref_equity": round(ref_equity, 2),
        "book_move_pct": round(book_move, 2),
        "cash": round(float(book.get("cash", 0.0)), 2),
        "positions": positions,
        "working_orders": orders,
        "alerts": alerts,
        "notes": notes,
        "warnings": [],
    }


# ------------------------------------------------------------------ CLI
def main():
    ap = argparse.ArgumentParser(description="Extended-session risk watch (read-only)")
    ap.add_argument("--session", required=True, choices=sorted(SESSIONS))
    ap.add_argument("--desk", default="swing")
    ap.add_argument("--book", default="paper_book.json")
    ap.add_argument("--quotes", default="pm_quotes.json")
    ap.add_argument("--tradability", default="pm_tradability.json")
    ap.add_argument("--broker", default=None)
    ap.add_argument("--journal", default="watch_journal_current.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--now", default=None)
    ap.add_argument("--quote-max-age-min", type=float,
                    default=WATCH_RULES["quote_max_age_min"])
    a = ap.parse_args()

    now = _now(a.now)
    book = _load(a.book)
    if book is None:
        print(f"FATAL: {a.book} not found in {BASE}", file=sys.stderr)
        sys.exit(2)

    if not (book.get("positions") or book.get("working_orders")):
        print(f"WATCH QUIET [{a.desk}] — the book holds no position and no working order. "
              "Nothing to watch; nothing written.")
        return

    prices, refused = quotes_to_prices(_load(a.quotes) or {}, now, a.quote_max_age_min)
    trad = parse_tradability(_load(a.tradability) or {})
    entry = watch(book, prices, trad, a.session, now, a.desk)

    if refused:
        # Not an alert. A name that has not printed in the extended session is the
        # normal state of most of the tape at 19:00, and calling that UNPROTECTED every
        # night is how a real UNPROTECTED banner stops being read.
        entry["notes"].append("Quotes unusable this session (no extended print, halted, "
                              "or older than the window): " + ", ".join(refused))
        entry["quotes_refused"] = refused

    if a.broker:
        raw = _load(a.broker)
        if raw:
            for w in broker_divergence(raw, book):
                entry["warnings"].append(w)
                entry["alerts"].append({"severity": "critical", "symbol": "*",
                                        "kind": "divergence", "push": w[:190],
                                        "message": w})

    quiet = not entry["alerts"] and not entry["warnings"]
    entry["quiet"] = quiet

    lab = SESSIONS[a.session]["label"]
    hdr = (f"[{lab} watch · {a.desk} desk · READ-ONLY] {entry['date']} "
           f"{_et(now).strftime('%H:%M')} ET   equity {_money(entry['equity'])} "
           f"({entry['book_move_pct']:+.2f}% vs the last decision mark)   "
           f"{len(entry['positions'])} position(s), {len(entry['working_orders'])} "
           "working order(s)")
    print(hdr)
    for p in entry["positions"]:
        px = _money(p["price"]) if p["price"] is not None else "no print"
        mv = f"{p['move_pct']:+.2f}%" if p["move_pct"] is not None else "     —"
        age = f"{p['age_min']:.0f}m" if p.get("age_min") is not None else "  —"
        print(f"  {p['symbol']:<6}{px:>13}{mv:>10}{age:>6}  stop "
              f"{_money(p['stop']) if p['stop'] else '—':>12}  {p['status']}")
    for n in entry["notes"]:
        print(f"  · {n}")
    for al in entry["alerts"]:
        print(f"  ! {al['message']}")

    if quiet:
        print(f"\nWATCH QUIET [{a.desk}] — every stop and target checked, nothing fired, "
              "no divergence. Nothing written: do not project_write the watch journal.")
        return

    journal = _load(a.journal, {"entries": []}) or {"entries": []}
    entries = [e for e in (journal.get("entries") or [])
               if e.get("run_key") != entry["run_key"]]
    entries.append(entry)
    entries.sort(key=lambda e: (e.get("date", ""), e.get("ts", ""), e.get("desk", "")))
    journal["entries"] = entries[-WATCH_RULES["journal_max"]:]
    journal["updated"] = entry["ts"]
    journal["_readme"] = (
        "Extended-session risk watch. One entry per (date, session, desk) that raised "
        "something. READ-ONLY runs: the watch never mutates a paper book, because "
        "fractional orders cannot be placed outside regular hours, so an overnight exit "
        "it logged would be a fill no real account could have gotten. Alerts here are "
        "acted on by the next decision slot. Quiet runs write nothing at all.")
    out = a.out or os.path.join(BASE, f"watch_journal_next-{a.desk}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, indent=2)

    crit = [al for al in entry["alerts"] if al["severity"] == "critical"]
    print(f"\nWATCH ALERT [{a.desk}] — {len(entry['alerts'])} alert(s), "
          f"{len(crit)} critical. Journal -> {out}")
    print(f"  project_write {os.path.basename(out)}  ->  claude/pm-watch-journal.json")
    print("  THE BOOK IS NOT WRITTEN. This run took no decision and changed no position.")
    for al in entry["alerts"]:
        if al["severity"] == "critical" or al["kind"] in ("target", "move"):
            print(f"PUSH: {al.get('push') or al['message'][:190]}")


if __name__ == "__main__":
    main()
