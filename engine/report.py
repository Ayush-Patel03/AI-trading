"""report.py — the weekly review's numbers: refusals taxonomy, gate counterfactuals (E5),
closed-trade attribution and a benchmark-relative desk return. Read-only.

WHAT IT READS
-------------
  * PM journals — the `claude/pm-journal*.json` docs (`{"entries": [...]}`) or the runner's
    `pm_journal_current*.json`; one entry per run with `date`, `slot`, `desk`, `decisions`
    (`place-buy` / `fill-buy` / `fill-sell` / `cancel` / `expire`), `skipped`
    (`[{symbol, reason}]`), `warnings`, `equity`, `invested`.
  * paper books — `paper_book*.json` / `paper-book*.json`: `closed_trades` with an exit
    `reason` (stop / target / trim / thesis / rebalance / flatten), `pnl`, `pnl_pct`.
  * bars (optional) — `get_equity_historicals` output, the same file validate.py and ic.py
    read; needed for the counterfactual and the SPY benchmark.

It never writes to any of them. The only outputs are `--md` and `--json`.

THE REFUSALS TAXONOMY
---------------------
`classify(reason)` maps a journal `skipped` reason string to one rule. The rules the plan
named plus six the journal actually emits that are management-side or structural, not
gates; forcing those into a gate they are not would make the counterfactual lie:

| rule | what pm.py (and the modules it quotes) emits |
|---|---|
| `house_symbol_cap` | `House cap: SYM would be $… over the N% single-name house limit` |
| `house_sector_cap` | `House cap: SECTOR would be $… over the N% sector house limit` |
| `house_exposure` | `house exposure (enforced): … — no new entries house-wide …` (K-03) |
| `var_stress` | `VaR / stress (enforced): … — no new entries on this desk …` (K-04) |
| `ladder` | `ladder rung N: drawdown …`, `ladder cool-off: …`, `soft daily level: …`, `re-entry after the … drawdown halt`, `Drawdown ladder halt — …`, `ladder flatten wanted to fire — no fresh price`, `ladder ×0.50 sized it to $… under the $… broker minimum` |
| `kill_switch` | `Daily loss −3.10% breached the 3% kill switch`, `HALT: daily loss … kill-switch limit — no new entries` |
| `halt` | any other `halt` / `HALT:` the day carries as `halt_reason` |
| `macro_gate` | `macro gate: no new entries ahead of CPI at 08:30 ET` |
| `scan_stale` | `scan is dated …, not today — entries frozen`, `scan is 331 minutes old — over the 240-minute freshness limit — entries frozen`, `no scan results available this run` |
| `spread` | `bid/ask spread 1.19% of price, over the 1.0% entry limit — too expensive …` |
| `price_drift` | `price drifted +12.0% since the scan — rescored at the next scan …` |
| `sector_cap` | `Sector limit: already 3 positions in Information Technology (max 3)` (portfolio.py) |
| `coverage` | `Only 60% of the evidence base was available (…) — under the 70% floor`, `Price sources disagree by …% — not tradeable`, `no scan row carried enough data to size a position`, `no fresh price — …` (a holding the exit pass could not judge) |
| `earnings_gate` | reserved: any reason naming `earnings`; nothing emits one today |
| `broker_policy` | `legacy_pdt: PDT guard: 2/3 day trades used — no new entries …`, `intraday_margin: projected intraday margin deficit …`, `cash_settled: …`, `… wanted to fire — PDT guard: selling would be day trade 1/3 and this is not a stop — held`, `Over the position cap but not trimmable — …` |
| `min_notional` | `Order would be $4.10 — below the broker's $5.00 minimum`, `Sized to zero shares under these limits`, `trim sized to $3.20, under the $5.00 broker minimum` |
| `max_entries` | `run cap: 3 new entries per slot, queued for the next one`, `HALT: at max positions (8/8)`, `Stopped proposing at 8 total positions`, `Cumulative risk cap: book already carries …` |
| `stop_policy` | `chandelier: stop 101.2 is not below the 100.00 entry — cannot size a position` (K-07: the desk's stop policy struck no usable stop; `<policy>: $3.20 at a 9.1% stop distance is under the $5.00 broker minimum` is `min_notional`) |
| `slot` | `power-hour places no new entries — a day-limit entry placed at the last slot expires unfilled …` (FILL-01) |
| `desk_mandate` | `desk 'pullback': 13 scan row(s) outside this desk's mandate (…)` |
| `working_order` | `already has a working order`, `N working buy order(s) (…) counted against the … caps … (CAP-01)` |
| `once_per_session` | `Score 47 — weakening, but it was already trimmed today — one trim per name per session`, `… over the cap, but it was already rebalanced today — one rebalance per name per session` |
| `deadband` | `15.4% of equity, over the 15% cap but inside the 1.0pt rebalance deadband — a price wobble is not a breach` |
| `orb` | the ORB desk's own refusals (D-03, orb.py): `ORB screen: RVOL 1.20x outside the top 20`, `ORB rank 4: red opening candle — the paper shorts it; this account is long-only`, `ORB notional cap binds: …`, `ORB: max 5 concurrent names — no room this run`, `no intraday bars — no ORB today (bars_5m.json was not staged)`, `ORB desk: entries are the 09:35 sentinel's buy-stops above the opening range — a decision slot places nothing on this desk` |
| `other` | anything else; the raw text is kept in `detail` and listed under `refusals.other` so a new string is seen, never silently absorbed |

Precedence is the order of `_RULES` below: a string that names several things (the soft
daily level mentions the kill switch it is *not*) is classified by the first rule that
matches. `tests/test_report.py` harvests every reason literal from pm.py, portfolio.py,
broker_policy.py and ladder.py by AST and fails if any of them lands in `other`.

Each refusal also carries a `side`: `book` for a book-wide gate (`symbol == "*"` — the
journal names no candidates, so it counts but cannot be measured), `manage` for a holding
the manager wanted to sell or trim and could not, `entry` for a named candidate the entry
pass turned away. Only `entry` refusals enter the counterfactual.

E5 — THE COUNTERFACTUAL
-----------------------
For each rule, the forward return of the names it refused versus the names admitted
(`place-buy` decisions) on the same dates. Entry for both is the NEXT session's open (that
session's close when the bar has no open); the forward return over `h` sessions is then
exactly `validate.forward_returns`' convention from that entry session — the close `h`
bars later over the entry price. One observation per (date, symbol) per rule, however many
slots repeated the refusal. The difference is a per-date series (mean refused minus mean
admitted on each date that has both) and its 90% interval is `ic.block_bootstrap_ci`
with block = horizon, the same machinery the quantile spread uses, because forward windows
on nearby dates overlap. The question answered per rule: *did the names the gate refused
underperform the names it admitted?* An interval entirely below zero says yes; entirely
above zero says the gate cost return; straddling zero says nothing. Under 30 observations
on either side it is **not a sample** whatever the interval says.

ATTRIBUTION AND BENCHMARK
-------------------------
Closed-trade P&L by exit reason, by desk, by entry-score decile and by verdict and setup
(the score, setup and verdict come from the `place-buy` decision that opened the position
— closed trades do not carry them). Desk return over the window against SPY over the same
window from the bars, and exposure-adjusted: the average invested fraction times SPY's
return is what a passive position of the same average size would have earned.

The honesty budget: every number in the markdown carries its `n`; anything under 30 says
"not a sample"; there is no win rate and no Sharpe anywhere in this file.

Usage
-----
    python3 report.py --journals <dir or files> --books <dir> [--bars bars.json]
                      [--since DATE] [--md out.md] [--json out.json] [--counterfactual]
                      [--horizons 5,10,20]
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import sys

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

import validate
import ic

DEFAULT_HORIZONS = (5, 10, 20)
MIN_N = 30
NOT_A_SAMPLE = "not a sample"

RULES = ("sector_cap", "house_symbol_cap", "house_sector_cap", "spread", "price_drift",
         "scan_stale", "macro_gate", "broker_policy", "ladder", "house_exposure", "var_stress",
         "earnings_gate", "min_notional", "max_entries", "kill_switch", "halt", "coverage",
         "stop_policy", "slot", "desk_mandate", "working_order", "once_per_session", "deadband",
         "rotation", "orb", "other")

# Precedence order. Each entry: (rule, compiled pattern). The first match wins.
_RULES = [
    # D-01: the rotation desk's own lines — a decision slot that is not one, a note, a
    # sleeve under the minimum, a re-affirmed holding — all begin "rotation".
    ("rotation", r"^rotation[:\- ]"),
    ("house_symbol_cap", r"house cap:.*single-name house limit"),
    ("house_sector_cap", r"house cap:.*sector house limit"),
    ("house_exposure", r"house exposure"),
    ("var_stress", r"var / stress"),
    ("ladder", r"ladder|soft daily level|re-entry after|drawdown"),
    ("kill_switch", r"kill.?switch|daily loss"),
    ("macro_gate", r"macro gate"),
    # D-03: the ORB desk's own screen, sizing and slot refusals (orb.py / pm.orb_entry_pass)
    ("orb", r"\borb\b|opening.range|stock in play|no intraday bars"),
    ("scan_stale", r"scan is .*entries frozen|no scan results available"),
    ("spread", r"bid/ask spread"),
    ("price_drift", r"price drifted"),
    ("sector_cap", r"sector limit:"),
    ("coverage", r"evidence base|price sources disagree|no scan row carried enough data|"
                 r"no fresh price|no price at all|cannot be trusted"),
    ("earnings_gate", r"earnings"),
    ("broker_policy", r"wanted to fire|not trimmable|pdt guard|day.?trade|legacy_pdt|"
                      r"intraday_margin|cash_settled|margin|restricted|deficit|good-faith|"
                      r"settled shares|settlement"),
    ("stop_policy", r"cannot size a position"),
    ("min_notional", r"broker minimum|broker's \$|below the broker|sized to zero"),
    ("max_entries", r"run cap:|max positions|stopped proposing|cumulative risk cap"),
    ("halt", r"\bhalt"),
    ("slot", r"power-hour places no new entries"),
    ("desk_mandate", r"outside this desk's mandate"),
    ("working_order", r"working (buy )?order"),
    ("once_per_session", r"already (trimmed|rebalanced) today|one (trim|rebalance) per name"),
    ("deadband", r"deadband"),
]
_COMPILED = [(rule, re.compile(pat, re.IGNORECASE)) for rule, pat in _RULES]
_MANAGE = re.compile(r"wanted to fire|not trimmable|no fresh price", re.IGNORECASE)


def _rule_of(text):
    for rule, pat in _COMPILED:
        if pat.search(text):
            return rule
    return "other"


def classify(reason):
    """{"rule": one of RULES, "detail": the raw text}. Unknown text is `other`, verbatim.

    A proposal blocked by portfolio.py arrives as its warnings joined with "; " — the hard
    reasons first, in the order build_proposals checks them, then the informational trims.
    Such a string is classified by its FIRST segment (the first hard reason), and only
    falls back to the whole text when that segment is unknown."""
    text = "" if reason is None else str(reason)
    parts = [x for x in text.split("; ") if x.strip()]
    if len(parts) > 1:
        first = _rule_of(parts[0])
        if first != "other":
            return {"rule": first, "detail": text}
    return {"rule": _rule_of(text), "detail": text}


def side_of(symbol, reason, rule):
    if symbol in (None, "", "*"):
        return "book"
    if rule in ("once_per_session", "deadband") or _MANAGE.search(str(reason or "")):
        return "manage"
    return "entry"


# ---------------------------------------------------------------- loading (read-only)
def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _journal_entries(raw):
    if isinstance(raw, dict):
        raw = raw.get("entries")
    if not isinstance(raw, list):
        return []
    return [e for e in raw if isinstance(e, dict) and e.get("date") and
            (e.get("run_key") or "skipped" in e or "decisions" in e)]


def load_journals(paths, since=None):
    """Entries from every journal file (or every *.json in a directory that parses as a
    journal), de-duplicated by (desk, run_key), sorted by (date, ts). `since` is an ISO
    date; earlier entries are dropped."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.json"))))
        else:
            files.append(p)
    seen, out = {}, []
    for fn in files:
        try:
            entries = _journal_entries(_read_json(fn))
        except (ValueError, OSError):
            continue
        for e in entries:
            if since and e["date"] < since:
                continue
            e = dict(e)
            e["desk"] = e.get("desk") or "swing"
            key = (e["desk"], e.get("run_key") or (e["date"], e.get("slot"), e.get("ts")))
            if key in seen:
                continue
            seen[key] = True
            out.append(e)
    out.sort(key=lambda e: (e["date"], e.get("ts") or ""))
    return out


