"""broker_policy.py — the day-trade / margin regime the Portfolio Manager trades under.

The PM used to carry one regime, hard-wired: the FINRA pattern-day-trader rule (three
day trades per five rolling business days under $25,000, one held back as an exit
hatch). FINRA Regulatory Notice 26-10 amended Rule 4210 effective 2026-06-04 and
replaced that rule with an intraday margin requirement; Robinhood adopted it the same
day, and the Agentic account is under it. The old guard is still worth keeping — it is
what every backtest and every journal before June was written against, and a broker
that has NOT adopted the amendment still enforces it — so the regime is now a policy
object, chosen by name, and pm.py asks it four questions:

    sellable(...)          may this sale go, and how much of it
    entries_allowed(...)   may this slot place new entries at all
    buying_power(...)      how much of the cash is actually spendable
    record_sale / record_buy   whatever bookkeeping the regime needs

Three policies ship:

    legacy_pdt        exactly the guard pm.py enforced before this module existed, with
                      the same numbers. Selecting it reproduces the old behaviour
                      byte-for-byte; the test suite pins that.
    intraday_margin   FINRA 4210 as amended by Regulatory Notice 26-10 (effective
                      2026-06-04; Robinhood adopted 2026-06-04). No day-trade counting,
                      no $25,000 threshold. Sales are always allowed. The constraint is
                      an intraday maintenance deficit — equity must cover 25% of long
                      market value after any proposed transaction — and a PRACTICE of
                      leaving deficits unmet is what restricts the account. THE DEFAULT.
    cash_settled      a cash account: T+1 settlement, entries limited to settled cash,
                      good-faith violations counted, three in twelve months restricts.

Every note a policy emits is prefixed with its name so a journal line can never be
misread as coming from a regime the book is not under.

Resolution order for the name, in get_policy():
    --broker-policy CLI arg > book["broker_policy"] > desks.json rules.broker_policy
    > engine-config.json "broker_policy" > "intraday_margin".

Stdlib only, like every module in engine/.
"""
import datetime as dt

VALID = ("legacy_pdt", "intraday_margin", "cash_settled")
DEFAULT = "intraday_margin"

# The numbers the old guard ran on. pm.PM_RULES still carries the same keys pointing at
# these values, so a desk can override them through pm_rules exactly as before.
LEGACY_PDT_RULES = {
    "pdt_max_day_trades": 3,        # FINRA (pre-26-10): 3 per 5 rolling business days under $25k
    "pdt_window_business_days": 5,
    "pdt_reserve": 1,               # keep one day trade back as an exit hatch
    "pdt_equity_threshold": 25000.0,
}

INTRADAY_MARGIN_RULES = {
    "maintenance_pct": 25.0,        # maintenance requirement on long market value
    "min_equity": 2000.0,           # the margin-account minimum the amendment kept
    "de_minimis_pct": 5.0,          # a deficit under min(5% of equity, $1,000) ...
    "de_minimis_abs": 1000.0,       # ... is a call to meet, not a mark against the account
    "cure_business_days": 5,        # a deficit must be met within five business days
    "practice_window_days": 90,     # two counted deficits inside this window is a practice
    "practice_count": 2,
    "freeze_days": 90,              # and a practice freezes new entries for this long
}

CASH_SETTLED_RULES = {
    "settlement_business_days": 1,  # T+1 since 2024-05-28
    "gfv_window_days": 365,         # three good-faith violations in rolling twelve months ...
    "gfv_max": 3,
    "restrict_days": 90,            # ... restricts the account to settled cash for 90 days
}


# ------------------------------------------------------------------ calendar helpers
def business_days_back(today, n):
    """The n most recent business days ending at `today` (inclusive), ISO strings."""
    out, d = [], today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out


def next_business_day(d, n=1):
    """The n-th business day strictly after `d`."""
    while n > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def business_days_between(start, end):
    """Business days strictly after `start` up to and including `end`; 0 if end <= start."""
    n, d = 0, start
    while d < end:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _date(s):
    return dt.date.fromisoformat(str(s)[:10])


def day_trades_in_window(book, today, window_business_days):
    window = set(business_days_back(today, window_business_days))
    return len([d for d in book.get("day_trades", []) if d.get("date") in window])


