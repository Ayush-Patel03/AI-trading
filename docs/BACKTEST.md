# Measuring the edge — the backtest harness

**Status: built, tested, wired into nothing.** No scheduled task runs any of this. It exists
so the question the whole system is built around can be answered in an afternoon instead of a
quarter, and so the three changes queued behind that answer can be judged rather than argued
about.

---

## 0. The question, stated precisely

Not "does the system work". That question has no answer yet and cannot be given one by
anything in this repo. The answerable question is:

> Given a score the model assigned on day D, did higher-scoring names go on to outperform
> lower-scoring ones over the following 5, 10 and 20 sessions — by enough to survive costs?

`validate.py` has always been able to answer it. It just had nothing to read. It takes
archived scan records, and the only records that exist are the live ones, four a day since
2026-09-01. The 20-session horizon does not reach `n=30` until roughly 2026-09-29, and
nominal `n` badly overstates what is there: nineteen semiconductor-adjacent names scored on
one morning in one regime are close to **one** observation, not nineteen.

`backtest.py` produces the same records from history. Same scanner, same archive format, same
validator — no new statistics, no second implementation of anything.

```
bars ──► backtest.py ──► records/*.json ──► validate.py ──► validation.md
          (replay)        (archive.record        (unchanged)
                           format, verbatim)
```

---

## 1. What it can test, and what it cannot

Only two of the five pillars can be reconstructed from bars:

| Pillar | Max | Historical? |
|---|---:|---|
| Trend & structure | 25 | **yes** — moving averages from closes |
| Momentum & position | 15 | **yes** — 52-week change, range position, relative volume, RSI |
| Fundamentals | 20 | only from a point-in-time table (§3) |
| Catalyst & analyst | 20 | no — consensus ratings and targets are not recoverable |
| Market intelligence | 20 | no — retail sentiment as it stood on a past Tuesday is gone |

So a backtest row scores on **40 of 100 points**, and the engine's own coverage
normalisation handles it — the same mechanism a live run uses when a source dies. That is not
a workaround; it is the behaviour `scanner.py` was already built to have.

Two consequences worth stating out loud:

- **Every backtest row is capped below Strong Buy** (40% coverage against the 70% floor), and
  under `min_coverage_to_propose` none of them would be *sized* on a live run either. The
  backtest measures **ranking**, not the proposal path. Do not read a backtest row as a trade.
- The claim it supports is narrow: *does the trend-and-momentum core rank forward returns.*
  That is still the first question worth asking, because those 40 points are the half most
  likely to be carrying the signal — and if they do not rank returns, nothing bolted on top
  will save it.

**Using today's analyst target to score a date in March is look-ahead bias**, and a backtest
with look-ahead in it is worse than no backtest: it produces a confident number, wrong in the
flattering direction, that nothing downstream can detect. The harness has no path that does
it.

---

## 2. The three biases you cannot remove, only state

1. **Survivorship.** The universe is the symbol list in the bars file, and a bars file lists
   companies that exist *today*. Names that delisted, went bankrupt or were acquired out of
   the index are absent, and they are disproportionately the losers. Every summary and every
   record carries the warning. Do not delete it because the report reads better without it.
   `universe_history.py` produces point-in-time index membership from Wikipedia's change
   tables and `backtest.py --universe-history <json>` intersects every replay date with the
   names that were members that day — removed names included, **if the bars file has their
   bars**. That reduces the bias and relabels the warning `REDUCED NOT REMOVED`; the file's
   own bias statement rides on the summary, every record and the ledger row.
   `docs/DATA.md` §3 has the procedure and what still has to be stated.
2. **The universe was chosen with hindsight**, for the same reason.
3. **Entry at the scored close.** A row is scored on D's close and the forward return runs
   from it. Real entry is the next open at best. The gap between those two is a real cost and
   `fills.py` (§5) is how it gets measured.

### 2a. The followed set — the live record is survivorship-free from its first slot (S-03)

