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
| **Earnings history — trailing 8 quarters (EPS estimate/actual, report date, am/pm)** | `get_earnings_results` | `earnings_quality.py` → `earnings_history.json` (P-06, §1b) | daily, held + top candidates | **1 symbol/call** | Report-dated; the estimate is the consensus as the connector holds it today, not as it stood before the print — a mild look-ahead in SUE the write-up must state |
| News with full bodies | `get_equity_news` | catalyst, analyst targets; **negative-headline source for `veto.json` (§1a)** | per slot | 1 symbol/call | Minute-stamped articles. Analyst targets appear only in article text, so they are not a series |
| VIX / SPX / NDX | `get_indexes` → `get_index_quotes` | regime | per slot | many | Live to the second. **Not scraped.** No history through the connector; the backtest leaves `vix` absent rather than proxied |
| Level 2 depth | `get_equity_price_book` | PM fills sanity | on demand | 4 symbols | Live only |
| Options: chains, IV, volume, OI, put/call | Options-activity saved scan `cc3b6743-…` (`get_option_quotes` is 403) | intelligence pillar | per slot | market-wide | Live only; `sentiment.py` gates thin chains. `implied_move_pct` stays null without a straddle |
| Retail attention (RH) | `get_popular_watchlists` + `get_watchlist_items` | universe (live) | per slot | per list | Live only. **This is the attention universe `universe.py` exists to replace** |
| Liquid movers | Universe saved scan `55138bd9-…` | universe (live) | per slot | market-wide | Live; relative volume is understated early in the session |
| SEC filings | `get_sec_filing*` | on demand | — | — | Filing-dated |
| **10-K / 10-Q text change (E16)** | **EDGAR `data.sec.gov/submissions` → Archives primary document; `engine/filings.py`** | **scan rows (`features.filing_*`, not scored)** | **after each filing; a fetch step on the box, then the batch CLI** | **10 req/s, `User-Agent` mandatory — see §5** | **Filing-dated; `features_for` nulls anything filed after the scan date — see §5** |

### Scraped (WebSearch → WebFetch; provenance-gated in scheduled runs)

| Feed | Source | Used by | Cadence | Limit | PIT handling |
|---|---|---|---|---|---|
| Price cross-check, analyst rating + target, GICS sector | `stockanalysis.com/stocks/<T>/`, `/statistics/`, `/company/` | scan | per name per slot | 15-min WebFetch cache; polite | Live page. `price_sources` and `price_disagreement_pct` recorded per name; >3 % apart = conflict |
| Sector rotation YTD | `stockanalysis.com/etf/compare/xlk-vs-…` | regime | daily | one call | Has served untimestamped prior-session rows; day-changes from individual pages |
| WSB mentions | `apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` | `sentiment.py` | per slot | **page 1 only** (ranks recompute between requests) | **No vendor timestamp**; snapshot per slot is the only history that exists |
| StockTwits trending + gauge | `api.stocktwits.com/api/2/trending/symbols.json`, `stocktwits.com/symbol/<T>` | `sentiment.py` | per slot | keyless; never the `streams/symbol` endpoint (variable-TTL cache, 50 h stale observed) | No timestamp; snapshot per slot |
| Reddit raw posts | `arctic-shift.photon-reddit.com/api/posts/search?…&sort=desc` | `sentiment.py` | per slot | ~120k req/h; `sort=desc` mandatory | `created_utc` per post — the one attention feed with real timestamps |
| Insider purchases (panel) | `marketbeat.com/insider-trades/purchases/`; `efts.sec.gov` full-text search | scan (display panel) | daily | efts is open JSON | Filing date is the signal time (~1 day lag on marketbeat) |
| **Insider transactions (feature)** | **EDGAR full-index `form.idx` → Form 4 complete submission → `ownershipDocument` XML — `insiders.py`** | **`features.insider_*` on every scan row (E15)** | **daily, staged as `insiders.json` / `form4/`** | **SEC fair access: User-Agent with a contact, ≤ 10 req/s; run on the box** | **Trade date in the XML, filing date in the index / SGML header; the signal drops trades filed after `as_of` — see §4** |
| Breadth ($ADDN, $ADRN, $S5FI, $S5TW), highs/lows | `barchart.com` | regime | daily | — | **Timestamp does not render — undated** |
| Market-wide put/call, off-exchange share | `cboe.com/us/options/market_statistics/daily/`, `/us/equities/market_statistics/` | regime | daily | — | Put/call carries no date stamp; label prior-session |
| Macro calendar | `tradingeconomics.com/united-states/calendar`, `investing.com/economic-calendar/` | PM macro gate | daily | — | Forward calendar; times in UTC on TE |
| IPO calendar | `iposcoop.com/ipo-calendar/`, RH *IPO Access* list | scan | daily | — | Forward; lockups estimated as IPO + 90/180 d |
| Earnings transcripts | `stockanalysis.com/stocks/<T>/transcripts/` → alphastreet / fool.com | deep dives | on demand | 2 calls per name | Dated by call |
| **Index membership history** | **Wikipedia S&P 500 / S&P 400 pages — `universe_history.py`** | **backtest universe** | **weekly, or before a harness run** | **one GET per page, project User-Agent** | **Add/remove effective dates per ticker — see §3** |

