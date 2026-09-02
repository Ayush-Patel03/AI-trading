# Portfolio layer

Scan Desk **proposes**. It never places an order. Execution is manual in Robinhood.

## ACCOUNT LOCK — read this first

**Only ever touch the Robinhood account nicknamed "Agentic"** (a limited-margin individual
account; it is the single account where `agentic_allowed` is true). Get its account number from
`get_accounts` at run time and use only that one.

Vishal has several other Robinhood accounts, including a large one. **Ignore them permanently.**
Do not read them, do not report on them, do not aggregate them into equity or risk figures, and
do not mention their holdings — unless he explicitly names one in the current request. If a
question is ambiguous, it means the Agentic account.

This is a standing instruction, not a default. Do not "helpfully" widen the scope.

## The Robinhood connector — corrected 2026-08-31

The 2026-08-27 note here said no Robinhood MCP existed. **That is now out of date.** An official
Robinhood connector is attached, and it was verified working on 2026-08-31:

- `get_accounts` returns six accounts, **exactly one with `agentic_allowed=true`** — a
  limited-margin individual account. Its id, nickname and mask live in the private
  `claude/engine-config.json` and are deliberately not in this public repo. The account
  lock above is enforced by the connector itself, not just by this document.
- `get_portfolio` and `get_equity_positions` on that account returned $50.00 cash,
  $50.00 buying power, zero positions — matching `claude/portfolio.json` exactly.

Never resolve the account by nickname. Call `get_accounts` and take the one where the
connector marks it agentic-tradable; the other five are read-only to this system and are
out of scope permanently.

**The connector does expose order-placing tools.** Scan Desk still does not use them.
"Scan Desk proposes, the user executes" is Vishal's standing policy, not a technical
limitation — do not quietly upgrade a proposal into an order because the capability now
exists. If he ever wants that to change, he has to say so explicitly, and the risk gates
in this file become live controls rather than advisory ones.

Do NOT build an unofficial-API path (credential-scraping libraries). It violates Robinhood's
terms and the egress allowlist blocks the domain anyway. The alternative for real automation
remains the **trading-system repo's Alpaca path**, which already has execution, risk gates and
a kill switch.

## The contract

- **Broker is the source of truth** for shares, average cost and cash — read live from the
  Robinhood MCP each scan (`get_portfolio` and `get_equity_positions` on the Agentic account).
  Never invent a holding or a cash balance, and never carry a stale one forward silently.
- **Every proposal needs approval.** Output order tickets — shares, entry, stop, target, dollar
  risk — for the user to place. Never describe a proposal as executed, filled or placed.
- Ask for updated holdings whenever the last update is more than a few sessions old; a stale
  cash balance silently corrupts every position size.

## Fractional shares

Robinhood trades fractional; the trading-system repo floors to whole shares because Alpaca does
not. `calculate_position_size(..., fractional=True)` keeps the remainder, rounded to 6 decimals.
Leave this on — with a small account, whole-share sizing rounds every proposal to zero and the
system silently does nothing. There is a $1.00 minimum notional per order; anything under it is
a hard block.

**This bit the dashboard too.** Until 2026-08-31 the order tickets formatted share counts with
`%d`, so every fractional proposal rendered as **0 shares** while the sizing math underneath was
correct. Fixed in `render_portfolio.py::_shares()`. If you touch ticket rendering, keep the
fractional formatting — on a $50 account every real ticket is fractional.

Affordability is capped by **cash**, not equity. Sizing against total equity proposes orders
the account cannot fill once money is tied up in open positions; `calculate_position_size` takes
a `cash_available` argument and `build_proposals` decrements it as it fills the book.

## Sizing — mirrors the trading-system repo, do not invent alternatives

From `src/execution/position_sizing.py`:

```
shares = (equity × risk_pct / 100) / (entry − stop)      # then capped by affordability
```

Risk percent is conviction-scaled exactly as `calculate_conviction_position_size` does, between
`min_risk_per_trade_pct` 0.5% and `max_risk_per_trade_pct` 2.0%. Scan Desk score maps to the 0–2
conviction scale as `(score − 45) / 20`, so 45 → floor risk, 65 → mid, 85+ → full 2%.

Then multiply by a correlation adjustment. The repo computes this from return series
(`average_correlation_to_book`); this data path has no price history, so `portfolio.py` uses a
**sector/industry overlap proxy** and labels it as such on the dashboard. Never present the proxy
as a computed correlation.

## Risk gates — every one mirrors a rule already in the repo

| Gate | Value | Source |
|---|---|---|
| Max risk per trade | 2% | `profiles.py` |
| Max positions | 10 | `live.py` |
| Max single position | 15% of equity | `profiles.py` |
| Cumulative risk cap | 20% | `engine.py` |
| Max per GICS sector | 3 | `sectors.py` |
| Max deployed | 85% (15% cash reserve) | `live.py` |
| Daily loss halt | 3% | kill switch |
| Min score to propose | 60 | Scan Desk |

The 60-point floor assumes a **100-point evidence base**. Before coverage normalisation
(2026-08-31) a scan with a dead source scored every name 15-20 points low and this gate
silently blocked the entire book — it looked like "no setups today" rather than "no data
today". Scores are now normalised to the pillars that had data, so the floor means what it
says again. A row under 70% coverage is capped at Buy, which keeps thin evidence from
sizing a full-conviction position.

**Hard blocks vs soft trims.** A hard block (sector limit, price conflict, cumulative risk, zero
shares) stops the proposal. A soft trim (position cap, deployed cap) reduces size but the proposal
still stands — and **still consumes sector, risk and deployed budget**. Getting this wrong lets
five names through a three-per-sector limit; it was a real bug, do not reintroduce it.

A candidate whose price confidence is `conflict` is blocked outright. Never size a position on a
price two sources disagree about by more than 3%.

## Stops are ATR-based as of 2026-08-31

This section used to say stops were structural because "ATR needs OHLC bars this data path does
not have", and to switch the moment that changed. It has changed: the Robinhood connector returns
a year of daily bars for ten symbols in one call, and `technicals.py` computes Wilder ATR(14)
from them — verified against the connector's own ATR to eight decimal places.

`derive_levels()` now places the stop at **1.5× ATR below entry** (the repo uses 1.2×; 1.5× suits
a multi-day swing hold), clamped into a **3-12% band**: a quiet name must not get a stop inside
its own daily noise, and a wild one must not get a stop a third of the way down. When a 50-day or
200-day MA sits within 2% of the ATR stop, the structural level wins — it is the better place to
rest a stop, and the ticket says which basis was used.

Targets remain 3R. With no ATR available the old structural logic runs and the basis is labelled
`structure` with an explicit "no ATR available" note. **Always report the stop with its basis.**
An 11% ATR stop on a name like Ciena and a 4% stop on a calmer one are not interchangeable, and
the difference is the whole point of sizing against volatility.

## Holding review

Each scan re-judges every holding against the current scan: `exit` if the setup broke below the
200-day or the score fell under 45, `trim` if the score is 45–55 or price is above the analyst
target, `hold` otherwise, `unscanned` if the name was not in the universe. A holding you cannot
score is reported as unscanned, never assumed fine.
