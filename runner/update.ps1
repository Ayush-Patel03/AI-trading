# update.ps1 — pull the promoted engine and prove it still runs here. Vishal runs this after
# a promotion of `production`; a session may ask for it (via the health check), never run it.
$ErrorActionPreference = "Stop"
git -C C:\ai-trading-runner\engine pull --ff-only origin production
& C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\runner\selftest.py
