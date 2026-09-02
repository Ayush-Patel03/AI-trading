# Verified source map

First probed 2026-08-27, re-probed in full 2026-08-31, **re-probed again 2026-09-01** from the
Cowork sandbox. **Reachable is not the same as fresh** — read the staleness section before
trusting any price.

> **2026-09-01: `claude/engine/COLLECTION.md` supersedes this file for retail sentiment, the
> universe screen and options positioning.** Where the two disagree, COLLECTION.md wins. The
> corrections that forced it are marked **[CORRECTED 09-01]** below. Full evidence:
> `claude/engine/source-probe-2026-09-01.md`.

## THE RULE THAT COMES BEFORE ALL OTHERS — WebFetch is for PAGES, not DATASETS

**[ADDED 09-01]** WebFetch summarises fetched content with a small model. On a large file it
sees a fragment, **does not say so**, and answers confidently. Asked for the total call/put
volume in the Cboe MU option chain it returned "5 calls, 9 puts" (MU trades ~30k contracts a
day); asked for the at-the-money contract on a $956 stock it returned strike $500; asked
explicitly for strikes 940-970 it replied `NO STRIKES IN RANGE FOUND`, highest received $765.
The FINRA short-volume file "ended before NVDA". slickcharts' S&P 500 came back 331 rows of 500.

Never ask WebFetch for a sum, a count, a lookup inside a big file, or the top N of a long list.
**Never trust a date it CALCULATES** — it converted a four-hour-old Reddit timestamp to
"May 2026". Quote raw timestamps; do arithmetic in Python.

## PRIMARY SOURCE: the Robinhood connector (added 2026-08-31, extended 2026-09-01)

**Robinhood MCP calls are not scraping.** They are not subject to the egress allowlist, the
`PROVENANCE_REQUIRED` gate, or WebFetch's 15-minute cache, and they return authoritative
exchange data rather than a parsed web page. Everything the connector covers should come from
the connector.

| Tool | Gives | Batch |
|---|---|---|
| `get_equity_quotes` | Live last trade with venue timestamp, bid/ask, and the **official SIP settled prior close** | 20 symbols |
| `get_equity_historicals` | **OHLCV bars.** Daily, intraday down to 15-second, adjusted or raw | **10 symbols/call** |
| `get_equity_fundamentals` | Today's OHLCV, market cap, float, shares out, P/E, P/B, 52-week range **with dates**, avg volume 2wk/30d, dividend schedule, sector/industry, employees, CEO | 10 symbols |
| `get_financials` | Revenue, gross profit, net income, **net margin** by fiscal quarter or year | 20 symbols |
| `get_equity_technical_indicators` | RSI, MACD, ATR, ADX, MFI, CCI, Williams %R, Bollinger, Keltner, Donchian, Supertrend, VWAP, OBV, EMA/SMA, pivot points | 1 symbol |
| `get_earnings_calendar` | **Market-wide forward calendar**: date, **am/pm timing**, EPS estimate and actual, `verified` flag. Up to a 31-day window, `high_market_cap` filter | market-wide |
| `get_equity_news` | Full article bodies, minute-stamped, including analyst PT changes and FactSet consensus | 1 symbol |
| `get_indexes` + `get_index_quotes` | **Live VIX, SPX, NDX** to the second | many |
| `get_equity_price_book` | Level 2 depth — resting size per price level | 4 symbols |
| `get_option_chains` / `get_option_instruments` / `get_option_quotes` | Expirations and contract quotes → implied move. **`get_option_quotes` returns 403 on this account** | — |
| `get_sec_filing*` | Filing index, facts and content | — |
| **`get_popular_watchlists` + `get_watchlist_items`** | **[NEW 09-01]** Trending stocks, 100 most popular, Daily movers, Upcoming earnings, IPO Access. Read-only; following a list is NOT required to read it | per list |
| **`run_scan`** | **[NEW 09-01]** A real server-side screener. Two scans exist — see below | market-wide |

### The saved scans (created 2026-09-01 on the Agentic account)