def desk_of_book_path(path):
    """paper-book-pullback.json -> pullback; paper_book.json / book_swing.json -> swing."""
    name = os.path.basename(path).lower()
    name = re.sub(r"\.json$", "", name)
    name = re.sub(r"^(paper[-_]?)?book[-_]?", "", name)
    return name or "swing"


def load_books(path):
    """{desk: book} from a directory of paper books (or one file). A file is a book when it
    carries `positions` and `closed_trades`."""
    files = sorted(glob.glob(os.path.join(path, "*.json"))) if os.path.isdir(path) else [path]
    out = {}
    for fn in files:
        try:
            b = _read_json(fn)
        except (ValueError, OSError):
            continue
        if not (isinstance(b, dict) and "positions" in b and "closed_trades" in b):
            continue
        out[desk_of_book_path(fn)] = b
    return out


def load_bars_oc(path):
    """{SYMBOL: [(date, open, close), ...]} oldest first, the same file shapes
    validate.load_bars accepts. `open` is None when the bar has no open_price."""
    raw = _read_json(path)
    blocks = raw if isinstance(raw, list) and raw and isinstance(raw[0], dict) \
        and "symbol" not in raw[0] else [raw]
    series = {}
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            rows = (blk.get("data") or {}).get("results") or blk.get("results") or []
        for res in rows or []:
            sym = (res.get("symbol") or "").upper()
            if not sym:
                continue
            pts = {}
            for b in res.get("bars") or []:
                if b.get("interpolated"):
                    continue
                c = validate._f(b.get("close_price", b.get("close")))
                d = str(b.get("begins_at") or b.get("date") or "")[:10]
                if c and d:
                    pts[d] = (validate._f(b.get("open_price", b.get("open"))), c)
            merged = {d: (o, c) for d, o, c in series.get(sym, [])}
            merged.update(pts)
            series[sym] = sorted((d, o, c) for d, (o, c) in merged.items())
    return series