Dead and never-retry sources are listed in `docs/scan-sources.md`; do not re-probe them from a
scheduled task.

### 1a. Veto sources — short reports, negative news, halts (P-03, 2026-09-10)

`engine/veto.py` reads one staged file, `$SCAN_DIR/veto.json`, and nothing else. The
scheduled task assembles it from three sources; none is a feed the engine calls itself.

| List | Source | How it is collected | Window the engine applies |
|---|---|---|---|
| `short_reports` | the publishers in **`docs/veto-publishers.md`** — their own sites and X accounts (Hindenburg, Muddy Waters, Citron, Culper, Fuzzy Panda, Grizzly, Spruce Point, Viceroy, Blue Orca, Iceberg, Wolfpack, Kerrisdale, Bonitas, J Capital, Hunterbrook, Scorpion, Gotham City, Bleecker Street) | WebSearch → WebFetch on each publisher's index page or X feed, once per scan day before the 08:00 slot; one row per report `{symbol, publisher, date, url, title}` with the report's own date | **20 weekdays** → entry veto; a held name is flagged `review: "short-report"` |
| `negative_news` | `get_equity_news` for every held name and every candidate the scan will score | the headline and body are read by the task; `severity` is **"high"** for fraud or accounting allegations, a restatement, an auditor resignation, a regulatory or DOJ action, a guidance withdrawal, a going-concern note, a delisting notice, a failed trial or a recall; **"medium"** for a downgrade, a lawsuit, an executive departure, a missed print. Until the LLM extractor (a later task) classifies these, the task classifies by hand and stages only what it read | **5 weekdays**, high severity only → entry veto; medium is journaled as a note |
| `halts` | the exchange's halt list (`nasdaqtrader.com/trader.aspx?id=tradehalts`, `nyse.com/trade-halt-current`) and the broker's `get_equity_tradability` for held names | one row per symbol halted today `{symbol, date, reason}` | **today only** → entry veto |

What the engine does with the file is in `docs/PM.md` ("The veto") and `docs/SCAN.md` §3c.
The rules are in `veto.RULES`. Absent file = no override anywhere, never "checked and
clean": `scanner.py` prints `VETO FEED: not staged` and the board carries no `veto` field.
The publisher list is maintained by hand; the file says so at the top.

**PIT handling.** Every row carries the report's or headline's own date and the engine
ignores rows dated after the run — so a replay with a historical `veto.json` is honest to
the day, and the archive record keeps `veto`, `veto_reasons` and `pre_veto_verdict` per row
for the counterfactual (`report.py` rule `veto`).

### 1b. Earnings history — the E22/E23 inputs (P-06, 2026-09-10)

`get_earnings_results` returns the trailing up to eight quarters for one symbol — EPS
estimate and actual, report date, am/pm timing. The scheduled task writes them, mapped as
`earnings_quality.py`'s docstring specifies (year+quarter → `fiscal_quarter`, `report.date` →
`report_date`, `eps.actual` / `eps.estimate` → `eps_actual` / `eps_estimate`, `surprise_pct`
recomputed), into `$SCAN_DIR/earnings_history.json` as `{SYMBOL: [quarters]}`, then runs

```bash
python3 earnings_quality.py --history earnings_history.json --bars bars.json \
                            --as-of $(date +%F) --out earnings_quality.json
```

`bars.json` is the same `get_equity_historicals` payload `technicals.py` reads and **must
include SPY**, or every announcement-window return is null. `scanner.py` merges the five
keys (`sue`, `ear_3d`, `reg_residual`, `earnings_agreement`, `days_since_earnings`) into each
row's `features` dict. Scored by nothing; `docs/BACKTEST.md` §6f has the recipes.