| Scan | id |
|---|---|
| Scan Desk — Universe (liquid movers) | `55138bd9-616c-4226-8996-50bf7aabdf22` |
| Scan Desk — Options activity | `cc3b6743-6019-4dc4-bf86-88cfbbd80042` |

The Options activity scan is how the `get_option_quotes` 403 is routed around: its OPTION filter
group returns live **implied volatility, historical volatility, total call/put volume, open
interest, relative options volume** and a put/call ratio column, market-wide. It does **not**
give a straddle price, so `implied_move_pct` stays null — see COLLECTION.md §2.

### Do not compute what one call already gives you — and do not make ten calls for one answer

`get_equity_technical_indicators` is one symbol per call. **`get_equity_historicals` is ten.**
One bars call for ten symbols plus `technicals.py` produces MA50, MA200, RSI, ATR, true relative
volume, the overnight gap and the 52-week range for all ten — replacing what would otherwise be
thirty-plus indicator calls.

This was verified rather than assumed: `technicals.py`'s SMA(50) for NVDA returned 208.42 against
the connector's 208.41999999999993, and its ATR(14) returned 7.663022265 against 7.663022261.

### Bars come back as a FILE, not as context

A ten-symbol year of daily bars is ~90 KB and the harness persists it to a local path instead of
returning it inline. Point `technicals.py --bars` straight at that path. Never read the bars into
context to do the maths by hand.

### What the connector does NOT have — keep scraping these

Analyst consensus rating and mean price target (they appear only inside `get_equity_news` article
text), short float / short interest, beta, PEG, debt/equity, ROE, forward P/E, and **GICS sector
labels** — the connector uses its own taxonomy (NVDA is "Electronic Technology") and the
scanner's `Sector` column returns **numeric codes** (206, 207, 309), so this still needs the
stockanalysis `/company/` scrape.

### The price discrepancy that justifies all of this

On 2026-08-31 the connector had CIEN at **$378.87** with a $53.6B market cap and a $90.00–$637.51
52-week range. Boards built on the scraped path had been carrying CIEN near **$112**.

## THE ACCESS RULE FOR EVERYTHING STILL SCRAPED

**In a scheduled (unattended) run, a direct `WebFetch` on a URL you have not surfaced through
`WebSearch` FAILS.** Verified 2026-08-31 from inside a scheduled session:

```
{"error_type":"PROVENANCE_REQUIRED","source":"target",
 "message":"The permission request for this URL was not answered in time.
            Ask the user to approve the fetch or include the URL in a message, then try again."}
```

The route is: `WebSearch` with `allowed_domains`, then `WebFetch` the surfaced URL.

- One search per ticker surfaces several of its pages at once.
- **Prefer HTML pages to raw API paths** — search engines do not index the latter.
- `PROVENANCE_REQUIRED` is recoverable — search, then retry the fetch once.
- **[ADDED 09-01] `PROXY_REJECTED` with `source: proxy` is OUR egress allowlist, not the site.**
  That is an admin request, not a dead source. Reddit and openinsider fail this way. A real
  site-side block looks different (`ROBOTS_DISALLOWED`, or a hard 403 with `source: target`).
- Tell deep-dive subagents this explicitly or they will fetch directly and come back empty.

## Working — use these

### Price, fundamentals, classification
| URL | Gives |
|---|---|
| `stockanalysis.com/stocks/<T>/` | Live intraday price with timestamp, fundamentals, analyst rating + target, minute-stamped headlines. **Freshest scraped source found.** |
| `stockanalysis.com/stocks/<T>/statistics/` | MA50, MA200, beta, short float, avg volume, growth, margins, forward P/E, PEG, D/E, ROE, earnings date |
| `stockanalysis.com/stocks/<T>/company/` | **Sector, Industry, SIC code, employees** — the real classification |
| `stockanalysis.com/list/sp-500-stocks/`, `/list/biggest-companies/` | ~400 rows per call, triage only. **No volume column as of 2026-08-31.** |
| ~~`stockanalysis.com/markets/gainers/`, `/losers/`, `/active/`~~ | **[RETIRED 09-01]** Ran a full session stale twice. Replaced by the connector watchlists + saved scan. Do not go back to them. |
| `slickcharts.com/sp500` | **[NEW 09-01]** S&P 500 constituents + index weights + live change. Universe only — WebFetch truncated it at 331 of 500 rows, so never derive counts from it. |

