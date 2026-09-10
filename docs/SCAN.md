# Scan Desk engine + run procedure — the authoritative copy

**Every scheduled scan loads the Python files from `claude/engine/` in this project, not the
copies bundled in the `market-scan` skill.** Audited and repaired 2026-08-31; verified from
inside a real scheduled session the same day (`claude/engine/preflight-report.md`); rebuilt the
same day onto the Robinhood connector as the primary data source; patched again the same
evening against the "Agentic Trading Audit" board (section 10); extended 2026-09-01 with the
source probe's findings (section 12).

Read this whole file before collecting anything. Sections 1-3 are what actually changed.
**Section 3 now lists EIGHT engine files — `archive.py` is imported by `scanner.py` and
`render.py` at load time, and a run that does not copy it dies on its first Python command.
`sentiment.py` is the eighth and is deliberately NOT imported by anything.**
**Section 3 also carries the hand-off to the Portfolio Manager (`claude/latest-scan.json`),
which no scan had written before 2026-09-01.** Section 9 lists the live data defects found at
the 15:00 slot on 2026-08-31 — 9.2 is now fixed inside `technicals.py`, 9.1 is routed around
as of 2026-09-01 (section 12) and 9.3 still needs a network change.

**Section 12 is new and takes precedence for sentiment, universe and options collection.**

---

## 1. COLLECT FROM THE CONNECTOR FIRST. Scrape only what it lacks.

Robinhood MCP calls are **not** scraping. No egress allowlist, no `PROVENANCE_REQUIRED` gate,
no 15-minute cache, and authoritative exchange data rather than a parsed page. All verified
live 2026-08-31.

| What you need | Call | Symbols per call |
|---|---|---|
| Live price, bid/ask, official SIP prior close, extended-hours print | `get_equity_quotes` | 20 |
| **OHLCV bars** | `get_equity_historicals` | **10** |
| Market cap, float, P/E, 52-week range with dates, avg volume, today's open and volume | `get_equity_fundamentals` | 10 |
| Revenue, gross profit, net income, net margin by quarter | `get_financials` | 20 |
| Forward earnings calendar — date, **am/pm**, EPS estimate, verified flag | `get_earnings_calendar` | whole market |
| Full news bodies incl. analyst PT changes and consensus | `get_equity_news` | 1 |
| **Live VIX / SPX / NDX** | `get_indexes` → `get_index_quotes` | many |
| Level 2 depth | `get_equity_price_book` | 4 |
| **Universe / retail attention lists** | `get_popular_watchlists` → `get_watchlist_items` | per list — **see §12** |
| **Implied volatility, put/call volume, options OI** | `run_scan` on the Options activity scan | whole market — **see §12** |
| Implied move (power hour only) | chains → instruments → quotes | — **BLOCKED, see §9.1 and §12** |

### The one mistake that would waste the whole gain

`get_equity_technical_indicators` takes **one symbol per call**. Do not loop it.
`get_equity_historicals` takes **ten**. One bars call plus `technicals.py` yields MA50, MA200,
RSI(14), ATR(14), ATR%, true relative volume, the overnight gap and the 52-week range for all
ten symbols — replacing thirty-plus indicator calls.

This is verified, not assumed: `technicals.py` SMA(50) on NVDA returned 208.42 against the
connector's 208.41999999999993; ATR(14) returned 7.663022265 against 7.663022261. Use the
indicator tool only for something `technicals.py` does not compute (MACD, ADX, Bollinger, VWAP),
and only on a name that earns the call.

### Bars arrive as a FILE, not as context

Ten symbols × a year of daily bars is ~90 KB, and the harness saves it to a local path instead
of returning it inline. Point `technicals.py --bars` at that path. **Never read bars into
context to do the maths by hand.**

### Still scraped, because the connector does not have it

