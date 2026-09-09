"""universe.py — screen a STATED universe, instead of whatever retail is talking about.

WIRED INTO NOTHING YET. The live scan still screens the connector's popular watchlists plus
the liquid-movers saved scan. This module is the replacement, built so the backtest can judge
whether it is actually better before it changes what the book trades.

WHY IT MATTERS MORE THAN ANY WEIGHT
-----------------------------------
The current universe is drawn from `get_popular_watchlists` — Trending stocks, 100 most
popular, Daily movers — which is a measure of retail ATTENTION, not of anything the model
scores. Three consequences, all observed on 2026-09-02:

  * all three desks held the same seven names;
  * 34% of the combined book sat in Information Technology, four of the seven in
    semiconductors;
  * on 2026-09-01 all ten of the top ten were Information Technology.

"Three desks" is currently three views of one trade, and three desks at a per-desk sector cap
of three is a cap of nine. No amount of tuning the pillar weights changes what the screen
never surfaced. This is the highest-leverage change available to the model, and it is also
the one most likely to make the paper record look WORSE at first, because retail attention
and short-horizon momentum are correlated.

POINT-IN-TIME MEMBERSHIP
------------------------
An index's members change. Screening 2024 with 2026's membership list is survivorship bias
wearing a respectable name: the 2026 list is, by construction, the companies that did well
enough to still be in it. So the membership file may carry history, and a backtest date only
ever sees the snapshot that was current then:

    {"index": "Russell 1000",
     "history": [{"as_of": "2024-06-28", "symbols": [...]},
                 {"as_of": "2025-06-27", "symbols": [...]}]}

A flat `{"index": ..., "as_of": ..., "symbols": [...]}` is accepted for live use, where
today's membership is the correct membership — but `screen(..., as_of=<a past date>)` on a
flat file REFUSES, rather than quietly reaching backwards with today's list.

    python3 universe.py --members members.json --bars bars.json --out universe.json
"""
import argparse
import json
import os
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

SCREEN = {
    "min_price": 3.00,               # sub-$3 is a different game with different mechanics
    "min_dollar_volume": 5_000_000,  # median daily $ volume — below this the spread is the trade
    "dollar_volume_window": 20,
    "max_symbols": None,             # None = no cap; set one to bound collection cost
}


def _f(v):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def members_asof(doc, as_of=None):
    """(symbols, snapshot_date). Refuses to reach backwards with a forward-dated list."""
    if not isinstance(doc, dict):
        raise SystemExit("REFUSED: --members must be a JSON object")
    hist = doc.get("history")
    if isinstance(hist, list) and hist:
        snaps = sorted((s for s in hist if isinstance(s, dict) and s.get("as_of")),
                       key=lambda s: s["as_of"])
        if as_of:
            live = [s for s in snaps if s["as_of"] <= as_of]
            if not live:
                raise SystemExit(
                    f"REFUSED: the earliest membership snapshot is {snaps[0]['as_of']}, which "
                    f"is after {as_of}. Screening a date with a later membership list is "
                    "survivorship bias — the list is the companies that lasted.")
            snap = live[-1]
        else:
            snap = snaps[-1]
        return [s.upper() for s in (snap.get("symbols") or [])], snap["as_of"]

    syms = doc.get("symbols")
    if not isinstance(syms, list):
        raise SystemExit("REFUSED: --members needs `symbols` or a `history` of snapshots")
    stamp = doc.get("as_of")
    if as_of and stamp and as_of < stamp:
        raise SystemExit(
            f"REFUSED: this membership list is dated {stamp} and you asked for {as_of}. A "
            "flat list describes today only. Supply a `history` to screen a past date.")
    return [s.upper() for s in syms], stamp