def _record_day_trade(book, pos, shares, today, reason):
    """The record pm.py has always written: closing shares acquired today is a day trade.
    Reads the position's intraday tag BEFORE pm.py decrements it."""
    intraday = pos.get("intraday_shares", 0.0)
    if intraday > 0 and shares > 0:
        closed_intraday = min(shares, intraday)
        if closed_intraday > 0:
            book.setdefault("day_trades", []).append(
                {"date": today.isoformat(), "symbol": pos["symbol"],
                 "shares": round(closed_intraday, 6), "reason": reason})


# ------------------------------------------------------------------ the interface
class Policy:
    """One regime. Subclasses override what they gate on; the defaults gate nothing."""
    name = "base"

    def __init__(self, pm_rules=None, risk_rules=None):
        # Both dicts are held BY REFERENCE: a desk's pm_rules / rules override applied in
        # pm.main() is seen at call time, exactly as the inline guard saw it.
        self.pm_rules = pm_rules if pm_rules is not None else dict(LEGACY_PDT_RULES)
        self.risk_rules = risk_rules if risk_rules is not None else {}

    def _note(self, text):
        return f"{self.name}: {text}"

    def sellable(self, pos, want_shares, reason, book, today, equity):
        """(shares actually sellable, note or None). 0.0 means the sale is refused."""
        return want_shares, None

    def entries_allowed(self, book, today, equity):
        """(bool, note or None). A note with True is informational; with False it is why."""
        return True, None

    def buying_power(self, book, today, cash):
        """How much of `cash` this regime lets the book spend on new entries."""
        return cash

    def record_sale(self, book, pos, shares, today, price, reason=None):
        _record_day_trade(book, pos, shares, today, reason)

    def record_buy(self, book, pos, shares, today, price):
        return None

    def state(self, book, today, equity):
        """What the journal and pm_state carry. The three legacy keys are always present."""
        return {"broker_policy": self.name,
                "day_trades_used": day_trades_in_window(book, today, 5),
                "day_trade_limit": None, "pdt_applies": False}

    def describe(self):
        return self.name


# ------------------------------------------------------------------ 1. legacy PDT
class LegacyPDT(Policy):
    """The guard pm.py enforced before 2026-06-04, unchanged.

    Under $25,000 equity: three day trades per five rolling business days, one held back
    as an exit hatch (entries stop at 2 of 3), a same-day exit allowed only for a stop, and
    where only part of a position is intraday the settled part is sold if it clears the
    broker minimum. Above the threshold the guard disables itself."""
    name = "legacy_pdt"

    def _max(self):
        return self.pm_rules["pdt_max_day_trades"]

    def applies(self, book, equity):
        return equity < self.pm_rules["pdt_equity_threshold"]

    def used(self, book, today):
        return day_trades_in_window(book, today, self.pm_rules["pdt_window_business_days"])

    def sellable(self, pos, want_shares, reason, book, today, equity):
        if not self.applies(book, equity):
            return want_shares, None
        intraday = pos.get("intraday_shares", 0.0)
        if intraday <= 0:
            return want_shares, None
        used = self.used(book, today)
        settled = max(0.0, pos["shares"] - intraday)
        if used < self._max() and reason == "stop":
            return want_shares, self._note("Day trade used to honour a stop — "
                                           f"{used + 1}/{self._max()} in the rolling window")
        if settled >= want_shares:
            return want_shares, None
        if settled * pos.get("last_price", 0) >= self.risk_rules.get("min_notional", 0):
            return settled, self._note(f"PDT guard: only the {settled:.6f} settled shares are "
                                       f"sellable ({used}/{self._max()} day trades used)")
        return 0.0, self._note(f"PDT guard: selling would be day trade {used + 1}/{self._max()} "
                               "and this is not a stop — held")

    def entries_allowed(self, book, today, equity):
        used = self.used(book, today)
        if self.applies(book, equity) and used >= self._max() - self.pm_rules["pdt_reserve"]:
            return False, self._note(f"PDT guard: {used}/{self._max()} day trades used "
                                     "— no new entries, one is held back as an exit hatch")
        return True, None

    def state(self, book, today, equity):
        return {"broker_policy": self.name,
                "day_trades_used": self.used(book, today),
                "day_trade_limit": self._max(),
                "pdt_applies": self.applies(book, equity)}

    def describe(self):
        r = self.pm_rules
        return (f"legacy_pdt: {r['pdt_max_day_trades']} day trades per "
                f"{r['pdt_window_business_days']} business days under "
                f"${r['pdt_equity_threshold']:,.0f}, {r['pdt_reserve']} held back as an exit hatch")