The backtest cannot escape bias 1; the **live** record can, and now does. Every symbol that
appears in a scan snapshot (SCAN.md §13) is added to `archive/followed.json` in the state repo
— `{symbol, first_seen, first_slot, last_seen, horizon_end_date, status}` — the moment it is
first scanned, and it stays `open` for twenty business days after it was last seen whether or
not it is still on the screen. A name that stops scoring, falls off the popular watchlists or
delists is exactly the loser a survivorship-biased record forgets; here it is on the roster
until its forward horizon has run, and only then `closed` (seen again, it reopens with
`first_seen` intact). `history.py` maintains it on every scan; no separate sweep has to
remember to run.

`validate.py` already observes each (date, ticker) from the records themselves, so a name that
left the universe is still scored on every day it did scan — what it needs from outside is its
**bars**, and that is the seam. The bar-fetching prompt fetches for the open list:

```bash
python3 history.py --followed --archive <state>/archive [--as-of YYYY-MM-DD]   # one symbol a line
python3 validate.py ... --archive <state>/archive     # adds followed_open / followed_without_bars
```

`followed_without_bars` non-empty means the fetch missed a name that left the universe, and
the record is biased by exactly those names until it is filled.

---

## 3. Point-in-time fundamentals

`--financials` accepts an explicit table where **every row carries the date it became
public**:

```json
{"NVDA": [{"available_from": "2026-02-26", "revenue_growth_pct": 54.2,
           "profit_margin_pct": 52.4}]}
```

`available_from` is a filing date, not a fiscal period end. A row without one is **refused**,
not dated by guesswork — using a quarter-end as an availability date leaks roughly six weeks
of hindsight into every observation, which is exactly the kind of error that produces an
encouraging number and no way to notice.

Building that table from `get_financials` is a data task, not an engine task, and is not done.
The harness enforces the correctness property; something else supplies the rows.

---

## 4. `universe.py` — the screen this is really about

The live scan screens the connector's popular watchlists: a measure of retail **attention**,
not of anything the model scores. Measured on 2026-09-02, that is why all three desks held the
same seven names, why 34% of the combined book was Information Technology, and why on
2026-09-01 all ten of the top ten were.

Three desks is currently three views of one trade. No pillar weight fixes what the screen
never surfaced, which makes this the highest-leverage change available — and also the one most
likely to make the paper record look *worse* at first, because retail attention and
short-horizon momentum are correlated.

`universe.py` screens a stated index membership on price and median dollar volume, names every
exclusion, and takes point-in-time membership snapshots so a past date is screened with the
list that was current then. **Run the backtest both ways and compare** before changing what
the live scan screens.

---

## 5. `fills.py` — the cost model

PM.md section 11: *"Slippage is a flat 0.25% on exits. It is a placeholder, not a
measurement."* Three specific ways it flatters the book, all corrected here:

| | Old | Honest |
|---|---|---|
| A sale | last print − 0.25% | **at the bid**, plus the SEC/FINRA sale fee |
| A stop broken by a gap | stop − 0.25% | **at the open** — a gap opens *through* a stop |
| A buy limit met by a gap | at the limit | **at the open** — a real order fills better |

The stop case is the big one, and it is worst on exactly the days that matter. The limit case
runs the other way and is included deliberately: a cost model that only ever corrects against
the book is not a cost model, it is a haircut.

**`fills.py` is wired into nothing.** `pm.py` keeps its current model until someone decides to
switch, because changing the cost model on a live book makes yesterday's equity curve and
today's incomparable for reasons nobody can see afterwards. Switch on a session boundary, note
it in the journal, and say so in PM.md section 3.

---

## 6. Running it

```bash
# One get_equity_historicals call per ten symbols; include SPY, it sets the calendar.
python3 backtest.py --bars bars.json --start 2024-01-01 --end 2026-06-30 \
                    --every 5 --out-records records/ --summary backtest.json

# the same run on point-in-time index membership (docs/DATA.md §3): the bars file must
# then cover the names that were members on those dates, not today's list
python3 backtest.py --bars bars.json --start 2024-01-01 --end 2026-06-30 --every 5 \
                    --universe-history ../experiments/universe_sp500.json \
                    --out-records records/ --summary backtest.json

python3 validate.py --records records/ --bars bars.json \
                    --horizons 5,10,20 --out validation.json --md validation.md
```

