# Runbook — dead-man's switch tripped

**Symptom.** `C:\ai-trading-state\health\deadman.json` carries `"tripped": true`; the
dead-man's task pushed `DEAD-MAN TRIPPED — runner silent`; the state repo has a commit
`deadman TRIPPED <date> <N> missed engine deadman`. The runner was not heard from for two
expected sentinel/PM runs during market hours (`health\heartbeat.json` older than both).

**How it shows up.** `health.json` (the health slot's sheet, `health\<date>.json`):
`deadman` is `fail` with the trip record as its value; `runner_heartbeat` is `warn`/`fail`
with `value.missed` ≥ 2; `book_freshness` reads the same silence per desk; `coverage_today`
lists the missing slots and counts the `aborted: true` row the switch appended
(`slot: "deadman"`, `engine_source: "deadman"`). Every open position on every desk carries
`protective_stop: {level, placed_at, reason: "deadman", basis, paper: true}`; every working
paper **buy** is gone; each desk's journal has a `slot: "deadman"` entry listing the
cancels and the stamps.

**What the switch did, and did not do.** It acted on the **paper ledger only**. The level
it stamped is the position's own stop (or entry − 2.5×ATR when it had none) — the same
stop `pm.py` fires on at the next run it gets. No broker order was placed; PM.md
"Dead-man's switch" says what live mode would do and why it is not wired.

**Recovery — the session, now.**
1. Find out why the runner is silent: `runner-unreachable.md` (transport), `box-dark.md`
   (the box), `engine-pull-failed.md` (a broken engine after an update). The trip is a
   symptom; the silence is the fault.
2. Get one run through. Any runner invocation — the next sentinel is enough — writes a
   fresh heartbeat; the next dead-man firing (≤ 30 min) clears the record
   (`tripped: false`, `cleared_at`) and commits `deadman CLEARED …`. Do not edit
   `deadman.json` by hand and do not delete it; the clear is the record that the runner
   came back.
3. Lead the next report with the unwatched stretch: `deadman.json.missed_runs` names the
   runs that did not happen; the price the book saw at `last_heartbeat` and the price it
   sees now is the cost of the outage (PM.md §12c). List any `UNPROTECTED` warning in the
   deadman journal entries — a position with no stop and no ATR got a `null` level.
4. The cancelled buys are not re-placed. If the setup still stands at the next decision
   slot the entry pass will place it again on its own; if it does not, the switch was right.

**Recovery — Vishal.** Only what the underlying runbook says. Once the runner is back,
confirm `git -C C:\ai-trading-state log --oneline -5` shows the TRIPPED and CLEARED
commits and that they pushed (the switch commits without pushing; `git-push-rejected.md`
if `[ahead N]` stands after the next runner push).

**The stamps stay.** `protective_stop` is a record, not a control: `pm.py` ignores it,
the board does not show it, and a second trip on the same day keeps the first stamp. It is
how the weekly review can see which positions sat unwatched, for how long, at what level.

**Do not.** Run the sentinel by hand in the cloud sandbox "to cover the gap" — that is the
transport the runner replaced and would leave two authorities for one book. Do not clear
`deadman.json` to make the health sheet pass. Do not raise `--max-missed` on the box; the
threshold changes by pull request in `docs/runner/deadman-task.md`.
