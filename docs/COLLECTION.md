# Collection procedure — sentiment, universe, options positioning

Added 2026-09-01 from the source probe (`claude/engine/source-probe-2026-09-01.md`).
**This document is authoritative for everything in it and overrides any older
instruction in a task prompt.** README section 1 still governs price, bars and
fundamentals; this governs the crowd, the universe and options positioning.

---

## 0. The rule that comes before all the others

**`WebFetch` is for PAGES, never for DATASETS.**

WebFetch runs a small model over the fetched content and returns that model's
summary. On a large file it sees a fragment, does not say so, and answers
confidently from the part it got. Proven on 2026-09-01:

| Asked | Answer | Reality |
|---|---|---|
| Cboe MU chain, total call/put volume | "5 calls, 9 puts" | MU trades ~30k contracts a day |
| Same file, the at-the-money contract | strike $500 | MU last trade **$956.65** |
| Same file, strikes 940-970 explicitly | `NO STRIKES IN RANGE FOUND`, highest received $765 | the full chain is in the file |
| FINRA short-volume file, four symbols | only AAPL; "excerpt ends before NVDA" | all four are in the file |
| slickcharts S&P 500 | 331 rows | 500 rows |

So: never ask WebFetch for a sum, a count, a lookup inside a big file, or "the top N
of" a long list. If a number requires reading a whole dataset, the current fetch path
cannot produce it and the honest output is null. **Also never trust a date WebFetch
CALCULATES** — asked to convert a Reddit `created_utc` it returned "May 2026" for a
timestamp that was four hours old. Quote raw timestamps; do the arithmetic in Python.

---

## 1. Universe — the connector's own lists, not the gainers page

`markets/gainers/` and `markets/active/` served Friday's rows on Monday and
contributed nothing usable on 2026-08-31. Replace them with two connector calls:

```
get_popular_watchlists                     -> list ids
get_watchlist_items(list_id=<id>)          -> symbols
```

Read-only. No egress gate, no cache, and **nothing is written to the account** —
following a list is NOT required to read it. Use these three:

| List | id | Use |
|---|---|---|
| Trending stocks | `ea06e8e3-e435-4d19-9b51-33f8930d8cb4` | retail attention at an actual broker |
| 100 most popular | `e8ef4c1f-244f-4db5-a582-c4c37f3c8e8e` | holder-count popularity |
| Daily movers | `eddbebe5-34cc-4df1-953c-d3e3cb55bc19` | movers — **filter it, see below** |

Also available and worth a call at the slots that need them: *Upcoming earnings*
(`a18cdf8c-46c3-4585-be8f-d2cd57ec8bb1`) as a cross-check on the earnings calendar,
and *IPO Access* (`8ce9f620-5bb0-4b6a-8c61-5a06763f7a8b`) alongside iposcoop.

**Daily movers must be filtered.** On 2026-09-01, 14 of its 20 entries were illiquid
foreign ADRs and OTC tickers (BLFBY, MITUY, ZSHGY, WEDXF, HBGRY, CSCMY, ANZLY, PZCUY,
INDFY, JMPLD, RDIB). `sentiment.py --watchlists` applies the filter and names every
exclusion in `_meta.watchlist_excluded`. Trending stocks and 100 most popular are clean.

Proof the lists are current: on 2026-09-01 **FRVO** appeared on Trending stocks, on
Daily movers, on StockTwits trending AND in the options scan, while the connector
quoted it at $18.85 against a $15.38 prior close — **+22.6% intraday**. **RZLV**
appeared on both lists at -19.5%.

### The saved scans (created 2026-09-01 on the Agentic account)

| Scan | id | What it does |
|---|---|---|
| Scan Desk — Universe (liquid movers) | `55138bd9-616c-4226-8996-50bf7aabdf22` | mcap > $2B, close > $5, 30d avg vol > 1M, **relative volume > 1.5x** |
| Scan Desk — Options activity | `cc3b6743-6019-4dc4-bf86-88cfbbd80042` | mcap > $1B, options volume > 5k, **relative options volume > 2x** |

Run with `run_scan`. Results are evaluated live at request time.

**Time-of-day caveat on the universe scan.** `Relative volume` is day volume against
a *full-day* 30-day average, so it is mechanically understated early in the session:
at 11:20 ET the 1.5x filter returned only 4 names. That is correct behaviour, not a
fault — but do not read "few rows" as "quiet tape" at the 08:00 and 10:00 slots. The
watchlists carry the breadth at those slots; the scan carries precision later.

