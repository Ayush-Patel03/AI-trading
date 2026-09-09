"""paper_mirror.py — let Scan Desk see the book the Portfolio Manager actually holds.

STATE-01. `portfolio.py` reads `portfolio.json`, which has always been the LIVE account
mirror: $50 of cash and no positions, because the live Agentic account is flat under the
paper lock. So from the moment the paper books held anything, the Scan Desk portfolio panel
described an account nobody trades, and every gate that panel applies — the sector cap, the
max-position count, the deployed-room and cumulative-risk budgets, "do we already own this"
— was computed against zero holdings.

The Portfolio Manager's own gates catch a double entry, so this never bought a name twice.
What it did do is worse in a quieter way: the scan kept ranking and proposing names the
system already held, and the board's risk panel read clean while the real book sat at 34% in
one sector. A panel that cannot be wrong is not a check.

This writes `portfolio.json` from the paper books instead, while the mode is paper, labelled
PAPER everywhere it surfaces. It is a projection, not a second source of truth: it is
rebuilt from the books every scan and nothing ever reads back from it into a book.

    python3 paper_mirror.py                    # every desk in desks.json — the house
    python3 paper_mirror.py --desk swing       # one book
    python3 paper_mirror.py --out portfolio.json

Exit 0 wrote a mirror. Exit 2 wrote NOTHING and said why — a scan that cannot see the book
must fall back to the previous behaviour visibly, not publish an invented panel.

The house is the default deliberately. Three desks trading one scan hold the same names, and
the number the sector cap has to be measured against is the combined one — the same argument
HOUSE-01 made inside the manager (PM.md section 14).
"""
import argparse
import json
import os
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

MODE_LOCK = ("This mirror is built from PAPER books. It must never be used to describe a "
             "funded account: the moment a book goes live, the broker is the source of "
             "truth and this file has no business existing. See docs/PM.md section 1.")


def _load(name, default=None):
    try:
        with open(os.path.join(BASE, name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def desk_books(desks_path="desks.json", only=None):
    """[(desk_name, book_filename)] for the desks whose book is staged in the run dir."""
    desks = (_load(desks_path) or {}).get("desks") or {}
    out, missing = [], []
    for name, cfg in desks.items():
        if only and name != only:
            continue
        book = (cfg or {}).get("book")
        if not book:
            continue
        if os.path.exists(os.path.join(BASE, book)):
            out.append((name, book))
        else:
            missing.append(name)
    return out, missing


def merge(books):
    """Combine paper books into one holdings view.

    Shares add; cost basis is share-weighted, because two desks that bought the same name at
    different prices have one combined basis and averaging the averages would be wrong.
    Working buy orders are NOT positions and are not included — `portfolio.py` sizes against
    positions, and the manager already counts pending orders itself (CAP-01)."""
    cash = 0.0
    open_equity = 0.0
    by_symbol = {}
    for _, book in books:
        cash += float(book.get("cash") or 0.0)
        day = book.get("day") or {}
        open_equity += float(day.get("open_equity") or 0.0)
        for p in book.get("positions") or []:
            sym = p.get("symbol")
            shares = float(p.get("shares") or 0.0)
            if not sym or shares <= 0:
                continue
            cur = by_symbol.setdefault(sym, {
                "symbol": sym, "shares": 0.0, "cost": 0.0,
                "gics": p.get("gics"), "industry": p.get("industry"),
                "last_price": p.get("last_price"), "desks": []})
            cur["shares"] += shares
            cur["cost"] += shares * float(p.get("avg_cost") or 0.0)
            if p.get("last_price"):
                cur["last_price"] = p["last_price"]
            cur["gics"] = cur["gics"] or p.get("gics")
            cur["industry"] = cur["industry"] or p.get("industry")
    positions = []
    for sym in sorted(by_symbol):
        c = by_symbol[sym]
        positions.append({
            "symbol": sym,
            "shares": round(c["shares"], 6),
            "avg_cost": round(c["cost"] / c["shares"], 6) if c["shares"] else 0.0,
            "last_price": c["last_price"],
            "gics": c["gics"], "industry": c["industry"]})
    return positions, round(cash, 2), round(open_equity, 2)


def build(desks_path="desks.json", only=None):
    """The mirror dict, or (None, reason) when it must not be written."""
    found, missing = desk_books(desks_path, only)
    if not found:
        return None, ("no paper book is staged in the run directory — "
                      "nothing to mirror, and an empty panel is a lie, not a default")
    loaded = []
    for name, fn in found:
        book = _load(fn)
        if not isinstance(book, dict):
            return None, f"{fn} did not parse as a book"
        if (book.get("mode") or "paper") != "paper":
            return None, (f"{fn} is not in paper mode. {MODE_LOCK}")
        loaded.append((name, book))

    positions, cash, open_equity = merge(loaded)
    invested = sum((p["last_price"] or p["avg_cost"]) * p["shares"] for p in positions)
    equity = invested + cash
    daily = round((equity / open_equity - 1) * 100, 2) if open_equity else 0.0
    names = [n for n, _ in loaded]
    return {
        "_what": ("PAPER holdings, projected from the Portfolio Manager's paper books so "
                  "Scan Desk's portfolio panel and its risk gates describe the book the "
                  "system actually holds (STATE-01). Rebuilt from the books every scan; "
                  "nothing reads back from it. " + MODE_LOCK),
        "mode": "paper",
        "source": "paper_mirror",
        "desks": names,
        "account": {"label": "PAPER — %s desk%s combined" % (len(names),
                                                             "" if len(names) == 1 else "s"),
                    "mode": "paper", "desks": names},
        "cash": cash,
        "daily_pnl_pct": daily,
        "positions": positions,
    }, (f"desk book(s) not staged: {', '.join(missing)}" if missing else None)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--desks", default="desks.json")
    ap.add_argument("--desk", default=None, help="mirror one desk instead of the house")
    ap.add_argument("--out", default="portfolio.json")
    a = ap.parse_args(argv)

    mirror, note = build(a.desks, a.desk)
    if mirror is None:
        print(f"PAPER MIRROR REFUSED: {note}", file=sys.stderr)
        return 2
    if note:
        print(f"NOTE: {note} — the mirror covers only the books that were staged.",
              file=sys.stderr)
    with open(os.path.join(BASE, a.out), "w", encoding="utf-8") as f:
        json.dump(mirror, f, indent=2)
    invested = sum((p["last_price"] or p["avg_cost"]) * p["shares"] for p in mirror["positions"])
    print(f"PAPER MIRROR  {a.out}  |  {len(mirror['desks'])} desk(s): "
          f"{', '.join(mirror['desks'])}  |  {len(mirror['positions'])} name(s)  |  "
          f"invested ${invested:,.2f}  cash ${mirror['cash']:,.2f}  "
          f"equity ${invested + mirror['cash']:,.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
