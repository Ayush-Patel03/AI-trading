# Data — every feed the engine uses, and what each one cannot tell you

**Status:** started 2026-09-10 with the universe history (S-10). This is the registry the
implementation plan calls for: one row per feed, with where it comes from, how often it can
be read, how hard it can be hit, and — the column that matters for anything backtested —
whether a value can be dated to the moment it became knowable. `docs/scan-sources.md` and
`docs/COLLECTION.md` keep the operational detail (URLs, traps, staleness evidence); this file
is the index over them and the place a new feed gets a row before code reads it.

Two rules carry over from those files and apply to every row below:

1. **WebFetch is for pages, not datasets.** It summarises with a small model that sees a
   fragment of a large file and does not say so. Sums, counts, lookups inside a big file and
   "the top N of" a long list are not available on that path; the honest value is null.
2. **A value without a timestamp is undated, not current.** Label it. Barchart's breadth
   quotes and Cboe's daily statistics are both in this category.

---

## 1. Feed registry

Cadence is how often the engine reads it, not how often the source updates. "PIT" is
point-in-time: can a historical value be dated to when it became public? **no history** means
the source only ever serves *now*, so it cannot be backtested at all — the pillar it feeds is
absent from every replay (see `docs/BACKTEST.md` §1).

### Prices, bars, fundamentals — the Robinhood connector (primary)

| Feed | Tool / endpoint | Used by | Cadence | Limit | PIT handling |
|---|---|---|---|---|---|
| Live quotes, bid/ask, SIP prior close | `get_equity_quotes` | scan, PM, sentinel | every slot; PM hourly | 20 symbols/call | Live only. `pm.py` refuses quotes older than 30 min; `venue_last_trade_time` is the stamp |
| Daily / intraday OHLCV bars | `get_equity_historicals` | `technicals.py`, backtest | per slot; harness once | **10 symbols/call**; ~90 KB per ten-symbol year, persisted to a file | Bars are dated; `backtest.upto()` is the no-look-ahead boundary. **Adjusted vs raw is a parameter — record which.** Delisted coverage: not verified; see §3 |
| Fundamentals snapshot (mcap, float, P/E, 52-wk with dates) | `get_equity_fundamentals` | scan fundamentals pillar | per slot | 10 symbols/call | **no history** — today's value only. A backtest never sees it |
| Quarterly / annual financials | `get_financials` | scan (margins, growth) | per slot | 20 symbols/call | Fiscal-period dated, **not filing-dated**. Usable in a backtest only through a table with `available_from` = filing date (`backtest.py --financials`); the SEC `companyfacts` `filed` field is the way to build one |
| Technical indicators | `get_equity_technical_indicators` | cross-check only | rarely | 1 symbol/call | Computed from bars; `technicals.py` reproduces them from one bars call instead |
| Earnings calendar (am/pm, estimate, actual) | `get_earnings_calendar` | earnings gate, catalyst pillar | daily | 31-day window | Forward calendar; consensus is as-of-announcement only, no revision history |
| News with full bodies | `get_equity_news` | catalyst, analyst targets | per slot | 1 symbol/call | Minute-stamped articles. Analyst targets appear only in article text, so they are not a series |
| VIX / SPX / NDX | `get_indexes` → `get_index_quotes` | regime | per slot | many | Live to the second. **Not scraped.** No history through the connector; the backtest leaves `vix` absent rather than proxied |
| VIX, VIX3M term structure (history) | Cboe CSVs — §1c | options desk gates (`vix.json`), regime | daily | one GET per index | Dated daily closes; the one VIX series with a real date column. Stage the last close as `vix.json` |
| Level 2 depth | `get_equity_price_book` | PM fills sanity | on demand | 4 symbols | Live only |
| Options: chains, IV, volume, OI, put/call | Options-activity saved scan `cc3b6743-…` (`get_option_quotes` is 403) | intelligence pillar | per slot | market-wide | Live only; `sentiment.py` gates thin chains. `implied_move_pct` stays null without a straddle |
| Retail attention (RH) | `get_popular_watchlists` + `get_watchlist_items` | universe (live) | per slot | per list | Live only. **This is the attention universe `universe.py` exists to replace** |
| Liquid movers | Universe saved scan `55138bd9-…` | universe (live) | per slot | market-wide | Live; relative volume is understated early in the session |
| SEC filings | `get_sec_filing*` | on demand | — | — | Filing-dated |

