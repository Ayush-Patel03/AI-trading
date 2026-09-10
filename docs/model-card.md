---
card: Scan Desk score, trend+momentum core
engine_version: production @ 423ed33 (main @ 89bf3f2 at the time of writing; the card is re-issued at every promotion)
engine_sha: 423ed33
purpose: Rank a hand-picked US equity universe four times a market day for three paper desks (swing, pullback, momentum); the score is the entry gate and the size input, never an order
desks: swing, pullback, momentum — paper mode, absolute; live mode is not implemented
training_window: 2024-08-21 → 2026-08-28 (backtest replay, --every 5 sessions; no parameter was fitted to it — the score is the hand-written incumbent)
universe: hand-picked 69 names incl. SPY/QQQ/SMH/IWM (bars_all.json); survivorship: the list was chosen in 2026 from names that still exist and still trade, so it is survivor-biased by construction and over-weights the winners of the window
n_observations: 6,834 (date × ticker), 100 dates
ic_10d: ρ −0.040, p < 0.002 (pooled Spearman, 10 sessions)
ic_20d: ρ −0.045, p < 0.002 (pooled Spearman, 20 sessions)
ic_5d: ρ −0.013, p 0.29 (5 sessions — indistinguishable from zero)
quintile_spread: bottom quintile beat the top by 0.7–1.1% at 10–20 sessions (top minus bottom −0.7% / −1.1%)
slices: ex-ETF (ρ −0.038 / −0.042, spread −0.62% / −1.04%); ex-ETF ex-semis (ρ −0.035 / −0.043, spread −0.75% / −1.37%) — the sign holds in every slice
trials_on_ledger: 4 (BT-2026-09-10 and its three slices; the next trial is number 5)
dsr: not yet computed — needs a per-trial series of risk-adjusted returns; by the seventh trial an in-sample pass is what noise looks like (Bailey & López de Prado 2014)
live_validation: n = 0 at every horizon as of 2026-09-10 — no scored name has yet reached a 5-session forward window under the paper record
verdict: the current core does not rank forward returns; where it ranks at all it ranks them backwards at 10–20 sessions. Not promoted.
known_failure_regimes: attention-driven universe (the names were picked because they were being talked about, so the score inherits crowding); one-sector concentration (semiconductors dominate both the universe and the top decile, and a sector move reads as a signal); event days (an earnings print swamps every pillar)
monitoring_rolling_ic: per-date IC on the week's archive via ic.py at 5/10/20 sessions, Newey–West t; a t past ±2 on the wrong sign for four consecutive weeks re-opens this card
monitoring_n_eff: house N_eff under 3.0 (sector proxy) blocks new entries house-wide (K-03); reported on every journal entry
monitoring_shadow_gap: booked-versus-shadow day gap warn at $25, fail at $100 (health.py); cumulative gap as a share of realised P&L on the review page
rollback_rule: promote `production` only after a close, never intraday; revert = fast-forward `production` to the prior tag and `git reset --hard origin/production` on the box (docs/runbooks/engine-pull-failed.md). The box never patches the engine in place.
owner: Vishal (vpatel) — authorised paper-only on 2026-08-31
date: 2026-09-10
---

# Model card — Scan Desk score (trend + momentum core)

This card describes the scoring model the `production` branch runs for the three paper
desks. It is a template with every field filled from what is known on 2026-09-10, and it is
re-issued whenever `production` moves. The front-matter block above is what
`engine/render_review.py` renders on the weekly review page; it is parsed as plain
`key: value` lines, so keep it one line per field.

## What the model is

The Scan Desk score is a hand-written 100-point rule set — five pillars (trend and
structure 25, momentum and position 15, fundamentals 20, catalyst and analyst 20, market
intelligence 20) multiplied by a regime gate — that ranks a universe four times a market
day. The portfolio manager (`engine/pm.py`) uses the rank as the entry gate and the score
as a size input; every order it books is paper. Nothing in the score was fitted to data:
the backtest below is a *test* of the incumbent, not a training run.

## What the backtest found

The 2026-09-10 replay (`claude/reviews/backtest-2026-09-10.md`, ledger row
`BT-2026-09-10`) scored the trend + momentum core (40 of the 100 points — the only pillars
the bars can reconstruct) over 2024-08-21 → 2026-08-28 on 69 hand-picked names, every fifth
session, 6,834 observations. The pooled Spearman between score and forward return is
**negative** at 10 and 20 sessions (ρ −0.040 and −0.045, p < 0.002) and indistinguishable
from zero at 5. The bottom quintile beat the top by 0.7–1.1%. Removing the ETFs, and then
the semiconductors, changes the magnitude and not the sign. In plain words: the part of the
score the bars can see does not rank returns, and at two to four weeks it ranks them
backwards.

The per-date IC (`ic.py`, Newey–West t with lag = horizon) has not yet been run on the same
records; the numbers above are `validate.py`'s pooled figures, which treat 6,834 rows from
100 dates as independent and are therefore optimistic in the honest direction — the real
evidence against the score is if anything weaker in absolute terms, but the sign is the
sign.

## What is not known

- **Live validation: n = 0.** The paper record started on 2026-09-01; no scored name has
  yet completed a forward window under it. The Saturday validation task will fill this in
  one week at a time; until n reaches 30 at a horizon the live IC is *not a sample*.
- **DSR: not computed.** The deflated-ratio correction needs a per-trial series of
  risk-adjusted returns and the ledger's trial count; the count is 4, the series does not
  exist yet. The card carries the reminder rather than an invented number.
- **The other 60 points.** Fundamentals, catalyst and market intelligence cannot be
  replayed from bars, so the backtest says nothing about them either way.

## Known failure regimes

1. **Attention-driven universe.** The 69 names were picked in 2026 because they were being
   discussed; the score inherits the crowding and the survivorship of that choice. A
   point-in-time S&P 500 membership file exists (`experiments/universe_sp500.json`) and
   the next counted trial should use it.
2. **One-sector concentration.** Semiconductors dominate both the universe and the top
   decile, so a sector move reads as a ranking signal. The house exposure block (N_eff,
   largest sector) exists to catch this on the book side; it does not fix the score.
3. **Event days.** An earnings print swamps every pillar; the earnings gate refuses the
   entry but the score still ranks the name.

## Monitoring

| signal | source | threshold |
|---|---|---|
| rolling IC | `ic.py` on the week's archive, 5/10/20 sessions | Newey–West t past ±2 on the wrong sign for four consecutive weeks re-opens this card |
| N_eff | journal `house.exposure.n_eff` (sector proxy unless bars staged) | under 3.0 blocks new entries house-wide (K-03) |
| shadow gap | `health.py` `shadow_gap` row; review page shadow ledger | day gap warn $25, fail $100; cumulative gap as a share of realised P&L reported with its n |

## Rollback

`production` is promoted only after a close, never intraday. Reverting is a fast-forward
of `production` to the prior tag followed by `git reset --hard origin/production` on the
box (`docs/runbooks/engine-pull-failed.md`). Nobody patches the engine on the box.

## Honesty budget

This card, and the review page that renders it, carry no win rate and no risk-adjusted
ratio; every aggregate carries its n and anything under 30 says *not a sample*. The one
sentence that matters: **the current core does not rank returns, and the paper desks are
running on it so that the record accumulates evidence, not because the evidence is in.**