def liquidity(bars, as_of=None, window=None):
    """{SYMBOL: {price, dollar_volume}} — the median of the last `window` sessions.

    Median, not mean: one earnings day at ten times normal volume should not qualify a name
    that is untradeable on the other nineteen."""
    window = window or SCREEN["dollar_volume_window"]
    out = {}
    for sym, rows in (bars or {}).items():
        hist = [b for b in rows
                if not as_of or ((b.get("begins_at") or "")[:10] <= as_of)]
        hist = hist[-window:]
        dvs, last = [], None
        for b in hist:
            c, v = _f(b.get("close_price")), _f(b.get("volume"))
            if c and v:
                dvs.append(c * v)
                last = c
        if dvs and last:
            dvs.sort()
            mid = len(dvs) // 2
            med = dvs[mid] if len(dvs) % 2 else (dvs[mid - 1] + dvs[mid]) / 2.0
            out[sym] = {"price": last, "dollar_volume": med}
    return out


def screen(members_doc, bars=None, as_of=None, rules=None):
    """(kept, excluded, meta). Every exclusion carries its reason — a screen that silently
    drops names is indistinguishable from a screen that is broken."""
    r = dict(SCREEN, **(rules or {}))
    symbols, snapshot = members_asof(members_doc, as_of)
    liq = liquidity(bars, as_of, r["dollar_volume_window"]) if bars else {}

    kept, excluded = [], []
    for sym in sorted(set(symbols)):
        stat = liq.get(sym)
        if bars and not stat:
            excluded.append({"symbol": sym, "reason": "no price history in the bars file"})
            continue
        if stat:
            if stat["price"] < r["min_price"]:
                excluded.append({"symbol": sym,
                                 "reason": f"${stat['price']:.2f} is under the "
                                           f"${r['min_price']:.2f} floor"})
                continue
            if stat["dollar_volume"] < r["min_dollar_volume"]:
                excluded.append({"symbol": sym,
                                 "reason": f"median ${stat['dollar_volume']:,.0f}/day is under "
                                           f"the ${r['min_dollar_volume']:,.0f} liquidity floor"})
                continue
        kept.append(sym)

    capped = None
    if r.get("max_symbols") and len(kept) > r["max_symbols"]:
        kept.sort(key=lambda s: -(liq.get(s, {}).get("dollar_volume") or 0))
        capped = len(kept) - r["max_symbols"]
        for sym in kept[r["max_symbols"]:]:
            excluded.append({"symbol": sym, "reason": "outside the max_symbols cap by "
                                                      "liquidity rank"})
        kept = sorted(kept[:r["max_symbols"]])

    meta = {
        "_what": ("A screened candidate universe from a STATED index membership, not from "
                  "retail attention. See engine/universe.py for why that distinction is the "
                  "highest-leverage change available to the model."),
        "index": members_doc.get("index"),
        "membership_snapshot": snapshot,
        "as_of": as_of,
        "members_in": len(set(symbols)),
        "kept": len(kept),
        "excluded": len(excluded),
        "capped_out": capped,
        "rules": r,
        "liquidity_measured": bool(bars),
    }
    return kept, excluded, meta


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--members", required=True)
    ap.add_argument("--bars", help="get_equity_historicals output, for the liquidity screen")
    ap.add_argument("--as-of", dest="as_of")
    ap.add_argument("--max-symbols", type=int)
    ap.add_argument("--out", default="universe.json")
    a = ap.parse_args(argv)

    doc = json.load(open(a.members, encoding="utf-8"))
    bars = None
    if a.bars:
        import backtest
        bars = backtest.load_bars(a.bars)

    rules = {"max_symbols": a.max_symbols} if a.max_symbols else None
    kept, excluded, meta = screen(doc, bars, a.as_of, rules)
    payload = dict(meta, symbols=kept, excluded_detail=excluded)
    with open(os.path.join(BASE, a.out), "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"UNIVERSE  {meta['index'] or 'unnamed'} @ {meta['membership_snapshot'] or 'undated'}"
          f"  |  {meta['members_in']} members -> {len(kept)} kept, {len(excluded)} excluded")
    if not bars:
        print("  note: no --bars, so nothing was screened on price or liquidity — "
              "membership only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