**Scan cells are RAW, not display-formatted.** A `% Change` cell of `0.23146944` means
**+23.15%**, not 0.23% — every "%" column holds a ratio and must be multiplied by 100. Large
numbers arrive in scientific notation (`1.98e+11`), floats carry precision noise
(`-0.6800000000000068`), and an empty string means the datapoint is undefined for that
instrument (Market cap on an ETF, ATR on a recent IPO) while the row still matched the filters.

**The scan's `Sector` column returns numeric codes** (206, 207, 309, 104), not GICS
labels. GICS still requires the stockanalysis `/company/` scrape.

---

## 2. Options positioning — the 403 is routed around, not solved

`get_option_quotes` still returns HTTP 403 (README 9.1). But the **scanner** exposes
the OPTION filter group, and the Options activity scan returns, per name, live:

`Implied volatility` · `Historical volatility` · `Total call volume` ·
`Total put volume` · `Open interest` · `Options volume` · `Relative options volume` ·
a `Put/call volume ratio` expression column · `Earnings date`

Verified live 2026-09-01: TSLA IV 0.3815 / put-call 0.93 on 989k contracts;
AAPL IV 0.2281 / 0.37; PFE IV 0.1839 / **3.31** on 202k contracts and 2.26M open
interest; GTLB IV 0.8567 with IV/HV 1.55.

### Two rules that make this safe

**A put/call ratio off a thin chain is one block trade, not positioning.** In the same
run APG showed a ratio of **625.1** from 16 calls against 10,002 puts on 95 total open
interest; JAN 5,819 calls against 5 puts on ZERO open interest; STEP 10,009 against 1.
`sentiment.py` gates on both legs >= 500 contracts and >= 5,000 open interest, keeps
the rejected number as `put_call_ratio_illiquid` with a reason string, and never lets
it reach `put_call_ratio`. Show a rejected ratio as an anomaly if it is interesting;
never as sentiment.

**IV is NOT the implied move.** README 9.1 forbids substituting an estimate for
`implied_move_pct`, because the board renders that field as "options price a ±X%
move" — a claim only a real straddle quote supports. `sentiment.py` therefore emits
`expected_move_pct` (IV × √(days/365)) under its own name with an explicit basis
string, and **leaves `implied_move_pct` null**. Two different claims, two different
fields. Do not merge them.

---

## 3. Retail sentiment — what to fetch, and what never to fetch

| Source | Endpoint | Status |
|---|---|---|
| ApeWisdom WSB | `apewisdom.io/api/v1.0/filter/wallstreetbets/page/1` | **PAGE 1 ONLY** — see below |
| StockTwits trending | `api.stocktwits.com/api/2/trending/symbols.json` | live to the minute |
| StockTwits gauge | `stocktwits.com/symbol/<T>` (HTML) | live; transcribe score + label |
| Reddit raw | `arctic-shift.photon-reddit.com/api/posts/search?subreddit=<sub>&limit=100&sort=desc` | **`sort=desc` MANDATORY** |

### NEVER fetch these

- **`api.stocktwits.com/api/2/streams/symbol/<T>.json`** — cached with a variable TTL.
  Measured on 2026-09-01: NVDA's newest message was **50 hours old**, TSLA 31h, a small
  cap 10h, while `trending.json` was current to the minute. A scan polling symbol
  streams scores weekend chatter as today's. `sentiment.py` refuses this payload shape.
- **ApeWisdom page 2 and beyond.** Ranks recompute between HTTP requests, so page 2
  re-emits page-1 tickers with different numbers (AXON came back as rank 99 with 9
  upvotes *and* rank 101 with 10). Concatenating pages double-counts.
- **`apewisdom.io/` homepage** when you mean WSB — it aggregates all tracked
  subreddits and returns a different universe (SPY 252 vs 223 on the same day).
- **AltIndex** as a primary. It stamped "August 31, 2026 6:00 PM PST" while claiming a
  5-minute refresh, and its counts are 2-3x ApeWisdom's for the same tickers on the
  same day (TSLA 359 vs 157). Different corpus. Never mix the absolute numbers.