`--every 5` is weekly. `--every 1` produces heavily overlapping observations: `n` goes up
fivefold and the *independent* information does not, so the p-values get better while the
evidence does not. Prefer weekly, and treat a daily run as a robustness check rather than a
bigger sample.

**What a pass looks like.** A positive Spearman with a small p-value at 10 and 20 sessions,
a top-quintile-minus-bottom-quintile spread comfortably wider than the round-trip cost
`fills.round_trip_cost_pct()` reports, and the same sign on the forward paper record. Any one
of those alone is not evidence.

**What a fail looks like, and it is a real possibility worth naming.** A Spearman near zero,
or a quintile spread inside the cost of trading. If that is the answer, the honest response is
to rebuild the model, not to re-run it with different parameters until it passes — every
re-run on the same data spends some of the evidence, and enough re-runs guarantee a pass that
means nothing.

---

## 6a. Trial ledger and IC

**Why the trial count matters.** Every configuration tried against the same two years of
bars spends some of the evidence, whether or not anyone wrote it down. Bailey & López de
Prado ("The deflated Sharpe ratio", 2014) put a number on it: with roughly two years of data,
about **seven** independently tried configurations are enough for the *best* of them to show
an in-sample Sharpe of 1 from pure noise. Harvey, Liu & Zhu ("…and the cross-section of
expected returns", 2016) set the bar for a new factor at **t ≥ 3, on a hold-out**, precisely
because so many have already been tried. Neither correction can be applied if nobody knows
how many trials there were. `ledger.py` exists to keep that one number honest, and it is
append-only for the same reason: a trial that was run and then deleted still spent the
evidence. The ledger already stands at **4** (§ backfill below), so the next trial is number
5, and by the seventh an in-sample pass is what noise looks like.

**The ledger.** `experiments/ledger.jsonl`, one JSON object per line, written by
`engine/ledger.py` and by `backtest.py --ledger`. Row schema:

| key | |
|---|---|
| `id` | `"E10"`, or auto `"X-<yyyymmdd>-<n>"` |
| `hypothesis` | one sentence: what this trial claims |
| `config_diff` | dict or string: what differs from the incumbent |
| `harness_cmd` | the argv that produced it |
| `window` | `{"start", "end"}` |
| `universe` | `{"name", "n_symbols"}` (`n_symbols` may be null); with `--universe-history` also `file`, `n_symbols_on_start`, `n_symbols_on_end`, `n_ever`, `n_members_with_bars`, `n_members_without_bars` |
| `universe_bias` | the membership file's bias statement, or null without `--universe-history` |
| `n` | observations |
| `horizons` | e.g. `[5, 10, 20]` |
| `in_sample` | metrics dict — from `backtest.py`, the `ic.py` score table per horizon |
| `out_of_sample` | dict, or **null until a hold-out has been measured** |
| `n_trials_to_date` | prior trial rows + 1 — the multiple-testing counter |
| `decision` | null until a human sets one |
| `date` | ISO date |
| `engine_sha` | `config.engine_sha()`, may be null |

A decision is a separate row — `{"kind": "decision", "ref": <id>, "decision": …}` —
appended with `--set-decision`; decision rows do not count as trials, and readers fold the
latest one onto its trial.

```bash
python3 engine/ledger.py --path experiments/ledger.jsonl --list
python3 engine/ledger.py --path experiments/ledger.jsonl --set-decision E10 "not promoted — …"

# a counted run: the row is appended after the records are written, and the run prints
# "trial N of the ledger". Without --ledger the run says it was NOT counted.
python3 engine/backtest.py --bars bars_all.json --start 2024-08-21 --end 2026-08-28 \
    --every 5 --out-records records/ --ledger experiments/ledger.jsonl \
    --experiment-id E10 --hypothesis "…" --config-diff '{"features": ["ret_12_7"]}'
```

**Backfill.** Rows 1–4 were backfilled by hand from `claude/reviews/backtest-2026-09-10.md`
(the run predates the ledger): the 2026-09-10 core backtest and its three slices (ex-ETFs,
ex-semis, per-horizon), each counted as a trial because each was a look at the same data.
`experiments/README.md` says so next to the file.

**`ic.py` — the per-date rank IC.** `validate.py` pools every (date, ticker) observation
into one Spearman; that stays, but a pooled correlation over 6,800 rows from 100 dates
treats each row as independent and they are not. `ic.py` computes, per horizon:

- the **IC**: Spearman(score, forward return) *within each date's cross-section*; its mean,
  its standard deviation, and its **t-statistic with a Newey–West (Bartlett) variance, lag =
  horizon**, because forward windows overlap and consecutive ICs are autocorrelated (the
  formula is in the module docstring);
- the **pooled Spearman**, so the number `validate.py` prints is alongside for comparison;
- the **quantile spread**: top minus bottom quintile (terciles when the median cross-section
  is under 10 names) per date, averaged, with a **block-bootstrap 90% interval** (block =
  horizon, fixed seed, so the same input gives the same interval);
- `--by-feature`: the same table for every numeric feature a row carries (a `features` dict
  if present, else the unscored fields `validate.FIELDS` tracks). This is where the
  window-split experiment lives: which input ranks returns in-sample, and does it still on
  the hold-out.

```bash
python3 engine/ic.py --records records/ --bars bars_all.json --horizons 5,10,20 \
    --by-feature --md ic.md --json ic.json
# ic.json carries the observations, so it is itself a valid --records input (no --bars needed)
```

It reads exactly the records `backtest.py` writes, through `validate.py`'s own loaders. Same
caveats as everything above: it describes a replay, on a survivor universe, and forecasts
nothing. A spread whose 90% interval straddles zero is not a spread.

---

## 6b. The research features and the E10 / E2 recipes (S-04, 2026-09-10)

Every backtest record — and, on a live run where `technicals.py` had the bars, every scan
row, archive record and snapshot row — now carries a `features` dict from
`technicals.features()`. **Scored by nothing.** `ic.py --by-feature` is the only reader.
The keys, all fractions (0.10 = +10%), windows in sessions (21 to a month, 252 to a year),
each `null` when the history does not cover it:

| key | formula | paper | expected sign |
|---|---|---|---|
| `ret_12_7` | close[t−147]/close[t−252] − 1 | Novy-Marx 2012 | **+** |
| `ret_6_2` | close[t−42]/close[t−126] − 1 | Novy-Marx 2012 | ≈ 0 |
| `ret_12_1` | close[t−21]/close[t−252] − 1 | Jegadeesh & Titman 1993 | + |
| `ret_1m` | close[t]/close[t−21] − 1 | Jegadeesh 1990 | **−** (reversal) |
| `ret_5d` | close[t]/close[t−5] − 1 | Lehmann 1990 | **−** |
| `close_to_52wk_high` | close[t] / max high over 252 | George & Hwang 2004 | + |
| `max_1m` | max daily return over 21 sessions | Bali, Cakici & Whitelaw 2011 | **−** |
| `rv_20d` | √252 × std of 20 daily log returns | (input to E13 sizing) | − / ? |
| `atr_pct` | ATR14 / close (fraction; the row-level field is in percent) | — | **? — E2** |
| `turnover_20d` | mean 20-day volume / shares outstanding | Lee & Swaminathan 2000 | interaction |
| `rs_20d_vs_spy` | stock 20d − SPY 20d | (existing definition, as a fraction) | + |
| `industry_rs_20d` | sector ETF 20d − SPY 20d | Moskowitz & Grinblatt 1999 | + |
| `stock_vs_industry_rs_20d` | stock 20d − sector ETF 20d | — | ? (E1 decomposition) |
| `resid_mom_12_1` | Σ residuals, all but the last 21, of the 252-day regression on SPY (+ sector) | Blitz, Huij & Martens 2011 | + |
| `beta_252` | the SPY slope of that regression | — | (control) |
| `ivol_20d` | √252 × std of the last 20 residuals | Ang, Hodrick, Xing & Zhang 2006 | − |
| `overnight_share_20d` | Σ(open/prev close − 1) / Σ(close/prev close − 1), 20 sessions | Lou, Polk & Skouras 2019 | + |

The regression behind `resid_mom_12_1` / `beta_252` / `ivol_20d` is **through the origin**:
the estimation window is the momentum window plus one month, and with an intercept OLS
residuals sum to zero over the sample, so "all but the last month" would collapse to minus
the last month — a reversal signal wearing a momentum label. The module docstring says the
same. Attention (`wsb_mentions`, watchlist counts) is not a bars feature; its sign test
(expected **−**) is S-09 and reads the sentiment fields off the live archive.

**Inputs the run needs.** Daily bars for the universe **plus SPY plus the eleven sector
ETFs** (XLK, XLF, XLV, XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC), from **252 sessions before the
first replay date** (2024-08-21 → bars from 2023-08-01 or earlier). A `sector_map.json` of
`{SYMBOL: ETF}` — GICS sector → SPDR ETF; the sector comes from the Wikipedia constituents
table's "GICS Sector" column (the same page `universe_history.py` reads, which does not yet
keep that column) or from the scan snapshot's `gics` field for names the scan has followed.
Optionally a
`shares_outstanding.json` of `{SYMBOL: shares}` from `get_equity_fundamentals` for
`turnover_20d` — a static snapshot, so the run's summary and every record carry the
`TURNOVER IS APPROXIMATE` warning; rank it, never quote its level.

