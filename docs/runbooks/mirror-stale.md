# Runbook — mirror stale

**Symptom.** The dashboard on the box (`C:\ai-trading-mirror\`, served by nginx) shows a
`manifest.json` older than two slots while the state repo has newer commits.

**How it shows up.** The page's "as of" chip; `mirror age` on the health sheet (plan 1.5).
The books in git are fine — this is the projection that is behind, not the state.

**Recovery — a session.** The mirror is built from the state repo by `engine/mirror.py` on the
box. Run it against the local clone through Desktop Commander:
```
start_process("C:\\ai-trading-runner\\.venv\\Scripts\\python.exe C:\\ai-trading-runner\\engine\\engine\\mirror.py --base C:\\ai-trading-state\\.mirror-stage --out C:\\ai-trading-mirror --slot <slot> --run-id <run_id> --previous C:\\ai-trading-mirror\\manifest.json")
```
after staging `books\*.json` → `paper_book*.json`, `journals\*.json` → `pm-journal*.json`,
`scans\latest.json` → `latest-scan.json`, `scan-index.json`, `scan-history.json` and
`coverage\pm-coverage.json` into `.mirror-stage` under the names `mirror.py` expects. A
non-zero exit is a refusal (a book moving backwards, a banned shape) — report it, do not
work around it. Then `prune_orphans.py` and `verify_published.py` as the mirror task already
does (state dump §4.2).

**Recovery — Vishal.** If nginx is down, `C:\ai-trading-tools\serve.ps1`. If the mirror sync
task itself is failing, its health doc under `claude/health/` names the step.

**Do not.** Edit the scrubber to make a refusal pass. Do not copy files from `.runs\` into the
web root — that directory is scratch and may hold a half-written run.