- **tradestie** (DNS dead), **swaggystocks** (JS shell), **redlib/teddit/PullPush**
  (all blocked or rate-limited), **Twitter/X and every Nitter mirror** (robots-blocked
  at the fetcher, before auth — a paid key would not help).

### Arctic Shift — the Reddit route that works

Reddit's own hosts are blocked by **our egress allowlist**, not by Reddit:
`reddit.com/robots.txt` is itself proxy-rejected, and WebFetch would have had to read
that file to report a robots block. Arctic Shift is the Pushshift successor, keyless,
~120k req/hour, and returns raw post AND comment bodies — the first route to a
sentiment score this system computes itself rather than renting.

Verified 2026-09-01 15:13 UTC: newest r/stocks post was **24 minutes old**.

- `sort=desc` is mandatory. Without it the default ordering returns ~55-day-old
  records and looks like a coverage gap.
- Server-side `query=` full-text search returns HTTP 500. Filter tickers locally.
- Comments: `/api/comments/search` with the same parameters.

---

## 4. Run the normaliser — every trap is handled in code, not in the prompt

```bash
cd "$SCAN_DIR"
python3 sentiment.py \
    --apewisdom apewisdom.json \
    --stocktwits-trending st_trending.json \
    --stocktwits-gauges st_gauges.json \
    --reddit reddit_posts.json \
    --watchlists rh_watchlists.json \
    --quotes quotes_min.json \
    --options-scan options_scan.json \
    --earnings-days earnings_days.json \
    --out sentiment.json \
    --merge-into scan_data.json          # writes retail blocks straight into candidates
# then, as before:
python3 scanner.py
```

Every flag is optional; a missing source degrades to absent rather than to zero, and
coverage normalisation handles it. Exit 2 means NO source answered — report that as a
degraded scan rather than publishing a board with an empty Intelligence pillar.

**`sentiment.py` is a DATA dependency, not an import.** Nothing imports it, so a task
prompt that copies the old file list cannot crash the way `archive.py` did on
2026-08-31 (ENG-01). Copy it when you can; when you cannot, the scan still runs and
the Intelligence pillar simply drops out of the denominator.

### What it fixes, that a prompt cannot be trusted to remember

| Trap | Handling |
|---|---|
| Duplicate tickers across ApeWisdom pages | deduped keeping the **lowest** rank; reported |
| `rank_24h_ago: 0` sentinel | → None, no rank delta computed |
| `mentions_24h_ago: null` or `0` | flagged `wsb_new_entrant`, **no** fake % change, no divide-by-zero |
| Corrupt upvotes (HIMS 3 mentions / **9001** upvotes; BAX **-1**) | clamped to 500/mention, floored at 0; reported |
| English-word tickers (IT ranked 20th, ALL 45th on 2026-09-01) | stop-list; ambiguous-but-real names (DTE, KEY, ALL...) kept **only** when a second feed corroborates |
| Crypto pairs in StockTwits trending (JASMY.X) | excluded from equity sentiment |
| `trending_score` does not sort the ranks | list position preserved verbatim; neither used as the ranking key |
| Thin-chain put/call ratios | gated, kept as `put_call_ratio_illiquid` with a reason |
| Vendors publish no timestamp | `_meta.vendor_timestamps_available: false` is put on the board |

---

## 4a. Filings — `filings_signal.json` (P-02 / E16, 2026-09-10)

A second optional staged input, alongside `technicals.json` and `sentiment.json`. It carries
the 10-K/10-Q text-change score ("Lazy Prices", `docs/DATA.md` §4) and is read by
`scanner.py` at the top of a run; when it is not in `$SCAN_DIR` nothing changes. **Not
scored** — it lands on each row's `features` block for `ic.py --by-feature`.

The scan slot does **not** fetch filings. A separate task on the box does, at most weekly
per name, under the SEC fair-access rules (`User-Agent` with a contact, ≤ 10 requests/s):

```bash
# on the box: URLs per symbol, in order — submissions index, then the two primary documents
python3 filings.py --edgar-plan --symbols ACME,WIDG --cik-map cik.json
# fetch step saves <batch>/<SYMBOL>/current.htm + prior.htm + meta.json
#   meta.json = {"filed", "form", "prior_filed", "accession", "prior_accession"}
#   (form, filed and the accessions straight from data.sec.gov/submissions; prior = same
#    form ~one year earlier, filings.pick_filing_pair does the choosing)
python3 filings.py --batch <batch> --out filings_signal.json
# then stage filings_signal.json into $SCAN_DIR before:
python3 scanner.py
```