def closes_of(series_oc):
    return {s: [(d, c) for d, _, c in pts] for s, pts in series_oc.items()}


# ---------------------------------------------------------------- refusals
def _week(date_str):
    y, w, _ = dt.date.fromisoformat(date_str).isocalendar()
    return f"{y}-W{w:02d}"


def refusals(entries):
    """Per-rule counts by week and by desk, and the list of refusals.

    One record per `skipped` item per run (entries are already unique by run_key), so a
    book-wide gate that fires at every slot counts once per slot — that IS how often it
    refused. The counterfactual de-duplicates by (date, symbol) itself."""
    rows = []
    for e in entries:
        for s in e.get("skipped") or []:
            if not isinstance(s, dict):
                continue
            c = classify(s.get("reason"))
            sym = s.get("symbol") or "*"
            rows.append({"date": e["date"], "week": _week(e["date"]), "desk": e["desk"],
                         "slot": e.get("slot"), "symbol": sym, "rule": c["rule"],
                         "side": side_of(sym, s.get("reason"), c["rule"]),
                         "detail": c["detail"]})
    by_rule, by_week, by_desk, by_side = {}, {}, {}, {}
    for r in rows:
        by_rule[r["rule"]] = by_rule.get(r["rule"], 0) + 1
        by_week.setdefault(r["week"], {})
        by_week[r["week"]][r["rule"]] = by_week[r["week"]].get(r["rule"], 0) + 1
        by_desk.setdefault(r["desk"], {})
        by_desk[r["desk"]][r["rule"]] = by_desk[r["desk"]].get(r["rule"], 0) + 1
        by_side[r["side"]] = by_side.get(r["side"], 0) + 1
    return {
        "n": len(rows),
        "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
        "by_week": {w: dict(sorted(v.items(), key=lambda kv: -kv[1]))
                    for w, v in sorted(by_week.items())},
        "by_desk": {d: dict(sorted(v.items(), key=lambda kv: -kv[1]))
                    for d, v in sorted(by_desk.items())},
        "by_side": by_side,
        "list": [{"date": r["date"], "desk": r["desk"], "slot": r["slot"], "symbol": r["symbol"],
                  "rule": r["rule"], "side": r["side"]} for r in rows],
        "other": sorted({r["detail"] for r in rows if r["rule"] == "other"}),
        "_rows": rows,
    }


