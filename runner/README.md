# runner — the engine on the box

`run.py` is the single entry point for every scheduled slot on `ns-ayushoffice`. A Claude
session never writes code on the box. It writes **inputs** — the raw Robinhood quote payload,
the scan's collected data, options chains, sentiment payloads — into
`C:\ai-trading-inputs\<run_id>\` with a `manifest.json`, then starts the runner through
Desktop Commander. The runner verifies the inputs, stages a run directory exactly the way
the test suite and the task prompts stage one, runs the engine as subprocesses of its own
interpreter, writes the outputs back into the local clone of the private `ai-trading-state`
repo, records a run manifest, and commits and pushes. Every refusal leaves a record.

Paper mode is inherited whole from the engine (`docs/PM.md` section 1). The runner passes
no `--mode`, calls no order tool, and cannot flip the flag the book carries.

Stdlib only. The box installs nothing beyond `tzdata` (see `requirements.txt` for why).

## Install on the box (PowerShell, once — Vishal)

```powershell
# 1. the engine, at the promoted branch
New-Item -ItemType Directory -Force C:\ai-trading-runner | Out-Null
git clone --branch production https://github.com/Ayush-Patel03/AI-trading C:\ai-trading-runner\engine

# 2. the interpreter and its one dependency
py -3.12 -m venv C:\ai-trading-runner\.venv
C:\ai-trading-runner\.venv\Scripts\python.exe -m pip install --upgrade pip
C:\ai-trading-runner\.venv\Scripts\python.exe -m pip install --require-hashes -r C:\ai-trading-runner\engine\runner\requirements.txt

# 3. the private state repo, with the deploy key (already registered on the GitHub repo)
#    The key file is yours; the runner never reads it directly — git does.
#    Forward slashes in the key path: core.sshCommand is run through git's own sh.
git -c core.sshCommand="ssh -i C:/Users/AyushPatel/.ssh/ai-trading-state-deploy -o IdentitiesOnly=yes" `
    clone git@github.com:Ayush-Patel03/ai-trading-state.git C:\ai-trading-state
git -C C:\ai-trading-state config core.sshCommand "ssh -i C:/Users/AyushPatel/.ssh/ai-trading-state-deploy -o IdentitiesOnly=yes"
git -C C:\ai-trading-state config user.name  "ai-trading-runner"
git -C C:\ai-trading-state config user.email "runner@ai-trading.local"

# 4. the identifiers — copied by hand from the project's claude/engine-config.json.
#    Lives next to the venv, NOT inside the engine clone and NOT inside the state repo.
notepad C:\ai-trading-runner\engine-config.json

# 5. prove it
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\selftest.py
```

`selftest.py` replays a frozen input set (`tests/fixtures/`) through the runner against a
throwaway state repo it `git init`s in a temp directory. It must print `selftest PASS`.
It also checks the tz database, because a Windows Python without `tzdata` makes the
engine's scan-freshness and macro gates silently pass.

### Updating after a promotion — `update.ps1` (Vishal, never a session)

```powershell
C:\ai-trading-runner\engine\runner\update.ps1
```

Two lines: `git pull --ff-only origin production` in the engine clone, then `selftest.py`.
If the selftest fails, the box is on a broken engine — `git -C C:\ai-trading-runner\engine
checkout <previous sha>` and say so in the health doc.

### The state repo layout

