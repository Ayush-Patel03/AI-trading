# Portfolio Manager — doctrine

Scan Desk screens the market. The **Portfolio Manager** owns the book: it protects open
positions, harvests targets, rebalances, and decides which of Scan Desk's proposals become
orders. Read this whole file before running it.

Authorised by Vishal on 2026-08-31. Four runs a market day, chained **forty-five minutes**
behind each scan slot (re-timed from fifteen on the evening of 2026-08-31 — section 6 says
why). Its mandate is entries, exits, take-profit and rebalancing — and it runs in
**paper mode**.

- **Live board:** see `boards` in the private `claude/engine-config.json`
  The rolling Trade Desk, rewritten in place every run. Always republish it by passing that
  URL as `url`; publishing it without one forks a second rolling board.
- **Archived boards:** a run that changed the book publishes its own frozen copy too,
  titled *Trade Desk — Midday, 31 Aug 2026*, which is never rewritten. **Section 8b is the
  rule for when** — a quiet run does not get one.
- **Engine:** `claude/engine/pm.py` + `render_pm.py` + `archive.py`, importing
  `claude/engine/portfolio.py` for every risk rule. **All four must be copied into the run
  directory** — `pm.py` imports `archive` at load time, and until 2026-09-01 no task prompt
  copied it, so every scheduled run would have died on its first Python command. The project
  copies are authoritative; the `market-scan` skill bundle holds older files with the same names.
  `claude/engine/watch.py` is the extended-session watch (section 12c) and is a separate
  program — it imports pm.py's quote parser and nothing else, and the two watch tasks are the
  only things that run it.
- **Book:** `claude/paper-book.json` — **one living file, never forked per run**
- **Book history:** `claude/book-history/<run_id>.json` — an immutable copy of every
  revision that actually changed something
- **Journal:** `claude/pm-journal.json` — one entry per run, and the archive's index
- **Skill:** `/portfolio-manager`
- **Strategy desks:** `claude/engine/desks.json` — section 13. The main book above is the
  `swing` desk; `claude/paper-book-pullback.json` / `claude/pm-journal-pullback.json` and
  `claude/paper-book-momentum.json` / `claude/pm-journal-momentum.json` are the other two.
- **Paper capital:** **$5,000 per desk** since 2026-08-31 evening. At $50 every position was
  capped at $7.50, Robinhood's $1 minimum distorted sizing and PDT bound constantly, so the
  logged decisions were not the ones real capital would make. The live account stays at
  $50 under the lock and is read every run for divergence only.
- **Scheduled tasks (EDT crons; the DST task `trig_01FFYGDB6Sui2MSJkPuRkF4d` shifts all of
  them on 2026-10-30):**
  `trig_01XG76TChZ7WVtyn2JopTkJx` pre-market **08:45 ET** (`45 12 * * 1-5`),
  `trig_01Q7cuMtxqeAkLHds1mLSFsR` opening-range **10:45** (`45 14 * * 1-5`),
  `trig_01YKgkDB7mRXJTbMdPRaMbmo` midday **13:15** (`15 17 * * 1-5`),
  `trig_017Dpdi7EdX5ADSP3Gy1cVnS` power-hour **15:45** (`45 19 * * 1-5`);
  `trig_01CrQvGcZrn469zjsPCX5uXS` the **sentinel**, hourly **09:35–15:35 ET**
  (`35 13-19 * * 1-5`) — section 12;
  `trig_01N1MLaAME5mE8PXPnu2yzDk` the **pre-open watch**, 07:00 ET (`0 11 * * 1-5`) and
  `trig_01MpAZKmqRBfyjR2uyVuEfqu` the **after-hours watch**, 16:20 ET
  (`20 20 * * 1-5`) — section 12c;
  `trig_01Bem6ST315PcrAfTR27i1YC` the **daily health check**, 16:15 ET (`15 20 * * 1-5`);
  `trig_01Qwa5QR7CkhcZVygPgogMUQ` the **weekly review**, Fridays 16:30 ET (`30 20 * * 5`);
  `trig_012PZZ5YMDo7Hmuza1Vbv4JF` the **score validation**, Saturdays 09:00 ET
  (`0 13 * * 6`) — README section 11. A one-off check, `trig_018y5gXv3gNafba2GULRoDaU`,
  fires 2026-09-01 11:00 ET to verify the first real scan→manager hand-off.

---

## 0. Where this file lives, and what is not in it

This is the doctrine, versioned in the public `AI-trading` repo and read from the clone —
not from a project doc. Change it by pull request, not by `project_write`.

Deliberately **not** here, because this repo is public: the Robinhood account id and mask,
the artifact board URLs, and anything else identifying. Those are in the private
`claude/engine-config.json`, staged into `$SCAN_DIR` at run time and read by
`engine/config.py`. The account lock is unchanged in substance — see section 1.

---

## 1. MODE LOCK — paper. Read this before anything else.

The manager decides in full and executes **nothing**. Every order is simulated against
`claude/paper-book.json`. No Robinhood order tool is called. Not `place_equity_order`,
not `review_equity_order`, not `cancel_equity_order` — not once, not "just to preview".

This is not a technical limitation. The connector exposes order placement and it works.
It is the authorisation Vishal actually gave: **paper first, flip a flag later.** The
2026-08-31 conversation that created this system chose paper mode explicitly over
autonomous and approval-gated execution.

So: a future session that finds the manager useful, or is asked "can you just buy it",
does not get to upgrade the mode. Changing it requires Vishal saying so, in the current
request, in words that mean live money. Until then the flag in the book is the whole
policy and the answer to "why didn't it buy" is "because it is a paper book, by design."

This is also consistent with `PORTFOLIO.md`, which was rewritten at 18:48Z on the same day
and still reads "Scan Desk **proposes**. It never places an order." That rule governs Scan
Desk. The manager is a separate component with its own authorisation — and in paper mode it
places no order either, so nothing here contradicts it. The day the mode flips, that
sentence in `PORTFOLIO.md` needs a companion paragraph saying the manager is the exception
and why. Do not let the two documents drift into disagreeing about who may place an order.

The live account **is** read every run — `get_accounts`, `get_portfolio`,
`get_equity_positions` on the Agentic account — to keep the lock honest, to notice if
Vishal funds or trades it by hand, and to keep the live code path warm. Reading is not
trading. The read is where a divergence between the paper book and reality gets caught,
and a divergence is reported, never quietly reconciled. **Since 2026-08-31 evening this is a
mechanism, not a promise:** the raw `get_equity_positions` payload goes to `pm.py --broker`,
which emits a `LIVE ACCOUNT DIVERGENCE` warning for every symbol the live account holds.

### The account lock is inherited whole

`PORTFOLIO.md` governs. Only the limited-margin individual account the connector marks
agentic-tradable. Resolve it from `get_accounts` at run time; never by nickname. Its id,
mask and expected cash live in the private `claude/engine-config.json` and are deliberately
not in this repo — see section 0. The other five accounts are out of scope permanently and
are not read, reported or aggregated.

### Going live — NOT YET IMPLEMENTED. Read this before believing the checklist below.

The 2026-08-31 audit (LIVE-01) tested `--mode live` and found that the checklist below
describes an engine that does not exist yet. Three things are wrong at once, and any one of
them would put a real, unprotected position in a real account:

1. **Live mode still simulates its own fills.** `mode` is consulted only to decide what goes
   into `pm_orders.json`. Run live, and `pm.py` both hands you a ticket to place at the broker
   *and* books the fill itself on the next run. The claim "in live mode `pm.py` stops mutating
   positions" is false today.
2. **Exits are never emitted as tickets.** Only new entries reach `orders_to_place`. A live
   stop-out would be recorded in the book and sent nowhere — the board shows it closed while
   the real position stays open. This is the most dangerous gap in the system.
3. **There is no reconciliation.** `--broker` (added 2026-08-31 evening) only *warns* on a
   divergence; it does not make broker state the truth, and nothing in `pm.py` reconciles
   positions, fills or cash against the account.

Until those three are built and tested, the mode flag is not a go-live switch — it is a
foot-gun. The checklist is kept as the *target*, not the procedure:

1. Vishal says so explicitly. Record the date in this file.
2. The paper book has a track record worth acting on: enough closed trades to mean
   something, and a look at whether the wins came from the model or from the fill
   assumptions. Section 3 explains why the second question is not rhetorical. The weekly
   review (section 12) is what produces that evidence.
3. Build and test live execution: no self-fills in live mode; exit and rebalance tickets
   emitted alongside entries; broker state reconciled as the first step of every run.
4. Set `mode` to `live` in `claude/paper-book.json`, or pass `--mode live`.
5. Every risk gate in `PORTFOLIO.md` becomes a live control rather than an advisory one.
6. The one thing that does **not** carry over: a real limit order can be partially filled,
   rejected, or filled at a price the paper model never considered. Reconciliation against
   the broker becomes the first step of every run, not a sanity check.

---

## 2. What it is allowed to do

| Action | Trigger | Size |
|---|---|---|
| Open | A Scan Desk proposal that clears every gate in `portfolio.py` — **never at the power-hour slot** | as sized there |
| Stop out | Price at or below the structural stop | whole position |
| Thesis exit | Setup became Broken Trend, or score fell under 45 | whole position |
| Take profit | Price at or above the 3R target | half, then stop to breakeven |
| Trim | Score 45-55, or trading through the analyst target | a third |
| Rebalance | Position value over the 15% cap | back to the cap |
| Flatten | Book 8% under its high-water mark — rung 3 of the drawdown ladder (section 7) | every position with a fresh price |

**Sizing, stops and every risk limit come from `portfolio.py` unchanged.** The manager
imports it; it does not fork it, re-derive it, or "improve" it. That file mirrors
`src/execution/position_sizing.py` in the trading-system repo and was audited on
2026-08-31. If a limit needs to change, change it there so Scan Desk's proposals and the
manager's orders never disagree about what is allowed.

The manager was built against the 18:48Z rebuild of `portfolio.py`, in which
`derive_levels()` returns a **five**-tuple ending in `basis_kind`. It does not call that
function directly — `build_proposals` does — but a future change to its signature is the
kind of thing that breaks quietly, so check it if the engine is rebuilt again.

What lives in `pm.py` is execution policy only: order lifecycle, fills, the broker policy
(section 4), the kill switch, the journal.

### No entries at power hour

An entry never fills in the run that places it (section 3), and `roll_day()` expires every
day order at the start of the next session *before* the fill pass runs. So an entry placed at
the last slot of the day could never be evaluated for a fill — a quarter of all entry
decisions were structurally dead on arrival (audit FILL-01). Since 2026-08-31 evening the
power-hour run places no new entries; it runs exits, trims and rebalancing, and the
power-hour scan's ranking is tomorrow's pre-market watchlist.

### Sizing excludes names that already have a resting order

`build_proposals()` used to size every qualifying candidate, including names with a working
buy order, spending their notional from the cash budget — which had *also* already been
subtracted as reserved cash. Three resting orders silently starved four good candidates in
testing (audit SIZE-01). Those names are now filtered out before sizing.

### Pending orders count against every cap, not just cash

SIZE-01 above fixed the CASH half of the problem. The COUNT half was still open until
2026-09-01: `build_proposals` reads `marked["positions"]` for the sector cap, the
max-positions slot count, the cumulative-risk budget, the correlation multiplier and the
max-deployed room — and a working order is not a position, so a name the book had already
committed to was invisible to all five.

The swing book ended 2026-09-01 **four deep in Information Technology against a cap of
three**. NVDA was placed at 10:45 and was still resting unfilled at 13:15, so the sector cap
counted MU and SNDK, called CRDO the third, and allowed it. Twenty minutes later the sentinel
filled NVDA, CRDO and HOOD together and the book was over its own limit with no rule having
been broken at any single moment.

CAP-01 passes working buy orders to `build_proposals` as pseudo-positions carrying their
symbol, GICS, industry and notional, and adds their committed notional to `invested` for the
deployed-room calculation. Every run holding pending orders journals one line naming them.
`portfolio.py` is untouched — the manager sanitises its own input, it does not fork the rules.

**It is preventive, not corrective.** It stops a fourth name being added; it does not unwind a
breach that already exists. `rebalance_pass` warns on sector concentration and never
force-sells, so an over-cap book stays over-cap until those positions close on their own
rules. That is deliberate, and it means a concentration warning on the board is a decision for
Vishal, not something the engine is about to fix.

### The spread gate

Broker quotes carry bid and ask. `quotes_to_prices()` records the spread as a percentage
of price and the entry pass refuses any name over `PM_RULES["max_spread_pct"]` (1.0%),
naming it in the journal. On a book this size the spread on a thin name is a larger cost
than anything the scoring model reasons about, and a limit entry that pays it is a bad
trade before the thesis is even tested.

### The macro gate

Scheduled tape-wide releases are binary events for every name at once, exactly as earnings
are for one. The pre-market scan writes the day's releases into `meta.macro_events`
(`[{date, name, time_et}]`) and `pm.py` freezes new entries ahead of one — but only for a
release that actually moves the tape, and only when it is close.

**Retuned 2026-09-01 after the first live day, because the first version would have stopped
the book trading.** That day's calendar was a Fed governor speech, JOLTS and ISM; the next
day's was ADP and a 14:00 Beige Book. Gating on any same-day entry for the whole day would
have frozen every entry slot on both days — the 15:45 run places no entries by design, so
the book could not have opened a position at all. A gate that fires on every calendar item
does not protect the book, it silently stops it.

So two conditions must both hold:

1. **High impact** — the name matches `MACRO_HIGH_IMPACT`: FOMC / rate decision, CPI, PCE,
   non-farm payrolls or the employment situation, GDP, Powell. ADP is explicitly excluded
   (it is not the payrolls report). A Fed speech, JOLTS, ISM, the Beige Book and weekly
   claims are reported in the journal and never gate.
2. **Close** — it prints within `PM_RULES["macro_gate_lookahead_min"]` (120) minutes and has
   not printed yet. A 14:00 FOMC does not freeze the 08:45 or 10:45 slots; it freezes 13:15.
   A high-impact release with **no time given** is treated as pending all day, because an
   unknown time cannot be cleared.

Exits, trims and rebalancing always stay live through a gate. Tomorrow's releases are noted
as overnight risk and never gate.

### Trim discipline

A name that keeps weakening gets **trimmed at most once per session, at most twice
total** — the third trim signal closes the position instead. Without this the manager
nibbles the same losing position every slot and calls it risk management. This was a real
behaviour in the first simulation: XOM trimmed four times in three days, each one a
separate commission-free way of being wrong slowly.

### Rebalance discipline — the same rule, opposite sign

