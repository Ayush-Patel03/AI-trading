Publish the "ai trading" system's state to the dashboard served from ns-ayushoffice. NOT a trading run: no runner, no scan, no PM, no book edit. The state repo C:\ai-trading-state is on the box already, so nothing is read from the project and nothing is ferried: copy files on the box, run mirror.py once, verify.

RULES. Paper, absolute: never call place_equity_order, review_equity_order, cancel_equity_order or any option/crypto order tool, not even to preview. Account lock: only the Robinhood account get_accounts marks agentic_allowed=true, resolved at run time, never by nickname. Reading is not trading. Never write code to the box; the only path you write is C:\ai-trading-inputs\<run_id>\. Never edit the scrubber, mirror.py, the runner, the engine clone or C:\ai-trading-state. Do not work around a refusal: exit 1 means no - do not edit inputs to pass, do not run the engine in the sandbox, do not write a book by hand.

Directories: C:\ai-trading-staging (inputs, not served), C:\ai-trading-mirror (the WEB ROOT; only mirror.py and prune_orphans.py write here), C:\ai-trading-tools (helpers; do not modify). NEVER write into C:\ai-trading-mirror yourself. Desktop Commander only (ToolSearch first; never device_* tools).

STEP 1 - stage from the state repo. start_process powershell.exe:
$s='C:\ai-trading-state'; $d='C:\ai-trading-staging'; Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue; New-Item -ItemType Directory -Path "$d\health" -Force | Out-Null
Copy-Item "$s\books\swing.json" "$d\paper_book.json"; Copy-Item "$s\books\pullback.json" "$d\paper_book_pullback.json"; Copy-Item "$s\books\momentum.json" "$d\paper_book_momentum.json"
Copy-Item "$s\journals\swing.json" "$d\pm-journal.json"; Copy-Item "$s\journals\pullback.json" "$d\pm-journal-pullback.json"; Copy-Item "$s\journals\momentum.json" "$d\pm-journal-momentum.json"
Copy-Item "$s\scans\latest.json" "$d\latest-scan.json"; Copy-Item "$s\scan-index.json" $d; Copy-Item "$s\scan-history.json" $d; Copy-Item "$s\coverage\pm-coverage.json" $d; Copy-Item "$s\health\*.md" "$d\health\" -ErrorAction SilentlyContinue
Get-ChildItem $d -Recurse | Select-Object FullName,Length
All six books/journals plus latest-scan, scan-index and pm-coverage must be listed; a missing file means STOP and report - do not improvise.

STEP 2 - read_file C:\ai-trading-staging\pm-coverage.json; from the NEWEST run on the newest date take engine_sha (write_file it, 7+ hex chars only, to C:\ai-trading-staging\engine_sha; none = no file), <SLOT> = its slot, <RUNID> = "<date>-<slot>".

STEP 3 - mirror.py EXACTLY ONCE. start_process powershell.exe, timeout_ms 120000:
$prev='C:\ai-trading-mirror\manifest.json'; $a=@('--base','C:\ai-trading-staging','--out','C:\ai-trading-mirror','--slot','<SLOT>','--run-id','<RUNID>'); if (Test-Path $prev) { $a += @('--previous',$prev) }
C:\ai-trading-runner\.venv\Scripts\python.exe C:\ai-trading-runner\engine\engine\mirror.py @a; Write-Output "EXIT=$LASTEXITCODE"
Expect "mirror payload: N files, NN,NNN bytes" and EXIT=0; ignore its PUSH block. NON-ZERO: STOP - do not edit the scrubber, delete the offending file, or re-run without --previous; capture the reason verbatim.

STEP 4 - only after EXIT=0: python C:\ai-trading-tools\prune_orphans.py; python C:\ai-trading-tools\verify_published.py; Write-Output "VERIFY_EXIT=$LASTEXITCODE". VERIFY_EXIT 0 and "PROBLEMS: none" or the published payload is suspect - an account mask reaching the web root is the most serious outcome this task can produce; never downplay it.

REPORT: slot and run_id published, manifest generated_at, the three book revisions, file count, what prune removed, the verify result. Write claude/health/<today>-mirror-sync-failed.md ONLY on failure (step and verbatim error). Box or Desktop Commander unreachable is normal: the dashboard serves its last payload and stamps its age; one line, stop.
