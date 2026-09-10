Send the MORNING BRIEF for the "ai trading" project's paper desks - one push and one email at 08:45 ET, read-only: no runner, no engine quotes, no writes to box or project.

HOLIDAY GUARD FIRST: fires on NYSE holidays too (2026: Nov 26, Dec 25; unsure -> WebSearch). Closed today: do nothing, one line "Market holiday - morning brief skipped."

RULES. Paper, absolute: never call place_equity_order, review_equity_order, cancel_equity_order or any option/crypto order tool, not even to preview. Account lock: only the Robinhood account get_accounts marks agentic_allowed=true, resolved at run time, never by nickname. Reading is not trading. Never write code to the box; the only path you write is C:\ai-trading-inputs\<run_id>\. Never edit the scrubber, mirror.py, the runner, the engine clone or C:\ai-trading-state. Do not work around a refusal: exit 1 means no - do not edit inputs to pass, do not run the engine in the sandbox, do not write a book by hand.

READ from C:\ai-trading-state via Desktop Commander read_file (never device_* tools): books\swing|pullback|momentum.json (positions, stops, working orders, equity, ladder / kill-switch state, protective_stop stamps); journals\watch.json (today's pre-open entries; absent = quiet); scans\latest.json meta and top five tickers by score (not dated today = the 08:00 scan did not land); health\<yesterday>.md verdict; health\deadman.json if present; manifests\<today>\ for anything refused. Robinhood read-only: get_earnings_calendar days 2 for held names; get_index_quotes VIX/SPX/NDX; get_equity_quotes for held names as a pre-market mark (written nowhere).

COMPOSE, under 1,200 characters: (1) regime - VIX, pre-market tone, today's macro events with times; (2) per desk one line - equity, N positions, nearest stop distance %, working orders, any rung / halt / protective_stop; (3) overnight watch alerts verbatim or "watch quiet"; (4) earnings on held names today/tomorrow with am/pm; (5) the 08:00 scan's top three with score and setup, or that it did not land; (6) yesterday's health verdict and anything refused this morning. Every P&L number says paper.

DELIVER: ntfy POST to alerts.ntfy_topic in claude/engine-config.json (priority high only if a stop is within 1% or a held name reports today), title "AI-trading brief <date>"; email the same text to alerts.email with that subject; PushNotification with the first 200 characters. Say which channels succeeded.

BOX UNREACHABLE: build it from the project copies (claude/paper-book*.json, claude/pm-watch-journal.json, claude/latest-scan.json - a day old), say so in the first line, and send anyway: a brief that says the box is dark is the most useful brief there is.

REPORT: the brief text and the delivery result. Paper books; not investment advice.