Trim was capped from day one; `rebalance_pass` was not, and it nibbled the **winning**
position instead. On 2026-09-02 NVDA was rebalanced four times — three on swing, once on
momentum — each time it crossed the 15% cap on a rising price. The amounts were trivial
(0.039, 0.082, 0.007, 0.069 shares) and the combined P&L was **+$0.86**, so nothing was lost.
At real size, four shaves a day on the one name that is working is a persistent drag, and it
is the same failure the trim cap exists to prevent.

Two guards, added 2026-09-02, because they stop different things:

- **Count** — `max_rebalances_per_session` (1). A name rebalanced today is not rebalanced
  again this session; the run journals why. The next session it is eligible again, because a
  name that keeps running away from the cap on a later day is a real breach again.
- **Size** — `rebalance_deadband_pct` (1.5). The trigger is cap + deadband, so a position
  oscillating around 15.0% is left alone. **The cap is still the cap:** once it does fire, the
  position is trimmed back to 15%, not to the trigger. The deadband decides *when* a breach is
  worth acting on, never *what* the limit is.

Both are preventive and neither is corrective: like every other cap here, they gate an action,
they do not unwind a book that is already over.

---

## 3. The fill model — where paper flatters or slanders itself

Understand this before reading any P&L off the board.

**An entry never fills in the run that places it.** It rests as a day limit at the price
the scan observed, and fills on a *later* slot only if the price comes back to the limit —
then at the limit, because that is where a resting order sits. Unfilled orders die at the
session roll and the manager re-decides.

**Exits are marketable and fill in the same run**, 0.25% against the book. Protection that
can go unfilled is not protection.

**Targets do not rest as sell limits.** When the slot price is at or through the 3R target
the scale-out fires as a marketable sale at the slot price less 0.25%, exactly like any
other exit. That is *more* conservative than a limit filled at the target, not less — read
the P&L knowing it. This paragraph used to describe a resting limit the engine never
created (audit M3); **corrected 2026-09-02**, in the doctrine and in `pm.py`'s own
docstring, so the two cannot drift again.

Two biases follow, in opposite directions, and they do not cancel:

- *Against the book:* it sees **four prices a day, not a tape**. An intraday spike through
  a target, or a wick through a stop that recovers by the next slot, is invisible. Real
  fills are often faster and better than a limit-priced fill on a later slot.
- *For the book:* a gap **through** a limit is recorded as a fill at the limit, when reality
  would have filled better — or, on the exit side, much worse. Slippage past a stop is
  modelled as a flat 0.25%, and a real gap-down is not 0.25%.

So the equity curve is a **decision log with a P&L attached, not a backtest.** Judge the
manager on whether its decisions were defensible given what it knew. Do not present the
paper return as an expected live return, and do not let a good paper number be the argument
for going live on its own.

---

## 3b. Shadow fills and execution cost (K-06, added 2026-09-10)

The booked fill model above is unchanged, and its output is byte-identical to what it was
before this section existed — `tests/test_shadow_fills.py` proves it against a frozen
pre-change output. What changed is that the book now carries a **second price next to
every fill**, and a running total of the difference.

### What a shadow fill is

Every booked fill — an entry filling at its resting limit, a stop, a thesis exit, a target
scale-out, a trim, a rebalance, a ladder flatten — gets a `shadow_price`: what a marketable
order of that size would have paid **against the quote the engine actually saw**.

    buy   →  ask + k × half_spread + add_on
    sell  →  bid − k × half_spread − add_on

- `half_spread` is (ask − bid) / 2 from the broker quote the run was priced on.
- `k` is how many half-spreads beyond the touch the order pays, **by slot**
  (`PM_RULES["shadow"]["k_by_slot"]`): 1.0 at pre-market, opening-range and power-hour,
  where the spread is wide and the tape is moving; 0.0 at midday, where a marketable order
  on a liquid name really does fill at the touch; 0.5 for the sentinel and ad-hoc runs.
- `add_on` is the size/impact cost the spread does not carry, in bps of the mid, by
  liquidity tier: **2 bp** for a large cap, **15 bp** for a name under $10m of dollar ADV
  (`small_cap_adv_usd`). The tier comes from the candidate's `adv_usd`, else
  `avg_volume_20d × price`, else `volume × price`; when none is known the name is treated as
  large and the basis string says so. The guess errs toward the smaller cost on purpose —
  a tier nobody measured must not manufacture a penalty.
- **Gap through a stop** (`gap_through_stops: True`): a stop's shadow touch is capped at
  min(last, stop). A stop is not resting at the broker (section 5); when the tape has
  printed through it, the print is the witness — never the stop price, and never a stale
  bid sitting above the print.
- **No two-sided quote** (the price came from the scan, or the quote was crossed): the
  shadow *is* the booked price, basis `no-quote`, gap zero. A missing quote records
  nothing rather than inventing something.

### Why never fill at mid

A model that fills at the mid is claiming the desk earns half the spread on every trade.
Nobody paying for liquidity earns the spread. A marketable order crosses it: the buyer
pays the ask, the seller hits the bid, and on a moving tape or a thin book the fill is
worse than the touch, not better. Half-spread guide, as a fraction of price:

| Tier | Half-spread | What it means on a $1,000 fill |
|---|---|---|
| Mega-cap (AAPL, NVDA, MSFT) | 0.5–2 bp | 5–20 cents |
| S&P 500 average | 2–5 bp | 20–50 cents |
| Russell 2000 | 10–40 bp | $1–$4 |

The flat 0.25% (25 bp) exit haircut is therefore **pessimistic** on a mega-cap at midday
and **optimistic** on a small cap at the open — and the whole point of the shadow ledger is
to find out which of those the book has actually been doing. On 2026-09-10's frozen
sequence (three large-cap midday fills) the shadow came in *inside* the booked haircut:
`cum_gap_usd` was negative. That is a finding about the book, not a reason to loosen it.

### What is recorded

Per fill, on the journal decision and (for sales) on the `closed_trades` record:

- `shadow_price` — the price above.
- `shadow_gap_usd` — `(booked − shadow) × shares` for a sale, `(shadow − booked) × shares`
  for a buy. **Positive means the paper book flattered itself**, in either direction.
- `implementation_shortfall_bps` — `|shadow_fill − decision mid| / mid × 1e4`, where the
  decision mid is `(bid + ask) / 2` **at the time of the decision**. For an entry that is
  the quote the order was *placed* on; `entry_pass` writes it onto the working order as
  `decision_mid`, and `simulate_fills` reads it back when the resting limit fills on a
  later slot. For every same-run sale the decision and the fill share one quote. `null`
  when there was no two-sided quote. The shortfall is measured on the *shadow* fill, not
  the booked one: it is the execution cost a real desk would report, and the booked
  haircut is a bookkeeping convention, not a cost.
- `shadow_basis` — the formula in words, so a board can never show a number without its
  provenance (`ask + 1×half-spread + 2bp (large cap)`, or `no-quote`).

Book level, `book["shadow"]`: `cum_gap_usd`, `n_fills`, `gap_share_of_realized_pct`
(cumulative gap as a percentage of |realised P&L|, null while nothing is realised) and a
per-year cost accumulator. State and the journal carry the same summary plus
**`equity_shadow` = equity − cum_gap_usd** — the equity the book would show if it had paid
the shadow price on every fill. The console prints `shadow gap $x (y% of realized)` on
every run.

### The execution-cost budget

