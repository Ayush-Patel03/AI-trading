# Runbook — engine pull failed

**Symptom.** `update.ps1` stops at `git pull --ff-only origin production` (diverged history,
network, credentials) or the selftest that follows it fails.

**How it shows up.** The health sheet's `engine_branch`/`engine_sha` checks, and every run
manifest's `engine_sha`, keep reporting the old commit after a promotion. The weekly review
sees decisions attributed to a sha that `production` no longer points at.

**Recovery — Vishal.**
```powershell
git -C C:\ai-trading-runner\engine fetch origin
git -C C:\ai-trading-runner\engine status -sb              # local edits? there should be none
git -C C:\ai-trading-runner\engine checkout -- .           # discard stray edits, if any
git -C C:\ai-trading-runner\engine reset --hard origin/production
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\selftest.py
```
If the selftest fails on the new engine, go back — `git reset --hard <previous sha>` (the
sha is in the last committed manifest) — and open the failure as an issue against `main`.
Until then the box keeps running the last engine that passed, which is the correct state.

**Recovery — a session.** None on the box. A session may *ask* for the update in the health
doc and the report, with the sha it expects. It never runs `git pull` on the box and never
edits a file under `C:\ai-trading-runner\engine`.

**Do not.** `pip install` anything beyond `runner\requirements.txt`. Do not promote
`production` from the box. Do not patch the engine in place to "get the slot out" — the
runner records the sha it ran, and an unattributable decision is worse than a missed slot.