**Fetching them.** `runner/fetch_bars.py` (Alpaca Basic, IEX feed, `adjustment=all`, stdlib
only; key file at `C:\ai-trading-runner\alpaca.env`, never in a repo) writes `bars_all.json`
in exactly the shape above, `bars_missing.json` for the names the source could not serve
(read it before `n_members_without_bars`), and `sector_map.json` from a Symbol / GICS Sector
CSV:

```bash
python3 runner/fetch_bars.py --universe experiments/universe_sp500.json --since 2024-08-21 \
    --symbols SPY,XLK,XLF,XLV,XLY,XLP,XLE,XLI,XLB,XLU,XLRE,XLC \
    --start 2023-08-01 --end today --keyfile /path/outside/the/repo/alpaca.env \
    --out bars_all.json --missing-out bars_missing.json \
    --sector-map-out sector_map.json --sectors-csv sp500_sectors.csv --resume
```

`--since` is the first replay date (every name that was a member from then on is fetched);
`--start` is 252 sessions earlier. `--resume` makes a re-run after a network failure pick up
where it stopped.

### The E10 recipe — window split, in-sample then hold-out

```bash
# in-sample: the first year
python3 engine/backtest.py --bars bars_all.json --start 2024-08-21 --end 2025-08-20 \
    --every 5 --sector-map sector_map.json --shares-outstanding shares_outstanding.json \
    --universe-history experiments/universe_sp500.json \
    --out-records records/e10_in/ --ledger experiments/ledger.jsonl \
    --experiment-id E10 --hypothesis "ret_12_7 ranks 20d forward returns; ret_1m and ret_5d rank them negatively; the 40-point core is loading on the wrong window" \
    --config-diff '{"features": "logged, unscored"}'
python3 engine/ic.py --records records/e10_in/ --bars bars_all.json --horizons 5,10,20 \
    --by-feature --md ic_e10_in.md --json ic_e10_in.json

# hold-out: the second year — run ONCE, after the in-sample table has been read and the
# expected signs written down
python3 engine/backtest.py --bars bars_all.json --start 2025-08-21 --end 2026-08-28 \
    --every 5 --sector-map sector_map.json --shares-outstanding shares_outstanding.json \
    --universe-history experiments/universe_sp500.json \
    --out-records records/e10_out/
python3 engine/ic.py --records records/e10_out/ --bars bars_all.json --horizons 5,10,20 \
    --by-feature --md ic_e10_out.md --json ic_e10_out.json

# record the hold-out on the ledger row
python3 engine/ledger.py --path experiments/ledger.jsonl --set-decision E10 "<what the hold-out said>"
```