def admitted(entries):
    """{(date, symbol)} of every place-buy decision, plus the decision's parsed reason."""
    out = {}
    for e in entries:
        for d in e.get("decisions") or []:
            if isinstance(d, dict) and d.get("action") == "place-buy" and d.get("symbol"):
                out.setdefault((e["date"], d["symbol"]), {"desk": e["desk"],
                                                          **parse_entry_reason(d.get("reason"))})
    return out


_ENTRY_RE = re.compile(r"^Score\s+(\d+(?:\.\d+)?)\s+(.+?)\s+—\s+(.+)$")


def parse_entry_reason(reason):
    """'Score 78 Pullback in Uptrend — Strong Buy' -> {score, setup, verdict}; None fields
    when the reason is not in that shape."""
    m = _ENTRY_RE.match(str(reason or "").strip())
    if not m:
        return {"score": None, "setup": None, "verdict": None}
    return {"score": float(m.group(1)), "setup": m.group(2).strip(), "verdict": m.group(3).strip()}


# ---------------------------------------------------------------- E5 counterfactual
def next_open_entry(series_oc, symbol, date):
    """(entry_date, entry_price) at the first session AFTER `date`: its open, else its
    close. None when the symbol has no later bar."""
    pts = series_oc.get(symbol.upper()) or []
    for d, o, c in pts:
        if d > date:
            return d, (o if o else c)
    return None


def forward_from_next_open(series_oc, keys, horizons):
    """{(date, symbol): {h: fwd_pct}} for every key that has an entry session and bars —
    validate.forward_returns from the entry session, on the closes."""
    closes = closes_of(series_oc)
    obs, keyed = [], []
    for date, sym in sorted(keys):
        e = next_open_entry(series_oc, sym, date)
        if e is None:
            continue
        obs.append({"date": e[0], "ticker": sym.upper(), "row": {"price": e[1]}})
        keyed.append((date, sym))
    validate.forward_returns(obs, closes, list(horizons))
    return {k: o["fwd"] for k, o in zip(keyed, obs) if o["fwd"]}


def _stats(xs):
    if not xs:
        return {"n": 0, "mean_pct": None, "median_pct": None}
    return {"n": len(xs), "mean_pct": round(validate._mean(xs), 3),
            "median_pct": round(validate._median(xs), 3)}


