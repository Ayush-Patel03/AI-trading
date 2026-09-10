# deadman - the dead-man's switch task

The complete, standalone prompt for this task lives in **`docs/runner/deadman-task.md`** (the fenced block under "The task prompt"), next to the program it calls, `runner/deadman.py`. It is deliberately not duplicated here: the prompt and the program's output lines are kept in step by `tests/test_deadman.py`, and a second copy is exactly the kind of drift the 2026-08-31 outage came from.

Schedule: `CRON_TZ=America/New_York 5,35 9-16 * * 1-5` - fourteen useful firings 09:35, 10:05, ..., 16:05; the 09:05 and 16:35 firings the expression also produces answer `DEADMAN QUIET - outside market hours` and cost nothing.

The call it makes, in full:

```
start_process("C:\\ai-trading-runner\\.venv\\Scripts\\python.exe C:\\ai-trading-runner\\engine\\runner\\deadman.py --state C:\\ai-trading-state")
```

It reads `health\heartbeat.json` (written by every `run.py` invocation) and `.runner.lock`, counts the expected sentinel/PM runs since the heartbeat from `runner/slots.json`, and at two missed (`--max-missed 2`, `--grace-min 20`) stamps `protective_stop` on every paper position, cancels working paper buys, appends a `slot: "deadman"` journal entry and an `aborted: true` coverage row, writes `health\deadman.json` and commits. Exit 0 quiet/ok/standing/cleared, 3 a fresh trip, 2 an error. The task pushes only on a fresh trip, a clear, or a checker that could not run. Runbook: `docs/runbooks/deadman-tripped.md`.