**What to read off `ic_e10_*.md`.** At the 20-session horizon, per feature: the IC mean, its
Newey–West t, and the quintile spread with its interval. The *signs* are the hypothesis:
`ret_12_7` **+**, `ret_6_2` **≈ 0**, `ret_1m` **−**, `ret_5d` **−**, `max_1m` **−**,
`atr_pct` **?** (E2 decides), attention **−** (S-09). A feature whose in-sample sign is right
and whose hold-out t is under 3 is *not yet* a feature (Harvey, Liu & Zhu 2016). A feature whose
hold-out sign flips is noise, and the in-sample table that suggested it spent a trial.
Interaction with `turnover_20d`: split the observations at the median turnover (the `ic.json`
observations carry it) and re-run `ic.py` on each half — momentum should be stronger and
reversal faster in the high-turnover half (Lee & Swaminathan 2000).

### The E2 recipe — is `atr_pct` a signal or a beta?

`atr_pct` was the one bars field that ranked forward returns *negatively* on the 2026-09-10
run. Two explanations, opposite implications: it is a real low-vol/lottery effect (then it
belongs in the score with a negative sign), or it is a beta proxy and the sample was a
drawdown (then it belongs in sizing, which E13 already does). Separating them:

1. From `ic_e10_in.json`, take each observation's `features.beta_252`, `features.atr_pct`,
   `features.max_1m`, `features.ivol_20d` and its `fwd["20"]`; from the bars take SPY's
   forward 20-session return on the same dates.