```
C:\ai-trading-state\
  books\swing.json  pullback.json  momentum.json      the living books (mirrors block stripped)
  journals\swing.json  pullback.json  momentum.json   the decision journals; watch.json for the watch
  coverage\pm-coverage.json                          COVER-01 rows, including aborted: true rows
  scans\latest.json  <scan run_id>.json               the hand-off and the compact records
  scan-index.json  scan-history.json
  archive\book-history\<pm run_id>.json               every revision that changed something
  archive\runs\<runner run_id>\                       boards, pm_state, engine stdout/stderr
  manifests\seed.json                                 what the repo was seeded from (runner/seed_state.py): source hashes, book revisions
  manifests\<date>\<slot>-<desk>.json                 one per invocation; the idempotency record (sentinel-<HH>-<desk>, watch-<session>-<desk>, <slot>-scan, health-<HHMM>)
  experiments\ledger.jsonl                            the experiment ledger (seeded from the engine repo's experiments/)
  health\<date>.json  <date>.md                      the health slot's sheet (engine/health.py + runner checks)
  health\heartbeat.json                              written by EVERY invocation, any outcome (K-05)
  health\deadman.json                                the dead-man's switch record when it has ever tripped
  .runs\  .runner.lock                                ignored; scratch and the lock
```

One commit per invocation: `<slot> <desk> <date> <run_id> engine <sha>` (or `… REFUSED` /
`… FAILED`). `git log` of this repo is the run history.

`runner/validate_state.py --state C:\ai-trading-state --engine C:\ai-trading-runner\engine`
checks the layout at any time: every JSON parses, no `mirrors` block and no banned shape
(the masked account number in either spelling) anywhere, every book at or above the
revision `manifests\seed.json`, the coverage rows and `archive\book-history` last saw it,
coverage rows well-formed. Exit 0/1. The health slot runs the same check as its
`state_layout` row.

### Seeding the state repo (once — `runner/seed_state.py`)

The project docs are the source of truth until the runner takes over; the state repo
starts as a copy of them, laid out the way `run.py` reads. `seed_state.py` maps a directory
of project-doc copies onto the layout above — `paper-book.json` → `books\swing.json`,
`paper-book-pullback.json` → `books\pullback.json`, `pm-journal*.json` → `journals\`,
`pm-watch-journal.json` → `journals\watch.json`, `pm-coverage.json` → `coverage\`,
`latest-scan.json` → `scans\latest.json`, `scans\<date>-<slot>.json` → `scans\`,
`scan-index.json`, `scan-history.json` (root, where `run.py` stages them from),
`book-history\*.json` → `archive\book-history\` (both the doc names and pm.py's staged
names are accepted; a `claude\` prefix is dropped; anything else is listed as skipped).
It STRIPS the `mirrors` block from every book, refuses the whole seed if any output still
matches `engine/mirror.py`'s banned shapes, refuses to move a book to a lower `revision`
than the target already holds, writes `manifests\seed.json`, `experiments\ledger.jsonl`,
`health\`, `.gitignore`, and a README for the state repo — all in memory first, then to
disk, so a refusal leaves nothing behind. With `--init-git` it runs `git init` on `main`
and makes ONE commit, `seed state from project docs <date>`. It never pushes; the remote
and the push are the person's step below.

**On the box (PowerShell — Vishal, with a session doing the copying).**

```powershell
# 1. the copies. A session with the project attached reads each doc (project_read) and
#    writes it, byte for byte, under C:\ai-trading-seed through Desktop Commander:
#      paper-book.json  paper-book-pullback.json  paper-book-momentum.json
#      pm-journal.json  pm-journal-pullback.json  pm-journal-momentum.json  pm-watch-journal.json
#      pm-coverage.json  latest-scan.json  scan-index.json  scan-history.json
#      scans\<date>-<slot>.json  (the compact records)      book-history\<run_id>.json
#    Nothing else from claude/ belongs here (plans, research, health notes are not state).
New-Item -ItemType Directory -Force C:\ai-trading-seed | Out-Null
Get-ChildItem C:\ai-trading-seed -Recurse -File | Select-Object FullName, Length

# 2. see the plan; nothing is written
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\seed_state.py `
    --from C:\ai-trading-seed --to C:\ai-trading-state --engine C:\ai-trading-runner\engine --dry-run

# 3. seed, with the engine repo's experiment ledger, and make the one commit on main.
#    If C:\ai-trading-state is already the (empty) clone from install step 3, the same
#    command seeds into it — an existing .git is kept, not re-initialised.
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\seed_state.py `
    --from C:\ai-trading-seed --to C:\ai-trading-state --engine C:\ai-trading-runner\engine `
    --ledger C:\ai-trading-runner\engine\experiments --init-git

# 4. prove it (exit 0), and read manifests\seed.json: the three book revisions must match the docs
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\validate_state.py `
    --state C:\ai-trading-state --engine C:\ai-trading-runner\engine