def _verdict(diff_ci, n_ref, n_adm):
    if diff_ci is None:
        v = "no interval — fewer than two dates with both refused and admitted names"
        if min(n_ref, n_adm) < MIN_N:
            v += f"; {NOT_A_SAMPLE} (n under {MIN_N})"
        return v
    lo, hi = diff_ci
    if hi < 0:
        v = "refused names underperformed the admitted names (90% interval below zero)"
    elif lo > 0:
        v = ("refused names OUTPERFORMED the admitted names — the gate cost return "
             "(90% interval above zero)")
    else:
        v = "no evidence either way (90% interval straddles zero)"
    if min(n_ref, n_adm) < MIN_N:
        v += f"; {NOT_A_SAMPLE} (n under {MIN_N})"
    return v


def counterfactual(entries, series_oc, horizons=DEFAULT_HORIZONS, refs=None):
    """Per rule and horizon: forward return of the refused names versus the admitted names
    on the same dates, with a block-bootstrap 90% interval on the per-date difference."""
    horizons = [int(h) for h in horizons]
    refs = refs if refs is not None else refusals(entries)
    adm = admitted(entries)
    refused_by_rule, unmeasured = {}, {}
    for r in refs["_rows"]:
        if r["side"] != "entry":
            unmeasured.setdefault(r["rule"], {"book_wide": 0, "manage": 0})
            unmeasured[r["rule"]]["book_wide" if r["side"] == "book" else "manage"] += 1
            continue
        refused_by_rule.setdefault(r["rule"], set()).add((r["date"], r["symbol"]))
    keys = set(adm) | {k for ks in refused_by_rule.values() for k in ks}
    fwd = forward_from_next_open(series_oc, keys, horizons)

    out = {"_what": ("E5: did the names each gate refused underperform the names it admitted "
                     "on the same dates? Entry = next session's open (else close); forward "
                     "return = validate.forward_returns from that session; the difference is "
                     "per-date (refused mean minus admitted mean) with ic.block_bootstrap_ci, "
                     "block = horizon. Describes the paper record; forecasts nothing."),
           "horizons": horizons, "entry": "next session open, else close",
           "n_admitted": len(adm),
           "n_admitted_with_bars": sum(1 for k in adm if k in fwd),
           "rules": {}, "unmeasured": unmeasured,
           "no_bars": sorted({s for (d, s) in keys if (d, s) not in fwd})}
    for rule, keys_r in sorted(refused_by_rule.items()):
        R = {rule: {"n_refused": len(keys_r),
                    "n_refused_with_bars": sum(1 for k in keys_r if k in fwd)}}
        for h in horizons:
            ref_by_date, adm_by_date = {}, {}
            for (d, s) in keys_r:
                v = fwd.get((d, s), {}).get(h)
                if v is not None:
                    ref_by_date.setdefault(d, []).append(v)
            for (d, s) in adm:
                if d in ref_by_date:
                    v = fwd.get((d, s), {}).get(h)
                    if v is not None:
                        adm_by_date.setdefault(d, []).append(v)
            ref_all = [v for vs in ref_by_date.values() for v in vs]
            adm_all = [v for vs in adm_by_date.values() for v in vs]
            both = sorted(d for d in ref_by_date if d in adm_by_date)
            diffs = [validate._mean(ref_by_date[d]) - validate._mean(adm_by_date[d]) for d in both]
            ci = ic.block_bootstrap_ci(diffs, block=max(1, h)) if len(diffs) >= 2 else None
            R[rule][str(h)] = {
                "refused": _stats(ref_all),
                "admitted": _stats(adm_all),
                "diff": {"n_dates": len(diffs),
                         "mean_pct": round(validate._mean(diffs), 3) if diffs else None,
                         "ci90_pct": [round(ci[0], 3), round(ci[1], 3)] if ci else None,
                         "block": max(1, h)},
                "sample": "ok" if min(len(ref_all), len(adm_all)) >= MIN_N else NOT_A_SAMPLE,
                "verdict": _verdict(ci, len(ref_all), len(adm_all)),
            }
        out["rules"].update(R)
    return out


# ---------------------------------------------------------------- attribution
def _bucket(score):
    if score is None:
        return "unknown"
    lo = int(score // 10 * 10)
    return f"{lo:02d}-{lo + 9:02d}"


def _pnl_stats(trades):
    pnl = [t["pnl"] for t in trades if t.get("pnl") is not None]
    pct = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    return {"n": len(trades),
            "pnl_total": round(sum(pnl), 2) if pnl else None,
            "pnl_mean": round(validate._mean(pnl), 2) if pnl else None,
            "pnl_pct_mean": round(validate._mean(pct), 3) if pct else None,
            "pnl_pct_median": round(validate._median(pct), 3) if pct else None,
            "sample": "ok" if len(trades) >= MIN_N else NOT_A_SAMPLE}


def _table(trades, key):
    groups = {}
    for t in trades:
        groups.setdefault(t.get(key) or "unknown", []).append(t)
    return {k: _pnl_stats(v) for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))}