### Scraped (WebSearch → WebFetch; provenance-gated in scheduled runs)

| Feed | Source | Used by | Cadence | Limit | PIT handling |
|---|---|---|---|---|---|
| Price cross-check, analyst rating + target, GICS sector | `stockanalysis.com/stocks/<T>/`, `/statistics/`, `/company/` | scan | per name per slot | 15-min WebFetch cache; polite | Live page. `price_sources` and `price_disagreement_pct` recorded per name; >3 % apart = conflict |
| Sector rotation YTD | `stockanalysis.com/etf/compare/xlk-vs-…` | regime | daily | one call | Has served untimestamped prior-session rows; day-changes from individual pages |
| WSB mentions | `apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` | `sentiment.py` | per slot | **page 1 only** (ranks recompute between requests) | **No vendor timestamp**; snapshot per slot is the only history that exists |
| StockTwits trending + gauge | `api.stocktwits.com/api/2/trending/symbols.json`, `stocktwits.com/symbol/<T>` | `sentiment.py` | per slot | keyless; never the `streams/symbol` endpoint (variable-TTL cache, 50 h stale observed) | No timestamp; snapshot per slot |
| Reddit raw posts | `arctic-shift.photon-reddit.com/api/posts/search?…&sort=desc` | `sentiment.py` | per slot | ~120k req/h; `sort=desc` mandatory | `created_utc` per post — the one attention feed with real timestamps |
| Insider purchases | `marketbeat.com/insider-trades/purchases/`; `efts.sec.gov` full-text search | scan | daily | efts is open JSON | Filing date is the signal time (~1 day lag on marketbeat) |
| Breadth ($ADDN, $ADRN, $S5FI, $S5TW), highs/lows | `barchart.com` | regime | daily | — | **Timestamp does not render — undated** |
| Market-wide put/call, off-exchange share | `cboe.com/us/options/market_statistics/daily/`, `/us/equities/market_statistics/` | regime | daily | — | Put/call carries no date stamp; label prior-session |
| Macro calendar | `tradingeconomics.com/united-states/calendar`, `investing.com/economic-calendar/` | PM macro gate | daily | — | Forward calendar; times in UTC on TE |
| IPO calendar | `iposcoop.com/ipo-calendar/`, RH *IPO Access* list | scan | daily | — | Forward; lockups estimated as IPO + 90/180 d |
| Earnings transcripts | `stockanalysis.com/stocks/<T>/transcripts/` → alphastreet / fool.com | deep dives | on demand | 2 calls per name | Dated by call |
| **Index membership history** | **Wikipedia S&P 500 / S&P 400 pages — `universe_history.py`** | **backtest universe** | **weekly, or before a harness run** | **one GET per page, project User-Agent** | **Add/remove effective dates per ticker — see §3** |

### 1c. Cboe VIX / VIX3M daily history — the options desk's regime gate (D-02)

The paper options desk (`docs/PM.md` §18) refuses new short vol when VIX > VIX3M or VIX > 30
and reads both from `vix.json` in the run directory. Cboe publishes the full daily history of
each index as a CSV on the CDN host that is already on the egress allowlist:

```
https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv
```

Columns `DATE,OPEN,HIGH,LOW,CLOSE` (VIX from 1990, VIX3M from 2007-12), one row per session,
updated after the close. The scheduled task stages the last row of each as

```json
{"vix": 17.42, "vix3m": 19.31, "as_of": "2026-09-09", "source": "cdn.cboe.com daily_prices"}
```

`options_desk.parse_vix` also accepts `{"vix": {"close", "date"}}` objects or `[{date, close}]`
rows per index, and any key casing. **PIT:** the CSV is the prior session's close during the
day — label it so; the live VIX from `get_index_quotes` is the intraday number and may be
used for `vix` when the session fetched it, with the CSV close for `vix3m` (the connector
does not serve VIX3M). Absent `vix.json` the gate fails closed: no new structures.