2. Regress `fwd_20` on `beta_252 × SPY_fwd_20` (and a semis-regime dummy, per the synthesis)
   — `technicals.ols` will do it, stdlib only — and keep the **residual**.
3. Rank-IC `atr_pct` against the residual, per date, Newey–West as usual. Do the same for
   `max_1m` and `ivol_20d`, and for `atr_pct` *after* also partialling out `max_1m`.

If `atr_pct`'s IC against the residual is near zero, it was beta: leave it out of the score
and let vol targeting handle it. If it survives, and survives `max_1m`, it is a signal in
its own right and gets a ledger row of its own before anything is promoted. Either way the
answer is one row: `--experiment-id E2`.

Steps 1–3 are one flag (S-07). `ic.py --beta-residual` takes SPY's forward return on each
observation date from the same bars (`validate.forward_returns` on SPY's closes), regresses
`fwd_h` on `beta_252 × SPY_fwd_h` pooled through the origin (`technicals.ols`), and prints
the IC of `atr_pct`, `rv_20d`, `max_1m` — and `beta_252` itself as the control — against
the raw forward return and against the residual, side by side, per horizon:

```bash
python3 engine/ic.py --records records/e10_in/ --bars bars_all.json --horizons 5,10,20 \
    --beta-residual --md ic_e2.md --json ic_e2.json
# ic_e10_in.json (written with --bars) already carries spy_fwd per observation and works
# as --records here without --bars
```

Read the two IC columns row by row: raw real and residual near zero means beta; the slope
`k` printed per horizon should sit near 1 if `beta_252` is doing its job. The semis-regime
dummy and the `max_1m` partial are not in the flag — split the observations file by hand
and re-run, exactly as the turnover split above.

---

## 6c. E5 — gate counterfactuals (S-07, 2026-09-10)

**The question.** Every gate in `pm.py` refuses names — the sector cap, the house caps, the
spread and price-drift limits, the run cap, the broker policy — and each refusal is a claim
that the name was better left alone. Nothing had ever checked. `engine/report.py` reads the
PM journals (`claude/pm-journal*.json`) and asks, per gate: **did the names the gate refused
underperform the names it admitted on the same dates?** It is read-only over the journals,
the books and the bars, and it writes nothing but its own `--md` / `--json`.

**What it does.**

1. *Refusals taxonomy.* `report.classify(reason)` maps every `skipped` reason string the
   engine emits to one rule — `sector_cap`, `house_symbol_cap`, `house_sector_cap`, `spread`,
   `price_drift`, `scan_stale`, `macro_gate`, `broker_policy`, `ladder`, `house_exposure`,
   `earnings_gate`, `min_notional`, `max_entries`, `kill_switch`, `halt`, `coverage`, plus
   `stop_policy`, `slot`, `desk_mandate`, `working_order`, `once_per_session`, `deadband` for
   what the journal says that is not a gate, and `other` with the raw text kept. The mapping
   table is in the module docstring; `tests/test_report.py` harvests every reason literal
   from `pm.py`, `portfolio.py`, `broker_policy.py` and `ladder.py` by AST and fails if one
   lands in `other`, so a new refusal cannot be added without a rule. Counts come out by
   rule, by ISO week and by desk. Each refusal has a side: `book` (a `*` gate — the journal
   names no candidates, so it is counted but cannot be measured), `manage` (a holding the
   exit pass wanted to sell and could not), `entry` (a named candidate). Only `entry`
   refusals are measured.
2. *The counterfactual.* Refused set = one (date, symbol) per rule; admitted set = the
   `place-buy` decisions. Entry for both is the **next session's open** (its close when the
   bar has no open); the forward return over h sessions is `validate.forward_returns`'
   convention from that entry session. The difference is a per-date series (mean refused
   minus mean admitted on each date that has both) and its 90% interval is
   `ic.block_bootstrap_ci` with block = horizon — the quantile-spread machinery, because
   forward windows on nearby dates overlap.