Analyst consensus rating and mean price target (they appear only inside `get_equity_news` prose),
short float, beta, PEG, debt/equity, ROE, forward P/E, **GICS sector labels** (the connector's
taxonomy is its own — NVDA is "Electronic Technology", not "information_technology"; the
scanner's own `Sector` column returns numeric codes, so this still needs the scrape), and retail
sentiment (ApeWisdom, StockTwits, Arctic Shift). Section 2 governs every one of those fetches.

### Why this matters beyond speed

On 2026-08-31 the connector had CIEN at **$378.87**, market cap $53.6B, 52-week range
$90.00–$637.51. Boards built on the scraped path had been carrying it near **$112**. An
authoritative quote with a venue timestamp and an official SIP close is a different class of
input from a parsed list page, and a ranking should not rest on the latter.

---

## 2. For anything still scraped — search before you fetch

**A direct `WebFetch` on a URL you have not surfaced through `WebSearch` FAILS in a scheduled
run.** Verified 2026-08-31 from a scheduled session:

```
{"error_type":"PROVENANCE_REQUIRED","source":"target",
 "message":"The permission request for this URL was not answered in time. ..."}
```

It reproduced **even for URLs written verbatim in the task prompt** — a scheduled run is
unattended, so the approval times out every time. The same URL fetched perfectly once a
`WebSearch` had surfaced it (NVDA $219.84, "Aug 31, 2026, 1:15 PM EDT").

- `WebSearch` with `allowed_domains: ["stockanalysis.com"]`, then fetch what it surfaces.
- One search per ticker unlocks several of its pages. Do not search per page.
- Prefer HTML pages to raw API paths — search engines do not index the latter.
- `PROVENANCE_REQUIRED` is recoverable: search, retry once. Never file it as a dead source.
  A real block looks different (`cnbc.com/quotes/.VIX` returns a hard 403).
- **A `PROXY_REJECTED` with `source: proxy` is OUR egress allowlist, not the site.** It is not
  a dead source either — it is an admin request. Reddit and openinsider fail this way.
- **Say this explicitly in every deep-dive subagent's prompt** or they will fetch directly and
  come back empty. Confirmed working at the 15:00 slot on 2026-08-31: six deep-dive agents,
  nine pages each, zero `PROVENANCE_REQUIRED` failures when the instruction was included.

### 2b. WebFetch is for PAGES, never for DATASETS (added 2026-09-01)

WebFetch summarises the fetched content with a small model. On a large file it sees only a
fragment **and does not say so**. Asked for MU's total call and put volume it answered "5 calls,
9 puts"; asked for the at-the-money contract on a $956 stock it returned strike $500; asked
directly for strikes 940-970 it replied `NO STRIKES IN RANGE FOUND` with the highest strike
received being $765. The FINRA short-volume file came back having "ended before NVDA".

Never ask WebFetch for a sum, a count, a lookup inside a large file, or the top N of a long
list. **And never trust a date it CALCULATES** — it converted a four-hour-old Reddit timestamp
to "May 2026". Quote raw timestamps and do the arithmetic in Python.

---

## 3. Run procedure

```bash
export SCAN_DIR=/home/claude/market-scan && mkdir -p "$SCAN_DIR"
```

`project_read` these **eight** files and write each into `$SCAN_DIR`, byte-exact. **Do this
first, or while the deep-dive subagents are collecting — never serially after them.** On
2026-08-31 the file writes, not the collection, were what pushed the 15:00 slot 63 minutes late.

| File | Purpose |
|---|---|
| `claude/engine/technicals.py` | OHLCV bars → MA50/MA200, RSI, ATR, rel volume, gap, 52w range |
| `claude/engine/scanner.py` | scoring engine → `scan_results.json` (imports `archive`) |
| `claude/engine/archive.py` | run ids, per-run file names, compact records, the scan index (imported by `scanner.py`, `render.py` and `pm.py`) |
| `claude/engine/render.py` | dashboard → `scan-desk.html` (imports `archive`) |
| `claude/engine/portfolio.py` | holdings + ATR-sized order proposals → `portfolio_state.json` |
| `claude/engine/render_portfolio.py` | portfolio panel (imported by `render.py`) |
| `claude/engine/history.py` | deterministic history merge — section 5 |
| `claude/engine/sentiment.py` | retail/options normaliser → `sentiment.json`, merged into `scan_data.json` — section 12. **Imported by nothing:** a run that fails to copy it degrades, it does not crash |

**`archive.py` was missing from this table until 2026-09-01.** It was added to the project at
20:42Z on 2026-08-31 — after that day's last scan had started — and three modules import it at
load time. Every task prompt that copied "six files" would have crashed with
`ModuleNotFoundError: No module named 'archive'` on its first `python3` command. The audit that
caught it is section 10. `sentiment.py` was deliberately built as a data dependency so it can
never repeat that failure.

**Never run the engine in place from the skill directory.** The synced skill bundle holds
*older files with the same names* — on 2026-08-31 its `scanner.py` was 23,822 bytes with no
coverage normalisation against the project's 30,099. Running it silently executes the pre-fix
engine: same command, exit 0, plausible output, every score 15-20 points low.

**Copying caution.** `scanner.py`'s final lines contain literal escape sequences (`•`,
`—`) inside string literals. Write them through unchanged. Verify:

```bash
grep -c 'u2022\|u2014' "$SCAN_DIR/scanner.py"   # expect 2
```

Note: a tool-based file write may silently normalise those two escapes into real `•` and `—`
characters. The program behaves identically either way, but the guard returns 0 and the copy is
no longer byte-exact. If it returns 0, rewrite just those two lines with the escapes intact
rather than ignoring the check — that is what the check is for.

Then:

```bash
cd "$SCAN_DIR"
# Intraday slots (10:00, 12:30, 15:00): pass the RAW get_equity_quotes response as --quotes so
# gap_pct and the 52-week fields describe TODAY (section 9.2 — fixed inside technicals.py).
python3 technicals.py --bars <bars file path> --fundamentals fundamentals.json \
                      --quotes quotes.json --out technicals.json
# Pre-market slot (08:00): there is no session open yet, so add --premarket — the gap becomes
# the extended-hours print against the official prior close.
python3 technicals.py --bars <bars file path> --fundamentals fundamentals.json \
                      --quotes quotes.json --premarket --out technicals.json
# merge technicals.json into each candidate in scan_data.json
# SENTIMENT (section 12): normalise the crowd + options payloads and merge them in.
python3 sentiment.py --apewisdom apewisdom.json --stocktwits-trending st_trending.json \
                     --stocktwits-gauges st_gauges.json --reddit reddit_posts.json \
                     --watchlists rh_watchlists.json --quotes quotes_min.json \
                     --options-scan options_scan.json --earnings-days earnings_days.json \
                     --out sentiment.json --merge-into scan_data.json
python3 scanner.py            # → scan_results.json + ranked console table
# >>> HAND-OFF, IMMEDIATELY: project_write scan_results.json → claude/latest-scan.json  <<<
# PAPER MIRROR (STATE-01): build portfolio.json from the paper books, so the panel and
# its risk gates describe the book the manager actually holds — not the flat live account.
python3 paper_mirror.py       # → portfolio.json  (exit 2 = refused; then skip portfolio.py)
python3 portfolio.py          # → portfolio_state.json  (only if portfolio.json exists)
python3 render.py             # → scan-desk.html
# publish, then ARCHIVE (section 3b):
python3 archive.py --record --artifact-url <live board url>    # → scan-record-<run_id>.json
python3 archive.py --index --current index_current.json --out index_merged.json
```

**Include SPY in one of the `get_equity_historicals` calls.** `technicals.py --benchmark SPY`
(the default) then derives `rs_20d_vs_SPY` / `rs_60d_vs_SPY` for every name — its trailing
return minus the benchmark's. It is *reported* in the momentum reasons and archived, and
deliberately **not scored** until the Saturday validation (section 11) shows it ranks forward
returns on this universe. That is the rule for every new data point from now on.

**Macro events.** The pre-market scan writes the day's scheduled releases into
`scan_data.json` as `meta.macro_events = [{date, name, time_et}]`; later slots carry them
forward from `claude/latest-scan.json`. `scanner.py` banners a same-day release in NOTABLE.
The Portfolio Manager gates entries on them, but only on a HIGH-IMPACT release (FOMC, CPI,
PCE, payrolls, GDP, Powell — not ADP, JOLTS, ISM, the Beige Book or a Fed speech) and only
within 120 minutes of the print: see PM.md, "The macro gate", retuned 2026-09-01 because the
first version would have frozen every entry slot on two consecutive real calendars.
Collect the whole calendar anyway — the non-gating items are still reported.

### 3b. Archive the run — designed on 2026-08-31, executed by nothing until 2026-09-01

`archive.py --record` writes a compact (~10 KB) record of the run — scores, pillars, verdicts,
the regime, every reported field in `ROW_KEEP`, the artifact URL — and the scan prompt
`project_write`s it to `claude/scans/<run_id>.json`. `archive.py --index` merges the run into
`claude/scan-index.json`, newest first, forty runs kept, and prints `PRUNE:` lines for records
that fell off the window. The audit found `scan-index.json` empty after nine runs: the code
existed, the prompts never called it. They do now, and **the archive is what section 11's
validation reads** — without it the model can never be tested.

### The hand-off to the Portfolio Manager

**The moment `scanner.py` succeeds, `project_write` `scan_results.json` verbatim to
`claude/latest-scan.json` — before rendering, before publishing.** The Portfolio Manager
(`claude/engine/PM.md`) fires 45 minutes behind each scan slot and reads that file for its
candidates. If it is missing or stale the manager freezes entries and the book trades nothing,
and it does so *gracefully* — the run reports success, the journal says "no scan results
available this run", and nothing looks broken. That is exactly what happened on every PM run of
2026-08-31: the instruction to write this file had been lost from this README when section 9 was
rewritten, and no task prompt carried it either. It is now in this file **and** in all four scan
task prompts, and the 2026-09-01 one-off verification task checks the file exists.

All eight resolve paths from `SCAN_DIR`. No path is hardcoded. The engine runs in **under a
second** — it is never why a scan is late.

---

## 4. Time budget

30 minutes of collection. When it runs out, stop and score what you have — coverage
normalisation makes an early stop safe and honest. **Target: `claude/latest-scan.json` written
within 40 minutes of the slot time.** The Portfolio Manager runs 45 minutes behind the slot and
enforces a 240-minute scan-freshness limit; a scan that lands hours late is a scan the manager
may refuse to enter on.

