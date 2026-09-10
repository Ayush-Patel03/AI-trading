# Runbook — git push rejected

**Symptom.** `outcome.json` shows `outcome: committed` with `"push_failed": true` and a
`push_error`; the runner's last log line reads `git: PUSH FAILED — the commit stays local`.
Exit code is still 0: the state is safe, on the box.

**How it shows up.** `git -C C:\ai-trading-state status -sb` shows `[ahead N]`; the health
sheet's `push_backlog` check is `warn` at 1 unpushed commit and `fail` at 2 (`value.ahead`); the project's synced copy of `ai-trading-state` stops
moving while the box keeps committing.

**Two causes, two fixes.**
1. *Network or key* (`Permission denied (publickey)`, `Could not resolve host`): Vishal checks
   the deploy key and connectivity, then `git -C C:\ai-trading-state push`. Every backed-up
   commit goes with it; nothing to reconcile.
2. *Rejected — non-fast-forward*: something committed to the remote that the box does not
   have (a session writing through the GitHub connector, an edit on github.com). Vishal, in
   PowerShell:
   ```powershell
   git -C C:\ai-trading-state fetch origin
   git -C C:\ai-trading-state log --oneline HEAD..origin/main      # what the remote has
   git -C C:\ai-trading-state rebase origin/main                    # the box's commits on top
   git -C C:\ai-trading-state push
   ```
   A conflict in `books\` or `journals\` means two writers changed the same book: the box's
   version is the authority (the engine ran there, under the lock). Resolve by keeping `ours`
   for those paths, then say in the health doc which remote commit was overridden and why.

**Recovery — a session.** Nothing on the box beyond reporting it. Do not write to
`ai-trading-state` through the GitHub connector while `[ahead N]` stands — that widens the
divergence.

**Do not.** `git push --force` from either side. Do not let the runner rebase automatically —
it deliberately leaves the commit local so a human sees the divergence before anything is
overwritten.