# ------------------------------------------------------------------ 2. intraday margin
class IntradayMargin(Policy):
    """FINRA Rule 4210 as amended by Regulatory Notice 26-10, effective 2026-06-04.

    No day-trade count and no $25,000 threshold. Sales are always allowed — an exit can
    only reduce the requirement. What the rule asks is that, after any proposed
    transaction, equity covers the maintenance requirement (25% of long market value);
    the shortfall is an intraday margin deficit (IMD). A deficit is a call: it must be
    met within five business days. Small deficits — under min(5% of equity, $1,000) — are
    de minimis and do not count toward the account's record. A deficit still unmet on the
    fifth business day, on an account that already has two counted deficits inside 90
    days, is a PRACTICE, and the account is restricted for 90 days. Here that restriction
    is `book["freeze_until"]`: no new entries, exits untouched.

    On a cash-only paper book (buying power = cash, no leverage) the requirement is met by
    construction and this policy never binds; it is implemented in full so that a book
    that IS overdrawn — a negative cash line, a fill the mark did not anticipate — is
    handled the way the broker would handle it rather than silently. The $2,000 margin
    minimum survived the amendment: below it the book can only spend its cash, which is
    already the case, so it is noted, not enforced twice.

    Day trades are still recorded — they cost nothing and the weekly review reads them —
    but nothing gates on them."""
    name = "intraday_margin"

    def __init__(self, pm_rules=None, risk_rules=None, **overrides):
        super().__init__(pm_rules, risk_rules)
        self.rules = dict(INTRADAY_MARGIN_RULES, **overrides)

    # -- the arithmetic -------------------------------------------------------
    def projected_deficit(self, book, equity):
        """Maintenance requirement minus equity once every working buy has filled.
        Positive means the book would be in deficit."""
        cash = float(book.get("cash") or 0.0)
        committed = sum(float(o.get("notional") or o.get("limit_price", 0) * o.get("shares", 0))
                        for o in book.get("working_orders", [])
                        if o.get("side") == "buy" and o.get("status") == "working")
        long_mv = max(0.0, (equity - cash)) + committed
        requirement = long_mv * self.rules["maintenance_pct"] / 100.0
        return round(requirement - equity, 2)

    def de_minimis(self, equity):
        return min(equity * self.rules["de_minimis_pct"] / 100.0, self.rules["de_minimis_abs"])

    def _events(self, book):
        return book.get("imd_events") or []

    def counted_recent(self, book, today):
        since = today - dt.timedelta(days=self.rules["practice_window_days"])
        return [e for e in self._events(book)
                if e.get("counted") and _date(e["date"]) >= since]

    def frozen(self, book, today):
        fu = book.get("freeze_until")
        return bool(fu) and _date(fu) > today

    def _update_events(self, book, today, deficit, equity):
        """Book today's deficit (or its cure) into imd_events. Returns the open event."""
        events = self._events(book)
        open_ev = next((e for e in events if not e.get("met")), None)
        if not events and deficit <= 0:
            return None                # the common case: nothing to book, no key to create
        if deficit > 0:
            if open_ev is None:
                open_ev = {"date": today.isoformat(), "amount": deficit,
                           "counted": deficit > self.de_minimis(equity), "met": None}
                events.append(open_ev)
            else:
                open_ev["amount"] = max(float(open_ev.get("amount") or 0.0), deficit)
                open_ev["counted"] = bool(open_ev.get("counted")) or deficit > self.de_minimis(equity)
        elif open_ev is not None:
            open_ev["met"] = today.isoformat()
            open_ev = None
        # ninety days is the only horizon anything reads; keep the list from growing forever
        keep = today - dt.timedelta(days=2 * self.rules["practice_window_days"])
        book["imd_events"] = [e for e in events if _date(e["date"]) >= keep or not e.get("met")]
        return open_ev

    # -- the interface --------------------------------------------------------
    def sellable(self, pos, want_shares, reason, book, today, equity):
        return want_shares, None

    def entries_allowed(self, book, today, equity):
        deficit = self.projected_deficit(book, equity)
        open_ev = self._update_events(book, today, deficit, equity)
        if self.frozen(book, today):
            return False, self._note(f"account restricted until {book['freeze_until']} — a "
                                     "practice of unmet intraday margin deficits; no new "
                                     "entries, exits untouched")
        if open_ev is not None:
            age = business_days_between(_date(open_ev["date"]), today)
            if (age >= self.rules["cure_business_days"]
                    and len(self.counted_recent(book, today)) >= self.rules["practice_count"]):
                book["freeze_until"] = (today + dt.timedelta(days=self.rules["freeze_days"])).isoformat()
                return False, self._note(
                    f"deficit of ${open_ev['amount']:,.2f} from {open_ev['date']} unmet after "
                    f"{age} business days with {len(self.counted_recent(book, today))} counted "
                    f"deficits in {self.rules['practice_window_days']} days — that is a practice; "
                    f"entries frozen until {book['freeze_until']}")
        if deficit > 0:
            return False, self._note(
                f"projected intraday margin deficit ${deficit:,.2f} "
                f"({self.rules['maintenance_pct']:.0f}% maintenance on long market value "
                f"exceeds ${equity:,.2f} equity) — no new entries until it is met"
                + ("" if open_ev is None or open_ev.get("counted") else "; de minimis, not counted"))
        if equity < self.rules["min_equity"]:
            return True, self._note(f"equity ${equity:,.2f} is under the "
                                    f"${self.rules['min_equity']:,.0f} margin minimum — entries "
                                    "limited to cash, which is what the paper book does anyway")
        return True, None

    def state(self, book, today, equity):
        open_ev = next((e for e in self._events(book) if not e.get("met")), None)
        return {"broker_policy": self.name,
                "day_trades_used": day_trades_in_window(book, today, 5),
                "day_trade_limit": None, "pdt_applies": False,
                "maintenance_pct": self.rules["maintenance_pct"],
                "projected_deficit": self.projected_deficit(book, equity),
                "open_deficit": open_ev,
                "imd_counted_90d": len(self.counted_recent(book, today)),
                "freeze_until": book.get("freeze_until"),
                "frozen": self.frozen(book, today)}

    def describe(self):
        return (f"intraday_margin: FINRA 4210 / Reg. Notice 26-10 — no day-trade cap, "
                f"{self.rules['maintenance_pct']:.0f}% intraday maintenance, deficits met within "
                f"{self.rules['cure_business_days']} business days; a practice freezes entries "
                f"for {self.rules['freeze_days']} days")