**PIT handling.** The report date is the announcement date and rows after the run's date
are excluded. The estimate is the consensus the connector serves *today* for that quarter,
which for an old quarter is the final pre-print consensus — fine — but for the latest quarter
may already have been revised; SUE therefore carries a small look-ahead on the newest
surprise that a backtest cannot remove with this source. State it in the write-up.

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

---

## 4. Insider transactions — Form 4 → `insiders.py` → `features.insider_*` (P-01, E15)

### Why a second insider feed

The scan's insider panel is a hand-collected list scraped at the 15:00 slot (SCAN.md §9.3):
a display, not a series, with no owner identity and no history, so nothing can be tested
against it. Cohen, Malloy & Pomorski (2012, *Decoding Inside Information*) showed the
information is in **which** insider trades: an insider who traded in the same calendar month
in each of the three prior years is *routine* (a plan, a bonus cycle, a tax date) and those
trades predict nothing; every other trade is *opportunistic*, and opportunistic buys earned
about 82 bp/month abnormal in their sample. The retail-friendly form of the same idea is the
**cluster buy**: two or more distinct insiders buying on the open market inside 30 days.
`engine/insiders.py` computes both from the primary source and the scanner logs them as
research features — **scored by nothing** until E15 says otherwise.

### The path from EDGAR to a feature

1. **Index.** `https://www.sec.gov/Archives/edgar/full-index/<YYYY>/QTR<n>/form.idx` — one
   fixed-width row per filing: form type, company, CIK, date filed, and the path of the
   complete submission. A Form 4 appears twice, once under the issuer's CIK and once under
   the reporting owner's; filter on the **issuer**. `insiders.full_index_url(date)` builds
   the URL; the file is ~50 MB a quarter and is downloaded once, not fetched through
   WebFetch (rule 1 above).
2. **Ticker → CIK.** `https://www.sec.gov/files/company_tickers.json` (`insiders.symbols_to_ciks`).
3. **Accession → document.** The index path `edgar/data/<cik>/<accession>.txt` under
   `https://www.sec.gov/Archives/` is the complete submission: an SGML header (which carries
   `FILED AS OF DATE`) wrapping the `ownershipDocument` XML inside `<XML>…</XML>`.
   `parse_form4` reads that file or the bare XML. It takes `issuer/issuerTradingSymbol`,
   `reportingOwner/rptOwnerName` + `rptOwnerCik` + the relationship flags, and every
   `nonDerivativeTransaction` (date, `transactionCode`, shares, price, acquired/disposed,
   shares after, direct/indirect, the `aff10b5One` flag). The derivative table is ignored.
4. **Fair access.** SEC requires a `User-Agent` naming a person and a contact
   (`"Jane Doe jane@example.com"`) and limits clients to 10 requests per second; anything
   else gets 403 and, repeated, an IP block. `insiders.py --fetch` reads the header from
   `$SEC_USER_AGENT`, refuses to run without an `@` in it, and sleeps to stay under the
   limit. **Run it on the box (or a laptop), never from a scheduled session's sandbox:** the
   egress proxy is the wrong place for a rate-limited crawl, and the fetched files are what
   gets staged, not the crawl.

```bash
# on the box: list, then fetch, the Form 4 submissions for the followed set
python3 engine/insiders.py --edgar-index form.idx --symbols AAPL,MSFT,NVDA \
    --company-tickers company_tickers.json --since 2026-06-01            # URLs only
SEC_USER_AGENT="Jane Doe jane@example.com" python3 engine/insiders.py --edgar-index form.idx \
    --symbols AAPL,MSFT,NVDA --company-tickers company_tickers.json --since 2026-06-01 \
    --fetch --form4-dir form4/
# then, anywhere: parse + signal
python3 engine/insiders.py --form4-dir form4/ --as-of 2026-09-10 \
    --out insiders_signal.json --dump-transactions insiders.json
```

### The staged file — `insiders.json`

What the scheduled task stages into `$SCAN_DIR` (COLLECTION.md §7). Either a bare list of
transactions or `{"_meta": {...}, "transactions": [...]}`; `insiders.py --dump-transactions`
writes the second shape. One transaction:

| key | | |
|---|---|---|
| `symbol` | `"AAPL"` | `issuerTradingSymbol`, upper-cased |
| `issuer_cik`, `issuer_name` | `"0000320193"`, `"Apple Inc."` | |
| `owner`, `owner_cik` | `"DOE JANE"`, `"0001214128"` | the CIK is the identity a cluster counts; the name is the fallback |
| `relationship` | `["director"]` | any of `director`, `officer`, `ten_percent_owner`, `other` |
| `officer_title` | `"Senior Vice President"` or null | |
| `date` | `"2026-08-14"` | the trade (`transactionDate`) |
| `filed` | `"2026-08-18"` or null | when it became public — from the index row or the SGML header; null on a bare XML |
| `code` | `"P"` | `transactionCode`: **P** purchase, **S** sale, A grant, M exercise, F tax withholding, G gift, … |
| `acquired_disposed` | `"A"` / `"D"` | |
| `shares`, `price`, `value` | `1000.0`, `150.25`, `150250.0` | value = shares × price |
| `shares_after` | `5000.0` or null | |
| `direct` | true / false / null | D vs I ownership |
| `plan_10b5_1` | false | the `aff10b5One` flag (schema X0508+); older filings say it only in a footnote, so false means *not flagged* |
| `accession`, `source` | `"0001214128-26-000123"`, `"form4/….xml"` | |

Only **P** and **S** enter the signal; every other code is kept for the record. A row with
no symbol or no date is skipped and counted in the warnings.

### The signal file — `insiders_signal.json`

`{"_meta": {as_of, window_days: 30, net_window_days: 90, routine_rule, history_since,
n_txns, n_symbols, n_with_filed_date, history_span, n_cluster_buy, sources, warnings},
"symbols": {SYMBOL: {...}}}`, per symbol:

`opportunistic_buyers_30d` (distinct owners), `unknown_history_buyers_30d` (the subset whose
history is too short for the rule), `opportunistic_buy_usd_30d`, `routine_buy_usd_30d`,
`buy_usd_90d`, `sell_usd_90d`, `net_insider_usd_90d` (buys − sales, open market only),
`cluster_buy` (≥ 2 distinct non-routine buyers in 30 days), `last_buy_date`, `n_txns`,
`n_open_market_30d`, `buy_classes_30d`.

**Routine needs history.** The rule looks at the owner's trades in the same month of each
of the three prior years. A stager that pulls 90 days of filings cannot see them, so nearly
every buy is *unknown* — which is counted as **not routine** (the paper's own definition of
opportunistic is "everything else") and reported as unknown beside it. A prior year the
history does not cover is never scored as a miss: `--history-since` tells the module the
date from which the staged history is complete, and without it the earliest staged trade is
used, which is the conservative reading.

**Point in time.** The windows use the trade date; a transaction whose `filed` date is after
`as_of` is dropped because it was not public yet. Without `filed` the window is optimistic
by up to two business days (the Form 4 deadline) and `_meta.warnings` says so. For the E15
replay the filing date is the one to use, and the index supplies it.

### What the scanner does with it

When `insiders_signal.json` is in `$SCAN_DIR`, `scanner.py` adds three keys to every
scored row's `features` dict — `insider_cluster_buy`, `insider_opportunistic_buy_usd_30d`,
`insider_net_usd_90d` — **null** for a name the signal has no transactions for, and records
`meta.insider_signal_meta` (`as_of`, counts, which candidates were covered). When the file
is absent the row is untouched, so "we did not look" and "no insider bought" stay
distinguishable and the golden output does not move. No pillar reads them; the
hand-collected `insider_panel` is unchanged; the archive record and the scan snapshot carry
the features exactly as they carry `technicals.features()`.

### The E15 test

*Do cluster-buy names outperform the rest over the next 20 and 60 sessions?* Stage the
signal on every scan for a few weeks, then `ic.py --by-feature` over the archive: the
`insider_cluster_buy` row is the binary split (top-minus-bottom quantile = cluster minus
the rest), `insider_opportunistic_buy_usd_30d` the dollar-weighted version, and
`insider_net_usd_90d` the control that should carry less than either if the paper is
right. Recipe and the honesty rules in `docs/BACKTEST.md` §6d.
## 5. 10-K / 10-Q text change — "Lazy Prices" (P-02, E16)