def attribution(entries, books, since=None):
    """Closed-trade P&L by exit reason, desk, entry-score decile, verdict and setup. The
    entry score / setup / verdict are joined from the latest place-buy decision for that
    desk and symbol on or before the trade's `opened` date."""
    placed = {}
    for e in entries:
        for d in e.get("decisions") or []:
            if isinstance(d, dict) and d.get("action") == "place-buy" and d.get("symbol"):
                placed.setdefault((e["desk"], d["symbol"]), []).append(
                    (e["date"], parse_entry_reason(d.get("reason"))))
    trades, unmatched = [], 0
    for desk, book in sorted(books.items()):
        for t in book.get("closed_trades") or []:
            if not isinstance(t, dict):
                continue
            if since and (t.get("closed") or "") < since:
                continue
            opened = t.get("opened") or ""
            cands = [p for p in placed.get((desk, t.get("symbol")), []) if p[0] <= opened]
            meta = max(cands, key=lambda p: p[0])[1] if cands else None
            if meta is None:
                unmatched += 1
                meta = {"score": None, "setup": None, "verdict": None}
            trades.append({"desk": desk, "symbol": t.get("symbol"), "opened": opened,
                           "closed": t.get("closed"), "reason": t.get("reason") or "unknown",
                           "pnl": validate._f(t.get("pnl")), "pnl_pct": validate._f(t.get("pnl_pct")),
                           "score": meta["score"], "score_bucket": _bucket(meta["score"]),
                           "verdict": meta["verdict"], "setup": meta["setup"]})
    return {"n_closed": len(trades), "n_unmatched_to_a_decision": unmatched,
            "all": _pnl_stats(trades),
            "by_exit_reason": _table(trades, "reason"),
            "by_desk": _table(trades, "desk"),
            "by_score_bucket": dict(sorted(_table(trades, "score_bucket").items())),
            "by_verdict": _table(trades, "verdict"),
            "by_setup": _table(trades, "setup")}


# ---------------------------------------------------------------- benchmark
def _spy_return(series_oc, start, end):
    pts = [(d, c) for d, _, c in (series_oc or {}).get("SPY") or [] if start <= d <= end]
    if len(pts) < 2:
        return None, None, None
    return (pts[-1][1] / pts[0][1] - 1.0) * 100.0, pts[0][0], pts[-1][0]


def benchmark(entries, series_oc=None):
    """Per desk: equity return over the journal window versus SPY over the same window,
    and exposure-adjusted (average invested fraction × SPY). From the journal entries'
    own equity / invested fields; the bars supply SPY."""
    out = {}
    by_desk = {}
    for e in entries:
        if e.get("sentinel"):
            continue
        if validate._f(e.get("equity")) is None:
            continue
        by_desk.setdefault(e["desk"], []).append(e)
    for desk, es in sorted(by_desk.items()):
        es.sort(key=lambda e: (e["date"], e.get("ts") or ""))
        eq0, eq1 = float(es[0]["equity"]), float(es[-1]["equity"])
        fracs = [float(e["invested"]) / float(e["equity"]) for e in es
                 if validate._f(e.get("invested")) is not None and float(e["equity"]) > 0]
        avg_inv = validate._mean(fracs)
        spy, s0, s1 = _spy_return(series_oc, es[0]["date"], es[-1]["date"])
        ret = (eq1 / eq0 - 1.0) * 100.0 if eq0 else None
        row = {"start": es[0]["date"], "end": es[-1]["date"], "n_entries": len(es),
               "equity_start": round(eq0, 2), "equity_end": round(eq1, 2),
               "return_pct": round(ret, 3) if ret is not None else None,
               "avg_invested_frac": round(avg_inv, 4) if avg_inv is not None else None,
               "spy_return_pct": round(spy, 3) if spy is not None else None,
               "spy_window": [s0, s1] if spy is not None else None,
               "exposure_adjusted_spy_pct": (round(avg_inv * spy, 3)
                                             if spy is not None and avg_inv is not None else None),
               "sample": "ok" if len(es) >= MIN_N else NOT_A_SAMPLE}
        row["excess_vs_exposure_adjusted_spy_pct"] = (
            round(ret - avg_inv * spy, 3)
            if ret is not None and spy is not None and avg_inv is not None else None)
        out[desk] = row
    return out


# ---------------------------------------------------------------- the report
def build(entries, books=None, series_oc=None, horizons=DEFAULT_HORIZONS, with_cf=False,
          since=None):
    refs = refusals(entries)
    res = {
        "_what": ("Weekly review numbers from the PM journals and paper books: refusals by "
                  "rule, gate counterfactuals (E5), closed-trade attribution, desk return "
                  "against SPY. Read-only; describes the paper record; forecasts nothing."),
        "window": {"since": since, "first": entries[0]["date"] if entries else None,
                   "last": entries[-1]["date"] if entries else None,
                   "n_entries": len(entries),
                   "desks": sorted({e["desk"] for e in entries})},
        "refusals": {k: v for k, v in refs.items() if k != "_rows"},
        "attribution": attribution(entries, books or {}, since=since),
        "benchmark": benchmark(entries, series_oc),
        "counterfactual": None,
    }
    if with_cf:
        if series_oc is None:
            res["counterfactual"] = {"refused": "no bars — pass --bars to measure the gates"}
        else:
            res["counterfactual"] = counterfactual(entries, series_oc, horizons, refs=refs)
    return res


