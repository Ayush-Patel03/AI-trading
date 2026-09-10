# The dead-man's switch task (K-05)

`runner/run.py` writes `C:\ai-trading-state\health\heartbeat.json` on every invocation —
any slot, any outcome, refusals and `already_done` included. `runner/deadman.py` is the
other half: an **independent** checker that counts how many expected sentinel / PM runs
have gone by during market hours since that heartbeat and, at two missed, presumes the box
dead and protects the paper book (`docs/runbooks/deadman-tripped.md`). This page is the
scheduled task that calls it.

## Why a different runtime

The sentinel, the four decision slots and the health slot are all Cowork scheduled tasks
that stage inputs and start `run.py` through Desktop Commander. If the *thing that starts
the runner* is what died — the Claude app signed out, the scheduler stalled, the session
hung at `start_process` — a checker on the same path dies with it and never notices. The
dead-man's task therefore:

- is its own scheduled task, with its own prompt, not a step inside any slot's prompt;
- does one thing: `start_process` on `deadman.py`, then reads its last line;
- touches no input file, no quote, no book — the program does the work and commits;
- takes no lock and imports nothing from `run.py`, so a runner broken by a bad engine
  pull cannot break it (`engine-pull-failed.md`).

It still depends on the box being reachable through Desktop Commander. If the box itself is
dark, `box-dark.md` applies and nothing on it can protect the book — that gap is the
reason the LIVE behaviour (broker-resident stops) is specified at all, and the reason it
stays unimplemented until the two-key live mode exists (PM.md §1).

## Schedule

Every 30 minutes, **09:35–16:05 ET, Monday–Friday** (fourteen firings: 09:35, 10:05, …,
16:05). The 09:35 firing sits on the first sentinel's nominal time and can only see the
08:45 PM slot; by 10:55 two runs (10:35 sentinel, 10:45 PM) are due and the switch is
armed. The 16:05 firing is the last one that can trip on the 15:45 power-hour PM. Outside
09:35–16:05 the program answers `DEADMAN QUIET — outside market hours` and does nothing.

Trip rule (`--max-missed 2`, `--grace-min 20`): an expected run counts as missed when its
nominal time plus twenty minutes has passed and the heartbeat is older than it. A live
`.runner.lock` younger than 45 minutes counts as a heartbeat — a run is in progress.

## The task prompt (complete, standalone)

```
You are the dead-man's switch for the AI-trading paper books. Do exactly this, nothing more.

1. Through Desktop Commander, start this process and wait for it to finish (it takes
   under ten seconds):

   start_process("C:\\ai-trading-runner\\.venv\\Scripts\\python.exe C:\\ai-trading-runner\\engine\\runner\\deadman.py --state C:\\ai-trading-state")

2. Read its output. The LAST line is one of:
     DEADMAN QUIET <ts> — outside market hours
     DEADMAN OK <ts> — N of 2 allowed missed
     DEADMAN TRIPPED (standing) since <ts>
     DEADMAN CLEARED <ts> — runner heartbeat <ts>
     DEADMAN TRIPPED <ts> — N missed run(s), protective stops stamped on M position(s)
   Exit code 0 is quiet/ok/standing/cleared, 3 is a fresh trip, 2 is an error.

3. If the last line is QUIET, OK or TRIPPED (standing): stop. Write nothing, publish
   nothing, do not touch the state repo, do not run anything else.

4. If the last line is DEADMAN TRIPPED <ts> — … (exit 3): the runner has not been heard
   from for two expected runs during market hours and the paper books now carry
   protective_stop stamps. Do all of the following, in this order:
   a. Read C:\ai-trading-state\health\deadman.json through Desktop Commander.
   b. Push a notification (ntfy topic from claude/engine-config.json, priority urgent):
      title "DEAD-MAN TRIPPED — runner silent", body:
      "<missed> expected sentinel/PM runs missed since <last_heartbeat>. Working paper buys
       cancelled; protective stops stamped on <N> position(s) across <desks>. Paper ledger
       only — no broker order was placed. Runbook: docs/runbooks/deadman-tripped.md"
   c. Send the same text by email to the address in claude/engine-config.json
      ("alerts.email"), subject "DEAD-MAN TRIPPED <date> — AI-trading runner silent".
   d. Write claude/health/<date>-deadman-tripped.md in the project with the deadman.json
      contents, the per-desk stamp counts, and the last five lines of the process output.
   e. Do NOT run the sentinel, the PM, the scan or the health slot yourself. Do NOT
      write to books\, journals\ or coverage\. Do NOT push the state repo.

5. If the last line is DEADMAN CLEARED: push one notification (priority default), title
   "Dead-man cleared — runner is back", body "heartbeat <ts>; the protective_stop stamps
   stay on the positions as the record. Review docs/runbooks/deadman-tripped.md step 3."
   Then stop.

6. If start_process fails or exit code is 2: push a notification (priority high), title
   "Dead-man checker could not run", body with the error text, and stop. Do not retry
   more than once. Do not try to reproduce the check by hand.

Paper mode is absolute. This task never calls an order tool, never edits a book, and
never asks a session to.
```

The ntfy topic and the alert email are identifiers and live in the private
`claude/engine-config.json` (`alerts.ntfy_topic`, `alerts.email`), never in this repo.

## What a trip pushes

| channel | when | content |
|---|---|---|
| ntfy (urgent) | fresh trip (exit 3) | missed count, last heartbeat, stamped positions per desk, "paper ledger only", runbook link |
| email | fresh trip | the same body, subject `DEAD-MAN TRIPPED <date> — AI-trading runner silent` |
| ntfy (default) | clear | heartbeat ts; stamps stay as the record |
| ntfy (high) | checker cannot run | the error text; one retry only |
| project doc `claude/health/<date>-deadman-tripped.md` | fresh trip | `deadman.json`, per-desk counts, process tail |

A standing trip pushes nothing on the later firings, by design: one alarm per outage.
The health slot's `deadman` check stays `fail` until the record clears, so the daily sheet
carries it too.

## What it does on the box, precisely

`deadman.py --state C:\ai-trading-state` (no `--push`: the trip is committed locally and
the next runner commit, or Vishal, pushes it — `git-push-rejected.md`):

1. reads `health\heartbeat.json` and `.runner.lock`; computes today's expected sentinel
   and PM times from `runner\slots.json` (`sentinel.times_et`, each slot's `nominal_et`);
2. outside market hours or on a weekend: quiet, exit 0;
3. fewer than two missed: `DEADMAN OK`, exit 0; already tripped: standing, exit 0;
4. otherwise, for `books\swing.json`, `pullback.json`, `momentum.json`: cancels every
   working **buy** (sells rest), stamps every position with
   `protective_stop: {level, placed_at, reason: "deadman", basis, paper: true}` — level =
   the position's `stop`, else `avg_cost − 2.5 × atr_14`, else `null` with an
   `UNPROTECTED` warning — bumps `revision`/`based_on_revision` so a concurrent runner
   write is refused by the §8 `--check`, appends a `slot: "deadman"` journal entry per
   desk, appends an `aborted: true` coverage row (`slot: "deadman"`, `engine_source:
   "deadman"`), writes `health\deadman.json` and commits `deadman TRIPPED <date> …`;
5. on a later firing with a heartbeat newer than the trip: rewrites `deadman.json` with
   `tripped: false, cleared_at, cleared_by_heartbeat` and commits `deadman CLEARED …`.

Live mode (not implemented, PM.md "Dead-man's switch"): step 4 would place a
broker-resident stop-limit per position at the stamped level. Nothing here does.