| Stage | Connector | Searches | Fetches |
|---|---:|---:|---:|
| Regime — live VIX/SPX/NDX, index quotes and fundamentals | 4 | 0 | 0 |
| Regime — sector rotation (11-ETF compare) | 0 | 1 | 1 |
| Universe screen — watchlists + saved scan (§12) | **4** | 0 | 0 |
| Deep dive — quotes + bars + fundamentals + financials, all 8 names | **4** | 0 | 0 |
| Earnings calendar — whole market | 1 | 0 | 0 |
| News — top 5 | 5 | 0 | 0 |
| Analyst / short float / beta / PEG / GICS — scraped, 8 names | 0 | 8 | 8 |
| Retail — ApeWisdom p1, StockTwits trending, gauges, Arctic Shift | 0 | 1 | 4 |
| Options positioning — one saved scan (§12) | 1 | 0 | 0 |
| Transcripts — midday only, ≤2 names | 0 | 2 | 4 |

Roughly **19 connector calls and 12 gated fetches**, against 35 gated fetches before. The
connector half has no provenance step and no cache — that is what buys the slot back. The
universe screen and options positioning moved from scrapes to connector calls on 2026-09-01,
which is why the connector column grew and the fetch column did not.

Carry-forward names scored earlier today do not need a fresh deep dive; refresh price and
volume only. Fundamentals do not change between 10:00 and 12:30.

If the run finishes more than 45 minutes after its slot time, set `meta.minutes_late` in
`scan_data.json` before running `scanner.py`. `render.py` then banners the board as a late scan.

**Budget reality check from 2026-08-31:** the four slots finished 170, 233, 114 and 58 minutes
after firing. 18 candidates at power hour cost 9 connector calls and nine parallel subagents —
that fits — but the run still finished 63 minutes late because the engine files were written
one at a time after collection. Dispatch every subagent in a single message, cap deep dives at
8 names, and write the engine files while the subagents run.

---

## 5. Writing history — do not hand-merge

`claude/scan-history.json` is written by four overlapping sessions a day, and **a scheduled
session can survive for days and resume**. On 2026-08-31 a scan fired the previous Friday
finished at 16:43 UTC and wrote Friday-dated scores over Monday's file, five minutes after it
had been reset; earlier that morning another stale session destroyed the day's pre-market entry
twelve seconds after it was written. Prose does not survive that, so the merge is a pure function:

```bash
# project_read claude/scan-history.json → history_current.json  (omit --current if absent)
# write your entry to history_entry.json — or let archive.py build it:
#   python3 archive.py --history-entry history_entry.json
#   {date, slot, time, regime_label, avg, top, coverage_avg, scores:{TICKER: score}}
cd "$SCAN_DIR"
python3 history.py --current history_current.json --entry history_entry.json \
                   --today $(date -u +%F) --out history_merged.json
# project_write history_merged.json back to claude/scan-history.json, verbatim.
```

Exit 2 means refused — stale-dated or malformed. **Do not work around a refusal.** Report the
run as stale and leave the file alone.

Note `history.py` drops any entry not dated today, including a deliberately kept reference entry
from a prior session. That is correct and intended — the dashboard shows one day's tape — but it
means the Friday 2026-08-28 baseline that sat in the file through Monday is gone after the first
merge of the day. Put anything worth keeping in the entry's `_note`, not in a sibling entry.

---

## 6. What changed in the model on 2026-08-31

**Momentum pillar, still 15 points, rebalanced to make room for real data:** 52-week change (4),
position in range (4), true relative volume (4), **RSI in the context of the setup** (3).

RSI is scored against the setup, never in the abstract. RSI 40 on a *Pullback in Uptrend* is the
mean-reversion entry and scores full marks; RSI 40 on a *Momentum* name is a trend failing while
its moving averages still look intact, and scores near zero. Context-free RSI would reward
exactly the wrong half of the board.

**ATR is not scored.** It is reported as a percentage of price and it sets the stop. Volatility
is a sizing input, not a virtue.

**Stops are ATR-based**: 1.5× ATR below entry, clamped to a 3-12% band, with a 50/200-day MA
inside 2% of that level taking precedence as the better place to rest a stop. Structural stops
remain the labelled fallback when no ATR is available.

**Earnings carry timing and an implied move.** `earnings_timing` (am/pm) comes from the calendar;
`implied_move_pct` from the option chain at power hour. A name reporting within seven days with
an implied move of 8%+ is called out in NOTABLE as *the position-size question, not the score
question* — a multi-day swing screen cannot hold a binary event on full size. As of 2026-09-01
`implied_move_pct` remains null (§9.1); `expected_move_pct` from scan IV is a DIFFERENT and
weaker claim and must never be written into it — see §12.

## 7. Earlier fixes (2026-08-31 audit)

`render.py` crashed on a null IPO price, killing every publish for four days.
`render_portfolio.py` used `%+,.2f`, not a legal printf conversion, and would have made the
portfolio panel vanish the moment a holding existed. Order tickets formatted fractional shares
with `%d`, rendering every real ticket as 0 shares. Sizing capped affordability against equity
rather than cash. A pillar with no data scored zero instead of leaving the denominator, so a
dead source silently rescaled the board and made Strong Buy unreachable. All fixed; the full
account is in `claude/cowork-market-scanner.md`.

