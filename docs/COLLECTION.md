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
| StockTwits symbol stream (optional, P-04) | `api.stocktwits.com/api/2/streams/symbol/<T>.json` → `st_symbol_<T>.json` | **bull share ONLY**, never attention — see §3b |
| Google Trends (optional, P-04) | any route that yields a dated interest series → `trends.json` | reported, not scored — see §3b |

### NEVER fetch these

- **`api.stocktwits.com/api/2/streams/symbol/<T>.json` as an ATTENTION input** — cached
  with a variable TTL. Measured on 2026-09-01: NVDA's newest message was **50 hours old**,
  TSLA 31h, a small cap 10h, while `trending.json` was current to the minute. A scan
  polling symbol streams scores weekend chatter as today's. `sentiment.py` still refuses
  this payload shape under `--stocktwits-trending`. Since P-04 the same payload, staged as
  `st_symbol_<T>.json`, is read for one different thing — the crowd's stated Bullish /
  Bearish split — and reported WITH the age of its newest message (§3b).
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

### Reddit — gone (P-04, 2026-09-10)

The Arctic Shift route (`arctic-shift.photon-reddit.com`) turned out to be a periodic dump,
not a feed, and Reddit's own API needs an approval this account does not have. The
`--reddit` flag, `reddit_posts.json` and the `reddit_*` keys are removed; nothing the scanner
scored ever read them. `docs/scan-sources.md` keeps the one-paragraph record.

## 3b. Attention fade — the optional files (P-04, 2026-09-10)

`sentiment.py` now measures whether retail attention is ARRIVING or LEAVING, not just how
much there is. Two of the inputs are optional files a slot may stage; the third is state the
runner carries between scans. Everything here is REPORTED, NOT SCORED: the scanner's pillar
arithmetic reads the same keys it always did, and the new fields ride on each candidate's
`retail` block and its `features` dict for the sign test in `ic.py --by-feature`.

**`st_symbol_<SYM>.json`** — one file per name, the RAW public symbol-stream response,
file name upper-case (`st_symbol_NVDA.json`). The symbol is taken from the FILE NAME, never
from the payload, so a stream staged under the wrong name is visible as the operator's
error. Exact shape read (every other key is ignored):

```json
{"symbol": {"id": 1, "symbol": "NVDA"},
 "messages": [
   {"id": 1001, "created_at": "2026-09-10T14:03:11Z",
    "entities": {"sentiment": {"basic": "Bullish"}}},
   {"id": 1002, "created_at": "2026-09-10T13:58:40Z",
    "entities": {"sentiment": {"basic": "Bearish"}}},
   {"id": 1003, "created_at": "2026-09-10T13:51:02Z",
    "entities": {"sentiment": null}}
 ]}
```

`entities.sentiment.basic` is `"Bullish"`, `"Bearish"` or absent/null (untagged). Output per
name: `st_bull_pct` = Bullish / (Bullish + Bearish) × 100, **null when no message is tagged**
(never 50), `st_bull_n`, `st_bear_n`, `st_msgs`, `st_newest_utc`, `st_stream_age_min` and
`st_stream_stale` (newest message over 24 h old — warned, still reported). Stage one for
each finalist when the slot has budget; missing files mean the keys are simply absent.

**`trends.json`** — a Google Trends interest series per symbol, from whatever route was
available (there is no keyless API; a hand-transcribed series is fine). Exact shape:

```json
{"NVDA": [{"date": "2026-08-20", "value": 48},
          {"date": "2026-08-21", "value": 51},
          {"date": "2026-09-10", "value": 90}],
 "MU":   [{"date": "2026-09-09", "value": 12}]}
```

`date` is `YYYY-MM-DD` (longer ISO strings are truncated), `value` a number. The newest
point is "today"; `trends_z` is its z-score against the trailing 20 points before it (sample
std), **null under 5 prior points or a flat series**; `trends_latest`, `trends_date`,
`trends_mean`, `trends_n` ride alongside.

**`archive/attention_history.json`** — NOT a collection input. `sentiment.py` maintains it
under `$SCAN_DIR` (`--attention-history`, default `archive/attention_history.json`; `''`
disables): the last 20 scans' ApeWisdom mention and upvote count per symbol, keyed by
scan hour so a re-run inside the hour replaces itself. The runner stages it from
`C:\ai-trading-state\archive\` before the scan and writes it back after, the same round trip
as `followed.json`. Shape, for the record:

```json
{"_meta": {"window": 20, "min_history": 5, "spike_z": 2.0, "updated": "2026-09-10T16:30:00+00:00"},
 "symbols": {"NVDA": [{"ts": "2026-09-09T16:30:00+00:00", "mentions": 300, "upvotes": 700},
                      {"ts": "2026-09-10T16:30:00+00:00", "mentions": 412, "upvotes": 900}]}}
```

From it, per ApeWisdom name: `attention_count` (today's mentions), `attention_z` (today
against the trailing 20 PRIOR scans' mean / sample std — **null under 5 prior scans or a flat
history**), `attention_spike` (`attention_z > 2`; null when the z is), `attention_rank_pct`
(percentile of today's mentions across the whole page-1 feed, 100 = most mentioned),
`attention_mean_20`, `attention_history_n`. A name on the history but off today's page 1 is
recorded at 0 so a fade shows as one. The first scan seeds the file and every z is null; the
sixth scan is the first with a number.

---

## 4. Run the normaliser — every trap is handled in code, not in the prompt

```bash
cd "$SCAN_DIR"
python3 sentiment.py \
    --apewisdom apewisdom.json \
    --stocktwits-trending st_trending.json \
    --stocktwits-gauges st_gauges.json \
    --stocktwits-symbols \                # every st_symbol_<SYM>.json in $SCAN_DIR (optional)
    --trends trends.json \                # optional
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
| English-word tickers (IT ranked 20th, ALL 45th on 2026-09-01) | ambiguous-but-real names (DTE, KEY, ALL, IT...) kept **only** when a second feed corroborates; the wider stop-list gated the (removed) Reddit text path |
| Crypto pairs in StockTwits trending (JASMY.X) | excluded from equity sentiment |
| `trending_score` does not sort the ranks | list position preserved verbatim; neither used as the ranking key |
| Thin-chain put/call ratios | gated, kept as `put_call_ratio_illiquid` with a reason |
| Vendors publish no timestamp | `_meta.vendor_timestamps_available: false` is put on the board |

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
better than what is already scored. Corroboration, `attention_z`, `st_bull_pct`, `put_call_ratio`,
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