### Sector rotation — one call gets all 11 GICS sectors with YTD
```
stockanalysis.com/etf/compare/xlk-vs-xlf-vs-xlv-vs-xle-vs-xly-vs-xli-vs-xlp-vs-xlu-vs-xlb-vs-xlre-vs-xlc/
```
Individual `/etf/<T>/` pages do NOT publish YTD; `/etf/<T>/performance/` is a 404. The compare
page is the only YTD source. Map: XLK=information_technology, XLF=financials, XLV=health_care,
XLE=energy, XLY=consumer_discretionary, XLI=industrials, XLP=consumer_staples, XLU=utilities,
XLB=materials, XLRE=real_estate, XLC=communication_services. **It has served untimestamped
prior-session rows three sessions running** — take day-changes from the individual pages.

### Retail sentiment — **[CORRECTED 09-01]**, see COLLECTION.md §3
| URL | Status |
|---|---|
| `apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` | **WORKS — PAGE 1 ONLY.** Ranks recompute between requests, so page 2 re-emits page-1 tickers with different values (AXON: rank 99 with 9 upvotes *and* rank 101 with 10). Concatenating pages double-counts. |
| ~~`apewisdom.io/api/v1.0/filter/stocks/page/1`~~ | **Works but no signal** — top ticker had 21 mentions, rank 18 had 1. |
| `api.stocktwits.com/api/2/trending/symbols.json` | **WORKS, live.** `trending_score` does NOT sort the ranks — do not use it as the ranking key. |
| `stocktwits.com/symbol/<T>` | **WORKS** — the 0-100 gauge is live. Price is reliable; **its day change % is not**. |
| ~~`api.stocktwits.com/api/2/streams/symbol/<T>.json`~~ | **NEVER USE.** Cached with a variable TTL: NVDA's newest message was **50 hours old** on 2026-09-01, TSLA 31h, a small cap 10h, while `trending.json` was current to the minute. `sentiment.py` refuses this payload shape. |
| `arctic-shift.photon-reddit.com/api/posts/search?subreddit=<sub>&limit=100&sort=desc` | **[NEW 09-01] WORKS.** Pushshift successor, keyless, ~120k req/hr, raw post AND comment bodies. Verified 24 minutes fresh. **`sort=desc` is mandatory** — without it the default ordering returns ~55-day-old records. Server-side `query=` returns 500; filter locally. |
| ~~`altindex.com/wallstreetbets`~~ | **Demoted.** Stamped "August 31 6:00 PM PST" while claiming a 5-minute refresh; counts are 2-3x ApeWisdom's for the same tickers. Different corpus — never mix absolute numbers. |
| ~~`tradestie.com/api/v1/apps/reddit`~~ | **DEAD 09-01** — DNS does not resolve. |
| ~~`swaggystocks.com`~~ | **DEAD** — JS shell, `api.swaggystocks.com` 404s. |

**Reddit's own hosts are blocked by OUR egress allowlist, not by Reddit.** **[CORRECTED 09-01]**
`reddit.com/robots.txt` is itself `PROXY_REJECTED` with `source: proxy`; Reddit serves that file
to everyone, so a robots block would have had to report itself as one. This is an admin request
(`claude/engine/allowlist-request.md`), not a permanent dead end. redlib, teddit, PullPush and
r.jina.ai were all tested and all fail.

**Neither ApeWisdom nor StockTwits publishes a timestamp**, so a stalled feed is undetectable
from the payload. And they do not corroborate each other: on 2026-09-01 they shared 4 of 15 top
names, and StockTwits' own two trending endpoints shared exactly ONE ticker. `sentiment.py`
therefore scores attention on corroboration and labels every single-source name.