## 8. Verification

Regression-tested against a 10-name fixture plus twenty degraded variants covering missing
technicals, wrong types, null collections, absent regime blocks, zero prices, stale history and
a 95-minute-late scan. Scanner, portfolio and render complete on every one. `history.py` passes
six merge/refuse/replace/late cases. `technicals.py` matches the connector's own SMA and ATR to
eight decimal places. ATR stop derivation is tested across volatile, calm, no-ATR, absurdly-wide
and near-zero-ATR inputs. The 2026-08-31 evening patches (section 10) were each tested on a
fixture before being written back: `technicals.py` on legacy / intraday / pre-market / degraded
inputs, `pm.py` on pending-order sizing, power-hour entries, minute-level staleness, the
concurrent-write guard and the broker-divergence warning, `scanner.py` on dict/string
`price_sources`. `sentiment.py` (2026-09-01) passes 105 assertions: every ApeWisdom trap, the
StockTwits stream refusal, the stop-list, Arctic Shift staleness, the watchlist filter, the
options liquidity gate, the merge contract against `scanner.py`'s exact retail keys, and
fourteen degraded-input variants.

---

## 8b. Board defects found on the 2026-09-03 pre-market run — FIXED

Both were found by a live run, after `scanner.py` had already succeeded, and both are the
same class of failure: the board making a claim the run had not earned.

- **RENDER-01 — `render.py` crashed instead of degrading when no board URL was staged.**
  `archive.LIVE_BOARD_URL` was left as a bare `None` by the repo migration (the identifier
  moved to the private `engine-config.json`) and nothing ever resolved it, so the snapshot
  ribbon died in `html.escape(None)`. The scan and the hand-off survived; the board did not.
  The migration's equivalence proof compared `pm_state.json` and could not have caught it.
  **Fixed:** `archive.LIVE_BOARD_URL` / `PM_BOARD_URL` now resolve through
  `config.board_url()` at import, and the ribbon omits the link when there is no URL —
  which is what `config.board_url()` documented `None` to mean all along.
- **RENDER-02 — an uncollected panel rendered as an empty one.** With no `insider_panel` in
  `scan_results.json`, the board still emitted both tables with headers and an empty body,
  which reads as *"we looked and found no insider activity"* when it means *"this slot does
  no insider work"*. Pre-market, opening-range and midday never collect insiders by design,
  so three boards out of four made the false claim every day. Same for IPOs. **Fixed:** an
  empty panel now renders "Not collected this slot" and says which slot owns the work.

`render.py` had no tests, which is why both shipped — it has no `__main__` guard, so
importing it runs it, and that is now exactly how it is tested (`tests/test_render_panels.py`).

---

## 9. LIVE DEFECTS — found at the 15:00 slot, 2026-08-31.

### 9.1 `get_option_quotes` returns HTTP 403 — the implied move cannot be computed. ROUTED AROUND 2026-09-01, still open at source.

The power-hour slot's distinctive job is pricing the overnight straddle. It cannot currently run.
`get_option_chains` and `get_option_instruments` both resolve normally (DELL's 2026-09-04 expiry
and the $465 call and put were located without trouble), but `get_option_quotes` returns a bare
`API error 403:` on every attempt — the account does not carry options-quote permission on the
connector. Retried once on a single instrument id; same result.

**What to do:** set `implied_move_pct` to null and say so. Do NOT substitute an estimate from
ATR, from historical post-earnings moves, or from anything else — the field is rendered as
"options price a ±X% move", which is a claim about what the option market thinks, and no other
input supports that sentence. The engine handles the null correctly: `earn_card()` still renders
the date and the am/pm timing, it just omits the price band.

**Partial route found 2026-09-01.** The scanner's OPTION filter group DOES return implied
volatility, call/put day volume and open interest for the whole market. That gives put/call
positioning and an IV-derived `expected_move_pct` — a weaker, differently-named claim. It does
NOT give a straddle price, so the paragraph above still stands unchanged. See §12.

**To fix for real (Vishal):** enable options data on the Robinhood account, then re-verify with a
single `get_option_quotes` call before promising an implied move in a report.

### 9.2 `gap_pct` and the 52-week range described the LAST COMPLETED SESSION. FIXED in `technicals.py` 2026-08-31 evening.

