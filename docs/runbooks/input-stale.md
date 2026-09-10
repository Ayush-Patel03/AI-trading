# Runbook — input stale

**Symptom.** `outcome.json` says `refused` with a reason of the form `pm_quotes.json: newest
quote is Ns older than as_of`, `inputs are N minutes old at invocation`, `as_of … is outside
the … window`, or `… is dated <yesterday>`.

**How it shows up.** Exit code 1. A `manifests\<date>\<slot>-<desk>.json` with
`outcome: refused`, and an `aborted: true` coverage row carrying the same reason — committed,
so the health check counts it as a slot that did not happen.

**Recovery — the session, immediately.**
1. Fetch the quotes again (`get_equity_quotes` for every holding and every candidate), write
   the raw payload, recompute the sha256, set `as_of` to the fetch time, write the manifest.
2. Re-run the same `start_process` command. The refused manifest does not block a retry;
   only `committed` does.
3. If the slot window has genuinely closed (the session ran an hour late), do not force it:
   the next sentinel is minutes away and holds the same stops. Say in the report that the
   slot was missed and why.

**Recovery — Vishal.** None needed. If refusals recur at the same slot, the task's schedule is
too tight for the session's collection time — widen `window_min` for that slot in
`runner\slots.json` by pull request, not on the box.

**Do not.** Edit `as_of` to fit. Do not retype the prices into `pm_prices.json` to dodge the
venue-timestamp check — a hand-typed price is exactly what PM.md §9 forbids on this path.
Do not pass `--now`; it exists for the selftest and is recorded in the manifest.