### Earnings calls
| URL | Gives |
|---|---|
| `stockanalysis.com/stocks/<T>/transcripts/` | Transcript index back to 2011 with dated links |
| `news.alphastreet.com/...` | Full transcript body — verified working |
| `fool.com/earnings-call-transcripts/` | Daily transcript index (~20/day) |
| `fool.com/earnings/call-transcripts/<YYYY>/<MM>/<DD>/<slug>/` | Full body: guidance figures, tone, complete analyst Q&A |
| `tipranks.com/calendars/earnings` | Forward calendar with EPS/revenue estimates |

**Transcript URLs are not constructible from a ticker.** Always resolve via the index page or
WebSearch first, then fetch. Budget two calls per name.

### IPO
| URL | Gives |
|---|---|
| `iposcoop.com/ipo-calendar/` | **PRIMARY forward calendar** — deals plus lead managers/underwriters |
| `stockanalysis.com/ipos/calendar/` | Secondary. Returned **zero rows** on 2026-08-31 while iposcoop listed five |
| `stockanalysis.com/ipos/` | Priced deals with return from offer |
| `stockanalysis.com/ipos/<YYYY>/` | Full year |
| `renaissancecapital.com/IPO-Center/Pricings` | Return-since-IPO cross-check |
| RH watchlist *IPO Access* | **[NEW 09-01]** `8ce9f620-5bb0-4b6a-8c61-5a06763f7a8b`, 48 names |

Lockup expiry is published by none of them — only estimable as IPO date + 90/180 days.

### Filings and insiders
| URL | Gives |
|---|---|
| `efts.sec.gov/LATEST/search-index?q=<phrase>&forms=<FORM>&startdt=&enddt=` | **Open Elasticsearch JSON**, server-side form and date filters. `forms=4` and `forms=13F-HR` both verified 09-01 — metadata only, so identifying a *purchase* means fetching each Form 4 XML for `<transactionCode>P</transactionCode>` |
| `marketbeat.com/insider-trades/purchases/` | **[NEW 09-01] The insider feed to use.** Market-wide Form 4 purchases with insider, role, shares, price, value. ~1 day behind filing |
| ~~`openinsider.com/*`~~ | **Unreachable** — `Host not in allowlist` from the egress proxy, plus an https-upgrade redirect loop. Allowlist request filed |

### Breadth, internals and options — **[UPDATED 09-01]**
| Metric | Source | Freshness |
|---|---|---|
| NYSE advance-decline difference | `barchart.com/stocks/quotes/$ADDN` | 15-20 min delayed; **the page's timestamp does not render** (`[[ item.tradeTime ]]`) — label undated |
| NYSE A/D ratio | `barchart.com/stocks/quotes/$ADRN` | same caveat |
| % S&P above 50-DMA / 20-DMA | `barchart.com/stocks/quotes/$S5FI`, `$S5TW` | same caveat |
| New highs / new lows | `barchart.com/stocks/highs-lows/summary` | self-dates; usually prior session |
| Market-wide put/call | `cboe.com/us/options/market_statistics/daily/` | **carries NO date stamp at all** — label prior-session unless corroborated |
| Off-exchange (TRF) share | `cboe.com/us/equities/market_statistics/` | timestamped; regime-level dark-pool proxy |
| Per-name IV / put-call / OI | the Options activity saved scan | live, authoritative |
| VIX | connector `get_indexes` → `get_index_quotes` | live to the second — **do not scrape this** |
| McClellan | **no free source found**; all `$MCON`/`$NYMO`/`$BPSPX` variants 404 | compute from the `$ADDN` series once ~40 days of history exist |

### Macro calendar — **[NEW 09-01]**
`tradingeconomics.com/united-states/calendar` (previous / consensus / forecast) and
`investing.com/economic-calendar/` (adds the **impact tier** the PM macro gate needs). Run both.
Times render in UTC on TradingEconomics — confirm the convention once before trusting a gate.