`get_equity_historicals` at `interval=day` returns bars only through the last *closed* session.
Mid-session on 2026-08-31 the final bar was **Friday 2026-08-28**, so everything derived from
`real[-1]` described Friday: the board would have published **ESTC gapping +24.1% "at the open"**
(Friday's earnings gap; the real Monday gap was **-2.51%**), MRVL -6.7% and IREN -7.1%, both
Friday's; `week52_high`/`week52_low` excluded today's range; `week52_change_pct` was measured to
Friday's close.

MA50, MA200, RSI and ATR *should* be computed on completed bars and were always correct. The
defect was in what the other three fields claimed to describe — and the fix used to be a manual
override every intraday session had to remember, which one task prompt actively contradicted.

**The fix is now inside `technicals.py`.** Pass the RAW `get_equity_quotes` response as
`--quotes` (and `--premarket` at the 08:00 slot):

- `gap_pct` = today's open (from `--fundamentals`) against the official SIP prior close (from
  `--quotes`); at pre-market, the extended-hours print against the prior close.
- `week52_high` / `week52_low` = the exchange's own 52-week range from `--fundamentals`, which
  includes today's session.
- `week52_change_pct` = the bar-derived change rebased onto the live trade price.
- Every symbol now carries `gap_basis`: `session_open`, `extended_hours`, or `prior_bar` when no
  live inputs were supplied — so a board can always say which claim it is making.

Without `--quotes` the script behaves exactly as before, so an old invocation still runs; it
just publishes yesterday's gap. **Every scan prompt now passes `--quotes`.**

### 9.3 openinsider.com is unreachable — the insider panel is UNKNOWN, not clean. STILL OPEN, replacement found.

Two independent failures, neither recoverable by retrying:
1. `WebFetch` force-upgrades http to https; openinsider serves a 302 back to http on both `/` and
   `/latest-insider-trading`, which is an unresolvable redirect loop.
2. The curl fallback returns HTTP 403 from the egress proxy: `Host not in allowlist: openinsider.com`.

The 12:30 run reached it (via the cluster-buy and weekly-purchase sibling pages, after
`/latest-insider-purchases` returned ROBOTS_DISALLOWED); the 15:00 run could not reach it at all.

**What to do:** use `marketbeat.com/insider-trades/purchases/` (verified 2026-09-01), which
returns a market-wide Form 4 purchase list with insider, role, shares, price and value, running
about a day behind filing. Failing that, carry the most recent slot's insider panel forward and
label every row `CARRIED FROM <slot>`. Form 4 filings are dated events — a filing from Aug 25 is
equally true at 12:30 and at 15:00 — so carrying forward is honest and keeps the Intelligence
pillar comparable between slots. What you must not do is publish an empty panel, which reads as
"no insider activity" when it means "we could not look".

**To fix (Vishal / org admin):** add `openinsider.com` to the session network egress allowlist.
That enables the curl path and sidesteps the https-upgrade loop entirely.

### 9.4 Two standing source traps, re-confirmed 2026-08-31

- **The 11-ETF compare page served untimestamped prior-session rows again** (its XLK +1.42%
  against a live +0.27% read off the individual page). Third consecutive session. Read sector
  day-changes from the individual `/etf/<T>/` pages; the compare page is the only YTD source that
  exists, so YTD is null whenever you reject it.
- **`markets/gainers/` and `markets/active/` served Friday's rows**, self-labelled "Updated Aug 28,
  2026", while the scan ran Monday. The universe screen contributed nothing usable. **As of
  2026-09-01 these are retired** — the universe comes from the connector's own watchlists and the
  saved scan (§12). Do not go back to them.

---

## 10. The 2026-08-31 evening audit — what was fixed, what is still open

An end-to-end audit ("Agentic Trading Audit", artifact `93568bde…`) ran the real engine against
the real book and found that, as wired, **no scheduled run could complete and no trade could
ever be opened.** Everything below was fixed the same evening, before the 2026-09-01 open.

| Finding | Was | Now |
|---|---|---|
| **ENG-01** blocker | `archive.py` imported by three modules, copied by none of the eight tasks → `ModuleNotFoundError` on the first Python command of every run | In the section 3 table and in all eight task prompts (scans say EIGHT files; PM prompts list it explicitly) |
| **HANDOFF-01** blocker | `claude/latest-scan.json` — the scan→manager hand-off — documented in three places, written by nothing | Section 3 hand-off step + a HANDOFF block in all four scan prompts; a one-off task at 11:00 ET on 2026-09-01 verifies the file exists |
| **TIMING-01** | PM chained 15 min behind scans that took 1-4 h; `scan_stale_minutes` declared and never read | PM crons moved to +45 min (08:45 / 10:45 / 13:15 / 15:45 ET); `pm.py` now enforces the 240-minute limit against `meta.scan_date + meta.time` |
| **FILL-01** | Power-hour entries could never fill (day orders expire at the roll before the next fill pass) | `pm.py` places no entries at the power-hour slot; the slot runs exits, trims, rebalancing |
| **SIZE-01** | Names with a resting buy were sized anyway, spending their cash from the budget twice | Excluded from `build_proposals` input before sizing |
| **STATE-02** | Divergence between the paper book and the live account "reported" by prose only | `pm.py --broker` takes the raw `get_equity_positions` payload and warns per symbol the live account holds |
| **DATA-01** | Section 9.2 gap defect fixed by a manual override the prompts contradicted | Fixed inside `technicals.py --quotes / --premarket` |
| **ARCH-01** | Section 8b of PM.md (frozen boards, `book-history/`, journal fields) executed by nothing | PM prompts now carry the full 8b write/publish order |
| **LIVE-01** | PM.md §1 described going live as a checklist; live mode still self-fills, never emits exit tickets, has no broker reconciliation | PM.md §1 now says NOT YET IMPLEMENTED, in those words |
| **M1** | `data_confidence()` crashed if `price_sources` arrived as a dict/list/string | Guarded — a collection counts as its size, anything else as one source |
| Market holidays | Every cron fired on NYSE holidays (Labor Day 2026-09-07 was six days away) | A holiday guard heads every scan and PM prompt |
| Alerting | Warnings lived in session transcripts | PM prompts push via `PushNotification` on any decision, warning, divergence or run failure |
| Evidence for going live | Nothing produced the track record PM.md §1 requires | Friday 16:30 ET weekly review task writes `claude/reviews/<ISO-week>.md` |