# ------------------------------------------------------------------ 3. cash account
class CashSettled(Policy):
    """A cash account. Sale proceeds settle the next business day (T+1) and cannot fund an
    entry until they have. Buying with unsettled proceeds is allowed; SELLING that position
    before the proceeds that paid for it settle is a good-faith violation. Three GFVs in a
    rolling twelve months restricts the account to settled cash for 90 days — which is what
    this policy enforces on every day anyway, so the restriction is flagged in state and
    notes rather than changing what the book may do.

    Book state: `unsettled` — a list of {date, settles, amount}; `settled_cash` is derived
    as cash minus the unsettled total, never stored, so a deposit or a hand edit to cash
    cannot leave the two out of step. `gfv` — a list of {date, symbol, detail}."""
    name = "cash_settled"

    def __init__(self, pm_rules=None, risk_rules=None, **overrides):
        super().__init__(pm_rules, risk_rules)
        self.rules = dict(CASH_SETTLED_RULES, **overrides)

    # -- settlement -------------------------------------------------------------
    def settle(self, book, today):
        """Drop every unsettled lot whose settlement date has arrived."""
        book["unsettled"] = [u for u in book.get("unsettled", []) if _date(u["settles"]) > today]
        return book["unsettled"]

    def unsettled_total(self, book, today):
        return round(sum(float(u.get("amount") or 0.0) for u in self.settle(book, today)), 6)

    def settled_cash(self, book, today):
        return round(max(0.0, float(book.get("cash") or 0.0) - self.unsettled_total(book, today)), 6)

    def restricted(self, book, today):
        ru = book.get("restricted_until")
        return bool(ru) and _date(ru) > today

    def gfv_recent(self, book, today):
        since = today - dt.timedelta(days=self.rules["gfv_window_days"])
        return [g for g in book.get("gfv", []) if _date(g["date"]) >= since]

    # -- the interface --------------------------------------------------------
    def buying_power(self, book, today, cash):
        return min(cash, self.settled_cash(book, today))

    def entries_allowed(self, book, today, equity):
        if self.restricted(book, today):
            return True, self._note(f"account restricted until {book['restricted_until']} after "
                                    f"{self.rules['gfv_max']} good-faith violations — entries "
                                    "limited to settled cash")
        return True, None

    def record_buy(self, book, pos, shares, today, price):
        """Called AFTER pm.py has debited the cash. Whatever the settled cash could not cover
        came out of unsettled proceeds: consume those lots, earliest to settle first, and
        stamp the position with the date it becomes safe to sell."""
        unsettled = sorted(self.settle(book, today), key=lambda u: u["settles"])
        cash = float(book.get("cash") or 0.0)
        shortfall = round(sum(float(u["amount"]) for u in unsettled) - cash, 6)
        if shortfall <= 0:
            return
        funds_settle = None
        for u in unsettled:
            if shortfall <= 0:
                break
            take = min(float(u["amount"]), shortfall)
            u["amount"] = round(float(u["amount"]) - take, 6)
            shortfall = round(shortfall - take, 6)
            funds_settle = u["settles"]
        book["unsettled"] = [u for u in unsettled if u["amount"] > 0]
        if funds_settle and (pos.get("funds_settle") is None or funds_settle > pos["funds_settle"]):
            pos["funds_settle"] = funds_settle

    def record_sale(self, book, pos, shares, today, price, reason=None):
        _record_day_trade(book, pos, shares, today, reason)
        self.settle(book, today)
        proceeds = round(shares * price, 6)
        if proceeds > 0:
            settles = next_business_day(today, self.rules["settlement_business_days"])
            book.setdefault("unsettled", []).append(
                {"date": today.isoformat(), "settles": settles.isoformat(), "amount": proceeds,
                 "symbol": pos["symbol"]})
        fs = pos.get("funds_settle")
        if fs and _date(fs) > today:
            book.setdefault("gfv", []).append(
                {"date": today.isoformat(), "symbol": pos["symbol"],
                 "detail": f"sold before the proceeds that funded it settle on {fs}"})
            if len(self.gfv_recent(book, today)) >= self.rules["gfv_max"]:
                book["restricted_until"] = (today + dt.timedelta(days=self.rules["restrict_days"])).isoformat()

    def state(self, book, today, equity):
        return {"broker_policy": self.name,
                "day_trades_used": day_trades_in_window(book, today, 5),
                "day_trade_limit": None, "pdt_applies": False,
                "settled_cash": self.settled_cash(book, today),
                "unsettled_cash": self.unsettled_total(book, today),
                "gfv_12m": len(self.gfv_recent(book, today)),
                "restricted_until": book.get("restricted_until"),
                "restricted": self.restricted(book, today)}

    def describe(self):
        return (f"cash_settled: T+{self.rules['settlement_business_days']} settlement, entries "
                f"from settled cash only, {self.rules['gfv_max']} good-faith violations in "
                f"{self.rules['gfv_window_days']} days restricts for {self.rules['restrict_days']}")