`PM_RULES["cost_budget_bps_per_year"] = 200` per desk. `fills.cost_budget_status(book,
today)` returns `ytd_cost_bps` = Σ(shortfall × notional) over the calendar year ÷ average
equity (the mean of the year's equity-curve points) × 1e4, the `budget_bps`, and
`share_used_pct`. Over 100% the run warns `EXECUTION COST BUDGET EXCEEDED` — the desk is
paying more to trade than its edge is likely worth, and the answer is to trade less or
trade wider names at quieter slots, not to lower the budget. 200 bp/year is deliberately
tight for a book that turns over as often as this one; if it binds, that is information.

### The plan

The book keeps booking at the old model until the shadow gap is *understood*: a few weeks
of `gap_share_of_realized_pct` across all three desks, split by slot and by tier, so the
switch is made on a measurement rather than on a feeling. Then, on a session boundary,
with a doctrine note here and a line in the journal, `pm.py` switches its booked fill to
the shadow price and the pre-switch equity curve is labelled as such on every board. Not
before: a silent change to the cost model makes yesterday's curve and today's incomparable
for a reason nobody can see. To switch the *recording* off — never the reason to do it —
set `PM_RULES["shadow"]["enabled"] = False`; the engine then writes exactly the pre-K-06
bytes.

---

## 4. Day-trade / broker policy — the regime the book trades under

Until 2026-09-10 the manager carried one regime, hard-wired: the FINRA pattern-day-trader
rule. It no longer describes the account. **FINRA Regulatory Notice 26-10** amended Rule
4210, **effective 2026-06-04**, and replaced pattern-day-trader counting with an intraday
margin requirement; **Robinhood adopted it on 2026-06-04**, and the Agentic account is under
it. The regime is now a **policy object** in `engine/broker_policy.py`, chosen by name, and
`pm.py` asks it four things: may this sale go and how much of it, may this slot place
entries at all, how much of the cash is spendable, and what to record after a fill. Every
note a policy writes into the journal is prefixed with its name (`intraday_margin: …`,
`legacy_pdt: …`) so a line can never be misread as coming from a regime the book is not
under. The journal entry and `pm_state.json` carry `broker_policy` on every run.

**Selection, in order of precedence:** `--broker-policy` on the command line, then
`broker_policy` in the book, then `rules.broker_policy` for the desk in `desks.json`, then
`broker_policy` in the private `engine-config.json`, then the default. **The default is
`intraday_margin`** — Vishal's decision, because that is the rule the account is actually
under. An unknown name is fatal (exit 2, naming the three valid ones); it is never silently
replaced with a default.

### `intraday_margin` — FINRA 4210 as amended by Notice 26-10 (default)

- **No day-trade count and no $25,000 threshold.** Day trades are still recorded in the
  book (`day_trades`) because the weekly review reads them, but nothing gates on them.
- **Sales are always allowed.** A stop, target, trim or rebalance on shares bought the same
  session goes through. The `UNPROTECTED … policy refused the sale` state of the old guard
  cannot arise under this policy.
- **The constraint is an intraday margin deficit.** After any proposed transaction, equity
  must cover the maintenance requirement — 25% of long market value (`maintenance_pct`),
  counting working buy orders as if filled. A shortfall is a call: `entries_allowed` refuses
  new entries while it stands, and the deficit is booked in `book["imd_events"]` with its
  date and amount. A deficit under **min(5% of equity, $1,000)** is *de minimis* — still a
  call, but not counted against the account's record.
- **A practice freezes the book.** A deficit still unmet on the **fifth business day**, on
  a book that already has **two counted deficits inside 90 days**, sets
  `book["freeze_until"]` to today + 90 calendar days. While frozen: no new entries, exits
  untouched. The freeze thaws on its own on the date stamped.
- The **$2,000 margin-account minimum** survived the amendment. Below it entries are limited
  to cash — which the paper book does anyway — so it is noted in the journal, not enforced
  twice.
- On a cash-only paper book (buying power = cash, no leverage) this policy is met by
  construction and never binds. It is implemented in full so that a book that *is*
  overdrawn — a negative cash line, a fill the mark did not anticipate — is handled the way
  the broker would handle it rather than silently.

### `legacy_pdt` — the guard the manager ran under until 2026-09-10

Selecting it reproduces the old behaviour byte-for-byte, with the old numbers (they still
sit in `PM_RULES` under their `pdt_*` keys, so a desk's `pm_rules` override reaches them as
before). Keep it for a broker that has not adopted the amendment, and for reading any
journal written before June.

- Under $25,000 equity, **three day trades per five rolling business days**; a fourth
  restricts the account for ninety days.
- Every sale that closes shares bought the same day is counted as a day trade.
- **New entries stop at 2 of 3 used.** One day trade is always held back as an exit hatch,
  because being unable to honour a stop is worse than missing an entry.
- A same-day exit is allowed only for a **stop**. A target or a trim on a position opened
  today waits for tomorrow, when the shares have settled.
- Where only part of a position is intraday, the settled part is sold if it clears the
  broker minimum and the rest held.
- **The state to fear.** If the budget is exhausted and a position opened today breaks its
  stop, the manager will not sell it. It emits `UNPROTECTED: <SYM> broke its stop and the
  legacy_pdt policy refused the sale` as a banner on the board and a warning in the journal.
  That is a deliberate trade — a ninety-day restriction is worse than one bad hold — and it
  is the loudest thing the system can say. If you see it, decide by hand. **Every PM task
  prompt pushes a phone notification on it** (section 12).
- Above $25,000 equity the guard disables itself.

### `cash_settled` — a cash account, T+1

- Sale proceeds settle the **next business day** and cannot fund an entry until they have.
  The book carries an `unsettled` list of `{date, settles, amount}`; settled cash is derived
  as cash minus the unsettled total, never stored, so a deposit or a hand edit to the cash
  line cannot leave the two out of step. Entries are sized against settled cash only.
- Buying with unsettled proceeds is allowed. **Selling that position before the proceeds
  that paid for it settle is a good-faith violation**, counted in `book["gfv"]` with the
  date. Three GFVs in a rolling twelve months set `book["restricted_until"]` to today + 90
  days; the restriction limits entries to settled cash — which this policy does on every
  day anyway — so it is flagged in the state and the journal rather than changing what the
  book may do.

The board's *Day trades* tile shows the count with the budget pips under `legacy_pdt`, and
the count with the policy's name and no pips under the other two: a budget that does not
exist is not drawn.

---

## 5. There is no resting stop in the market

Robinhood does not accept stop orders on **fractional** shares. On a $50 book every
position is fractional, so no protective order can rest at the broker. This is the single
largest structural gap in the design and it does not go away in live mode.

The consequence: **a position is protected only when the manager runs.** Four decision
slots a market day — the last at 15:45 ET, fifteen minutes before the close — plus the
hourly **sentinel** (section 12) from 09:35 to 15:35, so the longest unwatched stretch
inside the session is now about an hour instead of two and a half. Since 2026-09-01 the
**extended-session watch** (section 12c) also looks at 16:20 and 07:00 ET, so the overnight
hole is watched — but watching is all it can do, and section 12c explains why that is not a
shortcoming of the code. Still nothing on weekends, in the 20:00–04:00 stretch between the
two watches, or if a run fails. A gap-down opens straight through the stop and the manager
sells at the next DECISION slot's price, whatever that is — the watch will have named the
gap hours earlier, and naming it is not the same as being out of it.

Two things follow. The manager **fetches its own price for every holding** rather than
relying on the name appearing in that slot's scan universe — a holding the scan dropped is
still a holding, and an unpriced holding cannot be protected. And a position it cannot
price at all is flagged `UNPROTECTED` and carried untouched, never sold on a guess and
never assumed fine.

If the account is ever funded past the point where whole-share positions are practical,
revisit this: real stop orders at the broker are worth more than any refinement to the
scoring model.

---

## 6. Freshness — a stale price may value the book, never trade it

The Scan Desk README calls this the staleness trap and it is worse here, because the
consequence is a position rather than a wrong-looking number.

Every price carries a source and a `fresh` flag. `pm-fetch` (the manager's own fetch this
run) is fresh. A scan price is fresh only if the scan is dated today **and** no older than
`PM_RULES["scan_stale_minutes"]` (240) measured from `meta.scan_date + meta.time` in
America/New_York. The last price the book saw is never fresh.

That minutes limit was declared on day one and **never enforced** until the 2026-08-31
audit (TIMING-01): staleness was decided purely by calendar date, so the 08:00 scan counted
as perfectly fresh at 15:15 — seven hours and a whole session later. At the same time the
scans were taking one to four hours, so a manager fifteen minutes behind its scan never saw
the scan it was chained to. Both halves are fixed: the PM crons moved to forty-five minutes
behind each slot (08:45 / 10:45 / 13:15 / 15:45 ET) and the minutes limit is live. A scan
older than the limit freezes entries and is named in the journal with its age.

Stale prices are used to **value** the book, labelled as stale on the board. They never
fill an order, fire a stop, trigger a rebalance or size an entry. When the scan is stale
the manager says so on the board, freezes entries, and still manages any holding it could
price itself. This was caught in testing: a scan dated four days earlier filled an order
and stopped out a position at prices from the previous week.

---

## 7. The kill switch is a live control

A session loss of 3% or worse halts the book: working buy orders are cancelled, no new
entries for the rest of the session, a red banner on the board. **Exits stay live** — a
halt that blocked stop-losses would be the opposite of a safety feature.

The reference is the session's opening equity, set at the first run of each trading day.
The halt clears at the next session roll.

### Drawdown ladder — graded de-risking under the kill switch (K-02, 2026-09-10)

The kill switch is a cliff, and it was the only control against losing money. A desk could
bleed 7% over two weeks without any rule noticing, then lose 3% on the eighth day and halt
for one afternoon. Practitioners run a ladder instead — halve size at −5%, shut at −7.5% —
so the book de-risks *as* it loses rather than after. This is that pattern at the book's
own numbers, and it sits **under** the kill switch: it refuses earlier and never allows
more. The 3% daily kill above is untouched.

The yardstick is the **high-water mark** — the highest marked equity the book has ever
reached, kept on the book as `hwm` and only ever raised. Drawdown is
`(hwm − equity) / hwm`. The equity curve is capped at 400 points, so the HWM is stored
rather than re-derived from the curve; on a book that has never carried one it is seeded
from the highest equity on the curve, else from the seed capital.

| Level | Trigger | What changes | What does not |
|---|---|---|---|
| soft daily | session P&L at or below **−2%** (same day-P&L definition as the kill switch) | no **new** entries for the rest of the session — sticky until the roll | exits, trims, rebalancing; the 3% kill still trips on its own |
| rung 1 | **−4%** from the HWM | every new entry sized **× 0.5** | nothing else |
| rung 2 | **−6%** from the HWM | **no new entries**, reason journaled in `skipped` | exits, trims, rebalancing all still run |
| rung 3 | **−8%** from the HWM | **halt** through the kill switch's own path (`day.halted`, working buys cancelled) — then **flatten**: every position with a fresh price is sold at the slot price less exit slippage, through the broker policy; `cool_until` is set **5 business days** out | a position with no fresh price is reported `UNPROTECTED`, never sold on a stale mark |
| cool-off | any session on or before `cool_until` | no new entries | exits still run; the day halt itself clears at the roll as always |
| re-entry | after `cool_until`, equity still under the HWM | entries allowed again at **× 0.5** (`reentry_size_mult`) | the rungs re-arm from the post-halt equity: another 8% down from there halts again — the old HWM is not the yardstick for a cliff the book could never reach |
| regained | equity back at or above the HWM | full size, `cool_until` and the halt record cleared | — |

The multiplier is applied at exactly one place — where the entry pass fixes an order's
share count, after `build_proposals` has sized it — so `portfolio.py` stays byte-identical.
An order carries `meta.ladder_mult` and `meta.unscaled_shares` for the audit; a halved
order that falls under the broker minimum is skipped with the reason. The constants live in
`PM_RULES["ladder"]` (so the board and the alerts page read what the engine gates on):

```
"ladder": {"soft_daily_pct": 2.0,
           "rungs": [{"dd_pct": 4.0, "entry_size_mult": 0.5},
                     {"dd_pct": 6.0, "entry_size_mult": 0.0},
                     {"dd_pct": 8.0, "flatten": true, "cool_sessions": 5}],
           "reentry_size_mult": 0.5}
```

**Fields.** On the book: `hwm`, `cool_until` (ISO date or null), `ladder_halt`
(`{date, hwm, dd_pct, equity_after, cool_until}` — the re-entry base — or null),
`day.ladder_soft_hit`. On every journal entry and in `pm_state.json` under
`book.ladder` (plus `book.hwm`, `book.cool_until`): `dd_pct`, `hwm`, `rung` (0–3),
`entry_size_mult`, `entries_blocked`, `reason`, `soft_daily_hit`, `cool_until`,
`cool_active`, `reentry_active`, `reentry_base`, `base_dd_pct`, `halt`/`flatten` (true only
on the run that tripped rung 3), `regained`, and the `rules` it was judged under. The
console prints a `LADDER` line whenever the rung is above zero, entries are blocked or the
book is in re-entry. The ladder is judged once per run, on the same mark the kill switch
saw, on decision slots and sentinels alike — a rung-3 flatten is an exit, and the sentinel
exists to honour exits within the hour. The pure evaluation is `engine/ladder.py`;
`tests/test_ladder.py` walks one book down every rung and back.

Known limit: a withdrawal lowers equity without lowering the HWM and reads as drawdown.
Record it as a negative deposit and, if the ladder trips on it, reset `hwm` by hand.

### Dead-man's switch — when the manager itself goes silent (K-05, 2026-09-10)

The kill switch and the ladder only work while the engine runs. Section 5 is the reason
that matters: there is no resting stop in the market, so a box that stops running the
sentinel is a book with no stops at all. The dead-man's switch is the control for that case.
Every runner invocation writes `health/heartbeat.json`; an **independent** checker
(`runner/deadman.py`, its own scheduled task on a different runtime —
`docs/runner/deadman-task.md`) counts the expected sentinel and PM runs that have gone by
during market hours without one. At **two missed** it trips: every working paper buy is
cancelled, every open position is stamped `protective_stop: {level, placed_at, reason:
"deadman"}` at its own stop (or entry − 2.5 × ATR when it has none), a `deadman` journal
entry and an `aborted: true` coverage row are written, `health/deadman.json` records the
trip and the state repo commits. It is idempotent while tripped and clears itself
(`cleared_at`) on the first heartbeat after. In paper mode that stamp is a record — the
same stop `pm.py` would fire on at its next run — and nothing is sent anywhere.
**Live behaviour, specified and not implemented:** behind the two-key live mode of
section 1, a trip would place one broker-resident stop-limit order per position at the
stamped level (limit = level − 0.5 × ATR), so the book is protected by the exchange while
nobody is watching; a heartbeat would *not* cancel them — a human does, after reading
`docs/runbooks/deadman-tripped.md`. Until live mode exists no code path places that order.

---

## 8. Concurrent writes — the same problem that destroyed scan history

Four runs a day write the same book, and a scheduled session can survive for days and
resume. On 2026-08-31 a scan that fired the previous Friday finished on Monday and wrote
Friday's scores over Monday's file. The book is worth more than the history file; a stale
session writing an old book back would silently undo real decisions.

So the book carries a `revision`, and the write protocol is mechanical:

1. Read `claude/paper-book.json` into the run directory at the start.
2. Run the engine. It writes `pm_book_next.json` with `revision` incremented and
   `based_on_revision` recording what it started from.
3. **Immediately before writing back, re-read the project doc** into `fresh_probe.json` and
   run `python3 pm.py --book pm_book_next.json --check fresh_probe.json`.
4. Exit 0 means write it back. **Exit 2 means another session wrote while this one was
   working** — do not write, do not merge by hand, do not publish, journal the run as
   abandoned and stop.

The journal merges by `run_key` (`date#slot`), so a re-run replaces its own entry rather
than duplicating it, and entries stay in slot order.

---

## 8b. Every run that matters keeps its own board

Section 8 protects the book from a concurrent write. This protects the *record* from being
quietly overwritten, which is the same failure wearing different clothes.

Until 2026-08-31 all four runs republished the single Trade Desk artifact and wrote the same
four working files. The board that showed the fill was gone by the next slot, and there was
no way to ask what the book looked like when a decision was taken. Every run now carries a
**run id** — `<date>-<slot>`, e.g. `2026-08-31-power-hour` — and:

| Output | When | Lifetime |
|---|---|---|
| `trade-desk.html` → the rolling board, republished in place | every run | always current |
| `trade-desk-<run_id>.html` → its own frozen artifact | when the run earned one | permanent |
| `claude/book-history/<run_id>.json` | when the book changed | permanent |
| `pm_book_next-<run_id>.json`, `pm_state-<run_id>.json` in `$SCAN_DIR` | every run | the session |

**Until 2026-09-01 this section was executed by nothing** (audit ARCH-01): the engine
computed all of it and printed exactly what to write, and the task prompts wrote only the
book and the journal. The prompts now carry the full order below.

### When a run earns a frozen board

`archive.should_publish()` decides, and `pm.py` prints the answer. A board is published when:

- the run took **decisions** — a place, a fill, a sell, a cancel, an expiry; or
- the run raised **warnings** — `UNPROTECTED`, a kill-switch trip, refused quotes; or
- the **book fingerprint changed** against the previous run; or
- it is the **power-hour run and the book holds something** — so a day with an open position
  always leaves one board behind, even if no rule fired.

**The fingerprint hashes what the book IS, not what it is WORTH.** Positions, working orders,
the halt flag, realised P&L and the closed-trade count are in it. Prices, market values,
unrealised P&L, equity, `revision` and `last_run` are deliberately out. If the marks counted,
"publish when the book changed" would collapse into "publish always" the moment the book held
a single share — four near-identical boards a day, which is precisely how a real `UNPROTECTED`
banner gets scrolled past.

A quiet run is not a failure and does not hide anything: the rolling board still updates and
the journal entry is still written, with `changed: false` and an empty `change_reasons`.

### A re-run updates its board, it never forks one

The four scheduled slots get a time-free run id, so a re-run of the 13:15 book replaces
`2026-08-31-midday` everywhere — the snapshot file, the book-history doc, the journal entry.
Two rules keep that honest, and both exist because the naive version lost data in testing:

1. **A re-run cannot demote a slot that already published.** `pm.py` carries the superseded
   entry's `publish_snapshot`, `artifact_url` and `book_snapshot` forward. Without this, a
   quiet second attempt at a slot rewrote the journal entry with `publish_snapshot: false`
   over the top of an attempt that had really published a board — the artifact stayed live
   with nothing in the journal pointing at it.
2. **A re-run republishes to the URL it already has.** When the journal entry carries an
   `artifact_url`, `render_pm.py` prints `Artifact(..., url=...)` instead of offering to
   create a second board for the same slot. `attempt` on the entry counts the tries.

### The book is state, not a report

`claude/paper-book.json` stays **one file**. The `revision` / `based_on_revision` pair and
`pm.py --check` are the whole concurrency defence, and forking the book per run would defeat
both. What is archived is a *copy* of each revision that changed something, under
`claude/book-history/`, so the revision chain is auditable and a clobber is recoverable —
you can see exactly which revision was lost and what was in it.

### The journal is the index

`claude/pm-journal.json` already keyed by `run_key` and already kept 500 entries. It now also
carries, per run: `run_id`, `book_revision`, `book_fingerprint`, `changed`, `change_reasons`,
`publish_snapshot`, `attempt`, `artifact_url` and `book_snapshot`. That makes it the index of
the archive — read it to find any past board or any past book without opening a single record.
**Put the artifact URL on the entry before writing the journal back**; an entry with
`publish_snapshot: true` and no `artifact_url` is a board nothing can find.

### Where this sits in the run

```bash
python3 pm.py --slot <slot> --book paper_book.json --scan scan_results.json \
              --quotes pm_quotes.json --broker pm_broker.json --journal pm_journal_current.json
python3 render_pm.py          # prints exactly which artifacts to publish, in order
# re-read claude/paper-book.json -> fresh_probe.json; pm.py --check ...  (section 8)
# exit 2 -> write nothing, publish nothing, report abandoned.  exit 0 -> in this order:
#   (a) if PUBLISH: publish trade-desk-<run_id>.html (new, or url= for a re-run); put its URL
#       on this run's entry in pm_journal_next.json as artifact_url
#   (b) project_write pm_book_next.json           -> claude/paper-book.json
#   (c) if BOOK CHANGED: pm_book_next-<run_id>.json -> claude/book-history/<run_id>.json
#   (d) project_write pm_journal_next.json        -> claude/pm-journal.json
#   (e) republish the rolling trade-desk.html with url=<live board>
```

**The check comes before any publish.** A frozen board of a book that was never written
would describe a decision that never took effect.

## 9. What each run needs

| Input | Where from | Missing? |
|---|---|---|
| `paper-book.json` | project doc | fatal — nothing else is safe |
| `scan_results.json` | `claude/latest-scan.json`, written by the scan | entries frozen, holdings still managed |
| `pm_quotes.json` | raw `get_equity_quotes` for every holding **and** every candidate | that name is unpriced; a holding is then `UNPROTECTED` |
| `pm_broker.json` | raw `get_equity_positions` on the agentic account | no divergence check this run; say so |
| `desks.json` | `claude/engine/desks.json` | `--desk` refuses; the swing desk still runs |
| live account state | `get_accounts` / `get_portfolio`, read-only | note it and continue |

### Prices come from the broker, and the engine parses the payload itself

Write the **raw** `get_equity_quotes` response to `pm_quotes.json` and let `pm.py --quotes`
read it. Do not transcribe prices into a hand-written file. A mistyped price on this path
sizes a position or fires a stop; hand-transcription is precisely where that goes wrong, and
the parser also enforces things a human retyping numbers would skip:

- It takes whichever of `last_trade_price` / `last_non_reg_trade_price` has the more recent
  venue timestamp, as the connector's own guidance says.
- It **refuses** a symbol that has not traded or is not in an `active` state — a halted name
  has no tradeable price, and its last print is a trap.
- It **refuses** a quote older than 30 minutes. A stale quote is not a live price just
  because it came from the broker.

Refused symbols are named in the journal and fall back to the scan price, or to no price at
all. `pm_prices.json` still exists as a manual override and is outranked by broker quotes.

### Re-pricing candidates, and the drift block

The scan can be hours old by the time the manager runs. Sizing an entry and placing a limit
off that price while holding a live quote is simply wrong, so every candidate is **re-priced
to the live quote** before sizing — which also moves the ATR stop, since it is struck off the
entry price.

But a re-priced row is still carrying the *score* and *setup* the scanner computed at the old
price. So:

- Drift over **3%**: re-priced, entered, and named in the journal.
- Drift over **5%**: **not entered.** The row no longer describes the price. It waits to be
  rescored by the next scan.

This was a live bug, caught in testing on 2026-08-31: the manager held a fresh broker quote
and still placed its limit at the scan's price.

`claude/latest-scan.json` is the hand-off. **No scan had ever written it before 2026-09-01** —
the instruction had been lost from the README when its section 9 was rewritten, and no scan
prompt carried it, so every PM run of 2026-08-31 correctly reported "no scan" and traded
nothing. It is now in README section 3 and in all four scan prompts. Until a scan has run
under that instruction, the manager will correctly report that it has no scan and manage
holdings only. That is the designed degradation, not a failure — but it is also a scan-side
failure worth one plain line in the report.

---

## 10. Stops — ATR now, structure only as a fallback

This changed underneath the manager while it was being built. Until 2026-08-31 stops were
structural because the data path had no OHLC bars; the Robinhood connector now returns a
year of daily bars in one call and `technicals.py` computes Wilder ATR(14) from them, so
`derive_levels()` places the stop at **1.5x ATR below entry**, clamped into a **3-12% band**,
with a 50- or 200-day MA taking precedence when it sits within 2% of the ATR level. Targets
are still 3R.

For the manager this matters in three places:

1. **The basis travels with the position.** Every position and every working order carries
   `stop_basis`, `stop_basis_kind` (`atr` / `structure` / `breakeven`) and `atr_pct`, and the
   board prints which one is in force. An 11% ATR stop on a volatile name and a 4% stop on a
   calm one are not the same instrument, and averaging them in the reader's head is the
   mistake this labelling exists to prevent.
2. **A missing ATR is a real signal, not a formatting detail.** A name whose bars were
   insufficient falls back to the structural stop and says "no ATR available". The board
   counts how many positions are on each basis in the book header.
3. **A breakeven stop after a scale-out overrides both.** Once half a position is taken at
   the 3R target the stop moves to average cost and the basis becomes `breakeven` — that is
   deliberate and outranks whatever the volatility says.

Everything above is the `fixed_atr` policy, which every live desk runs. Since K-07 it is one
of three — section 16 — and `stop_basis_kind` can also read `chandelier`, `trail` or
`catastrophe` on a desk that has opted into another policy.

## 11. Known gaps

- **Four price observations a day.** Everything in section 3 follows from this.
- **No resting stops.** Section 5. The big one.
- **The overnight is watched, not managed.** Section 12c: fractional orders cannot be placed
  outside regular hours, so the 16:20 and 07:00 runs alert and nothing more. 20:00–04:00 and
  weekends are unwatched entirely.
- ~~The swing book is over its sector cap.~~ **Resolved 2026-09-02 at 09:47 ET**, not by a
  fix but by the CRDO stop-out. The swing book holds MU, SNDK and NVDA (three Information
  Technology) plus HOOD (Financials) — exactly at the cap, not over it. CAP-01 is what stops
  it recurring.
- ~~Caps are enforced on one desk at a time; nothing counts exposure ACROSS the three desks.~~
  **Closed by HOUSE-01 on 2026-09-02** — section 14. The manager now measures combined
  exposure per symbol and per sector across every desk book and refuses an entry that would
  breach either house cap. Preventive, not corrective: it does not unwind an existing breach.
- **Live mode is not implemented.** Section 1. Do not flip the flag. Since K-07 (section 17)
  the controls that would have to exist first are written as tested validators, and every
  paper run journals what they would have refused (`live_would_refuse`).
- **Stops are ATR-based, and the basis must always be reported.** See section 10 — and since
  K-07 (section 16) the stop is a per-desk POLICY: every live desk is still on `fixed_atr`,
  and a position carries the policy it was opened under.
- **The correlation multiplier is a sector-overlap proxy**, not computed from returns.
  Never present it as a correlation. (Since K-03, section 14b, the HOUSE-level N_eff *is*
  computed from returns when `bars.json` is staged, and is labelled `proxy` when it is not.
  The per-desk sizing multiplier in `portfolio.py` is unchanged.)
- **No options, no crypto, no shorts.** Long equity only.
- **Slippage is a flat 0.25%** on exits. It is a placeholder, not a measurement — but since
  K-06 (section 3b) every fill also carries a shadow price against the real quote and the
  book accumulates the gap, so the size of the placeholder's error is now measured rather
  than guessed. The booked number is unchanged until that measurement says how to change it.
- **Targets fill at market, not at the target** — see section 3. The behaviour is unchanged
  and deliberate; what changed on 2026-09-02 is that the doctrine and the code now say so in
  the same words.
- **Quotes are single-venue last prints.** Fine for a $50 book; on a thin name the bid/ask
  spread is a bigger cost than anything the model reasons about. The spread gate refuses
  entries over 1% and the shadow ledger (section 3b) now prices every fill against it.
- **The manager cannot act between slots.** Everything it knows is up to four hours old
  by the time the next run corrects it.
- ~~**Scan Desk's portfolio panel does not know about the paper book** (audit STATE-01).~~
  **Closed 2026-09-02.** `engine/paper_mirror.py` projects every paper book into
  `portfolio.json` before the scan's portfolio pass, labelled PAPER, so the panel and its
  sector caps describe the book the manager actually holds. It refuses to write rather than
  publish an empty panel, and refuses outright once any book leaves paper mode.
- ~~**Exit thresholds 45/55 are duplicated**; `starting_equity` never updates on funding;
  `_load()` accepts an absolute path.~~ **M2, M4 and M5 closed 2026-09-02.** The thresholds
  live once, in `portfolio.RULES`, and a static test forbids the literals coming back. The
  reported return is measured against `portfolio.capital_basis()` — the seed plus every
  entry in the book's `deposits` list — so funding the account no longer reports a return it
  did not earn. `_load()` resolves inside `$SCAN_DIR` or reports the file absent.
- **A frozen board is a snapshot of a decision, not of the market.** It shows the book as
  the manager saw it, at four prices a day. Section 3 still governs how to read the P&L on
  it, archived or live.

## 12. Operations — added 2026-08-31 evening

**Market holidays.** Every scan and PM cron fires Monday to Friday, holidays included, and
Labor Day 2026-09-07 was six days away. Every scan and PM prompt now opens with a guard:
confirm the market is open; if it is closed, run nothing, write nothing, publish nothing, and
report one line. A closed market cannot move a price, so the book is exactly as protected as
it was at the last close. Half-days: the 15:00 scan and the 15:45 PM skip; the 13:15 PM runs
as the final look. The two sentiment-refresh tasks were created from the trading-system
repo's API and cannot be edited by an agent, so they carry no guard; the scans check their
`_meta.generated_at` instead.

**Alerting.** A warning that only appears in a session transcript is a warning nobody saw.
Every PM prompt now delivers a push notification through the `PushNotification` tool when a
position is opened, closed, trimmed or rebalanced; the kill switch trips; anything is
`UNPROTECTED`; a `LIVE ACCOUNT DIVERGENCE` is raised; day P&L moves over 2%; a held position
carries earnings before the next open; or the run itself fails (import error, abandoned
write-back, engine exception, refused quotes on a holding).

**The weekly review** (`trig_01Qwa5QR7CkhcZVygPgogMUQ`, Fridays 16:30 ET) reads the journal,
the book history and the scan history and writes `claude/reviews/<ISO-week>.md`: the week's
decisions and the gates that blocked the rest, outcomes labelled PAPER, a model-versus-fill-
artefact section that answers the question section 3 says is not rhetorical, system health
(late scans, failed runs, carried-forward panels, 403s), and a verdict on whether the record
is accumulating evidence for or against the model. It never touches the book, never
recommends going live, and appends one line per week to `claude/reviews/index.md`. This is
the evidence the go-live checklist's second item requires.

The numbers in that review come from `engine/report.py --md` (S-07, `docs/BACKTEST.md`
§6c): stage the three `claude/pm-journal*.json` docs into a directory, the three
`claude/paper-book*.json` into another, add a bars file with SPY, and run
`python3 engine/report.py --journals journals/ --books books/ --bars bars.json
--counterfactual --md review.md`. It gives the week's refusals by rule, week and desk, the
E5 counterfactual per gate (did the names it refused underperform the names it admitted,
with a bootstrap interval and n), closed-trade P&L by exit reason, desk, entry-score decile
and setup, and each desk's return against exposure-adjusted SPY. It is read-only over the
journals and books; every number carries its n, anything under 30 says *not a sample*, and
it prints no win rate and no Sharpe. The review quotes it; it does not recompute it.

**The 2026-09-01 one-off** (`trig_018y5gXv3gNafba2GULRoDaU`, 11:00 ET) verifies that the
first morning under the rewritten prompts and engine actually completed: no `import archive`
crash, `claude/latest-scan.json` written, scans inside ~40 minutes, PM entries carrying
candidates and the 8b journal fields, revision chain intact, no unhandled warnings.

**The sentinel** (`trig_01CrQvGcZrn469zjsPCX5uXS`, hourly at :35 from 09:35 to 15:35 ET) is
the answer to section 5. It is not a decision slot. `pm.py --slot sentinel` prices holdings
and working orders only, runs fills and the exit pass — stops and targets — and nothing
else: no scan, no entries, no rebalance. Its run key carries the clock time
(`2026-09-03#sentinel-1435`) so each run is its own journal entry; it never adds a point to
the equity curve, so the curve stays one point per decision slot. A sentinel that takes no
decision and raises no warning is **QUIET** and writes nothing at all — no book revision, no
journal entry, no publish — so the book does not churn eleven times a day and a real
sentinel entry means something happened. It runs for all three desks; a desk with nothing
held is skipped before any quote is fetched. The prompt must quote every holding on every
desk: a holding it does not quote is reported `UNPROTECTED`, which is correct but noisy.
Fills at a sentinel are real fills — the book now sees roughly eleven prices a day rather
than four, which makes section 3's "four prices a day" caveat smaller, not gone.

**The daily health check** (`trig_01Bem6ST315PcrAfTR27i1YC`, 16:15 ET) is the permanent
form of the 2026-09-01 one-off: every task fired and succeeded, scans on time, the hand-off
file dated today, history and archive written, journals carrying the 8b fields with the
revision chain intact, every warning listed, both boards showing today. It writes
`claude/health/<date>.md`, pushes only on a FAIL, and exists because the failure mode this
system has actually shown is a run that reports success while writing nothing useful.

## 12c. The extended-session watch — added 2026-09-01

Section 5 says a position is protected only when the manager runs, and until today the running
stopped at 15:45 ET and did not resume until 08:45 the next morning. Seventeen hours, spanning
three sessions Robinhood actually trades: after-hours 16:00–20:00, the overnight session
20:00–04:00 on eligible names, and pre-market 04:00–09:30. Two read-only runs now look into it:
**after-hours at 16:20 ET** (the closing print, the after-hours reaction, anything released after
the bell) and **pre-open at 07:00 ET** (the overnight gap, an hour and forty-five minutes before
the 08:45 decision slot acts).

### It alerts. It does not trade — and that is a fact about the broker, not a shortcut

`get_equity_tradability` carries `extended_hours_fractional_tradability` per symbol, and the
connector's own guidance is blunt: *fractional and dollar-based orders place only in
regular_hours regardless of the flags.* Every position in every desk book is fractional. So
outside 09:30–16:00 the manager could not sell most of what it holds **even in live mode**. A
watch that booked a simulated overnight exit would be logging a fill no real account could have
gotten — the exact flattery section 3 exists to prevent, in the session where liquidity is
thinnest and the flat 0.25% slippage assumption is most wrong.

So `watch.py` has **no write path to a paper book**, by construction. It is a separate program
rather than a slot inside `pm.py` precisely so that "the overnight run never mutates the book" is
enforced by structure rather than by a flag a future session can flip. It imports `pm.py`'s
`quotes_to_prices` — so overnight prices are parsed by the same audited code as every other run —
and `broker_divergence`, and nothing else. Do not give it one. If a stop breaks at 19:00, the
honest output is an alert saying so and saying whether a human could act on it; the book changes
at the next decision slot, at that slot's price.

Three tradability cases, all real answers from the connector today: **NVDA** trades 24 hours
*and* accepts fractional extended-hours orders — a hand exit is possible tonight. **MU** and
**SNDK** trade 24 hours but not fractionally — the earliest exit is 09:30. **XOM**, not currently
held, does not trade outside regular hours at all — a name like that gaps and there is nothing to
act on. A whole-share position escapes the fractional restriction; a fractional one does not, whatever the name's own 24-hour flag says. The watch
fetches this every run rather than assuming it, because it is the difference between an alert
that is actionable and one that is only information.

### What fires, and what deliberately does not

| Raised | Condition |
|---|---|
| STOP BREACHED | the extended print is at or through the position's stop |
| TARGET REACHED | at or through the 3R target — the scale-out still waits for a decision slot |
| moved | ±3% or more against the last decision-slot mark |
| BOOK n% | the desk is at or through the 3% kill-switch level against its last marks |
| LIVE ACCOUNT DIVERGENCE | same check as every other run, same rule: reported, never reconciled |

**A holding with no overnight print is not an alert and is not `UNPROTECTED`.** Most of the tape
does not trade at 19:00, and firing a red banner every night on every illiquid name is how a real
`UNPROTECTED` banner stops being read. "No print in this session" and "the manager cannot see this
position" are different statements and the watch keeps them apart. Quotes are read with a
240-minute age window rather than the decision-slot 30, because an overnight print is sparse by
nature; the age of every price used is reported.

A run with no alert and no warning is **QUIET** and writes nothing at all — no journal entry, no
book revision, no publish. Same discipline as the sentinel, for the same reason.

### Where it writes, and where it does not

`claude/pm-watch-journal.json`, its own file, keyed `<date>#<session>#<desk>`. Deliberately **not**
`claude/pm-journal.json`: that file is the decision-slot index of section 8b, `pm.py` re-sorts it
by `SLOT_ORDER` on every run, and an unknown slot would be sorted to the end of its day. Separate
file, no collision, and the archive index in section 8b stays exactly what it was. The watch
publishes no artifact either — the Trade Desk board is a snapshot of a book, and the watch changed
no book.

The 08:45 pre-market prompt now reads that journal and leads its report with whatever the overnight
flagged, because an alert nothing acts on is not risk management. The gap between the price the
watch saw and the price the 08:45 slot fills at **is the cost of the unwatched window**, and the
morning run is told to name it out loud rather than let it disappear into the fill.

### Honest limits

- **20:00–04:00 is still dark.** Two runs, not four. If the book starts holding names that move in
  the true overnight session, a 19:45 and an 04:15 run are the obvious next two — the machinery
  takes a `--session` argument and a cron.
- **Post-bell earnings moves develop after 16:20.** The watch catches the first reaction, not the
  settled one. It flags a held name reporting tonight, which is the part that matters most.
- **It cannot act, so on a bad night it is a notification and nothing else.** That is worth
  something — Vishal can close a position by hand where the name allows it — and it is worth
  strictly less than a resting stop at the broker, which remains the real fix if the account is
  ever funded past fractional sizing (section 5).

## 12d. Sentinel coverage — the heartbeat (COVER-01)

Section 12 says a sentinel that takes no decision and raises no warning is QUIET and writes
nothing at all: no book revision, no journal entry, no publish. That rule is right and it
stays — eleven quiet writes a day is how a real `UNPROTECTED` banner gets scrolled past.

The cost of it went unnoticed until 2026-09-02. **"This desk was checked and nothing fired"
and "this desk was never run" left byte-identical records: nothing at all.** The 09:47
sentinel that day left journal entries for swing (a CRDO stop) and momentum (an MDB fill) and
none for pullback, and working out which of the two had happened to pullback took reading the
engine source. Nothing outside a session transcript could answer it — so the daily health
check has never been able to verify that the sentinel actually covered all three desks, on
any day, and a sentinel silently failing to run one desk would look exactly like a calm hour.

### The heartbeat

Every `pm.py` run — quiet or not, sentinel or slot — now writes `pm_heartbeat<suffix>.json`
and prints a `COVERAGE` line. A few hundred bytes: timestamp, desk, slot, quiet flag and why,
position and working-order counts, decision and warning counts, and the revision of the book
**as stored** (a quiet run reports `based_on_revision`, because the revision `run()`
increments is never written).

The book and the journals stay exactly as untouched by a quiet run as they were before. The
heartbeat is proof of coverage and carries no book state and no decision content.

### `claude/pm-coverage.json`

The caller merges the run's heartbeats into one project doc — **one write per sentinel run,
not one per desk**, so seven small writes a day rather than zero or twenty-one:

```json
{
  "_what": "Proof that each desk was looked at, including on runs that wrote nothing else.",
  "days": {
    "2026-09-02": {
      "runs": [
        {"ts": "...", "slot": "sentinel",
         "desks": {"swing": {"quiet": true, "positions": 4, "warnings": 0},
                   "pullback": {"quiet": true, "positions": 3, "warnings": 0},
                   "momentum": {"quiet": false, "positions": 4, "decisions": 1}}}
      ]
    }
  },
  "keep_days": 10
}
```

The daily health check asserts against it: **seven sentinel runs, three desks each, on every
trading day.** A missing desk on a run is now a FAIL with a name attached instead of an
absence nobody can see.

## 13. Strategy desks — same engine, separate books

"More traders" done properly is not more agents; it is the same audited engine run under
different mandates against **separate** books, so a result can be attributed to a setup
class rather than to the blend. `claude/engine/desks.json` defines them:

| Desk | Mandate | Book / journal |
|---|---|---|
| `swing` | unfiltered — every candidate that clears the gates | `claude/paper-book.json`, `claude/pm-journal.json` (the Trade Desk board) |
| `pullback` | setups *Pullback in Uptrend* / *Early Recovery*, RSI 25–55 — buy the dip inside an uptrend | `claude/paper-book-pullback.json`, `claude/pm-journal-pullback.json` |
| `momentum` | setup *Momentum*, RSI 50–72 — trend continuation, not exhaustion | `claude/paper-book-momentum.json`, `claude/pm-journal-momentum.json` |
| `rotation` | **inactive template** (E26) — sector-ETF momentum, monthly, `time_catastrophe` | none — section 16 |
| `orb` | **inactive template** (E24) — opening-range breakout, intraday, `chandelier` k_init 0.1, flatten at close | none — section 16 |
| `options` | **inactive template** (E27, D-02) — XSP/SPY put credit spreads, paper, `kind: options` | none until activated — section 19 |

Every desk starts from the same $5,000 of paper capital, runs under the same paper lock,
account lock, risk rules (`portfolio.RULES`, overridable per desk in `desks.json` but not
overridden today), spread gate, macro gate and broker policy (section 4; overridable per
desk through `rules.broker_policy`), and is written back under the
same revision / `--check` protocol, and names its stop policy as `rules.stop_policy`
(section 16; every live desk is on `fixed_atr`). `pm.py --desk <name>` loads the desk, filters the scan
rows to its mandate (the number declined is journaled once, not listed — a pullback desk is
not "skipping" momentum names), and suffixes every output file and run id with the desk
name so three desks can run in one directory. Desks publish no boards; the weekly review
compares them and says which mandate is earning its keep, with n stated.

Adding a desk is a `desks.json` entry, two empty project docs (book and journal), and the
desk's name in the PM and sentinel prompts. It is not a new engine.

## 14. House exposure — the caps that see all three desks (HOUSE-01)

Section 13 gives each desk its own $5,000 book so results attribute cleanly. Section 11 then
admitted the cost, and it was the last line of that list nobody had acted on:

> Caps are enforced at sizing time, on one desk at a time. CAP-01 makes pending orders count
> within a desk; nothing counts exposure ACROSS the three desks, which trade the same scan
> universe and today hold the same handful of AI/semiconductor names in all three books.

That is not a theoretical gap. Measured on 2026-09-02 at 10:35 ET, across the three books:

| | across all desks | % of the $14,927 combined book |
|---|---|---|
| NVDA | $1,518 (swing + momentum) | 10.2% |
| SNDK | $1,205 (swing + pullback) | 8.1% |
| HOOD | $1,188 (swing + momentum) | 8.0% |
| MU | $1,041 (swing + pullback) | 7.0% |
| **Information Technology** | **$5,077** | **34.0%** |

Six of the seven distinct names were Information Technology and four of them were
semiconductors. Every desk was inside its own 3-per-sector cap the whole time and no rule
anywhere could see the third of the combined book sitting in one sector. Three desks at a cap
of three is a cap of nine.

### What the engine now does

`pm.py` auto-discovers the other desks' books in `$SCAN_DIR` from `desks.json` — no new CLI
flag, deliberately, because a flag the four task prompts each have to remember is a flag one
of them will be missing. It then computes combined exposure per symbol and per GICS sector,
counting **positions and working buy orders alike** (committed capital is not free capital —
the same argument CAP-01 made inside one book), and applies two caps:

| `PM_RULES` | Default | Meaning |
|---|---|---|
| `house_max_symbol_pct` | **15%** | one ticker across every book, as a % of combined equity |
| `house_max_sector_pct` | **40%** | one GICS sector across every book |
| `house_caps_enabled` | `true` | `--no-house-caps` measures and reports without refusing |

**These two numbers are a risk-policy choice, not a derivation, and they are Vishal's to
change.** 15% mirrors the existing per-desk single-name cap applied one level up. 40% was
picked so that today's 34% Information Technology does not retroactively freeze a book that
was built under the old rules, while still binding before the combined book becomes a
one-sector bet. Change them in `PM_RULES` in `pm.py`.

### Where it sits, and what it deliberately does not do

The house gate runs **last**, after `portfolio.py` has approved a trade for this desk in
isolation. It can only ever refuse; it never resizes and never allows something the per-desk
rules blocked. It lives in `pm.py` rather than `portfolio.py` because that module mirrors the
repo's per-book rules and stays byte-identical — the house is a Portfolio Manager concept,
and the manager sanitises its own input rather than forking the rules. Same doctrine as
CAP-01.

**Preventive, not corrective**, exactly like CAP-01 and `rebalance_pass`. It stops a new
entry pushing past a cap. It does not sell anything to get back under one. An existing house
breach is named in the journal as `HOUSE CONCENTRATION` and is a decision for Vishal.

Every run journals a `house` block — combined equity, desk count, per-symbol and per-sector
percentages, the caps in force and which peer books were loaded — so the weekly review can
read cross-desk exposure without reconstructing three books.

### An unmeasured house is not a safe one

If no peer book is staged in the run directory, the engine does **not** quietly fall back to
per-desk caps. It prints a NOTE to stderr, raises a warning on any decision slot, and the
journal's `house` block is `null`. The three books must all be staged before the engine runs
— the skill's step 1 already stages them, and this is now load-bearing rather than
convenient.

## 14b. House exposure — how many bets is the house really making (K-03, added 2026-09-10)

Section 14's two caps answer one question each: is any single name over 15%, is any single
sector over 40%. They cannot tell a combined book of eight names that all move together from
eight independent bets, they cannot see that every desk is long the same factor, and they
cannot say how much of the house is one position dressed as seven. `engine/house.py`
measures those things, once per run, on the same combined book (positions **and** working
buy orders across every desk) that HOUSE-01 tallies. It is pure: holdings and equity in, a
dict out; nothing in it reads a file, mutates a book or touches an order.

### The metrics

All weights are fractions of **combined** equity, summed per symbol across desks.

| Field | Definition | Without `bars.json` |
|---|---|---|
| `n_eff` | effective number of independent bets, **1 / (w̃ᵀ ρ w̃)**, with w̃ the invested weights renormalised to sum to 1 (cash is not a bet: a single 10% position is one bet, not a hundred). With ρ = I this is exactly the Herfindahl count **1 / Σ w̃²**, reported alongside as `n_eff_weights`. Eight equal names → 8; eight equal names at ρ̄ = 0.6 → ≈ 1.5: eight tickers, one and a half bets. | **sector proxy** — ρ = 1 inside a GICS sector, `rho_default` (0.3) across sectors. Harsher than a measured ρ by construction (six semiconductor names count as one bet) and labelled `proxy` everywhere it is shown; `measured` when bars are staged. A name with no bars keeps the proxy value pair by pair. |
| `beta_w` | **Σ wᵢ βᵢ** — beta-weighted exposure as a fraction of combined equity (cash carries β 0). β from 252 daily returns against the benchmark in the bars file (`SPY`, which must be in the same `get_equity_historicals` call). Names without a β are left out of the sum and `beta_coverage_pct` says how much of the book was measured — they are **not** counted as β 0. | `null`, never 0. An unmeasured beta is not a low one. |
| `momentum_crowd_pct` | share of combined equity in names whose 12-1 month return (close 21 bars ago over close 252 bars ago, minus one) is over **+50%** — the momentum-crowding measure. | `null`. |
| `largest_sector`, `largest_sector_pct` | the biggest GICS sector as a share of combined equity — HOUSE-01's own number, restated so one block carries the whole picture. | measured. |
| `top_symbol`, `top_symbol_pct` | the biggest single name as a share of combined equity. | measured. |
| `overlap_pct`, `overlap_equity_pct` | share of **distinct** names held by two or more desks, and the share of combined equity sitting in those names. A desk holding a name and a working buy on it counts once for that desk. **The desk-overlap meter on the mirror reads these two fields.** | measured. |

`bars.json` is the same file `technicals.py` reads — the raw `get_equity_historicals`
payload, or its `results` array — staged in `$SCAN_DIR` by the caller when a slot fetched
bars (`--bars` names it; default `bars.json`). It is optional and usually absent: the
sentinel and most slots stage none, and the block says `"bars": "absent"` and which names
had none. If a future `technicals.features()` exposes `beta_252` / `ret_12_1` it is used
(imported lazily, any failure ignored); otherwise `house.py` derives both from the closes.

### Thresholds, and what they do

| `PM_RULES["house_exposure"]` | Default | Flag when |
|---|---|---|
| `min_n_eff` | **3.0** | `n_eff` below it |
| `max_beta_w` | **0.8** | `beta_w` above it |
| `max_momentum_crowd_pct` | **60** | `momentum_crowd_pct` above it |
| `rho_default` | **0.3** | the cross-sector ρ the proxy assumes |
| `enforce` | **`false`** | — |

A metric that is `null` never flags. **With `enforce` off — the default — the block is
reported and gates nothing**: it is written to `state["house"]["exposure"]`, to the
journal's `house.exposure` (the compact summary), to every desk's heartbeat and from there
onto the coverage row (`exposure`, additive — a reader that ignores it loses nothing), and
to the console as one line:

```
house: n_eff 1.3 (proxy) · β·w n/a · sector 34% IT · overlap 57%   FLAGS n_eff  [reported]
```

A reported-only flag raises **no** journal warning, on purpose: `archive.should_publish`
treats any warning as a reason to publish a board, and a standing N_eff flag would publish
one every slot.

**With `enforce` on, a flagged breach refuses NEW ENTRIES house-wide** — every desk, since
the house is one book — and journals a `skipped` reason starting `house exposure
(enforced):` plus a `HOUSE EXPOSURE` warning. The gate sits in `entry_pass` after the
ladder check and **after** the exit pass has already run. Exits are never touched by
anything in `house.py` or by this gate, in either mode: a crowded house is a reason not to
add, never a reason not to sell. The sentinel reports the block and takes no entry decision
either way, so a quiet sentinel stays quiet.

### What the books score today

The three fixture books (the 2026-09-02 house, 7 names, $14,927, 42% invested), with no
bars: `n_eff_weights` 5.72, `n_eff` **1.27 (proxy)** — six of the seven names are
Information Technology and the proxy treats them as one bet — β·w n/a, momentum crowd n/a,
IT 34.0%, NVDA 10.2%, overlap 57% of names (HOOD, MU, NVDA, SNDK) holding 33% of equity.
That flags `n_eff` against the 3.0 floor and, with `enforce` off, refuses nothing. The floor
is a risk-policy choice and it is Vishal's to change; the proxy is a lower bound on the real
N_eff, and staging bars turns it into a measurement.

---

## 14c. VaR and stress — what the desk stands to lose (K-04, added 2026-09-10)

Section 7's kill switch and the K-02 ladder react to a loss after it has happened; section
14b says how crowded the house is. Neither says how much THIS desk's book stands to lose on
an ordinary bad day, or what it would have done through the sessions this decade that broke
the most books. `engine/var.py` answers both, once per run, for one desk's marked positions
(working buys are not exposure until they fill). It is pure — weights and per-symbol daily
returns in, a dict out — and nothing in it reads a file, mutates a book or touches an order.

### The numbers

Weights are position market value over **desk** equity, so cash carries a return of 0 and a
40%-invested book's VaR is a share of desk equity, not of the invested slice.

| Field | Definition | Without `bars.json` |
|---|---|---|
| `var.var_pct` | **1-day 99% historical-simulation VaR**: over the dates every held name shares (newest 504 at most — two trading years), the portfolio return Σ wᵢ rᵢ is sorted and the loss at the k-th worst observation, k = ⌊(1−α)·n⌋ (at least 1), is the VaR. 500 days at α = 0.99 is the 5th-worst day. Positive percent of desk equity. **Null with a reason under 120 overlapping days**, or when no held name has bars, or when the book is flat — an unmeasured VaR is never 0. | `null`, reason `no bars.json staged this run` |
| `var.cvar_pct` | the mean loss of those k worst observations. Never below the VaR. | `null` |
| `var.n_days`, `var.window`, `var.coverage_pct`, `var.uncovered` | how many shared days, their span, what share of the invested weight had bars, and which names had none (left out of the series, named, never counted as 0). | 0 / null |
| `stress[<window>].pnl_pct` | the book's cumulative P&L, % of desk equity, had each holding repeated its **own** return through the window (`coverage: "own"`). When the staged bars do not reach that far — a two-year file covers neither 2020 nor 2022 — the holding's contribution is **β₂₅₂ × the benchmark's window return**, from SPY's own bars when they cover it (`benchmark_basis: "bars"`) else the index return recorded in `var.BENCH_WINDOW_RET` (`"index"`), and the window is labelled `coverage: "proxy"`. A name with neither bars nor a beta is unmeasured and `measured_pct` says how much of the book was (`"partial"`); a name is never given β = 1. | `{}` |
| `worst_stress`, `worst_stress_window` | the most negative **long-side** window. | `null` |

The five windows, inclusive of the return dated on each bound:

| Window | What | Side |
|---|---|---|
| `2020-03-16` | the COVID crash's worst single session (S&P 500 −11.98%) | long |
| `2022` | 2022-01-03 → 2022-12-30 cumulative, the rate-shock bear (−19.4%) | long |
| `2024-08-05` | the yen-carry unwind session (−3.0%) | long |
| `2025-04-03/04` | the two tariff sessions, cumulative (−10.5%) | long |
| `2025-04-09` | the +9.5% tariff-pause squeeze | **short** — a long book gains; `short_pnl_pct` is the loss a short book (the options desk, later) would take. It never counts toward `worst_stress`. |

`bars.json` is the same file `technicals.py` and section 14b read — the raw
`get_equity_historicals` payload with SPY in the same call — staged in `$SCAN_DIR` when a
slot fetched bars. It is optional and usually absent: the sentinel and most slots stage
none, and the block says `"bars": "absent"`.

### Thresholds, and what they do

| `PM_RULES["var"]` | Default | Flag when |
|---|---|---|
| `alpha` | **0.99** | — |
| `max_var_pct_of_desk` | **3.0** | `var_pct` above it |
| `max_stress_multiple_of_halt` | **2.0** | `−worst_stress` above it × `halt_pct` (16% by default) |
| `halt_pct` | **8.0** | the ladder's rung-3 halt, restated here |
| `enforce` | **`false`** | — |

A null VaR or an unmeasured stress never flags. **With `enforce` off — the default — the
block is reported and gates nothing**: the full block goes to `state["risk"]`, the compact
summary to the journal's `risk` block, and `var_pct` / `worst_stress` /
`worst_stress_window` to every heartbeat (quiet or not) and from there onto the desk's
coverage row, all additive; the console prints one line:

```
risk: VaR99 1.84% (cvar 2.61%, 498d) · worst stress 2022 -9.4% (proxy)   FLAGS var  [reported]
risk: VaR n/a (no bars.json staged this run)
```

As with 14b, a reported-only flag raises **no** journal warning, so a standing flag does
not publish a board every slot.

**With `enforce` on, a flagged breach refuses NEW ENTRIES on that desk** and journals a
`skipped` reason starting `VaR / stress (enforced):` plus a `VAR / STRESS` warning
(`report.py` files it under `var_stress`). The gate sits in `entry_pass` after the K-03
house-exposure check and **after** the exit pass has already run. Exits are never touched
by anything in `var.py` or by this gate, in either mode: a book that could lose too much is
a reason not to add, never a reason not to sell. The sentinel measures and reports the block
and takes no entry decision either way, so a quiet sentinel stays quiet.

### What the fixture book measures today

The swing fixture (four names, MU 7.6%, NVDA 8.8%, SCHW 11.2%, SNDK 10.2% of desk equity)
with no bars: everything null with the reason, nothing flagged, nothing refused. The 3%
cap and the 2× halt multiple are risk-policy choices and they are Vishal's to change;
staging a bars file that reaches back two years turns the April 2025 windows into
measurements of the names' own moves and leaves 2020 / 2022 / 2024-08-05 on the labelled
proxy until a longer file is staged.

---

## 15. The chain snapshot — the option chain as priced (S-01, added 2026-09-10)

When an option chain payload is staged in the run directory (`option_chains.json`,
`option_chain.json`, `options_chain.json`, `option_quotes.json`, or per-symbol
`option_chain-<SYM>.json`), `pm.py` writes, after pricing and the decision run:

    $SCAN_DIR/archive/chain_snapshot/<date>-<slot>.jsonl.gz

Line 1 is `{"_meta": {run_id, slot, date, as_of, engine_sha, kind, n_rows, n_symbols,
source_files, schema}}`. Then one line per contract — `symbol, expiry, dte, strike, type,
bid, ask, mid, last, volume, open_interest, iv, delta, gamma, theta, vega, spot` — for the four
nearest expiries and the ten strikes either side of spot per expiry and type; and one
`{"_derived": true}` line per symbol: `spot, expiries, nearest_expiry, nearest_dte,
expiry_30_45, atm_iv_nearest, atm_iv_30d, atm_iv_60d, skew25, cpiv, os_ratio, share_volume,
em_1sd, em_1sd_pct, straddle_price, straddle_pct`. The derived row is computed from the
whole chain the payload carried, before the trim. Definitions: 30/60-day ATM IV are linear in
DTE between the bracketing expiries (null when not bracketed — no extrapolation); `skew25` is
(IV at put Δ −0.25 − IV at call Δ +0.25) / ATM IV at the first expiry with 30–45 DTE; `cpiv` is
the open-interest-weighted mean of (IV_call − IV_put) over strikes carrying both legs at that
expiry; `os_ratio` is Σ contract volume × 100 / share volume; `em_1sd` is spot × ATM IV ×
√(DTE/365) at the nearest expiry with at least one session to run, and `straddle_price` is ATM
call mid + ATM put mid there. Anything not computable is null, never 0. Spot falls back to the
staged quote, then the scan price; share volume comes from the scan_data candidate.

With no chain file staged the manager writes nothing. `get_option_quotes` still returns 403
(SCAN.md §9.1), so today this is the reader waiting for a route; the CLI
`python3 snapshots.py --run-dir . --out archive --chain --slot <slot> --as-of <iso>` writes the
header-only file (n_rows 0) so an absence is on the record. Non-fatal by rule: a failure is
recorded on `pm_state.json` as `chain_snapshot.error`, **never** on the journal or the book —
the book the runner writes stays byte-identical to a direct run. Each desk of a slot writes the
same file; the content is the same and the last writer wins.

Why: IV rank needs a history of ATM IV — after ~60 sessions of `atm_iv_30d` it becomes
computable per name, and nothing else in the system records it. E10/E17 read `skew25`, `cpiv`
and `os_ratio` at the slot; the slot-event simulator needs the straddle and `em_1sd` the
market was pricing at the moment the manager decided.

## 16. Stops by desk type (K-07, added 2026-09-10)

Until K-07 one stop rule served every desk: `portfolio.derive_levels()` — 1.5× ATR below
entry, clamped into the 3–12% band, a 3R target, half off at the target and the stop to
breakeven (section 10). That is a swing stop, and it is the right one for the swing book.
It is the wrong instrument for the other mandates, and the literature is specific about why:

- **Kaminski & Lo (2014), "When do stop-loss rules stop losses?"** — a stop-loss adds value
  when returns carry momentum or switch regimes, because the loss it realises is the start of
  a run rather than noise; on a random walk it subtracts value (it sells at a price with no
  information in it and pays the round trip), and on a **mean-reverting** series it is
  actively harmful, because it sells precisely into the reversal the strategy was built to
  hold through.
- **Han, Zhou & Zhu (2016), "A trend factor / Taming momentum crashes"** — on the momentum
  portfolio a 10% stop-loss cut the worst monthly loss from ≈ −50% to ≈ −11% and roughly
  doubled the Sharpe ratio, mostly by stepping out of the crashes. And across their tests a
  **wide stop with a smaller position dominates a tight stop with a larger one**: the
  tight stop gets hit by noise and pays the whipsaw; the wide one only fires on the real
  move, and the smaller size holds the dollar risk equal.

So there are now three policies in `engine/stops.py`, selected per desk by `rules.stop_policy`
in `desks.json` with parameters in `rules.stop_params`:

| Policy | Initial stop | Ongoing | Target | Time stop | For |
|---|---|---|---|---|---|
| `fixed_atr` (default) | 1.5× ATR, clamped 3–12% (`derive_levels`) | breakeven after the scale-out | 3R, half off | none | swing — **unchanged**, byte-for-byte |
| `chandelier` | entry − `k_init`×ATR14 (2.5) | trail = highest close since entry − `k_trail`×ATR14 (3.0), **ratchets up, never down** | none by default (`target_r` optional) — rank and trend exits do that job | `max_sessions` (40): closed if it has not made 1R by then | momentum, trend swing |
| `time_catastrophe` | entry − `k_cat`×ATR14 (3.5) — a catastrophe stop, nothing tighter | never moves | `target_pct` (4.0%) optional, whole position | `max_sessions` (6 for mean reversion, ~25 for rotation), unconditional | mean reversion, sector rotation |

**What every desk trades today is unchanged.** `swing`, `pullback` and `momentum` are all on
`fixed_atr`, and `tests/test_stops.py` proves it the same way K-06 did: a three-run sequence
(entry placed and filled, a stop, a 3R scale-out with the breakeven move, a session roll) was
frozen from the untouched engine (`tests/fixtures/pm_golden_prechange_stops.json`, written
from commit 3a72721 by `tests/stops_sequence.py` *before* pm.py was touched), and the engine
must still write those bytes plus only the new bookkeeping keys. The `momentum` desk's
`_note` records that E-K07 proposes `chandelier` there once the harness shows it on that
book's own trades; `pullback` is being retired — a mean-reversion mandate under a 1.5× ATR
stop is exactly the Kaminski–Lo failure — and stays on `fixed_atr` until it is closed out.

### How the engine applies a policy

- **At entry** (`entry_pass`), the desk's policy sets the initial stop and target. Under
  `fixed_atr` the proposal's own levels are the policy — `build_proposals` already called
  `derive_levels` — and nothing moves. Under any other policy the stop is re-derived and the
  position is **re-sized to the same dollar risk on the new distance**: a wider stop means
  fewer shares, never more risk, capped at the position cap and the cash left this run. The
  unscaled figure stays on the order (`meta.unscaled_shares`) with `meta.stop_policy`,
  `meta.initial_risk` and `meta.stop_params`.
- **Every run** (`exit_pass`), before the thesis checks, the position's policy `update()` is
  called with the slot price and the sessions held. A stop it raises is applied and journaled
  as a `raise-stop` decision ("stop raised from X to Y"); a stop is **never lowered** —
  `stops.apply_update` enforces that for every policy, so no policy can lower one by
  accident. An exit it calls is taken ahead of the thesis checks, exactly where the stop test
  sits today, and mapped onto the journal's words: `stop` and `trail` both book as a `stop`
  (the detail says "trailing stop" for a ratcheted one), `target` books as a `target` that
  closes the **whole** position, `time` is its own word. The scale-out and the breakeven move
  run under `fixed_atr` only.
- **Every position records** `stop_policy`, `initial_risk` (entry − initial stop, per
  share) and `initial_risk_usd`, `sessions_held` (weekday sessions since `opened`; the entry
  day is 0), `highest_close` and `trail_level`. A position opened before K-07 is recorded as
  `fixed_atr` — the rule it was sized under — on its next visit. `highest_close` is the
  highest price the book has *seen* since entry; the book observes four prices a day, not
  closes, and the field does not pretend otherwise.
- A position keeps the policy it was **opened** under. Changing a desk's `stop_policy` applies
  to new entries; the open book is not re-stopped underneath itself.

### Inactive desk templates

`desks.json` now also carries two desks with a top-level `"inactive": true`: `rotation`
(E26 — sector-ETF momentum, monthly, `time_catastrophe` with `max_sessions` 25 and `k_cat`
3.5, a `universe: sector-etfs` filter that routes its candidates to `engine/rotation.py`
instead of the scan — section 18) and `orb` (E24 — opening-range breakout, `chandelier`
with `k_init` 0.1 for the paper's 10%-of-ATR stop, `flatten_at_close` so nothing is held
overnight, `intraday_margin` because every trade is a day trade). They are mandates written
down with their exits so they can be wired later, not desks that trade: `pm.py --desk
rotation` exits 2 with the reason unless `--allow-inactive` is passed, **no book is
created** for them, and the peer loader, the paper mirror and the runner's peer staging all
skip them. Activating one is: set `inactive` to false, seed its book and the two project
docs, add the desk to `runner/slots.json` (the rotation desk's exact steps are in section
18). The `orb` template's wiring — its entries, fills, trail and flatten — is built (D-03)
and described in section 20; it still ships inactive.

## 17. Live guardrails — doctrine, validators, and the paper-mode audit (K-07)

Section 1 still governs: **live mode is not implemented** and this section does not change
that. What it adds is the set of controls that would have to stand between the manager and a
real order before anyone flips the flag, written as pure validators in `engine/guardrails.py`
with tests, so the go-live conversation is a review of a tested module rather than a design
session.

The reference frame is **Knight Capital, 1 August 2012**: a deployment left a retired test
routine live on one server, it sent roughly four million orders in forty-five minutes, the
firm lost $460m and was gone within the week. The regulatory response every broker-dealer
already operates under is **SEC Rule 15c3-5** (the Market Access Rule: pre-trade credit and
capital thresholds, erroneous-order and duplicate-order checks, and risk controls the firm
itself must own and cannot outsource) and **FINRA Regulatory Notice 15-09** (algorithmic
trading: kill switches, pre-deployment testing, change management, and the point that the
controls belong to the firm, not the vendor). The Agentic account is a retail account and
none of this binds it — but the reasoning is exactly right for a book an LLM session drives,
and the numbers are set to *this* book's size.

`PM_RULES["live_guardrails"]` (a desk's `pm_rules` may override the block):

| Block | Control | Value | Validator |
|---|---|---|---|
| `two_key` | environment flag | `AI_TRADING_LIVE` | `live_mode_allowed(env, path, now)` |
| | signed config | `live.signed.json` — `{body, sig}`, HMAC-SHA256 over the canonical body under `AI_TRADING_LIVE_KEY`, `expires_at` in the future, `issued_at` no older than `max_age_hours` 24 | |
| `per_order` | max notional | $750 | `check_order(order, ctx)` |
| | max distance from last print | 1.0% | |
| | max quote age | 60 s | |
| | max spread | 1.0% of price | |
| `per_day` | max orders per desk | 12 | `check_day(book, ctx)` |
| | max orders per symbol | 2 | |
| | max notional sent | 2.0× equity | |
| `deny` | min price | $5.00 | `check_symbol(row, ctx)` |
| | min dollar ADV | $10m | |
| | leveraged ETFs | refused | |
| | IPO seasoning | 90 days | |
| | volume spike | > 10× 20-day ADV | |
| | unexplained move | > 30% with no earnings event | |
| `circuit` | VIX | ≥ 35 | `check_circuit(ctx)` |
| | SPY intraday | ≤ −3.0% | |

Every validator returns `(ok, reasons)`. **Both keys must turn**: the flag alone does
nothing, the signed config alone does nothing, and an expired, tampered, mis-keyed or stale
config is refused with a reason naming the first thing that failed. A check whose input is
missing does not pass silently: under `strict` (the live default) it refuses — a circuit
breaker that cannot read the tape is open, not closed — and under non-strict it is skipped
and reported.

### The paper-mode audit

The one thing the paper path does with this module: on every run, every sized proposal is put
through `check_order` (notional, distance from last, quote age, spread) and `check_symbol`
(the deny list), non-strict, and what they **would have refused live** is journaled as
`jrn["live_would_refuse"]` — `[{symbol, notional, reasons}]`, an empty list when nothing would
have been refused. It changes no decision. Its purpose is to put on the paper record, before
the guardrails ever bite, how often they would — a desk whose proposals are refused on the
$750 ceiling every day is telling you the live sizing has to differ from the paper sizing,
and that is better learned from a journal column than from a rejected ticket. On the test
fixture the one proposal (SCHW, $557.94) clears every default ceiling and the audit is empty;
`tests/test_stops.py` proves the same run journals the refusal when the ceiling is lowered
under it. `pm.py` never calls `live_mode_allowed`, `check_day` or `check_circuit` — a static
test pins that.

## 18. The rotation desk — sector ETF momentum, monthly (D-01 / E26, added 2026-09-10)

**Status: INACTIVE.** `desks.json` ships `rotation` with `"inactive": true`, no book exists,
and `pm.py --desk rotation` exits 2 unless `--allow-inactive` is passed. Everything below is
wired and tested (`tests/test_rotation.py`); nothing below trades until the activation steps
at the end are taken, and those should not be taken before the E26 harness in
`docs/BACKTEST.md` § "E26" has been run and read.

### The mandate

- **Universe.** The eleven SPDR sector ETFs — XLK XLF XLV XLY XLP XLE XLI XLB XLU XLRE XLC —
  plus VEU (ex-US equity, ranked alongside them as the world's twelfth sector), SPY (the
  absolute-momentum reference; never held) and TLT (the bond leg; held only when the filter
  is off). Fourteen symbols; `rotation.UNIVERSE`.
- **Signal.** 12-1 month return, `technicals.features()["ret_12_1"]` =
  close[t−21] / close[t−252] − 1 (Jegadeesh & Titman 1993; Faber 2010's sector rotation).
  A symbol with fewer than 253 bars is *unranked* — never a 0 that would rank it.
- **Rule.** Rank the twelve risk names on `ret_12_1`. When SPY's trailing 12-month return
  exceeds the 3-month T-bill return over the same window (Antonacci 2014, absolute
  momentum) hold the **top 3, equal weight**; otherwise hold **TLT only**. The bill rate is
  `tbill_3m_pct` in `macro.json`; absent, it defaults to **0** and the journal and every
  replay summary say so (`tbill_default_used`). An unmeasurable SPY (short history) is
  treated as filter OFF — an unmeasured market is not a bull market.
- **Cadence.** Decisions only at the **last power-hour slot of the calendar month** — the
  last trading day on a weekday calendar with the NYSE's rule-based holidays
  (`rotation.nyse_holidays`; the first holiday table in the engine) — plus
  `--force-rebalance`. A **weekly check** at the last power-hour of the week acts only if a
  held sector ETF has dropped below rank 6 (or out of the ranking) or the absolute filter has
  flipped against what the book holds. Every other slot only manages holdings by their stops;
  its journal carries the compact decision block (`jrn["rotation"]`) with `acts: false` and
  the next decision date.
- **Stops.** `time_catastrophe`, `k_cat` 3.5, `max_sessions` 25 (section 16). The rank IS
  the exit; the catastrophe stop and the time stop are the safety net. A holding the rank
  re-affirms at a decision gets `hold_from` reset to that date, so the 25-session clock
  counts from the last decision, not the original fill.
- **Sizing.** Equal **sleeves** of desk equity — a third each — times the desk vol scalar
  (`portfolio.desk_vol_scalar` on SPY's `rv_20d`) when `rules.vol_target.enabled` is on,
  capped at the cash left this run. The bond leg takes all three sleeves (Antonacci's rule;
  `rules.rotation.bond_sleeves` 1 keeps two in cash). The pipeline's risk-based figure stays
  on the order as `meta.unscaled_shares`; the sleeve replaces it.
- **House caps.** The ETFs are sector-level by construction (XLK *is* Information
  Technology), so they are **exempt from HOUSE-01's per-name and per-sector caps**
  (`house_exposure` leaves them out of `by_symbol` / `by_sector`; `entry_pass` never asks
  `house_block` about them) and **included in K-03's exposure metrics** (they stay in
  `house["holdings"]`, so the beta and N_eff see them).

### Inputs

| File | Shape | Used for |
|---|---|---|
| `bars_etf.json` | the Robinhood `get_equity_historicals` payload (or `runner/fetch_bars.py` output) for the 14 symbols, **≥ 13 months of daily bars** | the rank, the filter, the candidate rows (`rotation.proposals`), and the K-03 beta (folded into the house bars) |
| `macro.json` | `{"tbill_3m_pct": 4.1}` | the absolute filter's hurdle; absent = 0, logged |
| `pm_quotes.json` | the usual broker quotes, **including the 14 ETFs** | the only price an entry or a rotation-exit may trade on — a bar close values, never trades (section 6), and the entry pass refuses an ETF without a fresh quote |

`rotation.proposals()` produces candidate rows in the exact shape `entry_pass` consumes
(`setup` "Sector Rotation", `verdict` "Buy", `coverage_pct` 100, `_source` "rotation") and
`pm.rotation_scan()` wraps them as this run's own scan, so the desk goes through the same
sizing, spread, drift, broker-policy and guardrail gates as every other desk. Its filter
`{"universe": "sector-etfs"}` admits only rows the rotation module produced — a
`scan_results.json` row can never reach this desk's sizing, and a scan row that claims to
be one is just a row to the swing desk (`tests/test_rotation.py` pins both).

### What a decision run writes

- Entries are placed as **marketable next-session orders** (`expires: "next-session"`,
  `fill_rule: "marketable"`): decided at 15:45, they survive exactly one session roll and
  fill at the next slot's quote plus the exit-slippage assumption, whatever the limit — the
  limit stays on the order as the decision price. A second roll expires them. The
  power-hour no-entry rule (section 2) is right for a day-limit and wrong for a monthly
  rotation; this is the one exception and it is gated on the desk's decision block.
- Holdings that left the target set are sold whole at the slot price less slippage,
  journaled **`rotation-exit`** with the rank that dropped them or the filter that flipped.
- Re-affirmed holdings are journaled as skipped ("re-affirmed in the target set — held,
  time stop restarts today").
- The journal entry and the state carry the compact decision block: `acts`, `mode`
  (`monthly` | `weekly` | `forced`), `why`, `filter_on`, `tbill_3m_pct`,
  `tbill_default_used`, `ranks`, `target`, `held`, `exits`, `affirmed`, `entries`,
  `sleeves_by_symbol`, `vol_scalar`, `next_rebalance`, `notes`. The bill-rate default and a
  short history are a **warning** on the run that acts and a journal line otherwise.

```bash
# a dry run against a staged book, any day, without activating the desk
python3 engine/rotation.py --bars bars_etf.json --macro macro.json --held XLK,XLE --as-of 2026-09-30
python3 engine/pm.py --allow-inactive --desk rotation --slot power-hour --now 2026-09-30T19:45:00Z \
    --quotes pm_quotes.json --bars-etf bars_etf.json --macro macro.json [--force-rebalance]
```

### Activation — in this order

1. **Read the harness.** `python3 engine/backtest.py --desk rotation --bars bars_etf.json
   --start 2016-01-01 --end <yesterday> --tbill tbill.json --corr-with <swing paper book>
   --ledger --summary rotation.json` (docs/BACKTEST.md § E26). The plan's acceptance is
   rho < 0.6 against the swing desk's curve and a drawdown the house can carry; a re-run with
   other parameters is another ledger trial.
2. **`engine/desks.json`**: set `rotation.inactive` to `false`.
3. **Seed the book**: `python3 engine/rotation.py --seed-book > <state repo>/books/rotation.json`
   (the same $5,000 every desk starts from), and create the two project docs
   `claude/paper-book-rotation.json` and `claude/pm-journal-rotation.json` (the mirror's
   targets, `doc_book` / `doc_journal` in desks.json).
4. **`runner/slots.json`**: add `"rotation"` to the top-level `desks` list and to the
   `desks` of `power-hour` (the decision slot) and `sentinel` (stop management); adding it
   to the other three slots is optional — the desk only manages holdings there. List
   `bars_etf.json` and `macro.json` under `inputs.pm.optional` for the record.
5. **Stage the inputs** with every power-hour run: `bars_etf.json` (the 14 symbols, ≥ 13
   months) and `macro.json` in the input manifest, and the 14 ETFs in `pm_quotes.json`. A
   run without `bars_etf.json` takes no decision and says so on stderr and in the journal;
   holdings are still managed by their stops.
6. The first decision is the next last-power-hour-of-the-month, or `--force-rebalance` once.
## 19. The paper options desk — XSP/SPY put credit spreads (E27, D-02, added 2026-09-10)

`engine/options_desk.py` is the fourth desk and the first that is not the equity engine. It
is **paper only, by construction**: the module imports nothing that can reach a broker
(`tests/test_options_desk.py` pins its import list), it writes no order file, and `pm.py
--desk options` runs it under exactly the protocol every other desk gets — revision,
`--check`, heartbeat, journal merge, `pm_book_next-options.json` / `pm_state-options.json`.
`desks.json` carries it as `"kind": "options"` with `"inactive": true`: `pm.py` refuses it
without `--allow-inactive`, no book is created for it, and the peer loader, the paper mirror
and the runner's peer staging skip it. The equity engine (`pm.run`) and the three equity desks
are untouched.

### The mandate (plan §6, Appendix H §2 / §7) — as implemented

| Rule | Implementation |
|---|---|
| Structure | XSP put credit spread; SPY when XSP has no usable chain. 30–45 DTE, the expiry nearest 40; short strike nearest 20Δ (chain delta, else Black–Scholes at the row's IV), ties to the lower strike; long strike = short − width |
| Width | **$5 is the maximum.** A 20Δ short collects less than 20% of the width as credit — always, at every IV — so a $5-wide risks over $400 per contract and the mandate's two numbers cannot both hold. The risk cap is the risk rule; the width is a structure parameter: the desk takes the widest of $5, $4, $3, $2 whose (width − credit) × 100 fits the cap, and journals the narrowing (`rules.width_fallback`; off, it does not trade under the cap) |
| Risk per structure | ≤ 8% of desk equity ($400 on $5k) = (width − credit) × 100 × contracts; contracts = ⌊cap / loss per contract⌋, at least 1 or no trade. The ladder's `entry_size_mult` scales the cap |
| Total risk | Σ max loss of open structures ≤ 40% of desk equity |
| Concurrency | ≤ 5 structures; ≤ 2 per sector (index underlyings are the `Index` sector); never on an underlying a stock desk holds or has a working buy on — SPY, XSP and SPX are aliases of one exposure for that test |
| Exits | 50% of max profit (debit to close ≤ half the credit) or 21 DTE, whichever first; **defensive close** when spot < short strike; a structure that reaches expiry anyway is cash-settled at intrinsic |
| Regime gates | no new short vol when VIX > VIX3M, VIX > 30, or SPY GEX < 0. GEX = Σ gamma × OI × 100 × spot² × 1% (calls +, puts −) from the chain snapshot; **no gamma/OI in the chain → the gate is skipped and the skip journaled**. No `vix.json` → the gate fails: an unmeasured regime is not a benign one |
| Portfolio limits | β-weighted delta (Σ net Δ × 100 × contracts × spot × β × 1%) within ±0.5% of **house NAV** (this desk + every peer stock desk's cash and marked positions; the desk alone when no peer is staged) per 1% SPY move; net short vega ≤ 0.5% of desk equity per vol point; \|net theta\| ≤ 0.3% of desk equity per day |
| Paper fill | net credit = (short mid − long mid) − $0.02 per leg; a leg with bid = 0 or (ask − bid)/mid > 10% is refused at entry. A close is **never** refused for width — protection does not sit unfilled — the width is journaled and the fill goes through at mid ± $0.02/leg |
| Mark | every slot from the chain mids; a leg the chain does not carry is marked by Black–Scholes at its last IV (`value_source: model`) and the journal says so; no IV and no spot → carried at the last mark and reported UNPRICED |
| Stress | weekly (first decision slot ≥ 7 days after the last): instantaneous shocks repriced leg by leg by Black–Scholes, S′ = S(1 + shift), σ′ = max(σ + shift, floor), T unchanged. Stored on `book["stress"]` and the journal; a scenario costing more than half the total-risk cap raises a warning |
| Ladder / kill switch | K-02 unchanged: rung 1 halves the per-structure cap, rung 2 blocks entries, rung 3 **flattens every structure at its mark** and cools off; the −3% daily kill blocks entries. Exits stay live at every rung |
| Broker policy | a credit spread is defined-risk: under `intraday_margin` (and `legacy_pdt`) the maintenance requirement **is** the max loss; under `cash_settled` the full width is reserved. `margin_state()` reports requirement and projected deficit; an entry that would leave the requirement over equity is refused. The 40% cap binds first |
| Shadow fills | **not applicable** — the paper fill (mid less slippage, plus fees, both ways) is the conservative model. `jrn["shadow"].applicable` is false |
| House exposure | when a stock desk runs with the options book staged as a peer, each open structure enters HOUSE-01's tally as its **beta-weighted-delta equity equivalent**: \|net Δ\| × 100 × contracts × spot × β, under the underlying's symbol and the `Index` sector, `kind: options-delta` (`house.options_holdings`). A short put spread is a hidden long and is counted as one; its cash (less the debit to close) is house equity |

### Fees (`rules.fees`, charged per contract per leg, on the open **and** the close)

| | Regulatory pass-through | Index-option fee | Commission |
|---|---|---|---|
| XSP (and SPX, NDX, RUT, VIX, DJX) | $0.04 | $0.35 | $0 |
| SPY (equity options) | $0.04 | — | $0 |

A one-contract XSP spread costs $0.78 to open and $0.78 to close; SPY $0.08 each way. The
$0.35 is Robinhood's index-option contract fee as researched on 2026-09-10 (Appendix H §5);
the regulatory line is the OCC/ORF/FINRA order of magnitude. Fees accumulate on the
structure (`fees`) and the book (`fees_paid`) and are inside every P&L number.

### Black–Scholes, in the standard library

`bs_price(S, K, T, r, sigma, put=True)` is the European price with T in years;
`bs_greeks()` returns delta, gamma, **theta per day** and **vega per vol point**;
`implied_vol()` is a bisection on [1e-4, 5.0] and returns None outside the no-arbitrage
band. `r` is `rules.risk_free` (4%). XSP is European and cash-settled, so the model is
exact in kind; SPY is American and the early-exercise premium on a 20Δ put is ignored. The
stress scenarios, the delta derivation when a chain row has no delta, and the mark of an
unquoted leg all go through these three functions and nothing else.

### What a slot needs staged

| File | Content | Without it |
|---|---|---|
| `option_chains.json` | any shape `snapshots.py` accepts (Robinhood rows, a Cboe payload, `{SYMBOL: {...}}`) with **puts for XSP and/or SPY at three or more expiries**, carrying bid, ask, `implied_volatility` and — for the delta pick and the GEX gate — delta, gamma, open_interest. Spot from `underlying_price` / `current_price` on the payload | structures are marked by model and no entry is possible |
| `vix.json` | `{"vix": 17.4, "vix3m": 19.2, "as_of": "2026-09-10"}` — from the Cboe CSVs in `docs/DATA.md` §1c (any key casing; `{close, date}` objects or `[{date, close}]` rows per index are accepted) | no new short vol (the gate fails closed) |
| `pm_quotes.json` | optional; the SPY spot fallback when the chain carries no spot | XSP only from the chain |
| the stock desks' books | `pm.load_peers` stages them; held underlyings are excluded, their equity is the house NAV | the desk's own equity is the NAV; nothing is excluded |

**Robinhood's XSP chain — assumed, not verified.** `get_option_quotes` still returns 403 on
this account and `get_option_chains` has not been called for an index root, so whether
XSP appears in it at all, under what `chain_symbol`, with what strike increments (the
reader assumes 1-point strikes near the money) and whether the rows carry greeks and open
interest are all assumptions the first staged chain will settle. The reader tolerates
every one of them being wrong: no delta → derived from IV; no IV → implied from the mid;
no gamma/OI → GEX gate skipped; no XSP → SPY.

### Activating it

1. Remove `inactive` from the desk in `desks.json`.
2. Seed the book: `python3 engine/options_desk.py --init-book paper_book_options.json`
   ($5,000, no structures) and `project_write` it to `claude/paper-book-options.json`; an
   empty `{"entries": []}` to `claude/pm-journal-options.json`.
3. Add `options` to `runner/slots.json` `desks` (and to the PM slots' desk lists), and have
   the scheduled task stage `option_chains.json` and `vix.json` with every PM slot.
4. The coverage row then carries the desk like any other; the heartbeat's `positions` count
   is the number of open structures.

The dead-man's switch (K-05) stamps no stop on a spread — defined risk bounds it, and the
21-DTE / breach exits fire on the next slot the runner reaches.

### The 60-session paper trial and the review

The desk runs paper-forward for 60 sessions (there is no chain archive to replay yet; the
S-01 chain snapshot is being accumulated for exactly that harness). The review, against the
Cboe PUT index's risk profile (Ennis Knupp / Cboe: ~10.3%/yr at ~9.9% SD over 1986–2008,
losing less than the S&P in big-down months but still losing), asks: realised P&L after fees
per structure and per session; win rate and the ratio of 50%-profit exits to DTE, breach and
flatten exits; the worst stress result recorded each week against what the mark actually did
on the worst session; net vega and theta against their caps; how often each gate blocked an
entry; and whether the desk's daily P&L correlates with the equity desks' — the plan's own
framing is that a short-vol sleeve is a higher-Sharpe form of equity beta, **not**
diversification, and the house tally counts it as the hidden long it is. Keep, resize or
retire at the review; `n` is stated.

## 20. The ORB desk — stocks-in-play opening-range breakout (D-03, E24; added 2026-09-10)

**Status: built, tested, INACTIVE.** `desks.json` still carries `orb` with `"inactive":
true`; `pm.py --desk orb` exits 2 without `--allow-inactive`, no book exists, and no slot
runs it. What changed is that the engine now knows how to run it: `engine/orb.py` and the
D-03 branches in `pm.py`. Activation is a config change (below), not a code change.

### The mandate

Zarattini, Barbon & Aziz (2024) — the "stocks in play" opening-range breakout, Appendix E
§1a of the research synthesis, plan §6. Each morning:

1. **Rank** the scan universe plus the held names by **opening relative volume**: the volume
   of the 09:30–09:35 bar divided by the 14-session average of *that same* 5-minute bucket
   (`orb.opening_rvol`). A 09:35 bar the size of a session does not move the 09:30 baseline;
   fewer than 5 prior sessions with the bucket is no baseline at all.
2. **Filter**: opening-bar close > $5, 14-day ADV > 1M shares, ATR14 > $0.50. ATR14 and ADV14
   come from `bars.json` (daily, strictly before today) when it is staged, else from the scan
   row's `atr_14` / `avg_volume_20d`, and the row says which (`stats_source`). No stats
   anywhere: rejected, never sized on a guess.
3. **Top 20** by RVOL are the stocks in play (`orb.stocks_in_play`; `orb.screen` also returns
   every rejected name with its reason, and the journal carries all of them).
4. **Direction** from the first candle: close > open → long. Close < open → the paper goes
   **short; this account cannot**, so the name is skipped and journaled (`red opening candle
   — the paper shorts it; this account is long-only`). Doji → skipped.
5. **Entry** = a buy-stop one tick (`breakout_tick` $0.01) above the opening-range high,
   placed at the **09:35 sentinel and no other run** (`orb.in_entry_window`: 09:30–10:00
   ET), live until **10:30** and cancelled unfilled by the 10:35 sentinel.
6. **Stop** = entry − **0.10 × ATR14** (the paper's stop; `stop_policy: chandelier`,
   `k_init 0.1`). **Size** for 1% of desk equity at risk on that distance, whole shares
   (Robinhood takes no fractional stop orders), **capped at 25% of desk equity notional**
   and at the cash available; the cap is journaled when it binds (`ORB notional cap binds:
   1% risk sized 500 sh ($25,305); capped at 25% of equity = 24 sh …`). On a $5,000 book
   at a 10%-of-ATR stop the cap binds on nearly every name — the risk actually carried is
   then a fraction of 1%, and the journal line says exactly how much. At most **5 concurrent**
   names (open positions plus working stop-buys); the desk's `pm_rules` raises
   `max_new_entries_per_run` to 5 to match.
7. **Management** — every hourly sentinel walks the 5-minute bars since its last visit
   (`orb.manage_position`): a bar whose low reaches the stop is a hit, exited at
   min(stop, that bar's open) — a gap through the stop exits at the open — otherwise the
   highest 5-minute **high** ratchets a chandelier trail **0.5 × ATR14** below it, up only
   (Chande & Kroll's chandelier hangs from the highest high; inside one bar the low is
   tested before the high can raise the trail, because the order of prints within a bar is
   unknown). **`k_trail 0.5` is a desk-specific parameter**: the `stops.py` chandelier
   default is 3.0 × ATR under the highest close the book has seen, a multi-day momentum
   trail; a trade that lives six hours trails six times tighter. The print still counts — a quote at or under the stop fires it
   even when the bars said nothing — and with no bars staged the generic chandelier update
   runs on the quote as it does for any other desk.
8. **Exit at the close**: `flatten_at_close` at the power-hour slot closes every position
   (`time` / "Flatten at close"), nothing is held overnight, and `max_sessions 1` closes
   anything that somehow survived at the next session. The ORB desk's positions are never
   exited on a scan score: the exit pass does not consult the scan row for them, and the
   standard entry pass places nothing on this desk at any decision slot.
9. **Broker policy** `intraday_margin`: every trade is a day trade and nothing counts them
   (section 4); `day_trades_used` is reported and gates nothing.

### The evidence, and the long-only caveat

The paper reports the strategy on US equities 2016–2023 with the top-20 RVOL universe, the
10%-of-ATR stop and the close as the exit, **long and short**. The short leg is a large part
of that result: a universe selected on opening volume contains as many gap-downs as gap-ups,
and the red-candle names are exactly the ones this desk skips. **The expectation for the
long-only half is a weaker result than the paper's, not a reproduction of it**, and the
skipped red candles are journaled so the weekly review can count what the missing leg
would have traded. Nothing here has been measured on this system's data yet; the harness
(`orb.replay`, docs/BACKTEST.md E24) exists so that it can be, and its data caveat is stated
there: IEX-only 5-minute volume biases the RVOL rank, so the replay is a directional check.

### What the engine does — the D-03 wiring in `pm.py`

- **`DESK.rules.orb`** is the switch: every ORB branch is gated on `_orb_rules()`, which is
  None for `swing`, `pullback` and `momentum`. Their behaviour is byte-identical (the K-06
  and K-07 goldens still pass); `tests/test_orb.py` pins that none of the three carries an
  `orb` block.
- **Working orders of type `"stop-buy"`** carry `trigger_price` (and `limit_price` equal to
  it, so reserved cash, the house tally, the margin projection and the board all read them
  unchanged), `valid_from_et` 09:35, `cancel_after_et` 10:30, `meta.stop`, `meta.stop_policy
  chandelier`, `meta.stop_params` and `meta.orb` (rank, rvol, or_high, or_low, direction,
  adv14, atr14, whether the notional cap or cash bound the size, the unscaled shares).
  `simulate_fills` works them against the 5-minute bars (`orb.check_stop_buy`): the first
  completed bar from 09:35 whose **high reaches the trigger** fills at **max(trigger, that
  bar's open) + the shadow haircut** — `fill_k` (1.0) half-spreads of the quote when there is
  a two-sided one, `fill_half_spread_bps` (5) of price otherwise — never in the run that
  placed it, and never on the 09:30 bar. Unfilled past 10:30: cancelled. No bars staged: the
  quote stands in (a print at or through the trigger fills at the print plus the haircut)
  and the journal says so. The fill bar is recorded on the position (`orb_entry_bar`,
  `orb_managed_through`) so management walks only the bars after it.
- **The exit pass** for an ORB position with bars: `orb.manage_position` on the bars after
  `orb_managed_through`, the raise journaled as `raise-stop` ("orb: stop raised from 50.51
  to 51.70 — chandelier trail: 0.5x ATR (1.00) under the $52.20 highest 5-min high"), a hit
  booked as a `stop` at the bar's exit price less `exit_slippage_pct` with the detail saying
  "A 5-min low broke the … stop — exit at … (the open, gap through | the stop)".
  The anchor lives in `highest_high` on the position (the bars' highs, never the print);
  the generic `highest_close` is kept at least that high so a bar-less visit — the quote
  path through `stops.py` — trails from the same anchor and never lowers it.
- **The 09:35 sentinel** (`orb_entry_pass`) honours every gate a standard entry honours —
  the kill switch, the ladder (entries blocked, or the size multiplier), the broker policy's
  entry gate, the house caps and the house-exposure block — and journals every name that
  did not make it (`jrn["orb"]` carries the ranked list; `report.classify` maps the desk's
  refusals to the `orb` rule). A flat ORB book at 09:35 is **not** a quiet sentinel: that is
  its entry slot, and `main()` runs it.
- **No `bars_5m.json`**: the desk logs `no intraday bars — no ORB today (bars_5m.json was not
  staged)` as a skip and a warning, prints it, and enters nothing. Holdings are still managed
  on the quote.

### Inputs per slot, when the desk is active

| Slot | Staged by the session | Read by the ORB desk |
|---|---|---|
| Pre-market scan (08:00) | `scan_data.json`, `bars.json` (daily, a year, SPY included) — as today | the scan universe (`scans/latest.json` → `scan_results.json`, staged by the runner for every slot) and the daily bars for ATR14 / ADV14 |
| **09:35 sentinel** | `pm_quotes.json` for held + working names as today, **plus `bars_5m.json`** = raw `get_equity_historicals` at `interval "5minute"`, `bounds regular`, `start_time` 15 sessions back, for the scan universe + held names (10 symbols per call) | ranks, sizes, places the stop-buys |
| 10:35 – 15:35 sentinels | `pm_quotes.json` as today, plus `bars_5m.json` for the held and working names (today's bars suffice: `start_time` today 09:30 UTC-equivalent) | fills the stop-buys, cancels the unfilled at 10:35, trails, fires stops on 5-minute lows |
| Power-hour PM (15:45) | `pm_quotes.json` as today | `flatten_at_close` closes everything; the entry pass places nothing |
| Other decision slots | — | not scheduled for this desk; if run, they manage holdings and place nothing |

**Connector budget for a 20-name universe at 09:35**: 2 × `get_equity_historicals` (10
symbols per call, 15 sessions × 78 bars ≈ 1,170 bars per symbol) + 1 × `get_equity_quotes`
(20 per call) + `get_accounts` = **4 calls**. If the upstream bar cap refuses 15 sessions at
5 minutes for 10 symbols, split the range in two (the file accepts a list of responses):
6 calls. Every later sentinel is 1 historicals call (today only, ≤ 10 held + working names)
+ the quotes call.

### Activation

1. Remove `"inactive": true` from `desks.json` → `orb`.
2. Create `claude/paper-book-orb.json` (a fresh $5,000 book, `broker_policy
   intraday_margin`) and `claude/pm-journal-orb.json`; mirror them to
   `C:\ai-trading-state\books\orb.json` and `journals\orb.json`.
3. Add `"orb"` to `runner/slots.json` under the **sentinel** and **power-hour** desks only
   (`bars_5m.json` is already an optional sentinel input).
4. In `docs/runner/prompts/sentinel.md` the "WHEN THE ORB DESK IS ACTIVE" lines take effect
   — the 09:35 session stages `bars_5m.json` for the universe; later sentinels for the held
   and working names.
5. Run `orb.replay` on real 5-minute history first (BACKTEST.md E24) and read it before
   the desk trades a single paper dollar. The desk ships inactive because that has not
   been done.