### News
`benzinga.com/quote/<T>` (dated headlines, but see staleness), `investing.com/equities/<slug>`
(analyst actions; slug not ticker), `stockanalysis.com/news/` (market-wide),
`globenewswire.com/en/search/keyword/<COMPANY>`, `prnewswire.com/news-releases/news-releases-list/`.
The connector's `get_equity_news` returns full article bodies and should be the primary.

## DEAD — never retry
`x.com` / `twitter.com` / `api.x.com` / every Nitter mirror (**[CONFIRMED 09-01]** robots-blocked
at the fetcher, *before* authentication — a paid key would not help), `reuters.com`, `cnbc.com`,
`marketwatch.com`, `apnews.com`, `ft.com`, `zacks.com`, `seekingalpha.com` (login wall),
`sec.gov/cgi-bin/browse-edgar` (robots), `sec.gov/Archives/edgar/daily-index/*` (403),
`nasdaq.com/market-activity/*` (JS, never hydrates) and its short-interest API (robots),
`swaggystocks.com` (JS), `tradestie.com` (DNS), `discountingcashflows.com` (405), `secform4.com`,
`insiderscreener.com` (404), `finviz.com/quote.ashx` (**404 on every quote URL, 09-01**),
`stockanalysis.com/stocks/<T>/earnings/` and `/markets/earnings/` and `/calendar/earnings/` (404),
`wsj.com/market-data/*` (403), `stockcharts.com` (robots / JS), `barchart.com/stocks/market-breadth`
(404), `barchart.com/options/unusual-activity/*` and `marketchameleon.com` (JS shells),
`macromicro.me` (403), `indexindicators.com` (JS), `api.gdeltproject.org` (**robots.txt
ConnectTimeout — unfixable from here**), `trends.google.com` per-keyword API (404; the RSS feed
works but returns general trending searches, useless for equities), `etf.com` (403),
`api.quiverquant.com` (paid), and every keyed news-sentiment API — AlphaVantage's free tier is
**25 requests/day**, Finnhub / Marketaux / newsdata / tickertick / stockgeist all 401/403/404
without a key, and `get_equity_news` already returns full bodies.

`cdn.cboe.com/api/global/delayed_quotes/options/<T>.json` and
`cdn.finra.org/equity/regsho/daily/CNMSshvol<date>.txt` are **reachable and current but unusable**
on this fetch path — see the WebFetch rule at the top. They are in the allowlist request.

Also unreachable for the sentiment bundle: `raw.githubusercontent.com` 404s on the
`trading-system` repo because it is private, and there is no `gh` CLI in the sandbox.
**`project_search` RAG is the only route to repo files**, it returns fragments rather than whole
files, and its index can lag the repo — always check `_meta.generated_at`.

## Staleness — the thing that will burn you

In a single session on 2026-08-27, NVDA came back as:

| Source | Price | Reality |
|---|---|---|
| stockanalysis `/stocks/nvda/` | $225.65 | correct, intraday, timestamped |
| stocktwits | $225.49 | correct, agrees within 0.07% |
| benzinga | $209.66 | prior session's close, **7.6% low** |
| stockanalysis homepage | $182.70 | badly stale |
| seekingalpha | $205.19 | stamped two months earlier |
| yahoo | ~$148 | months stale |

**Benzinga staleness varied per ticker in one run**: CIEN 14 days, LITE 6 days, DELL 2 days,
NVDA/VRT/WDC/PLTR 1 session, MU same-day. There is no single staleness offset to correct for.

Rules that follow from this:
1. Take prices from the connector. Use `stockanalysis.com/stocks/<T>/` only as the cross-check.
2. Record `price_sources` and `price_disagreement_pct` per name.
3. Under 1% apart = confirmed. 1-3% = approximate. Over 3% = **conflict, do not trade on it**.
4. Use Benzinga for headlines, never for prices.
5. WebFetch caches 15 minutes per URL — two fetches minutes apart can still differ.
6. **[ADDED 09-01]** A page that shows a value but no timestamp is not "current" — it is
   undated. Barchart's breadth quotes and Cboe's daily options statistics are both in this
   category. Label them, do not assume them.