3. *Attribution.* Closed-trade P&L by exit reason, by desk, by entry-score decile, by verdict
   and by setup; score, setup and verdict are joined from the `place-buy` decision that
   opened the trade, because closed trades do not carry them.
4. *Benchmark.* Desk equity return over the window from the journal entries against SPY over
   the same dates from the bars, and exposure-adjusted: average invested fraction × SPY is
   what a passive position of the same average size would have earned.

```bash
# stage claude/pm-journal.json, claude/pm-journal-pullback.json, claude/pm-journal-momentum.json
# into journals/ and the three claude/paper-book*.json into books/; bars.json must include
# SPY and every refused and admitted symbol from the next session onward
python3 engine/report.py --journals journals/ --books books/ --bars bars.json \
    --counterfactual --horizons 5,10,20 --md review.md --json review.json
# refusals + attribution only (no bars needed), from a date
python3 engine/report.py --journals journals/ --books books/ --since 2026-09-08 --md review.md
```

**How to read the interval.** Per rule and horizon the table shows the refused mean and
median, the admitted mean and median, each with its n, and the per-date difference with its
90% interval and the number of dates behind it. Interval entirely **below zero**: the refused
names did worse — the gate is doing its job. Entirely **above zero**: the refused names did
better — the gate cost return and should be argued about, not loosened by reflex. Straddling
zero: no evidence either way. Under 30 observations on either side the row says **not a
sample** and the interval is decoration; the first few weeks will say that everywhere, and
that is the honest answer. Book-wide gates (macro, stale scan, halts, the ladder, the broker
policy's entry freeze) appear under *not measurable* with their counts — measuring them
needs the scan archive's candidate lists on those dates, which is a later step.

**The honesty budget.** Every number in `--md` carries its n beside it, anything under 30
says *not a sample*, and there is no win rate and no Sharpe anywhere in the output — at this
size neither is evidence and both flatter. It describes the paper record and forecasts
nothing.

## 6d. E26 — the sector-rotation desk's own replay (D-01, 2026-09-10)

**The question.** The rotation desk (docs/PM.md section 18) is a monthly rule, not a scored
scan: rank the eleven SPDR sector ETFs and VEU on 12-1 month return, hold the top three
equal-weight when SPY's 12-month return beats the 3-month bill, else TLT. The scoring replay
above cannot test it — there is no score, no quintile and no per-trade horizon — so
`engine/rotation.py` carries its own harness, `rotation.replay()`, and `backtest.py --desk
rotation` runs it in place of the scoring replay and writes the ledger row **E26**.

**What it does.** SPY's sessions in the bars file are the calendar. At the last session of
each calendar month (and, with the weekly check on, at the last session of each ISO week)
it ranks, reads the filter against the bill rate as of that date, and decides; the trade is
booked at the **next session's open** (its close when the bar has no open) with a stated
one-way cost on every buy and sell (`--cost-bps`, default 5 — a liquid ETF's spread plus
fees; the engine's 25 bp no-quote fallback would be an order of magnitude too pessimistic
here). A re-affirmed holding is not re-trimmed, which is what the paper desk does too. The
weekly check acts only on a rank-6 drop or a filter flip against the holdings, as live.

**What it reports — and only this.** `monthly_returns`, `equity_curve`, `max_dd`,
`n_rebalances`, `corr_with_spy`, `turnover` (mean and total one-way), plus the counts
(`n_month_ends`, `n_decisions`, `bond_months`) and the decision `log`. **No Sharpe, no win
rate** — the honesty budget of section 6c applies unchanged. `--corr-with <curve>` adds the
Pearson correlation of its daily returns with another equity curve on their common dates — a
paper book (`equity_curve`, the last slot of each date wins), a replay summary, or a bare
`[{date, equity}]` — with its n and a `not_a_sample` flag under 30 points. The plan's
acceptance for the desk is **rho < 0.6 against the swing desk**, and the summary says
`acceptance_rho_lt_0_6` true or false with the n beside it.

**The bill rate.** `--tbill tbill.json` is either `{date: pct}` (looked up as-of each
decision date) or a `macro.json` with `tbill_3m_pct`; `--tbill-pct 4.2` is a constant.
Neither given, the rate is **0** and both the summary's `_warnings` and the ledger row's
`tbill_default_used` say so. A filter measured against zero is a different experiment from
one measured against the bill — do not compare the two as if they were one trial.

```bash
# bars_etf.json: the 14 symbols (XLK XLF XLV XLY XLP XLE XLI XLB XLU XLRE XLC VEU SPY TLT),
# daily, from at least 253 sessions before --start
python3 runner/fetch_bars.py --symbols SPY,TLT,VEU,XLK,XLF,XLV,XLY,XLP,XLE,XLI,XLB,XLU,XLRE,XLC \
    --start 2015-01-01 --end today --keyfile alpaca.env --out bars_etf.json
python3 engine/backtest.py --desk rotation --bars bars_etf.json \
    --start 2016-01-01 --end 2026-08-31 --tbill tbill.json \
    --corr-with books/swing.json --summary rotation.json --ledger --experiment-id E26
# variants are their own trials: --no-weekly, --cost-bps 10, --tbill-pct 0
```

**How to read it.** `bond_months` over `n_month_ends` is how often the filter was off;
`n_rebalances` over `n_decisions` is how often a decision actually traded (a re-affirmed
top three trades nothing); `turnover.mean_one_way` × 2 × `cost_bps_one_way` is the cost
drag per rebalance. `corr_with_spy` near 1 in the bull months is expected — three sector
sleeves are a beta — and the number that matters is `corr_with_other` against the swing
book, which is the diversification the desk was proposed for. `max_dd` is the drawdown the
house would have had to carry; read it against the K-02 ladder before activating.

**What it does not do.** It fills at the open, not at the next slot's quote plus slippage —
the paper desk's number is different and the summary says so. XLRE and XLC start in 2015 and
2018 respectively; before that they are unranked and the top three come from fewer names, so
a window that starts before 2018 is a ten-or-eleven-sector rule, not a twelve-sector one.
The parameters (12-1, top 3, rank 6, TLT) are the literature's, not fitted here; every re-run
with other parameters is another ledger trial and spends evidence like any other.

---

## 7. Where this sits in the plan

Phase 2 of the roadmap (`claude/health/2026-09-02-system-review-and-roadmap.md`) is *prove the
edge*, and its gate is: a positive, stable top-quintile-minus-bottom-quintile spread over
several hundred trades, surviving a realistic fill model, reproduced forward on the paper
record. This is the instrument that produces the first three of those four. Nothing in phases
3–6 should be built before it has been read.