Dead and never-retry sources are listed in `docs/scan-sources.md`; do not re-probe them from a
scheduled task.

---

## 2. Egress allowlist — the free stack

Scheduled runs go through an egress proxy. A host that is not on the allowlist fails with
`PROXY_REJECTED` / `source: proxy` (or a 403 on CONNECT from a shell), which is an admin
request, not a dead source. The plan's request for the free stack, in full:

```
data.sec.gov  www.sec.gov  efts.sec.gov
cdn.finra.org  api.finra.org
api.stlouisfed.org  api.bls.gov
cdn.cboe.com
paper-api.alpaca.markets  data.alpaca.markets
www.alphavantage.co
apewisdom.io  api.stocktwits.com  trends.google.com
en.wikipedia.org
ntfy.sh
```

**Observed 2026-09-10 from the build session:** `en.wikipedia.org` (and `en.m.wikipedia.org`,
`api.wikimedia.org`) is denied at the proxy for raw fetches; WebFetch reaches the page but
through a *cache-only* path that served a copy ending at the 2026-06-30 change row and
truncated the changes table at roughly 100 rows. `raw.githubusercontent.com` is reachable.
Until the allowlist entry lands, `universe_history.py` runs where Wikipedia is reachable (the
box, a laptop) and the JSON is committed; the file records its `source` and `fetched_at`.

---

## 3. Universe and survivorship

### What the problem is

`backtest.py` scores whatever symbols are in the bars file, and a bars file is a list of
names that exist today. Names that were acquired, went bankrupt, or fell out of the index are
absent, and they are disproportionately the losers. Screening 2024 with 2026's membership is
survivorship bias wearing a respectable name: the 2026 list is, by construction, the
companies that did well enough to still be in it. `docs/BACKTEST.md` §2 lists it first among
the biases you cannot remove, only state. This section is how it is *reduced*, and what still
has to be stated.

### The membership file

`engine/universe_history.py` reads Wikipedia's "List of S&P 500 companies" (and the S&P 400
page): the current-constituents table and the **"Selected changes to the list of S&P 500
components"** table, whose columns — verified on the live page 2026-09-10 — are

    Effective Date | Added (Ticker, Security) | Removed (Ticker, Security) | Reason

and walks the changes backwards from today's list. A ticker removed on *d* was a member up
to *d* (exclusive — the effective date is the first session it is out); a ticker added on *d*
has been a member from *d*. The result is one JSON file:

```
{"name": "sp500-history", "index": "sp500", "fetched_at": "…Z",
 "source": {...}, "since": "2019-01-18",
 "changes_span": {"oldest": "2019-01-18", "newest": "2026-08-18", "rows": 125},
 "n_current": 503, "n_ever": 671, "unparseable_rows": 0, "notes": [],
 "bias": "UNIVERSE sp500-history … RESIDUAL BIAS: …",
 "current": [...], "changes": [{"date", "added", "removed", "reason"}, ...],
 "membership": {"TWTR": [["2019-01-18", "2022-11-01"]], "META": [["2022-06-09", null]], ...}}
```

Intervals are `[from, to)`; `null` is open-ended (`to`) or "before the history starts"
(`from`). Tickers are kept as Wikipedia writes them (`BRK.B`); `normalize_ticker` gives the
hyphen form (`BRK-B`) Robinhood and Alpaca use, and the backtest matches either.

```bash
# fetch (one request per page) and write; --since clamps to the window you have bars for
python3 engine/universe_history.py --index sp500 --out experiments/universe_sp500.json --since 2019-01-18
# who was in on a date
python3 engine/universe_history.py --in experiments/universe_sp500.json --members-on 2025-03-14
# offline routes: a saved page, or a date,add,remove CSV plus a Symbol CSV
python3 engine/universe_history.py --index sp500 --html page.html --out …
python3 engine/universe_history.py --changes-csv changes.csv --current-csv sp500.csv --out …
```

A network failure is a message on stderr and exit 2; the file is written atomically, so a
partial file is never left behind.

