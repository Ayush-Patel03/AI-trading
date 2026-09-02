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
   `universe.py` reduces this where point-in-time index membership is available (§4).
2. **The universe was chosen with hindsight**, for the same reason.
3. **Entry at the scored close.** A row is scored on D's close and the forward return runs
   from it. Real entry is the next open at best. The gap between those two is a real cost and
   `fills.py` (§5) is how it gets measured.

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

## 7. Where this sits in the plan

Phase 2 of the roadmap (`claude/health/2026-09-02-system-review-and-roadmap.md`) is *prove the
edge*, and its gate is: a positive, stable top-quintile-minus-bottom-quintile spread over
several hundred trades, surviving a realistic fill model, reproduced forward on the paper
record. This is the instrument that produces the first three of those four. Nothing in phases
3–6 should be built before it has been read.