Shape of the staged file (full schema in `docs/DATA.md` §4):
`{"_meta": {generated_at, threshold, n_symbols, n_changers, skipped}, "<SYMBOL>": {filed,
form, prior_filed, accession, prior_accession, change_score: {sections, risk_factors_change,
mdna_change, overall_change, changer, n_sections_compared, threshold, weights}}}`.

Rules that are in the code, not the prompt:

| Trap | Handling |
|---|---|
| A filing dated after the scan | `features_for` returns all-null — not knowable yet |
| A row with no `filed` date | no features (the point-in-time guard cannot run) |
| A section the parser could not find | `null` in `sections`, excluded from `overall_change`; **never 0** |
| No comparable section at all | `overall_change` and `changer` are `null`, not "unchanged" |
| Table of contents / cross-references | lose to the longest line-start body, never the section |
| Symbol not in the file | row carries the three keys as `null` — "not covered" ≠ "no change" |
| Threshold | provisional 0.15, travels in every output; reset from the archive's 80th percentile when there is one |

`filings.py` is a data dependency of the scan and an import of `scanner.py`; it is in
`engine/MANIFEST.txt` and the CI stdlib allowlist, and it imports nothing else from the engine.

---

## 5. Corroboration — the finding the probe actually forced

On 2026-09-01 ApeWisdom and StockTwits trending shared **4 of 15** top names, and
StockTwits' *own two* trending endpoints shared **exactly one ticker** with each
other. ApeWisdom ranked GPRO 91st with 3 mentions while AltIndex ranked it 2nd with
201. Twenty of a hundred points were resting on numbers no second source confirms.

So `sentiment.py` emits `sources`, `sources_count` and `corroborated` per ticker, and
the merge writes a board warning naming every single-source name. A live example from
that day: **FRVO** carried three independent sources (StockTwits trending, the RH
watchlists, the options scan) while **MU** carried one (110 ApeWisdom mentions, nothing
else) — and a reader can now see the difference.

**Corroboration is REPORTED, NOT SCORED.** Per README section 11, no new data point
enters the scoring model until the Saturday validation shows it ranks forward returns
better than what is already scored. Corroboration, `reddit_tone`, `put_call_ratio`,
`iv_hv_ratio` and `expected_move_pct` all join relative strength in that queue. What
DID change the scores is the bug fixes above — those are corrections, not features.

---

## 6. Other sources verified this probe

Add when a slot has budget; none is load-bearing.

| Use | Endpoint | Caveat |
|---|---|---|
| Market-wide put/call | `cboe.com/us/options/market_statistics/daily/` | **carries NO date stamp** — label prior-session unless corroborated |
| Off-exchange share | `cboe.com/us/equities/market_statistics/` | timestamped; regime-level dark-pool proxy |
| Breadth | `barchart.com/stocks/quotes/$ADDN`, `$ADRN`, `$S5FI`, `$S5TW` | value renders, **timestamp does not** (`[[ item.tradeTime ]]`) — label undated |
| New highs/lows | `barchart.com/stocks/highs-lows/summary` | self-dates; usually prior session |
| Macro calendar | `tradingeconomics.com/united-states/calendar` + `investing.com/economic-calendar/` | TE has consensus/forecast, investing.com has the impact tier the PM gate needs |
| Insiders | `marketbeat.com/insider-trades/purchases/` | ~1 day behind; **replaces openinsider**, which is unreachable |
| Short volume | `cdn.finra.org/equity/regsho/daily/CNMSshvol<YYYYMMDD>.txt` | large file — section 0 applies, early-alphabet lookups only |

**Do not retry:** GDELT (robots.txt unreachable), Finviz (404 on every quote URL), FRED
(needs a key; CSV undecodable), Nasdaq short interest, WSJ market data, McClellan
(`$MCON`/`$NYMO` 404), etf.com flows, Google Trends per-ticker, secform4,
insiderscreener, and every keyed news-sentiment API — AlphaVantage's free tier is
25 requests/**day** and `get_equity_news` already returns full article bodies.

**Cboe option chains** (`cdn.cboe.com/api/global/delayed_quotes/options/<T>.json`) are
reachable and fresh but **unusable on this fetch path** — section 0. They become
viable only with a raw-bytes fetch route.
