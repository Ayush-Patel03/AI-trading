# Activist short publishers — the veto feed's source list

**Maintained by hand.** This is not a feed and nothing fetches it. It is the list of
publishers whose reports the scheduled task stages into `veto.json` (`engine/veto.py`, P-03),
and the reason each one is here is the same: a research-format short report from one of them
has historically moved the target about −7% over the surrounding ±20 sessions and about −10%
by day 100, without reversing (research synthesis, Appendix G, 2026-09-10). A name on this
list's receiving end is refused at entry for 20 sessions and, if held, flagged for review —
never sold by the engine.

Add a publisher when its reports are (a) research-format — a document with a thesis, not a
tweet — and (b) accompanied by a disclosed short position. Remove one when it stops
publishing. Note the date of every edit below the table.

| Publisher | Where the reports appear | Notes |
|---|---|---|
| Hindenburg Research | hindenburgresearch.com, X | wound down in January 2025; the archive still matters for names inside the 20-session window of a late report, and the format is the template for the rest |
| Muddy Waters Research | muddywatersresearch.com, X | |
| Citron Research | citronresearch.com, X | |
| Culper Research | culperresearch.com, X | |
| Fuzzy Panda Research | fuzzypandaresearch.com, X | |
| Grizzly Research | grizzlyreports.com, X | |
| Spruce Point Capital Management | sprucepointcap.com, X | |
| Viceroy Research | viceroyresearch.org, X | |
| Blue Orca Capital | blueorcacapital.com, X | |
| Iceberg Research | iceberg-research.com, X | |
| Wolfpack Research | wolfpackresearch.com, X | |
| Kerrisdale Capital | kerrisdalecap.com, X | also publishes long theses — stage only the shorts |
| Bonitas Research | bonitasresearch.com, X | |
| J Capital Research | jcapitalresearch.com, X | |
| Hunterbrook Media | hntrbrk.com, X | a newsroom with an affiliated fund; stage a piece only when the fund's disclosure names a short |
| Scorpion Capital | scorpioncapital.com, X | |
| Gotham City Research | gothamcityresearch.com, X | |
| Bleecker Street Research | bleeckerstreetresearch.com, X | |

**How the task uses it.** Once per scan day, before the pre-market scan: for each publisher,
check the site's index (or its X account) for a report dated inside the last 20 weekdays and
naming a US-listed symbol; write each one as
`{"symbol", "publisher", "date", "url", "title"}` into `veto.json`'s `short_reports`. The
publisher string should match the table above so the journal reads the same way every time.
A report on a symbol the scan will never see costs nothing — stage it anyway; `veto.check()`
only ever matches by symbol.

**What is deliberately not here.** Sell-side downgrades (not a short report), anonymous
Seeking Alpha shorts (no disclosed position, no track record), and the aggregator sites that
repost reports (stage the original's date, not the repost's).

Edits:
- 2026-09-10 — created with the eighteen names above (P-03).