`experiments/universe_sp500.json` is the committed copy. Its `source.note` says how it was
built and when; **read it before quoting the file** — the first one (2026-09-10) came from a
mirror of the Wikipedia tables because Wikipedia was egress-denied, and covers 2019-01-18
onward only.

### Wiring it into the backtest

```bash
python3 engine/backtest.py --bars bars_all.json --start 2024-08-21 --end 2026-08-28 --every 5 \
    --universe-history experiments/universe_sp500.json \
    --out-records records/ --ledger experiments/ledger.jsonl --experiment-id E10 --hypothesis "…"
```

On each replay date the candidate set is the bars file **intersected with `members_on(date)`**.
Without the flag nothing changes and the run carries the old `SURVIVORSHIP BIAS` warning; with
it the warning becomes `SURVIVORSHIP, REDUCED NOT REMOVED`, the summary and the ledger row
carry `universe: {name, n_symbols_on_start, n_symbols_on_end, n_ever, n_members_with_bars,
n_members_without_bars}` and `universe_bias` (the file's bias statement), and every record's
`data_warnings` carries it too. `n_members_without_bars` is the number to look at first: a
member with no bars is silently absent, which is the old bias in miniature.

### Price history for removed names — the part the membership file cannot supply

Membership is not price history. A removed name is scored only if the bars file has its bars
for the dates it was a member, and **a bars source that no longer serves delisted symbols
reintroduces exactly the bias the membership file removes.**

- **Alpaca Basic** (IEX, 2016+) often still serves bars for delisted and acquired symbols,
  but not reliably — **verify per symbol**: request the bars, and treat an empty response as
  "not covered", never as "did not trade". The harness reports the count.
- **Robinhood `get_equity_historicals`** keys on tradable instruments; a delisted symbol may
  return nothing or resolve to a different instrument. Do not assume it covers removed names.
- **Ticker renames** (FB → META, FISV → FI, BK → BNY) appear in the changes as a removal and
  an addition on the same date, or not at all. The membership is right either way; the bars
  file has to be keyed the way the bars source keys the *historical* symbol, and the same
  company may need two keys.

**The bars-fetch prompt** for a harness run therefore takes its symbol list from the file,
not from today's index:

```bash
# every name that was a member at any point in the window, plus the followed set and SPY
python3 engine/universe_history.py --in experiments/universe_sp500.json --members-on 2024-08-21
python3 engine/universe_history.py --in experiments/universe_sp500.json --members-on 2026-08-28
# union those two with every ticker in the file's `membership` whose interval overlaps the
# window (the names that came and went inside it), then the followed set; ten symbols per
# bars call; include SPY, it sets the calendar.
```

`runner/fetch_bars.py` does exactly that union (`--universe … --since <first replay date>`),
fetches from Alpaca Basic, and writes the names that came back empty to `bars_missing.json`
with `missing_former_members` split out — that file is the residual statement for the run.

Then run the backtest with `--universe-history` and read `n_members_without_bars` before
anything else in the summary.

### The residual bias — state it in every write-up

Every write-up that uses the file prints the file's `bias` string (the harness puts it in the
ledger row and the summary; `universe_history.bias_statement()` regenerates it for any window).
It states:

1. how many distinct names were members over the window and **how many of those are not in
   the current list** — the names a current-constituent backtest would have silently dropped;
2. how many change rows had no parseable ticker or date, and how many were inconsistent with
   the current list (a name added and never removed but absent today: a rename or delisting
   the table does not carry — kept as a member, which is the flattering direction, so it is
   counted);
3. that the source table is **"selected" changes**, close to complete since about 2019 and
   thin before that — the first file is clamped to 2019-01-18 for exactly this reason;
4. that bars for removed names exist only where the bars source still serves them, and that a
   member with no bars is silently absent.

What it cannot state and you must add by hand: whether the *reason* for removal correlates
with the signal being tested (a size-floor removal is a momentum loser by construction), and
whether the followed set — which is chosen today — leaks hindsight into the candidate list
independently of index membership. The upgrade trigger in the plan is Sharadar's constituent
history, which is complete and delisting-inclusive; until a promotion decision depends on it,
this file is the honest free version.
