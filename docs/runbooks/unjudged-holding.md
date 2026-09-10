# Runbook — unjudged holding

**Symptom.** A journal warning `<SYM>: not in this scan's universe — priced but unjudged`, or
`UNPROTECTED` on a position. The first means the manager has a live price for the holding but
the scan did not score it this slot, so the exit rules that need a score cannot run; the second
means it has no usable price at all — no quote in the payload, a halted state, or a quote older
than 30 minutes.

**How it shows up.** The warnings list on the journal entry and on the board row; the coverage
row's `warnings` count; the health sheet's `unjudged` count (plan 1.5). `UNPROTECTED` also
pushes a notification.

**Recovery — the session, at the next run.**
- `UNPROTECTED`: the quote payload was incomplete. Quote **every holding on every desk** plus
  every candidate (PM.md §9) before writing `pm_quotes.json`; then re-run the slot — it replaces
  its own entry. If the connector refuses the symbol (halted), say so; the stop cannot fire on a
  halted name and that is the honest state.
- `unjudged`: the scan universe moved on without the holding. The stop and target still fire on
  the live price; only score-based exits wait. Nothing to fix per run. If a name stays unjudged
  for days, ask the scan to carry held names forward (SCAN.md "carry-forward").

**Recovery — Vishal.** None. A persistent `UNPROTECTED` on a name that trades normally is a task
prompt that is not quoting all desks — fix the prompt, not the book.

**Do not.** Add a hand price to `pm_prices.json` to silence it. Do not sell an unjudged name by
hand because the warning looks alarming — it is a description, not an alert.