### Still open — do not assume these are done

- **9.1 options quotes 403** — partially routed around (§12), still needs the account change for
  a true straddle price. **9.3 openinsider egress** — replacement found (MarketBeat), the
  allowlist entry still needs an admin.
- ~~**STATE-01** — the Scan Desk portfolio panel reads the live-account mirror, not the paper
  books.~~ **Fixed 2026-09-02.** `engine/paper_mirror.py` projects all three paper books into
  `portfolio.json` before `portfolio.py` runs, labelled PAPER throughout. The panel, the sector
  cap, the max-position count and the deployed-room budget now describe the book the system
  really holds. It **refuses** (exit 2, writes nothing) when no book is staged or when any book
  is not in paper mode — an invented panel is worse than the old one. `mark_portfolio` also
  falls back to the holding's own GICS label, so a name that drops out of the scan universe no
  longer vanishes from the sector count.
- ~~**M2**–**M5**~~ **All four closed 2026-09-02.** The 45/55 exit and trim thresholds are now
  `portfolio.RULES["exit_score_below"]` / `["trim_score_below"]`, read by both modules, with a
  static test forbidding the literals returning. `pm.py`'s docstring no longer claims targets
  rest as sell limits — they fire as marketable sales, as PM.md §3 has always said. The reported
  return is measured against `portfolio.capital_basis()` — the seed plus every recorded deposit.
  `_load()` resolves inside `$SCAN_DIR` or reports the file absent.
- The two **sentiment refresh** tasks were created from the trading-system repo's API and cannot
  be edited by an agent — the holiday guard is not on them. Their `_meta.generated_at` staleness
  is caught by the scan prompts instead. **The probe did not investigate why that bundle goes
  stale; it remains open.**

### Second pass, later the same evening — what was added on top

| Addition | Where | What it does |
|---|---|---|
| Paper capital → **$5,000 per desk** | `claude/paper-book*.json` | decisions a real account would make; live account unchanged at $50 |
| **Risk sentinel** | `pm.py --slot sentinel`, task `trig_01CrQvGcZrn469zjsPCX5uXS` | hourly 09:35–15:35 ET exit pass on every desk; quiet runs write nothing (PM.md §12) |
| **Strategy desks** | `pm.py --desk`, `claude/engine/desks.json` | swing / pullback / momentum on separate books; PM.md §13 |
| **Spread gate** | `pm.py` | no entry when bid/ask > 1% of price |
| **Macro gate** | scan prompts → `meta.macro_events`; `scanner.py`; `pm.py` | no entries ahead of a same-day FOMC/CPI/payrolls release |
| **Relative strength vs SPY** | `technicals.py --benchmark` | reported and archived, not scored — section 11 decides |
| **Archive step wired** | scan prompts, section 3b | `claude/scans/<run_id>.json` + `claude/scan-index.json` finally written |
| **Score validation** | `validate.py`, task `trig_012PZZ5YMDo7Hmuza1Vbv4JF` | Saturday 09:00 ET — section 11 |
| **Daily health check** | task `trig_01Bem6ST315PcrAfTR27i1YC` | 16:15 ET, `claude/health/<date>.md`, pushes on FAIL only |

---

## 11. Validation — does the score actually rank forward returns?

`claude/engine/validate.py` is the cheapest possible test of the model and the gate every
proposed addition has to pass. It reads the archived scan records (section 3b) and a bars
file from `get_equity_historicals`, takes one observation per (day, ticker) — the last slot
of the day — and asks, per horizon of 5 / 10 / 20 sessions:

- **Spearman rank correlation** between score and forward return, with a p-value. Rank
  correlation, because the claim is ordinal — higher scores should do better — not that score
  is linear in return.
