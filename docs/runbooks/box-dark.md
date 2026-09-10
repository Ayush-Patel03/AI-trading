# Runbook — box dark

**Symptom.** `ns-ayushoffice` is off, asleep, signed out, or the Claude app / Desktop Commander
is not running. Every `mcp__remote-devices__Desktop_Commander__*` call fails, not just the runner.

**How it shows up.** The health sheet cannot be written at all (the health slot runs on the box).
The mirror on the web root stops updating (`mirror-stale.md`). Every slot's coverage row is
missing. The page chip shows the last commit age climbing past two slots.

**Recovery — Vishal only.** Power on, sign in, confirm the Claude app and Docker Desktop start
(state dump §6.1 — launch-at-login decides whether this recurs). Then, in PowerShell:
```powershell
Get-Content C:\ai-trading-state\.runner.lock          # if present, the box died mid-run
git -C C:\ai-trading-state status -sb                  # uncommitted files = a run that was cut off
```
A lock older than 45 minutes is taken over automatically by the next run; a younger one from a
dead pid can be deleted by hand. Uncommitted files under `books\`/`journals\` are the cut-off
run's write-back: leave them, the next run commits them with its own manifest. Then run
`selftest.py`.

**Recovery — a session.** Nothing on the box. Record the gap: write an `aborted: true` row
per `runner-unreachable.md` step 2 once the box is back, and lead the next report with the
unwatched stretch — the price the book saw last and the price it opens at is the cost of the
outage, and the 08:45 slot names it (PM.md §12c).

**Do not.** Move the runner into the cloud "temporarily". Do not `git reset --hard` the state
repo to make `status` clean — that discards a write-back.