**The evidence.** Cohen, Malloy & Nguyen (2020, *Journal of Finance*, "Lazy Prices"): firms
whose periodic filings change little against the prior year's filing of the same form
("non-changers") outperform the "changers" — up to 188 bp/month of five-factor alpha on the
Risk Factors section alone, 30–60 bp/month on broader whole-document measures — with the drift
playing out over roughly three months on a monthly rebalance. The changes that carry the
information are in Risk Factors, MD&A, litigation and the CEO/CFO language. The mechanism is
inattention: the document is long, the change is buried, and the price takes a quarter to
reflect it. That makes it a **slow negative screen** — a changer is a name to leave alone for
~60 sessions — and not an entry trigger, and it is not scored by anything until E16 has a
result.

**The feed — EDGAR, not a vendor.** Three URLs, all free, all filing-dated, `engine/filings.py
--edgar-plan --symbols A,B --cik-map cik.json` prints them per symbol in the order to pull:

1. `https://www.sec.gov/files/company_tickers.json` — ticker → CIK, once; keep it as the cik
   map (a symbol without a CIK gets this as step 0).
2. `https://data.sec.gov/submissions/CIK##########.json` (10-digit zero-padded CIK) — the
   filing index. `filings.recent` is column-oriented (`form[]`, `filingDate[]`,
   `accessionNumber[]`, `primaryDocument[]`, `reportDate[]`); `filings.pick_filing_pair(payload,
   "10-K")` turns it into `(current, prior)` where prior is the **same form filed 270–460 days
   earlier** (the paper's year-over-year comparison: 10-K vs last year's 10-K, Q2 10-Q vs last
   year's Q2 10-Q), falling back to the previous filing of that form. Amendments (`/A`) are
   skipped.
3. `https://www.sec.gov/Archives/edgar/data/<cik>/<accession-without-dashes>/<primaryDocument>`
   — the document itself, HTML (older filings: text). `filings.primary_document_url()` builds it.

Full-text search — `https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=10-K
&dateRange=custom&startdt=…&enddt=…` — is for finding filings by content (e.g. every 10-K that
mentions a phrase in a window); it is not on the per-symbol path.

**Fair access — the fetch step must obey this or the IP is blocked for ten minutes at a
time.** Every request carries `User-Agent: <project or company name> <contact email>`; at most
**10 requests per second**; `Accept-Encoding: gzip, deflate`; `Host: data.sec.gov` on the
submissions call and `Host: www.sec.gov` on the Archives. The engine never opens the socket —
`filings.py` is URL construction and parsing only, and the tests run without a network — so
the rules live in the fetch step on the box (`runner/`), which also keeps every fetched
document under the run archive: an accession never changes, so it never needs a second GET.
The fetch is a weekly task plus a per-name refresh when the earnings calendar shows a 10-Q
is due; a whole-universe first pull is `2 × N` documents and takes minutes at the rate limit,
not hours.

**What the module computes** (`engine/filings.py`, stdlib only: `html.parser`, `re`, `math`,
`difflib`):

* `extract_sections(text_or_html, form)` → `{risk_factors, mdna, legal_proceedings,
  business}`, by locating the Item headings — 10-K: Items 1A / 7 / 3 / 1; 10-Q: Part II
  Item 1A / Part I Item 2 / Part II Item 1 (a 10-Q has no Business section, it is `null`).
  Tolerant of HTML, tables, page headers ("Table of Contents" back-links, bare page numbers),
  case and `&nbsp;`. Every line-start `Item N` is a candidate; the table of contents loses
  because its body is one line, a cross-reference in prose loses because it is not at a line
  start, and the longest body wins. A section that cannot be located is **`null`, never `""`**.
* `similarity(a, b)` → cosine on term-frequency vectors (lower-cased, stop-words and bare
  numbers stripped — numbers move every year, the signal is the prose), Jaccard on the word
  sets, and `minimum_edit_ratio` = `difflib.SequenceMatcher.ratio()` over the **sentence**
  lists. The last is quadratic in the worst case and is only ever run on a section, never a
  whole filing. Identical → 1.0 on all three; nothing in common → 0.0; both empty → `null`.
* `change_score(current, prior)` → per-section `{cosine, jaccard, minimum_edit_ratio}` (or
  `null` when either side lacks the section), `risk_factors_change` = 1 − cosine(Risk
  Factors), `mdna_change`, `overall_change` = weighted mean of 1 − cosine over the sections
  present on **both** sides (weights Risk Factors 0.40, MD&A 0.30, Legal 0.15, Business 0.15,
  renormalised over what is present), `changer` = `overall_change >= threshold`,
  `n_sections_compared`, and the `threshold` and `weights` that produced the flag. No
  comparable section at all → `overall_change` and `changer` are `null`, not a non-changer.
* **The threshold is provisional.** `CHANGER_THRESHOLD = 0.15` is a top-quintile proxy taken
  from the paper's reported distribution of cosine similarity (its bottom similarity quintile
  sits at roughly 0.85 and below). The paper sorts on quintiles of a cross-section; we do not
  have one yet. When the archive holds a few hundred scored filings, set the threshold from
  the observed 80th percentile of `overall_change` (`--threshold` on the batch CLI) and record
  that in the staged file's `_meta`. Until then every output carries the threshold that made
  the flag, and the ic.py test below ranks on the continuous score, which does not depend on it.

