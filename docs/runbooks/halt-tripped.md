# Runbook — halt tripped (kill switch)

**Symptom.** A desk's book carries `"halted": true`; `pm.py` prints `*HALTED*`; the journal
entry has a kill-switch warning; the board shows the red banner. A session loss of 3% or worse
against the session's opening equity did it (PM.md §7).

**How it shows up.** The journal entry for that run, the book's `day` block, the coverage row's
warning count, the Trade Desk banner, and `HOUSE`/`halted` on the page's desk chip.

**What the halt does.** Cancels working buys and blocks new entries for the rest of the session.
**Exits stay live** — stops and targets still fire at every sentinel and slot. Nothing here is
broken; the switch worked.

**Recovery — nobody, on purpose.** The halt clears itself at the next session roll (the first
run of the next trading day). The remaining runs of the day keep pricing and protecting the
book. The session's job is to report it plainly, push the notification, and lead the next
morning's report with the size of the loss and what was sold.

**Recovery — Vishal, only if the halt is wrong.** If the equity reference is corrupt (a bad
fill price, a mis-marked book), the fix is to the input that caused it — a corrected quote
payload and a re-run of the slot, which replaces its own journal entry — never a hand edit of
`halted`.

**Do not.** Edit `books\<desk>.json` to clear the flag. Do not re-run the slot with different
quotes to "un-halt" it. Do not raise the 3% threshold on the box; `portfolio.RULES` changes by
pull request.
