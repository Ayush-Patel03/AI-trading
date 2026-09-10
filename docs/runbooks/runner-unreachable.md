# Runbook — runner unreachable

**Symptom.** The session's `start_process` call fails, hangs past two minutes, or returns
without `RUNNER …` on its last line and without `outcome.json` appearing in
`C:\ai-trading-inputs\<run_id>\`.

**How it shows up.** Nothing on the box wrote anything, so the runner could not leave its own
record. The gap is visible only as a missing `manifests\<date>\<slot>-<desk>.json`, a missing
coverage row for that slot, and a `last_commit_age_h` failure on the next health sheet. The
page shows the previous run's timestamp going stale.

**Recovery — the session, now.**
1. Retry once: `start_process` the same command. The runner is idempotent; a run that did
   complete answers `already_done`.
2. If it still cannot start, write the coverage row yourself so the gap is not silent: read
   `C:\ai-trading-state\coverage\pm-coverage.json` through Desktop Commander, append
   `{"ts": …, "slot": "<slot>", "desk": "<desk>", "aborted": true, "reason": "runner
   unreachable: <error text>", "engine_source": "session"}` under today's date, write it back,
   and say so in the report. Do not commit it — the next successful run's `git add -A` will.
3. Push a notification: a slot that did not run is a position that was not checked.

**Recovery — Vishal.** Check Desktop Commander is running and the box is signed in
(`box-dark.md`). Run `C:\ai-trading-runner\.venv\Scripts\python.exe
C:\ai-trading-runner\engine\runner\selftest.py`; a PASS means the runner is fine and the
problem is the transport.

**Do not.** Run the engine in the cloud sandbox instead and write the book back by hand — that
is the transport the runner replaced, and it would leave two authorities for one book. Do not
delete `.runner.lock` unless `box-dark.md` says the box rebooted mid-run.