**The staged file — `filings_signal.json`, in the run directory.** Written by
`python3 filings.py --batch <dir> --out filings_signal.json` from `<dir>/<SYMBOL>/current.htm +
prior.htm (+ meta.json)`; read by `scanner.py` (`filings.load_staged`). Shape:

```json
{
  "_meta": {"generated_at": "2026-09-10T07:40:00", "threshold": 0.15,
            "n_symbols": 2, "n_changers": 1, "skipped": ["NOPE: current/prior file missing"],
            "basis": "filings.py --batch"},
  "ACME": {
    "filed": "2026-08-01", "form": "10-K", "prior_filed": "2025-08-01",
    "accession": "0000000001-26-000001", "prior_accession": "0000000001-25-000001",
    "change_score": {
      "sections": {"risk_factors": {"cosine": 0.573, "jaccard": 0.339, "minimum_edit_ratio": 0.462},
                   "mdna": {"cosine": 1.0, "jaccard": 1.0, "minimum_edit_ratio": 1.0},
                   "legal_proceedings": {"cosine": 1.0, "jaccard": 1.0, "minimum_edit_ratio": 1.0},
                   "business": null},
      "risk_factors_change": 0.427, "mdna_change": 0.0, "overall_change": 0.171,
      "changer": true, "n_sections_compared": 3, "threshold": 0.15,
      "weights": {"risk_factors": 0.4, "mdna": 0.3, "legal_proceedings": 0.15, "business": 0.15},
      "basis": "weighted mean of 1 - cosine(...)", "form": "10-K",
      "sections_found": {"current": ["risk_factors", "mdna", "legal_proceedings"], "prior": ["..."]}
    }
  }
}
```

`filed`, `form`, `prior_filed`, `accession`, `prior_accession` come from `meta.json` beside
the documents (the fetch step writes it from the submissions payload); they are `null` when
it was not written, and a row with no `filed` date yields **no features at all** — the
point-in-time guard cannot run without it.

**What the scan row carries.** With the file staged, every row's `features` block gets three
keys (`null` for a symbol the file does not name):

| key | value |
|---|---|
| `filing_change_score` | `change_score.overall_change` — 0 identical, 1 disjoint |
| `filing_changer` | the boolean; `ic.py --by-feature` reads it as 1.0 / 0.0 |
| `filing_days_since` | calendar days from `filed` to the scan date |

All three are `null` when the filing's `filed` date is **after** the scan date — a filing is
knowable from its filing date, not before — which is what makes the same file safe to lay
over a historical replay. `scanner.scan()` takes the parsed file as an explicit argument
(`filings_signal=`) precisely so a backtest cannot pick up today's file by accident; only the
live `python3 scanner.py` path loads it from `$SCAN_DIR`. `meta.filings_signal` on the results
records `n_symbols`, `n_matched`, `n_changers` and the threshold. Without the file nothing is
added and the golden output does not move (`tests/test_filings.py`).

**The E16 test.** Do changers underperform non-changers over the next 20 and 60 sessions?
With the archive records carrying `features.filing_changer` and `features.filing_change_score`:

```bash
python3 engine/ic.py --records records/ --bars bars.json --horizons 20,60 --by-feature \
    --md ic_e16.md --json ic_e16.json
```

Read the `filing_change_score` row: the paper predicts a **negative** IC (more change, lower
forward return) and a negative top-minus-bottom quantile spread at both horizons, larger at
60. `filing_changer` gives the same test as a two-group split. The recipe, the sample-size
floor and the promotion rule are in `docs/BACKTEST.md` §6e. What this cannot tell you: whether
the effect survives the paper's 2020 publication (post-publication decay is the norm), and
whether it holds on a liquid large-cap universe where the filings are read faster — both are
reasons the sign test comes before any screen.

