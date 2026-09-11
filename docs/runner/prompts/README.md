# Scheduled-task prompts for the box runner (R-05)

One file per scheduled task, each a **complete, standalone prompt** rewritten for the runner
on `ns-ayushoffice` (`runner/README.md`). A session no longer stages the engine, runs
`pm.py` or `scanner.py`, or `project_write`s a book. Its only jobs are: fetch the live inputs
the box cannot fetch itself (Robinhood connector payloads, the scan's collected data, the
free sentiment feeds in `docs/DATA.md`), write them with a `manifest.json` into
`C:\ai-trading-inputs\<run_id>\`, start `runner/run.py` through Desktop Commander, read
`outcome.json`, summarise, and push a notification only on an alert.

Every prompt carries, verbatim: the paper lock and the account lock; never write code to
the box; never edit the scrubber, `mirror.py`, the runner, the engine clone or the state
repo; never work around a refusal. Every prompt carries the same failure ladder: runner
unreachable → retry once → write the `aborted: true` coverage row by hand
(`docs/runbooks/runner-unreachable.md` step 2) → if the box itself is dark, write
`claude/health/<date>-<slug>-blocked.md` in the project and stop.

**Nothing here is saved to a trigger yet.** These are for Vishal to paste in, in the order
under "Switching over", after R-04 (install + `selftest.py PASS` on the box).

## The tasks

Schedules are the current triggers' expressions, `CRON_TZ=America/New_York`. Trigger ids are
the ones live on 2026-09-10. "Reads / writes" is what changes against today's prompt.

| Task | Schedule | Prompt file | Trigger today | `run.py` call | Inputs the session writes | Reads / writes vs today |
|---|---|---|---|---|---|---|
| Scan 1/4 pre-market | `0 8 * * 1-5` | `scan-pre-market.md` | `trig_01Cq2a73GoDGJCK4G6qcei7W` | `--slot pre-market --step scan` | `scan_data.json`, `bars.json`, `fundamentals.json`, `quotes.json`, the eight sentiment payloads, optional `option_chains.json` | no engine in the sandbox; no `claude/latest-scan.json`, `scans/`, `scan-index`, `scan-history` writes; no board artifact |
| Scan 2/4 opening range | `0 10 * * 1-5` | `scan-opening-range.md` | `trig_01FAqdxByahDeJ6fxx75gtvD` | `--slot opening-range --step scan` | same | same; carry-forward reads `C:\ai-trading-state\scans\latest.json` |
| Scan 3/4 midday | `30 12 * * 1-5` | `scan-midday.md` | `trig_01CRJXL79m7A94msukZQCBSQ` | `--slot midday --step scan` | same | same |
| Scan 4/4 power hour | `0 15 * * 1-5` | `scan-power-hour.md` | `trig_01BM5jJ173cuK7ypqXoXLC4s` | `--slot power-hour --step scan` | same | same |
| PM 1/4 pre-market | `45 8 * * 1-5` | `pm-pre-market.md` | `trig_01XG76TChZ7WVtyn2JopTkJx` | `--slot pre-market --step pm` | `pm_quotes.json`, `pm_broker.json`, optional `option_chains.json` | books/journals/desks staged by the runner; watch journal read from the state repo; no book, journal, book-history or board writes |
| PM 2/4 opening range | `45 10 * * 1-5` | `pm-opening-range.md` | `trig_01Q7cuMtxqeAkLHds1mLSFsR` | `--slot opening-range --step pm` | same | same |
| PM 3/4 midday | `15 13 * * 1-5` | `pm-midday.md` | `trig_01YKgkDB7mRXJTbMdPRaMbmo` | `--slot midday --step pm` | same | same |
| PM 4/4 power hour | `45 15 * * 1-5` | `pm-power-hour.md` | `trig_017Dpdi7EdX5ADSP3Gy1cVnS` | `--slot power-hour --step pm` | same; quotes ferried **before** the bell | same |
| Sentinel | `35 9-15 * * 1-5` | `sentinel.md` | `trig_01CrQvGcZrn469zjsPCX5uXS` | `--slot sentinel` | `pm_quotes.json` (SPY alone when every book is flat), optional `pm_broker.json` | quiet writes a heartbeat + coverage row on the box; nothing in the project |
| Watch pre-open | `0 7 * * 1-5` | `watch-pre-open.md` | `trig_01N1MLaAME5mE8PXPnu2yzDk` | `--slot watch --session pre-open` | `pm_quotes.json`, `pm_tradability.json`, `pm_broker.json` | `journals/watch.json` written by the runner; no `claude/pm-watch-journal.json` write |
| Watch after-hours | `20 16 * * 1-5` | `watch-after-hours.md` | `trig_01MpAZKmqRBfyjR2uyVuEfqu` | `--slot watch --session after-hours` | same | same |
| Health check | `15 16 * * 1-5` | `health-check.md` | `trig_01Bem6ST315PcrAfTR27i1YC` | `--slot health --mirror C:\ai-trading-mirror` | `manifest.json` with `"files": {}` | **the one task that still writes project docs**: mirrors the state files back (books, journals, watch journal, coverage, latest scan, index, history, today's scan records) and writes `claude/health/<date>.md` + index |
| Weekly review | `30 16 * * 5` | `weekly-review.md` | `trig_01Qwa5QR7CkhcZVygPgogMUQ` | none (analysis in the sandbox) | — | reads journals/books/coverage/health/git log from the state repo; writes `claude/reviews/` only |
| Score validation | `0 9 * * 6` | `score-validation.md` | `trig_012PZZ5YMDo7Hmuza1Vbv4JF` | none (analysis in the sandbox) | — | reads `scan-index.json`, `scans/<run_id>.json`, `archive/followed.json` from the state repo; adds `ic.py`; writes `claude/reviews/` only |
| Mirror sync | `20 9,11,14,16 * * 1-5` | `mirror-sync.md` | `trig_01DBZpPG7QmYV6Z79rc1xRn6` | none | — | stages from `C:\ai-trading-state` with `Copy-Item` (no project reads, no 60 KB chunked transport); `mirror.py` from the runner's engine clone; steps 3–7 unchanged |
| **Dead-man's switch (new)** | `5,35 9-16 * * 1-5` | `deadman.md` → `docs/runner/deadman-task.md` | — | `runner/deadman.py --state C:\ai-trading-state` | — | K-05; pushes on a fresh trip, a clear, or a checker that cannot run |
| **Morning brief (new)** | `45 8 * * 1-5` | `morning-brief.md` | — | none (`engine/brief.py --state`, like `deadman.py`) | — | U-04; the program reads the three books, the three journals, `coverage/`, `scans/latest.json` and `journals/watch.json` and writes `brief.txt` + `morning-brief.html` into the inputs dir; the session delivers them. No lock, no state write |

Not trading, not covered here: `refresh-sentiment-premarket`, `refresh-sentiment-midday`,
`Peca email ticket gap scan`.

Character counts (2026-09-10): scans 4.6 k each, PM slots 4.3–4.6 k, sentinel 3.9 k, watches
4.5–4.6 k, health 4.2 k, weekly review 4.4 k, validation 3.5 k, mirror sync 3.9 k, morning
brief 4.9 k, deadman pointer 1.5 k. The ~2.5 k target was not reachable for a trading slot:
the verbatim standing rules (~620), the box procedure with the exact `start_process` line
(~900) and the failure ladder (~450) are ~2 k before a single slot-specific word. Detail that
used to be repeated (manifest shape, chunked writes, exit codes) now points at
`runner/README.md` instead.

## What the session still fetches, per slot

From `docs/PM.md` §9, `docs/SCAN.md` §1/§12 and `runner/slots.json`:

- **Scan**: the collection half of the `market-scan` skill / `docs/COLLECTION.md` — universe
  (RH watchlists + saved scan `55138bd9-…`), the 8-name deep dives (WebSearch → WebFetch for
  GICS sector, analyst rating/target, short float, catalysts, `next_earnings`), regime
  (`get_index_quotes`), `meta.macro_events`; `get_equity_historicals` (10/call, SPY included),
  `get_equity_fundamentals` (10/call), `get_equity_quotes` (20/call, last); ApeWisdom page 1,
  StockTwits trending + gauges, Arctic Shift, RH watchlists, `quotes_min.json`, the options
  saved scan `cc3b6743-…`, `get_earnings_calendar`. The runner runs `technicals.py`
  (`--premarket` on the 08:00 slot), `sentiment.py`, `scanner.py`, `paper_mirror.py`,
  `portfolio.py`, `render.py`, `archive.py`, `history.py`.
- **PM**: `get_equity_quotes` for every held/working symbol (books read from
  `C:\ai-trading-state\books\`) plus every candidate ticker in today's newest
  `scans\<run_id>.json`; `get_equity_positions`; optional `get_option_chains` for held names
  and finalists (the S-01 chain snapshot). The scan hand-off is `scans\latest.json` on the box.
- **Sentinel / watch**: quotes for the held/working set (`get_equity_tradability` and
  `get_equity_positions` for the watch). SPY alone when every book is flat, so the run is
  still recorded — the dead-man's switch reads the heartbeat every `run.py` invocation writes.
- **Health**: nothing (`"files": {}`); the runner runs `engine/health.py` over the state repo.

## Switching over

Do this in one sitting, after `selftest.py` prints `PASS` on the box and
`git -C C:\ai-trading-state status -sb` is clean.

1. **Seed the state repo** from the project's current copies once (`claude/paper-book*.json`
   → `books\`, `claude/pm-journal*.json` → `journals\`, `claude/pm-watch-journal.json` →
   `journals\watch.json`, `claude/pm-coverage.json` → `coverage\`, `claude/latest-scan.json`
   → `scans\latest.json`, `claude/scan-index.json`, `claude/scan-history.json`; strip the
   `mirrors` block from each book — the runner never carries it) and commit. Until this is
   done every PM/sentinel/watch invocation refuses with "no stored book".
2. **Health check first** (`health-check.md`, 16:15). It is re-runnable, needs no inputs, and
   proves the runner path end to end. Fire it by hand once; read `health\<date>.json`.
3. **Mirror sync** (`mirror-sync.md`). Proves the state repo → web root path. Fire by hand;
   `verify_published.py` must say `PROBLEMS: none`.
4. **Sentinel** (`sentinel.md`) and the two **watches**. Exits-only / read-only; a wrong
   input is a refusal, never a trade. Let one sentinel hour and one watch session run on
   the schedule and read their manifests.
5. **Dead-man's switch** (`docs/runner/deadman-task.md`) — new trigger, `5,35 9-16 * * 1-5`.
   Save it before the decision slots move: from here on a missed slot is noticed.
6. **The four scans**, morning first. Each scan's `scans\latest.json` is what the PM 45
   minutes later reads, so a scan on the box with a PM still in the cloud would leave that
   PM without a hand-off — switch each scan and its PM in the same step, or the scan the
   evening before its PM.
7. **The four PM slots**, then confirm on the next day's health sheet: 4 PM + 7 sentinel
   coverage rows, one `engine_sha`, no `aborted: true`.
8. **Weekly review** and **score validation** (they only read; switch them any Friday).
9. **Morning brief** (`morning-brief.md`) — new trigger, `45 8 * * 1-5`, after ntfy and the
   email address are in `claude/engine-config.json` (`alerts.ntfy_topic`, `alerts.email`)
   **and copied by hand into `C:\ai-trading-runner\engine-config.json`**, which is where
   the task reads them. The topic name is the password for a public topic, so it is never
   committed to either repo. Until P-08 adds the ntfy host to the egress allowlist the
   POST fails and the task delivers by push and email alone — which it says out loud.
10. Retire the project-doc writes in any prompt still carrying them; from step 2 on, the
    health check is the only writer of `claude/paper-book*.json` and friends.

Forcing an aborted row once (R-05 acceptance): start a PM prompt with the box signed out —
the session must leave `claude/health/<date>-<slot>-pm-blocked.md`, and the next health
sheet must FAIL on the missing coverage row.

## How each prompt fails closed

| Situation | What the prompt does |
|---|---|
| `start_process` refused or hangs > 2 min | retry once; then the hand-written `aborted: true` coverage row (uncommitted; the next run's commit sweeps it in) |
| Desktop Commander itself unavailable | `claude/health/<date>-<slug>-blocked.md`, push, stop — `docs/runbooks/box-dark.md` |
| exit 1 (refused: manifest, window, lock, stale quotes) | report the reason from `outcome.json`; the runner already wrote the aborted row; never edit inputs to pass |
| exit 2 (an engine step or git) | report; the abandoned desk is named; `docs/runbooks/git-push-rejected.md` if it was the push |
| a desk `check != 0` | that desk is abandoned this run and named; the other desks still landed |
| every book flat | quote SPY alone so the run is still recorded |