# ------------------------------------------------------------------ selection
POLICIES = {"legacy_pdt": LegacyPDT, "intraday_margin": IntradayMargin, "cash_settled": CashSettled}


def resolve_name(explicit=None, book=None, desk_cfg=None, engine_config=None):
    """The name only, so the order of precedence is testable without a book."""
    if explicit:
        return explicit
    if isinstance(book, dict) and book.get("broker_policy"):
        return book["broker_policy"]
    if isinstance(desk_cfg, dict):
        d = ((desk_cfg.get("rules") or {}).get("broker_policy")
             or desk_cfg.get("broker_policy"))
        if d:
            return d
    if engine_config is None:
        try:
            import config
            engine_config = config.load()
        except Exception:      # the engine may run without the private config staged
            engine_config = {}
    if isinstance(engine_config, dict) and engine_config.get("broker_policy"):
        return engine_config["broker_policy"]
    return DEFAULT


def get_policy(name_or_none=None, book=None, desk_cfg=None, pm_rules=None, risk_rules=None,
               engine_config=None):
    """The policy object for this run. Unknown names raise, naming the valid ones."""
    name = resolve_name(name_or_none, book, desk_cfg, engine_config)
    cls = POLICIES.get(str(name))
    if cls is None:
        raise ValueError(f"unknown broker policy {name!r}; valid names: {', '.join(VALID)}")
    return cls(pm_rules=pm_rules, risk_rules=risk_rules)