def _n(v, n, dp=2, signed=True, unit="%"):
    """A number with its n beside it — never one without the other."""
    if v is None:
        return f"— (n={n})"
    if unit == "$":
        return f"{'+' if v >= 0 else '-'}${abs(v):,.{dp}f} (n={n})"
    s = f"{v:+.{dp}f}" if signed else f"{v:.{dp}f}"
    return f"{s}{unit} (n={n})"


def _tag(n):
    return "" if n >= MIN_N else f" · {NOT_A_SAMPLE}"


def markdown(res):
    w = res["window"]
    L = ["# PM weekly review — refusals, gate counterfactuals, attribution, benchmark", ""]
    L.append(f"Window {w['first']} → {w['last']}, {w['n_entries']} journal entries across "
             f"{len(w['desks'])} desk(s): {', '.join(w['desks']) or '—'}. Paper record. "
             f"Every number carries its n; under {MIN_N} it is {NOT_A_SAMPLE}. No win rate, "
             "no Sharpe — neither is evidence at this size.")
    L.append("")

    R = res["refusals"]
    L.append(f"## Refusals — {R['n']} skipped items by rule{_tag(R['n'])}")
    L.append("")
    L.append("| rule | n |")
    L.append("|---|---:|")
    for rule, n in R["by_rule"].items():
        L.append(f"| {rule} | {n} |")
    L.append("")
    sides = R.get("by_side") or {}
    L.append(f"Sides: book-wide gates n={sides.get('book', 0)} (no names — counted, not "
             f"measurable), management-side n={sides.get('manage', 0)}, entry candidates "
             f"n={sides.get('entry', 0)} (the only ones the counterfactual can measure).")
    L.append("")
    if R["by_week"]:
        rules = list(R["by_rule"])
        L.append("| week | " + " | ".join(rules) + " |")
        L.append("|---|" + "---:|" * len(rules))
        for wk, v in R["by_week"].items():
            L.append(f"| {wk} | " + " | ".join(str(v.get(r, 0)) for r in rules) + " |")
        L.append("")
        L.append("| desk | " + " | ".join(rules) + " |")
        L.append("|---|" + "---:|" * len(rules))
        for dk, v in R["by_desk"].items():
            L.append(f"| {dk} | " + " | ".join(str(v.get(r, 0)) for r in rules) + " |")
        L.append("")
    if R["other"]:
        L.append("Unclassified reasons (rule `other` — add a rule in report.py):")
        for s in R["other"]:
            L.append(f"- {s}")
        L.append("")

    C = res.get("counterfactual")
    L.append("## E5 — did the names each gate refused underperform the names it admitted?")
    L.append("")
    if not C:
        L.append("Not run. `--counterfactual --bars bars.json` measures it.")
    elif "refused" in C:
        L.append(C["refused"])
    else:
        L.append(f"Entry = {C['entry']}; forward return = the close h sessions after the entry "
                 f"session over that entry; difference = per-date refused mean minus admitted "
                 f"mean, block-bootstrap 90% interval (block = horizon). Admitted names in the "
                 f"window: {C['n_admitted']} ({C['n_admitted_with_bars']} with bars).")
        L.append("")
        if not C["rules"]:
            L.append("No named entry refusals in the window — nothing to measure.")
        for rule, tbl in C["rules"].items():
            L.append(f"### {rule} — {tbl['n_refused']} refused name-dates, "
                     f"{tbl['n_refused_with_bars']} with bars{_tag(tbl['n_refused_with_bars'])}")
            L.append("")
            L.append("| h | refused mean | refused median | admitted mean | admitted median | "
                     "diff mean [90% CI] (dates) | reading |")
            L.append("|---:|---|---|---|---|---|---|")
            for h in C["horizons"]:
                t = tbl[str(h)]
                r, a, d = t["refused"], t["admitted"], t["diff"]
                ci = d["ci90_pct"]
                ci_s = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "[no interval]"
                dm = f"{d['mean_pct']:+.2f}%" if d["mean_pct"] is not None else "—"
                L.append(f"| {h} | {_n(r['mean_pct'], r['n'])} | {_n(r['median_pct'], r['n'])} | "
                         f"{_n(a['mean_pct'], a['n'])} | {_n(a['median_pct'], a['n'])} | "
                         f"{dm} {ci_s} (n={d['n_dates']} dates) | {t['verdict']} |".replace(
                             "% [no interval] (n=", "% (n="))
            L.append("")
        if C.get("unmeasured"):
            L.append("Not measurable from the journal (no candidate names): " + "; ".join(
                f"{r} book-wide n={v['book_wide']}, management n={v['manage']}"
                for r, v in sorted(C["unmeasured"].items())))
            L.append("")
        if C.get("no_bars"):
            L.append("No bars for: " + ", ".join(C["no_bars"]))
            L.append("")
    L.append("")

    A = res["attribution"]
    L.append(f"## Attribution — {A['n_closed']} closed trades{_tag(A['n_closed'])}"
             + (f", {A['n_unmatched_to_a_decision']} not matched to a place-buy decision"
                if A["n_unmatched_to_a_decision"] else ""))
    L.append("")
    for title, key in (("By exit reason", "by_exit_reason"), ("By desk", "by_desk"),
                       ("By entry-score decile", "by_score_bucket"), ("By verdict", "by_verdict"),
                       ("By setup", "by_setup")):
        tbl = A.get(key) or {}
        if not tbl:
            continue
        L.append(f"### {title}")
        L.append("")
        L.append("| bucket | n | P&L total | P&L mean | return mean | return median | |")
        L.append("|---|---:|---|---|---|---|---|")
        for k, v in tbl.items():
            L.append(f"| {k} | {v['n']} | {_n(v['pnl_total'], v['n'], unit='$')} | "
                     f"{_n(v['pnl_mean'], v['n'], unit='$')} | {_n(v['pnl_pct_mean'], v['n'])} | "
                     f"{_n(v['pnl_pct_median'], v['n'])} | {'' if v['sample'] == 'ok' else NOT_A_SAMPLE} |")
        L.append("")

    B = res["benchmark"]
    L.append("## Benchmark — desk return vs SPY over the same window")
    L.append("")
    if not B:
        L.append("No journal entries with equity in the window.")
    else:
        L.append("| desk | window | return | avg invested | SPY | exposure × SPY | excess | |")
        L.append("|---|---|---|---|---|---|---|---|")
        for desk, b in B.items():
            n = b["n_entries"]
            inv = f"{b['avg_invested_frac']:.0%} (n={n})" if b["avg_invested_frac"] is not None else f"— (n={n})"
            L.append(f"| {desk} | {b['start']} → {b['end']} | {_n(b['return_pct'], n)} | {inv} | "
                     f"{_n(b['spy_return_pct'], n)} | {_n(b['exposure_adjusted_spy_pct'], n)} | "
                     f"{_n(b['excess_vs_exposure_adjusted_spy_pct'], n)} | "
                     f"{'' if b['sample'] == 'ok' else NOT_A_SAMPLE} |")
        L.append("")
        L.append("n here is the number of decision-slot journal entries in the window; the "
                 "return is one number per desk however many entries there are, and one "
                 "number is not a distribution. SPY absent from the bars leaves those columns "
                 "empty rather than assumed.")
    L.append("")
    L.append("**Reading it.** A gate whose refused names underperformed the admitted ones, "
             "with an interval clear of zero and n past 30 on both sides, is doing its job; "
             "one whose refused names outperformed is costing return and should be argued "
             "about, not loosened by reflex. Everything else here describes the paper "
             "record and forecasts nothing.")
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journals", nargs="+", required=True,
                    help="journal files or directories of them (claude/pm-journal*.json)")
    ap.add_argument("--books", default=None, help="directory of paper books (or one book)")
    ap.add_argument("--bars", default=None, help="get_equity_historicals output(s), with SPY")
    ap.add_argument("--since", default=None, help="ISO date; earlier entries are ignored")
    ap.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS))
    ap.add_argument("--counterfactual", action="store_true", help="run E5 (needs --bars)")
    ap.add_argument("--md", default=None)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    horizons = [int(x) for x in a.horizons.split(",") if x.strip()]

    entries = load_journals(a.journals, since=a.since)
    if not entries:
        print("REFUSED: no journal entries found in " + ", ".join(a.journals), file=sys.stderr)
        return 2
    books = load_books(a.books) if a.books else {}
    series = load_bars_oc(a.bars) if a.bars else None
    if a.counterfactual and series is None:
        print("REFUSED: --counterfactual needs --bars", file=sys.stderr)
        return 2
    res = build(entries, books, series, horizons, with_cf=a.counterfactual, since=a.since)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    if a.md:
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(markdown(res))
    R = res["refusals"]
    print(f"{res['window']['first']} → {res['window']['last']}: {res['window']['n_entries']} "
          f"entries, {R['n']} refusals, {res['attribution']['n_closed']} closed trades")
    for rule, n in R["by_rule"].items():
        print(f"  {rule:<18} {n}")
    if R["other"]:
        print(f"  {len(R['other'])} unclassified reason string(s) — see refusals.other")
    C = res.get("counterfactual")
    if C and "rules" in C:
        for rule, tbl in C["rules"].items():
            h = str(horizons[-1])
            print(f"  E5 {rule:<16} {h}d: {tbl[h]['verdict']}")
    if a.json or a.md:
        print("-> " + ", ".join(p for p in (a.json, a.md) if p))
    return 0


if __name__ == "__main__":
    sys.exit(main())