Select-String -Path C:\ai-trading-state\books\*.json -Pattern '"mirrors"'     # must print nothing

# 5. the remote and the push — a person's step, with the deploy key (never a session)
git -C C:\ai-trading-state remote add origin git@github.com:Ayush-Patel03/ai-trading-state.git
git -C C:\ai-trading-state config core.sshCommand "ssh -i C:/Users/AyushPatel/.ssh/ai-trading-state-deploy -o IdentitiesOnly=yes"
git -C C:\ai-trading-state config user.name  "ai-trading-runner"
git -C C:\ai-trading-state config user.email "runner@ai-trading.local"
git -C C:\ai-trading-state push -u origin main

# 6. the copies have done their job
Remove-Item -Recurse -Force C:\ai-trading-seed
```

From then on the runner writes the repo and the project docs are read-only history; the
mirror (`docs/runbooks/mirror-stale.md`) is built from the state repo, not from the docs.

**From a cloud session (the GitHub connector's `push_files`).** When the box is not
reachable, seed in the container and push the tree with the connector — `push_files`
commits straight to `main` of `Ayush-Patel03/ai-trading-state`, so that call IS the push
and is the person's decision to make; `seed_state.py` still never pushes.

1. Read the docs with `project_read` and write them under a scratch `seed/` directory,
   under the names listed in step 1 above.
2. `python3 runner/seed_state.py --from seed --to state --ledger experiments --init-git`,
   then `python3 runner/validate_state.py --state state` (exit 0, or stop and say why).
3. Push in order, each call ≤ ~1 MB in total and never a file the validator did not see:
   - first: `books/swing.json`, `books/pullback.json`, `books/momentum.json`,
     `journals/swing.json`, `journals/pullback.json`, `journals/momentum.json`,
     `journals/watch.json`, `coverage/pm-coverage.json`, `scans/latest.json`,
     `manifests/seed.json`, `README.md`, `.gitignore` — the set the first PM run needs;
     message `seed state from project docs <date>`;
   - then: `scan-index.json`, `scan-history.json`, `scans/<date>-<slot>.json`,
     `experiments/ledger.jsonl`, `health/.gitkeep`, and `archive/book-history/*.json` in
     batches of a few files (the snapshots are the bulk).
   Read each file's bytes from `state/` as written — never retype a book.
4. Say in the health doc which commit(s) carry the seed. The box then clones the repo
   (install step 3) and its first run finds the books.

## How a session invokes it

The rewritten scheduled-task prompts — one per slot, plus the dead-man's switch and the
morning brief, with the switch-over order — are in `docs/runner/prompts/`.

```
start_process("C:\\ai-trading-runner\\.venv\\Scripts\\python.exe C:\\ai-trading-runner\\engine\\runner\\run.py --slot midday --desk all --inputs C:\\ai-trading-inputs\\<run_id> --state C:\\ai-trading-state")
```

Then read `C:\ai-trading-inputs\<run_id>\outcome.json` (and, for the full record,
`C:\ai-trading-state\manifests\<date>\<slot>-<desk>.json`). Exit codes: **0** committed or
already done, **1** refused (inputs, window, lock), **2** failed (an engine step, or git).

| `--slot` | when | `--step` | what runs |
|---|---|---|---|
| `pre-market`, `opening-range`, `midday`, `power-hour` | scan at 08:00/10:00/12:30/15:00 ET, PM 45 min later | `auto` = `scan` when the inputs carry `scan_data.json`, else `pm` | scan: `technicals.py` (if `bars.json`), `sentiment.py` (if any sentiment file), `scanner.py`, `paper_mirror.py`, `portfolio.py`, `render.py`, `archive.py --record`, `--index`, `--history-entry`, `history.py`. PM: `pm.py --slot … --desk …` per desk, then the section-8 `--check` against the stored book, then `render_pm.py` |
| `sentinel` | hourly :35, 09:35–15:35 ET | — | `pm.py --slot sentinel --desk …` per desk; a quiet desk leaves only a heartbeat |
| `watch` | 07:00 (`pre-open`) and 16:20 (`after-hours`) ET; `--session` optional | — | `watch.py --session … --desk …` per desk, read-only |
| `health` | 16:15 ET or on demand | — | `health.py --state … --engine … [--mirror …]` (thirteen pass/warn/fail checks over the state repo) plus the runner's `lock_free`, `engine_sha` and `state_layout` (`validate_state.py`) → `health/<date>.json` and `.md`; the manifest carries `health_verdict`. Pass `--mirror C:\ai-trading-mirror` for the mirror-age check |

The dead-man's switch (`runner/deadman.py`, K-05) is **not** a slot: it is its own scheduled
task on a different runtime that reads `health\heartbeat.json` and trips at two missed
sentinel/PM runs during market hours — `docs/runner/deadman-task.md`,
`docs/runbooks/deadman-tripped.md`.

The morning brief (`engine/brief.py`, U-04) is **not** a slot either. It is its own
scheduled task, `CRON_TZ=America/New_York 45 8 * * 1-5`, and it only reads: the three
books, the three journals, `coverage/pm-coverage.json`, `scans/latest.json` and
`journals/watch.json`. It writes a text body of at most 1,200 characters and a
self-contained HTML page in the Trade Desk palette into `C:\ai-trading-inputs\<run_id>\`,
and nothing at all into the state repo — so it takes no lock and cannot race the 08:45 PM
slot that fires at the same minute. Every number it reports is computed by whichever module
owns it (`ladder.state_for`, `pm.house_exposure`, `pm.house_metrics`, `fills.shadow_summary`,
`health.day_events`, `pm._is_high_impact`), so the brief cannot disagree with the manager.
`--ntfy <topic-url>` POSTs the text body with `urllib`; absent, it is a no-op and the exit
code is 0. The topic name is the password for a public ntfy topic, so it is committed
nowhere — it lives in `engine-config.json` next to the venv and is passed on the command
line — and the host needs an egress-allowlist entry before a scheduled run can reach it
(P-08). Prompt: `docs/runner/prompts/morning-brief.md`.

```
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\engine\brief.py --state C:\ai-trading-state --text <inputs>\brief.txt --html <inputs>\morning-brief.html [--ntfy <topic url>]
```

Exit 0 the brief was built, with or without alerts; exit 2 nothing under `--state` could be
read at all. A failed ntfy POST is reported on stderr and never fails the brief.

`--desk` is `swing`, `pullback`, `momentum` or `all` (the default). Use `all` from the
scheduled prompts: the idempotency key includes the desk argument, so `--desk swing` after a
committed `--desk all` is a second attempt at the swing desk (it replaces its own journal
entry, as PM.md §8b allows), not a no-op. Windows, cadences and the per-step input lists
live in `slots.json`. `--dry-run` verifies, stages and runs the engine but writes nothing
back and commits nothing. `--no-push` commits locally only.

Idempotency: `(date, key)` where the key is `<slot>-<desk>` for a PM run, `<slot>-scan` for
a scan, `sentinel-<HH>-<desk>` (one occurrence per ET hour), `watch-<session>-<desk>`, and
`health-<HHMM>` (health is always re-runnable). A key whose manifest says `committed` is
answered `already_done`, exit 0, nothing touched. `refused` and `failed` keys re-run.

The committed manifest is written before the commit that carries it, so its `git` block is
`null` on disk; `outcome.json` has the commit sha and the push result. After a push failure
the manifest is rewritten with `push_failed: true` and stays uncommitted until the next
run sweeps it in — that dirty file is the signal `docs/runbooks/git-push-rejected.md` reads.

### The input manifest a session must write

`C:\ai-trading-inputs\<run_id>\manifest.json`:

```json
{
  "as_of": "2026-09-10T17:15:03Z",
  "files": {
    "pm_quotes.json":    {"sha256": "31d2e92d…"},
    "scan_results.json": {"sha256": "11d2bebe…"},
    "pm_broker.json":    {"sha256": "8a01c3f0…"}
  }
}
```

- `as_of` — when the quotes were fetched, ISO-8601 UTC. It must fall inside the slot's
  window on today's ET date (`slots.json`; default ±90 minutes around the slot, the sentinel
  09:15–16:15, each watch session its own band) and the runner must be started within
  `max_input_lag_min` of it (25 minutes; 45 for the watch).
- `files` — every file the run needs, with the sha256 of its exact bytes. A listed file that
  is missing or whose hash differs is a refusal. A file in the directory but not in the
  manifest is **not staged** — the engine only ever sees what was verified.
- **Quote payloads** (`pm_quotes.json`, `quotes.json`) must be the raw `get_equity_quotes`
  response and their newest venue timestamp must be within 60 s of `as_of` (900 s for the
  watch). Hand-transcribed prices have no venue timestamps and are refused.
- Required per step: `pm` → `pm_quotes.json` (the scan comes from `scans/latest.json` unless
  `scan_results.json` is supplied); `sentinel` → `pm_quotes.json`; `scan` → `scan_data.json`
  (with technicals already merged, or `bars.json` + `fundamentals.json` + `quotes.json` for
  `technicals.py` to merge them); `watch` → `pm_quotes.json`; `health` → nothing (`"files": {}`).
- Optional and honoured when present: `pm_broker.json`, `pm_prices.json`,
  `pm_tradability.json`, the eight sentiment payloads named in `slots.json`.

Compute the hash on the bytes you wrote. To confirm what landed on the box:
`certutil -hashfile C:\ai-trading-inputs\<run_id>\pm_quotes.json SHA256`.

### What a refusal looks like

`outcome.json` with `"outcome": "refused"` and a plain `reason`; the same in
`manifests\<date>\<slot>-<desk>.json`; and a row in `coverage\pm-coverage.json` with
`"aborted": true` and that reason — committed (a refusal under another run's lock is the
one exception: it is written, not committed, and rides in the lock holder's commit), so the
health check and the page can see that the slot did not happen. A run that cannot reach the runner at all cannot leave that
row; the session must, per `docs/runbooks/runner-unreachable.md`.

### `fetch_bars.py` — daily bars for the harness (not a slot)

`runner/fetch_bars.py` fetches daily bars from Alpaca Market Data v2 (`feed=iex`,
`adjustment=all`, 200 requests/min on Basic, paginated by `next_page_token`) for every name
the membership file says was ever a member plus SPY and the sector ETFs, and writes
`bars_all.json` in the one shape `backtest.py`, `ic.py`, `validate.py` and `technicals.py`
read. Symbols the source cannot serve go to `bars_missing.json` — the residual survivorship
statement (`docs/DATA.md` §3); `--sector-map-out` builds the `{SYMBOL: ETF}` map the E10
recipe needs (`docs/BACKTEST.md` §6b). The Alpaca key lives at
`C:\ai-trading-runner\alpaca.env` (two lines, `APCA_API_KEY_ID=…` / `APCA_API_SECRET_KEY=…`)
— next to the venv, outside both repos, like `engine-config.json`; the program refuses a
`--keyfile` inside the engine clone. Stdlib only; tests never touch the network.

### What the runner never does

Places an order. Passes `--mode`. Pulls the engine (that is `update.ps1`, and it is
Vishal's). Merges by hand on a `--check` refusal (exit 2 → that desk's outputs are
discarded, its coverage cell says `abandoned`, the other desks still land). Rebases over a rejected push (the commit stays local;
`docs/runbooks/git-push-rejected.md`). Deletes anything from the state repo.