- The same correlation for **every pillar** and for **every reported-but-unscored field**
  (relative strength, RSI, gap, relative volume, 52-week change, upside to target, ATR%,
  coverage). A field that ranks returns more strongly than the score itself is a candidate to
  be scored; a field that does not, however plausible, is not.
- Mean return, median return and hit rate **by verdict** and **by setup**, and the
  **top-quintile minus bottom-quintile spread** by score — the number a strategy monetises.
- `n` under 30 at a horizon is reported as NOT ENOUGH DATA. For the first weeks that is the
  honest answer, not a defect.

```bash
python3 validate.py --records records/ --bars bars.json --horizons 5,10,20 \
                    --out validation.json --md validation.md
```

The Saturday task (`trig_012PZZ5YMDo7Hmuza1Vbv4JF`) pulls every record in `scan-index.json`
into `records/`, fetches bars for every ticker plus SPY (ten per call), runs this, and writes
`claude/reviews/validation-<ISO week>.md` and `.json` plus one line in
`claude/reviews/validation-index.md`. The weekly review quotes it.

**The rule this creates:** nothing is added to the scoring model — no new pillar, no new
weight, no data point promoted from reported to scored — without first showing up here with a
correlation stronger than what is already scored. Relative strength (section 3) is the first
candidate in that queue. As of 2026-09-01 it is joined by `corroborated` / `sources_count`,
`reddit_tone`, `put_call_ratio`, `iv_hv_ratio` and `expected_move_pct` — all REPORTED, none
scored. Everything here describes the paper record; it forecasts nothing.

---

## 12. Sentiment, universe and options collection (added 2026-09-01)

**`claude/engine/COLLECTION.md` is the authoritative procedure for all three and overrides
any older instruction in a task prompt.** Read it with this file. The short version:

- **Universe** comes from `get_popular_watchlists` + `get_watchlist_items` (Trending stocks,
  100 most popular, Daily movers — the last one filtered) and the saved scan
  `55138bd9-616c-4226-8996-50bf7aabdf22`. The stockanalysis gainers/active pages are retired.
- **Options positioning** comes from the saved scan `cc3b6743-6019-4dc4-bf86-88cfbbd80042`,
  which returns live IV, call/put volume, open interest and a put/call ratio market-wide. A
  ratio off a thin chain is a block trade, not sentiment — the liquidity gate is in code.
- **Retail** is ApeWisdom **page 1 only**, StockTwits `trending/symbols.json` and the symbol
  HTML gauge, and Arctic Shift for raw Reddit with `sort=desc`. **Never** the StockTwits
  per-symbol JSON stream — measured 50 hours stale.
- **`sentiment.py` handles every known trap in code** rather than relying on a prompt to
  remember it, and `--merge-into scan_data.json` writes the retail blocks in the exact shape
  `scanner.py` already reads.
- **Corroboration is the headline finding.** ApeWisdom and StockTwits agreed on 4 of 15 top
  names on 2026-09-01, and StockTwits' own two trending endpoints shared one ticker with each
  other. Single-source attention is now labelled as such on the board.

---

## 13. The scan snapshot — every input, as scored (S-01, added 2026-09-10)

`scanner.py` writes, right after `scan_results.json`, one gzip'd JSON Lines file per slot:

    $SCAN_DIR/archive/scan_snapshot/<date>-<slot>.jsonl.gz

The runner copies `archive/` back into the state repo unchanged, so the same path exists
there. Line 1 is `{"_meta": {run_id, slot, date, as_of, engine_sha, kind, n_rows, n_quoted,
source_files, schema}}`; every following line is one candidate — **every** candidate the
scanner was given, including the ones it dropped for having no usable price (`dropped: true`,
`score: null`). Each row is the whole candidate dict from `scan_data.json` (fundamentals, the
technicals.py fields, the sentiment blocks) with the scored row laid over it — `score`,
`raw_score`, `normalized_score`, `coverage_pct`, `missing_pillars`, the five `pillars`,
`verdict`, `setup`, `confidence`, `score_trail`, `rank` — plus `regime`,
`regime_multiplier`, and `bid`, `ask`, `last`, `quote_ts` read from the staged
`quotes.json` / `pm_quotes.json` (null, never 0, when no quote covers the name). The prose
(`reasons`, `*_note`) is left to the board; it is an output, not an input.

Why: the compact record keeps what the model *said*, this keeps what it *saw*. The slot-event
simulator replays a slot from it; E10 and E17 need the bid/ask and the unscored fields at the
moment of scoring, not a re-fetch; E27 needs the dropped names. It is written by rule
non-fatally — a failure lands as `scan snapshot NOT written: …` in `meta.data_warnings` and
the board still publishes — and the results meta carries `scan_snapshot` (the relative path)
on success. `python3 snapshots.py --run-dir . --out archive --scan` re-creates it from a
staged directory; `snapshots.read_snapshot(path)` returns `(meta, rows)`.

Every symbol in a snapshot is also folded into `archive/followed.json` (BACKTEST.md §2a).
