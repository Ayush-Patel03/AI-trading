# experiments/ — the trial ledger

`ledger.jsonl` is the append-only record of every configuration tried against the backtest
harness. One JSON object per line; `engine/ledger.py` reads and appends it, `engine/backtest.py
--ledger` appends a row per run. **Never rewrite a line.** A decision is a separate row
(`{"kind": "decision", "ref": <id>, ...}`) appended with `--set-decision`; a trial that was run
and then deleted still spent the evidence, which is the whole reason the counter exists.
See docs/BACKTEST.md, "Trial ledger and IC".

## Backfilled rows

Rows 1–4 (`BT-2026-09-10`, `BT-2026-09-10-ex-etf`, `BT-2026-09-10-ex-semis`,
`BT-2026-09-10-per-horizon`) were **backfilled by hand from the write-up**
`claude/reviews/backtest-2026-09-10.md`, not produced by `backtest.py --ledger`, which did
not exist when that run was made. Their `in_sample` metrics are the pooled Spearman and
quintile spreads `validate.py` reported (not the per-date IC table `ic.py` now writes), their
`engine_sha` is null, and the slices carry `n: null` where the write-up gave only
approximate counts. The three slices are counted as separate trials on purpose: each is a
look at the same data, and each look spends evidence.

    python3 engine/ledger.py --path experiments/ledger.jsonl --list

## `universe_sp500.json` — point-in-time S&P 500 membership

Written by `engine/universe_history.py` for `backtest.py --universe-history`. Read its
`source.note` and `fetched_at` before quoting it: the 2026-09-10 file was built from a mirror
of the Wikipedia tables (Wikipedia was egress-denied from the build session) and covers
2019-01-18 onward. Refresh with

    python3 engine/universe_history.py --index sp500 --out experiments/universe_sp500.json --since 2019-01-18

from a host that can reach `en.wikipedia.org`. The procedure, the bars-fetch that has to go
with it, and the bias every write-up must state are in `docs/DATA.md` §3.
