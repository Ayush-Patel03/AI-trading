"""Retail attention / crowd sentiment normaliser.

Turns the raw vendor payloads a scan collects into ONE normalised sentiment.json
that scanner.py merges into each candidate's `retail` block.

WHY THIS FILE EXISTS (source probe, 2026-09-01). The retail feeds were being read
straight into the Intelligence pillar, and every one of them carries a trap that
silently corrupts a score:

  * ApeWisdom recomputes ranks between HTTP requests, so page 2 re-emits tickers
    already on page 1 WITH DIFFERENT NUMBERS (AXON was rank 99 with 9 upvotes and
    rank 101 with 10 upvotes in the same probe). Concatenating pages double-counts.
  * New entrants carry rank_24h_ago = 0 and mentions_24h_ago = null. A naive delta
    divides by zero or ranks a brand-new ticker as the day's biggest riser.
  * Upvote counts have corrupt outliers: HIMS 3 mentions / 9001 upvotes, BAX -1.
  * There is no stop-list, so ordinary English words that happen to be tickers rank
    high on prose alone. On 2026-09-01 "IT" ranked 20th and "ALL" 45th.
  * NEITHER ApeWisdom NOR StockTwits publishes a timestamp, so a stalled feed is
    undetectable from the payload. Freshness can only be inferred from deltas.
  * The feeds do not corroborate each other. On 2026-09-01 ApeWisdom and StockTwits
    trending shared 4 of 15 top names, and StockTwits' two trending endpoints shared
    exactly ONE ticker with each other.

The last point is the important one: 20 of 100 points were resting on single-vendor
numbers that no second source confirms. So attention here is scored on CORROBORATION
— how many independent feeds name a ticker — and each vendor's raw counts are carried
as evidence rather than as the signal.

THE STOCKTWITS SYMBOL STREAM. api.stocktwits.com/api/2/streams/symbol/<T>.json is
cached with a variable TTL and was measured 50 hours stale for NVDA, 31h for TSLA and
10h for a small cap while the trending endpoint was live to the minute. It is still
REFUSED as a trending / attention input. What it IS good for is the crowd's stated
direction: every message carries entities.sentiment.basic (Bullish / Bearish / null)
and a created_at, so a staged `st_symbol_<SYM>.json` yields a bull share and the age
of its newest message — reported with that age, never as attention. The
stocktwits.com/symbol/<T> HTML gauge is a different thing and is fine.

ATTENTION FADE (P-04, 2026-09-10). The retail feeds were read as levels: "412 mentions,
rank 1". A level says nothing about whether attention is arriving or leaving, and the
research base (Appendix G) expects attention SPIKES to fade over the next two to four
weeks — a negative sign at that horizon. So this module keeps a rolling
`attention_history.json` in the archive directory — the last 20 scans' mention and
upvote count per symbol — and emits, per candidate:
  attention_z         (today's mentions − trailing-20-scan mean) / trailing std
  attention_spike     attention_z > 2
  attention_rank_pct  percentile of today's mentions across every name on the feed
  st_bull_pct         Bullish / (Bullish + Bearish) from a staged symbol stream
  trends_z            the same z on a Google Trends series when trends.json is staged
They are written into each candidate's `retail` block AND its `features` dict, so they
ride into the scan snapshot and the backtest records where the sign test runs. NOT
SCORED: scanner.py's pillar arithmetic reads the same keys it always did.

Reddit is gone. The Arctic Shift route was a monthly dump, not a feed, and the Reddit
API needs approval this account does not have (Appendix B); docs/scan-sources.md keeps
the note. Nothing in the pipeline named a Reddit field the scanner scored.

Paths resolve from SCAN_DIR, else this file's directory. No path is hardcoded.
"""
import argparse, glob, json, math, os, re, sys
from datetime import datetime, timezone

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------- stop-list
# Tickers that are also ordinary English words, conjunctions or units. A mention
# counter with no stop-list scores prose. Everything here was observed in a live
# ApeWisdom top-150 on 2026-09-01 except where noted as pre-emptive. A name on this
# list is kept only when two independent feeds name it (build()).
STOP_TICKERS = {
    "A","ALL","AM","AN","AND","ANY","ARE","AS","AT","BE","BIG","BY","CAN","CC","DAY",
    "DC","DD","DE","DK","DO","EOD","ES","EU","EV","FOR","GO","GP","HAS","HE","IG","IN",
    "IP","IQ","IS","IT","ITS","JUST","KEY","LOVE","MY","NEW","NOW","ON","ONE","OR",
    "OUT","PM","PR","PT","REAL","SD","SF","SO","TA","TD","TWO","UP","US","USA","VS",
    "WE","WELL","YOU","AI","CEO","CFO","ETF","FDA","GDP","IMO","IPO","IRA","LOL","NFA",
    "OTM","ITM","PE","RH","ROI","SEC","TLDR","YOLO","DTE","EPS","ATH","FOMO","HODL",
}
# With the Reddit text path gone (P-04) nothing reads STOP_TICKERS; it stays so a future
# text feed does not have to rediscover the list. build() gates on AMBIGUOUS_BUT_REAL only,
# exactly as it did before, so the set of names the scanner scores is unchanged.
# Names that are legitimately traded AND common words. Keep them, but only when the
# mention arrives cashtagged ($DTE) or the ticker is corroborated by another feed.
AMBIGUOUS_BUT_REAL = {"DTE","ALL","KEY","IT","ON","PR","ES","IP","DAY","LOVE","GO","UP"}

MAX_UPVOTES_PER_MENTION = 500   # HIMS reported 9001 upvotes on 3 mentions

# Attention fade (P-04).
ATTENTION_HISTORY = os.path.join("archive", "attention_history.json")   # under BASE
ATTENTION_WINDOW = 20           # trailing scans in the mean / std
ATTENTION_MIN_HISTORY = 5       # fewer prior scans than this and the z is null
ATTENTION_SPIKE_Z = 2.0         # attention_spike = attention_z > this
ST_STREAM_STALE_HOURS = 24      # a symbol stream whose newest message is older is flagged
ST_STREAM_FILE = "st_symbol_*.json"


def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _rows(payload, *keys):
    """Pull a list of rows out of a vendor payload of unknown exact shape."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in keys:
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        for v in payload.values():           # last resort: first list of dicts
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def _sym(row, *keys):
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


# --------------------------------------------------------------- ApeWisdom
def parse_apewisdom(payload, warnings):
    """Dedupe by ticker keeping the LOWEST rank, clamp upvotes, normalise sentinels.

    Returns {TICKER: {mentions, mentions_24h_ago, upvotes, rank, rank_24h_ago,
                      mention_change_pct, is_new_entrant}}
    """
    rows = _rows(payload, "results", "data", "tickers")
    if not rows:
        return {}
    out, dupes, clamped = {}, [], []
    for r in rows:
        tk = _sym(r, "ticker", "symbol")
        if not tk:
            continue
        rank = r.get("rank")
        rank = int(rank) if isnum(rank) else None
        prev = out.get(tk)
        if prev is not None:
            dupes.append(tk)
            # Ranks recompute between requests; the lowest rank is the earliest,
            # most authoritative observation. Keep it, discard the later row.
            if prev.get("rank") is not None and (rank is None or rank >= prev["rank"]):
                continue

        mentions = r.get("mentions")
        mentions = int(mentions) if isnum(mentions) else None
        m0 = r.get("mentions_24h_ago")
        m0 = int(m0) if isnum(m0) else None          # null -> None, stays None

        upv = r.get("upvotes")
        upv = int(upv) if isnum(upv) else None
        if upv is not None:
            if upv < 0:                               # BAX reported -1
                clamped.append(f"{tk} upvotes {upv}->0")
                upv = 0
            elif mentions and upv > mentions * MAX_UPVOTES_PER_MENTION:
                clamped.append(f"{tk} upvotes {upv}->{mentions * MAX_UPVOTES_PER_MENTION}")
                upv = mentions * MAX_UPVOTES_PER_MENTION

        r0 = r.get("rank_24h_ago")
        r0 = int(r0) if isnum(r0) else None
        if r0 == 0:                                   # sentinel for "was unranked"
            r0 = None

        # A new entrant has no baseline. It is NOT a +infinity riser and it is NOT
        # a faller; it is a name with no history, and that is what gets reported.
        new_entrant = m0 in (None, 0)
        chg = None if new_entrant else round((mentions - m0) / m0 * 100.0, 1) \
            if isnum(mentions) and isnum(m0) and m0 > 0 else None

        out[tk] = {"mentions": mentions, "mentions_24h_ago": m0, "upvotes": upv,
                   "rank": rank, "rank_24h_ago": r0,
                   "mention_change_pct": chg, "is_new_entrant": bool(new_entrant)}

    if dupes:
        warnings.append(
            f"ApeWisdom returned {len(set(dupes))} duplicate ticker(s) across pages "
            f"({', '.join(sorted(set(dupes))[:8])}{'...' if len(set(dupes)) > 8 else ''}) — "
            "deduped keeping the lowest rank. Fetch page 1 only to avoid this.")
    if clamped:
        warnings.append("Clamped corrupt upvote values: " + "; ".join(clamped[:6]))
    return out


# --------------------------------------------------------------- StockTwits
def parse_stocktwits_trending(payload, warnings):
    """trending/symbols.json or streams/trending.json. Refuses symbol streams.

    NOTE: `trending_score` does NOT sort the ranks — on 2026-09-01 rank 2 (QQQ)
    scored 7.479 while rank 3 (TSLA) scored 9.136. The list position is the vendor's
    own ordering and the score is a separate, undocumented number. We keep both and
    rank on neither: presence in the list is the only claim we make.
    """
    if isinstance(payload, dict) and "symbol" in payload and "messages" in payload:
        warnings.append(
            "REFUSED a StockTwits per-symbol stream payload. Those are cached with a "
            "variable TTL (measured 50h stale for NVDA on 2026-09-01) and must not "
            "feed sentiment. Use trending/symbols.json or the symbol HTML gauge.")
        return {}
    rows = _rows(payload, "symbols", "results", "data")
    out = {}
    for i, r in enumerate(rows, 1):
        tk = _sym(r, "symbol", "ticker")
        if not tk or "." in tk:      # JASMY.X etc — crypto, not an equity candidate
            continue
        out[tk] = {"trending_position": i,
                   "trending_score": r.get("trending_score"),
                   "watchlist_count": r.get("watchlist_count")}
    return out


def parse_stocktwits_gauges(payload, warnings):
    """{TICKER: {score, label, trending}} hand-transcribed from the symbol HTML page.

    The HTML gauge is live; only the per-symbol JSON stream is stale. Kept separate
    from the trending list so a caller cannot conflate the two endpoints — on
    2026-09-01 StockTwits' two trending endpoints shared exactly one ticker.
    """
    if not isinstance(payload, dict):
        return {}
    out = {}
    for tk, v in payload.items():
        if not isinstance(v, dict):
            continue
        sc = v.get("score")
        out[str(tk).upper()] = {
            "stocktwits_score": sc if isnum(sc) else None,
            "stocktwits_label": v.get("label"),
            "stocktwits_trending": bool(v.get("trending")),
        }
    return out


# --------------------------------------------------------------- StockTwits symbol streams
def _parse_ts(v):
    """ISO 8601 (StockTwits writes 2026-09-10T14:03:11Z) -> aware UTC datetime, or None."""
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_stocktwits_symbol_stream(payload, warnings, symbol=None, now=None):
    """One staged `st_symbol_<SYM>.json` (the public symbol stream) -> the crowd's stated
    direction. {"st_bull_pct", "st_bull_n", "st_bear_n", "st_msgs", "st_newest_utc",
    "st_stream_age_min", "st_stream_stale"} or None when the payload carries no messages.

    The stream is REFUSED as an attention input (parse_stocktwits_trending) because its
    cache is stale by a variable amount. The bull share is a different claim — "of the
    people who tagged a direction, how many said Bullish" — and it is reported WITH the
    age of the newest message so a stale stream is visible, never a fresh-looking number.
    """
    if not isinstance(payload, dict):
        return None
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        return None
    sym = symbol
    if not sym:
        sp = payload.get("symbol")
        sym = _sym(sp, "symbol") if isinstance(sp, dict) else (sp.upper() if isinstance(sp, str) else None)
    bull = bear = 0
    newest = None
    for m in msgs:
        if not isinstance(m, dict):
            continue
        ent = m.get("entities") if isinstance(m.get("entities"), dict) else {}
        sent = ent.get("sentiment") if isinstance(ent.get("sentiment"), dict) else {}
        basic = str(sent.get("basic") or "").strip().lower()
        if basic == "bullish":
            bull += 1
        elif basic == "bearish":
            bear += 1
        ts = _parse_ts(m.get("created_at"))
        if ts is not None:
            newest = ts if newest is None else max(newest, ts)
    now = now or datetime.now(timezone.utc)
    age_min = round((now - newest).total_seconds() / 60.0, 1) if newest else None
    stale = bool(age_min is not None and age_min > ST_STREAM_STALE_HOURS * 60)
    if stale:
        warnings.append(f"StockTwits symbol stream for {sym or '?'} is {age_min / 60:.0f}h old "
                        f"(newest message {newest.isoformat(timespec='minutes')}) — its bull "
                        "share describes an older crowd, not today's.")
    tagged = bull + bear
    return {"st_bull_pct": round(bull / tagged * 100.0, 1) if tagged else None,
            "st_bull_n": bull, "st_bear_n": bear, "st_msgs": len(msgs),
            "st_newest_utc": newest.isoformat(timespec="seconds") if newest else None,
            "st_stream_age_min": age_min, "st_stream_stale": stale}


def parse_stocktwits_symbol_streams(payloads, warnings, now=None):
    """{SYM: payload} -> {SYM: parse_stocktwits_symbol_stream(...)}, empties dropped."""
    out = {}
    for sym, payload in (payloads or {}).items():
        row = parse_stocktwits_symbol_stream(payload, warnings, symbol=str(sym).upper(), now=now)
        if row:
            out[str(sym).upper()] = row
    return out


def load_symbol_streams(pattern=ST_STREAM_FILE, base=None):
    """Every `st_symbol_<SYM>.json` under `base` -> {SYM: payload}. The symbol is the
    file name's, not the payload's: a stream staged under the wrong name is the
    operator's error to see, not one to paper over."""
    base = base or BASE
    out = {}
    for path in sorted(glob.glob(os.path.join(base, pattern))):
        name = os.path.basename(path)
        stem = name[len("st_symbol_"):-len(".json")] if name.startswith("st_symbol_") \
            and name.endswith(".json") else os.path.splitext(name)[0]
        if not stem:
            continue
        payload = _load(path)
        if payload is not None:
            out[stem.upper()] = payload
    return out


# --------------------------------------------------------------- Google Trends
def parse_trends(payload, warnings, window=ATTENTION_WINDOW, min_history=ATTENTION_MIN_HISTORY):
    """{symbol: [{date, value}, ...]} -> {SYM: {trends_latest, trends_date, trends_mean,
    trends_z, trends_n}}. The z is today's value against the trailing `window` points
    before it — the same construction attention_z uses on mentions, on a series that
    carries its own dates. Null with fewer than `min_history` prior points."""
    if not isinstance(payload, dict):
        return {}
    out = {}
    for sym, series in payload.items():
        if not isinstance(series, list):
            continue
        pts = []
        for r in series:
            if not isinstance(r, dict):
                continue
            v = r.get("value")
            if isnum(v) and isinstance(r.get("date"), str):
                pts.append((r["date"][:10], float(v)))
        if not pts:
            continue
        pts.sort()
        latest_date, latest = pts[-1]
        prior = [v for _, v in pts[:-1]][-window:]
        z, mean = zscore(latest, prior, min_history)
        out[str(sym).upper()] = {"trends_latest": latest, "trends_date": latest_date,
                                 "trends_mean": round(mean, 2) if mean is not None else None,
                                 "trends_z": z, "trends_n": len(prior)}
    return out


# --------------------------------------------------------------- attention history
def zscore(value, prior, min_history=ATTENTION_MIN_HISTORY):
    """(z, mean) of `value` against `prior` (sample std). (None, mean) when there are
    fewer than `min_history` prior points or they do not vary — a z over a flat history
    is not a large number, it is undefined."""
    if not isnum(value) or len(prior) < min_history:
        return None, (sum(prior) / len(prior) if prior else None)
    n = len(prior)
    mean = sum(prior) / n
    var = sum((x - mean) ** 2 for x in prior) / (n - 1) if n > 1 else 0.0
    if var <= 0:
        return None, mean
    return round((value - mean) / math.sqrt(var), 3), mean


def load_attention_history(path=None):
    """The rolling history: {"_meta": {...}, "symbols": {SYM: [{ts, mentions, upvotes}]}}.
    Missing or unreadable -> an empty one (the first scan has no history, by definition)."""
    p = path if path is None or os.path.isabs(path) else os.path.join(BASE, path)
    doc = _load(p) if p else None
    if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), dict):
        return {"_meta": {"window": ATTENTION_WINDOW, "updated": None}, "symbols": {}}
    return doc


def attention_features(aw, history, now=None, window=ATTENTION_WINDOW,
                       min_history=ATTENTION_MIN_HISTORY, spike_z=ATTENTION_SPIKE_Z):
    """Per-symbol attention features from today's ApeWisdom rows and the rolling history.

    Returns ({SYM: {attention_count, attention_upvotes, attention_z, attention_spike,
                    attention_rank_pct, attention_mean_20, attention_history_n}},
             updated_history).
    The z compares today's mention count with the trailing `window` PRIOR scans of the
    same symbol; the rank is today's percentile across every name on the feed (100 =
    the most mentioned). A symbol on the history but off today's page 1 is recorded at
    0 today — it fell below the page's floor — so a fade shows as one. A symbol whose
    stored history is all zeros is dropped from the file so it cannot grow without bound.
    Pure: the caller decides whether to write `updated_history` back.
    """
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    key = ts[:13]                       # date + hour: a re-run inside the hour replaces itself
    hist = {s: list(v) for s, v in (history or {}).get("symbols", {}).items() if isinstance(v, list)}
    out = {}
    if not aw:
        return out, {"_meta": {"window": window, "updated": (history or {}).get("_meta", {}).get("updated")},
                     "symbols": hist}
    counts = {s: (a.get("mentions") if isnum(a.get("mentions")) else 0) for s, a in aw.items()}
    n = len(counts)
    ordered = sorted(counts.values())
    for sym in sorted(set(counts) | set(hist)):
        today = counts.get(sym, 0)
        upv = (aw.get(sym) or {}).get("upvotes")
        prior_rows = [r for r in hist.get(sym, []) if isinstance(r, dict)
                      and str(r.get("ts", ""))[:13] != key]
        prior = [float(r["mentions"]) for r in prior_rows[-window:] if isnum(r.get("mentions"))]
        z, mean = zscore(today, prior, min_history)
        if sym in counts:
            below = sum(1 for v in ordered if v < today)
            rank_pct = round(below / (n - 1) * 100.0, 1) if n > 1 else 100.0
            out[sym] = {"attention_count": today,
                        "attention_upvotes": upv if isnum(upv) else None,
                        "attention_z": z,
                        "attention_spike": (z > spike_z) if z is not None else None,
                        "attention_rank_pct": rank_pct,
                        "attention_mean_20": round(mean, 2) if mean is not None else None,
                        "attention_history_n": len(prior)}
        rows = prior_rows + [{"ts": ts, "mentions": today,
                              "upvotes": upv if isnum(upv) else None}]
        rows = rows[-window:]
        if any(isnum(r.get("mentions")) and r["mentions"] > 0 for r in rows):
            hist[sym] = rows
        else:
            hist.pop(sym, None)
    return out, {"_meta": {"window": window, "updated": ts, "min_history": min_history,
                           "spike_z": spike_z}, "symbols": hist}


def write_attention_history(doc, path=None):
    p = path if os.path.isabs(path) else os.path.join(BASE, path)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    return p


# --------------------------------------------------------------- options positioning
# From the Robinhood scanner's OPTION filter group (scan "Scan Desk — Options
# activity", created 2026-09-01). get_option_quotes returns HTTP 403 on this account
# (README 9.1), but the SCAN service exposes implied volatility, call/put day volume
# and open interest — authoritative exchange data, one call for the whole market.
#
# LIQUIDITY GATE. A put/call ratio computed off a handful of contracts is one block
# trade, not positioning. Live examples from the first run on 2026-09-01: APG showed
# a ratio of 625.1 from 16 calls against 10,002 puts on 95 total open interest, JAN
# 5,819 calls against 5 puts, STEP 10,009 against 1. Those are single prints. PFE's
# 3.31 on 202k contracts and 2.26M open interest is a real crowd position. The gate
# separates them and says which side of it every row fell.
MIN_PC_LEG_VOLUME = 500        # both legs must clear this
MIN_PC_OPEN_INTEREST = 5000    # a real chain, not a new listing

def parse_options_scan(payload, warnings):
    """run_scan rows -> {TICKER: {iv, put_call_ratio, ...}} with a liquidity gate."""
    rows = _rows(payload, "results", "rows", "data")
    if not rows and isinstance(payload, dict):
        rows = _rows(dig_result(payload), "results")
    out, gated = {}, []
    for r in rows:
        tk = _sym(r, "ticker", "symbol") or _sym(r.get("columns") or {}, "Symbol")
        if not tk:
            continue
        col = r.get("columns") if isinstance(r.get("columns"), dict) else r

        def num(*names):
            for n in names:
                v = col.get(n)
                if isnum(v):
                    return float(v)
                if isinstance(v, str) and v.strip():
                    try:
                        return float(v)
                    except ValueError:
                        pass
            return None

        iv = num("Implied volatility", "implied_volatility")
        hv = num("Historical volatility", "historical_volatility")
        cv = num("Total call volume", "total_call_volume")
        pv = num("Total put volume", "total_put_volume")
        oi = num("Open interest", "open_interest")
        rov = num("Relative options volume", "relative_options_volume")
        tot = num("Options volume", "options_volume")

        row = {"iv": round(iv, 4) if iv is not None else None,
               "hv": round(hv, 4) if hv is not None else None,
               "options_volume": tot, "options_open_interest": oi,
               "relative_options_volume": round(rov, 2) if rov is not None else None,
               "call_volume": cv, "put_volume": pv}
        if iv is not None and hv is not None and hv > 0:
            # IV richer than realised vol is the market paying up for something.
            row["iv_hv_ratio"] = round(iv / hv, 2)

        liquid = (isnum(cv) and isnum(pv) and cv >= MIN_PC_LEG_VOLUME
                  and pv >= MIN_PC_LEG_VOLUME
                  and isnum(oi) and oi >= MIN_PC_OPEN_INTEREST)
        if isnum(cv) and isnum(pv) and cv > 0:
            ratio = round(pv / cv, 3)
            if liquid:
                row["put_call_ratio"] = ratio
                row["put_call_basis"] = "day volume, both legs liquid"
            else:
                row["put_call_ratio_illiquid"] = ratio
                row["put_call_basis"] = ("REJECTED: thin chain "
                                         f"({cv:.0f} calls / {pv:.0f} puts, OI {oi or 0:.0f}) "
                                         "— one block trade, not positioning")
                gated.append(tk)
        out[tk] = row
    if gated:
        warnings.append(
            f"Put/call ratio withheld as unreliable on {len(gated)} name(s) "
            f"({', '.join(sorted(gated)[:8])}) — fewer than {MIN_PC_LEG_VOLUME} contracts on a "
            f"leg or under {MIN_PC_OPEN_INTEREST} open interest. The raw ratio is kept as "
            "put_call_ratio_illiquid so the board can show it as an anomaly, never as sentiment.")
    return out


def dig_result(payload):
    """Unwrap the connector's {"data": {"result": {...}}} envelope if present."""
    if isinstance(payload, dict):
        d = payload.get("data")
        if isinstance(d, dict) and isinstance(d.get("result"), dict):
            return d["result"]
        if isinstance(payload.get("result"), dict):
            return payload["result"]
    return payload if isinstance(payload, dict) else {}


def expected_move_pct(iv, days_to_earnings):
    """IV-derived expected move. NOT the straddle price.

    README 9.1 forbids substituting an estimate for `implied_move_pct`, because the
    board renders that field as "options price a +/-X% move" — a claim about what the
    option market is paying, which only a real straddle quote supports. This is a
    different, weaker claim: what a lognormal move of THIS SIZE implies given the
    scan's implied volatility. It is emitted under its own name with its own basis
    string and never populates implied_move_pct.
    """
    if not (isnum(iv) and isnum(days_to_earnings)) or iv <= 0 or days_to_earnings < 0:
        return None
    return round(iv * ((max(days_to_earnings, 1) / 365.0) ** 0.5) * 100.0, 1)


# --------------------------------------------------------------- RH watchlists
ADR_OTC = re.compile(r"^[A-Z]{4}[YF]$")     # 5-letter ADR/OTC convention

def parse_watchlists(payload, warnings, quotes=None):
    """{list_name: [symbols]} -> universe candidates + why anything was excluded.

    Robinhood's *Daily movers* list is 70% illiquid foreign ADRs and OTC tickers
    (BLFBY, MITUY, ZSHGY, WEDXF... on 2026-09-01). Trending stocks and 100 most
    popular are clean. Filter before the universe screen sees them or the scanner
    is handed names the book cannot trade.
    """
    if not isinstance(payload, dict):
        return {}, []
    quotes = quotes or {}
    keep, dropped = {}, []
    for list_name, syms in payload.items():
        if not isinstance(syms, list):
            continue
        for i, s in enumerate(syms, 1):
            tk = s.get("symbol") if isinstance(s, dict) else s
            if not isinstance(tk, str):
                continue
            tk = tk.strip().upper()
            # Price first, so an exclusion reason is always the true reason. A
            # 5-letter ticker ending in Y or F is an ADR/OTC by convention, but it
            # is a HEURISTIC on the name alone — every drop is named in _meta so a
            # false positive is visible rather than silent.
            px = quotes.get(tk, {}).get("price") if isinstance(quotes.get(tk), dict) else None
            if isnum(px) and px < 1.0:
                dropped.append(f"{tk} (sub-$1 at {px}, from {list_name})")
                continue
            if ADR_OTC.match(tk):
                dropped.append(f"{tk} (5-letter ADR/OTC name pattern, from {list_name})")
                continue
            e = keep.setdefault(tk, {"rh_lists": [], "rh_best_position": None})
            e["rh_lists"].append(list_name)
            if e["rh_best_position"] is None or i < e["rh_best_position"]:
                e["rh_best_position"] = i
    if dropped:
        warnings.append(f"Excluded {len(dropped)} watchlist name(s) as untradeable for this "
                        f"book: {', '.join(dropped[:10])}{'...' if len(dropped) > 10 else ''}")
    return keep, dropped


# --------------------------------------------------------------- merge
SOURCE_KEYS = ("apewisdom", "stocktwits_trending", "rh_watchlist")

def build(apewisdom=None, st_trending=None, st_gauges=None, watchlists=None, quotes=None,
          options_scan=None, earnings_days=None, now=None, st_symbol_streams=None,
          trends=None, attention_history=None):
    """Every source in, one normalised map out. Pure: nothing here reads or writes a
    file. Returns the sentiment.json document; the updated attention history rides on
    it as `_attention_history` for the caller to write back (main() does, and strips it
    from the file it writes)."""
    warnings = []
    now = now or datetime.now(timezone.utc)
    aw = parse_apewisdom(apewisdom, warnings)
    stt = parse_stocktwits_trending(st_trending, warnings)
    stg = parse_stocktwits_gauges(st_gauges, warnings)
    sts = parse_stocktwits_symbol_streams(st_symbol_streams, warnings, now=now)
    tr = parse_trends(trends, warnings)
    wl, wl_dropped = parse_watchlists(watchlists, warnings, quotes)
    opt = parse_options_scan(dig_result(options_scan) if options_scan else None, warnings)
    earnings_days = earnings_days if isinstance(earnings_days, dict) else {}
    att, hist_next = attention_features(aw, attention_history, now=now)

    tickers = sorted(set(aw) | set(stt) | set(stg) | set(sts) | set(tr) | set(wl) | set(opt))
    out = {}
    for tk in tickers:
        a, t, g, w = aw.get(tk), stt.get(tk), stg.get(tk), wl.get(tk)
        o, s_, r_ = opt.get(tk), sts.get(tk), tr.get(tk)
        present = [name for name, v in
                   (("apewisdom", a), ("stocktwits_trending", t),
                    ("rh_watchlist", w), ("options_scan", o)) if v]
        # An ambiguous English-word ticker only counts when a second, independent
        # feed also names it. One vendor's prose match is not a finding. (This is the
        # SAME gate as before P-04: the wider STOP_TICKERS list gated the Reddit text
        # path only and is kept for the day a text feed returns — widening it to the
        # vendor aggregates would change which names the scanner scores.)
        if tk in AMBIGUOUS_BUT_REAL and len(present) < 2:
            continue

        row = {"sources": present, "sources_count": len(present),
               "corroborated": len(present) >= 2}
        if a:
            row.update({"wsb_mentions": a["mentions"],
                        "wsb_mentions_24h_ago": a["mentions_24h_ago"],
                        "wsb_upvotes": a["upvotes"], "wsb_rank": a["rank"],
                        "wsb_rank_24h_ago": a["rank_24h_ago"],
                        "wsb_mention_change_pct": a["mention_change_pct"],
                        "wsb_new_entrant": a["is_new_entrant"]})
            if a["rank"] and a["rank_24h_ago"]:
                row["wsb_rank_delta"] = a["rank_24h_ago"] - a["rank"]
        if t:
            row.update({"stocktwits_trending_position": t["trending_position"],
                        "stocktwits_watchlist_count": t.get("watchlist_count")})
            row["stocktwits_trending"] = True
        if g:
            row.update({k: v for k, v in g.items() if v is not None})
        if s_:
            row.update(s_)
        if r_:
            row.update(r_)
        if w:
            row.update(w)
        if o:
            row.update(o)
            em = expected_move_pct(o.get("iv"), earnings_days.get(tk))
            if em is not None:
                row["expected_move_pct"] = em
                row["expected_move_basis"] = (
                    "derived from the scan's implied volatility, NOT a straddle quote — "
                    "implied_move_pct stays null until get_option_quotes is enabled")
        # P-04: the attention-fade features. Null keys are written on purpose when the
        # history is too short: "not measured yet" must stay distinguishable from 0.
        if a:
            row.update(att.get(tk) or {k: None for k in ATTENTION_KEYS})
        out[tk] = row

    ranked = sorted(out.items(),
                    key=lambda kv: (kv[1]["sources_count"],
                                    kv[1].get("wsb_mentions") or 0), reverse=True)
    spikes = sorted(tk for tk, v in out.items() if v.get("attention_spike"))
    if spikes:
        warnings.append("ATTENTION SPIKE (z > 2 vs the trailing 20 scans) on " + ", ".join(spikes)
                        + " — the research base expects a spike to fade over 2-4 weeks; "
                          "reported, not scored.")
    n_hist = sum(1 for v in att.values() if v.get("attention_z") is not None)
    meta = {
        "generated_at": now.isoformat(timespec="seconds"),
        "sources_present": {k: bool(v) for k, v in
                            (("apewisdom", aw), ("stocktwits_trending", stt),
                             ("stocktwits_gauges", stg), ("stocktwits_symbol_streams", sts),
                             ("trends", tr), ("rh_watchlists", wl), ("options_scan", opt))},
        "ticker_count": len(out),
        "corroborated_count": sum(1 for v in out.values() if v["corroborated"]),
        "attention": {"scans_in_history": max((len(v) for v in hist_next["symbols"].values()),
                                              default=0),
                      "symbols_with_z": n_hist, "spikes": spikes,
                      "window": ATTENTION_WINDOW, "min_history": ATTENTION_MIN_HISTORY},
        "watchlist_excluded": wl_dropped,
        # Neither ApeWisdom nor StockTwits stamps its payload. This is the honest
        # statement of that, carried into the board rather than assumed away.
        "vendor_timestamps_available": False,
        "warnings": warnings,
    }
    return {"_meta": meta, "tickers": out,
            "top_by_corroboration": [k for k, _ in ranked[:25]],
            "universe": sorted(wl),
            "_attention_history": hist_next}


# --------------------------------------------------------------- merge into scan_data
# scanner.py reads c["retail"] with these exact keys. Emitting that shape means the
# scoring engine does not have to change at all — sentiment.py is a data dependency,
# not an import, so a task prompt that copies the old file list cannot crash on a
# missing module (the ENG-01 blocker of 2026-08-31).
SCANNER_RETAIL_KEYS = ("wsb_mentions", "wsb_mentions_24h_ago", "wsb_rank",
                       "stocktwits_score", "stocktwits_label", "stocktwits_trending")
# Carried through for the board and for validate.py. REPORTED, NOT SCORED: per the
# README section 11 rule, no new data point enters the scoring model until the
# Saturday validation shows it ranks forward returns better than what is scored now.
# P-04 — the attention-fade features. Written into the candidate's `retail` block like
# every reported key, AND into its `features` dict so scanner.py logs them on the row,
# archive.py keeps them in the compact record and the snapshot / backtest records carry
# them for ic.py --by-feature (the S-09 sign test). Scored by nothing.
ATTENTION_KEYS = ("attention_count", "attention_upvotes", "attention_z", "attention_spike",
                  "attention_rank_pct", "attention_mean_20", "attention_history_n")
ST_STREAM_KEYS = ("st_bull_pct", "st_bull_n", "st_bear_n", "st_msgs", "st_newest_utc",
                  "st_stream_age_min", "st_stream_stale")
TRENDS_KEYS = ("trends_latest", "trends_date", "trends_mean", "trends_z", "trends_n")
FEATURE_KEYS = ("attention_z", "attention_spike", "attention_rank_pct", "attention_count",
                "st_bull_pct", "trends_z")
REPORTED_KEYS = ("sources", "sources_count", "corroborated", "wsb_upvotes",
                 "wsb_rank_24h_ago", "wsb_rank_delta", "wsb_mention_change_pct",
                 "wsb_new_entrant", "stocktwits_trending_position",
                 "stocktwits_watchlist_count", "rh_lists", "rh_best_position",
                 "iv", "hv", "iv_hv_ratio", "put_call_ratio", "put_call_ratio_illiquid",
                 "put_call_basis", "options_volume", "options_open_interest",
                 "relative_options_volume", "call_volume", "put_volume",
                 "expected_move_pct", "expected_move_basis") + ATTENTION_KEYS + ST_STREAM_KEYS \
                + TRENDS_KEYS


def merge_into_scan_data(scan_data, sent):
    """Populate each candidate's retail block from sentiment.json. Pure function.

    Returns (updated_scan_data, report). Candidates absent from the sentiment map
    get an explicit empty marker rather than nothing, so 'no chatter' and 'we did
    not look' stay distinguishable downstream — the same distinction coverage
    normalisation exists to protect.
    """
    if not isinstance(scan_data, dict) or not isinstance(sent, dict):
        return scan_data, {"merged": [], "no_data": [], "error": "unusable input"}
    tickers = sent.get("tickers") or {}
    cands = scan_data.get("candidates")
    if not isinstance(cands, dict):
        return scan_data, {"merged": [], "no_data": [], "error": "no candidates block"}

    merged, none_found, uncorroborated = [], [], []
    for tk, c in cands.items():
        if not isinstance(c, dict):
            continue
        row = tickers.get(str(tk).upper())
        retail = dict(c.get("retail") or {})
        if not row:
            retail.setdefault("sentiment_checked", True)
            retail.setdefault("sources_count", 0)
            c["retail"] = retail
            none_found.append(tk)
            continue
        for k in SCANNER_RETAIL_KEYS + REPORTED_KEYS:
            if k in row and row[k] is not None:
                retail[k] = row[k]
        retail["sentiment_checked"] = True
        c["retail"] = retail
        # P-04: the attention features ride on the candidate's `features` dict too —
        # merged into whatever technicals.py already put there, never replacing it.
        # Null when not measured (short history, no stream, no trends), so a record
        # says "unmeasured" rather than omitting the key; a bool spike is stored as 0/1.
        feats = c.get("features") if isinstance(c.get("features"), dict) else {}
        for k in FEATURE_KEYS:
            v = row.get(k)
            feats[k] = (int(v) if isinstance(v, bool) else v)
        c["features"] = feats
        merged.append(tk)
        if not row.get("corroborated"):
            uncorroborated.append(tk)

    meta = scan_data.setdefault("meta", {})
    warns = list(meta.get("data_warnings") or [])
    sm = sent.get("_meta") or {}
    for w in (sm.get("warnings") or []):
        warns.append("SENTIMENT: " + w)
    if uncorroborated:
        warns.append(
            "SENTIMENT: single-source retail attention on " + ", ".join(sorted(uncorroborated))
            + " — one vendor's count with no second feed confirming it. On 2026-09-01 "
              "ApeWisdom and StockTwits agreed on 4 of 15 top names, so an uncorroborated "
              "attention number is weak evidence, not a finding.")
    if sm.get("vendor_timestamps_available") is False:
        warns.append("SENTIMENT: neither ApeWisdom nor StockTwits stamps its payload — "
                     "retail figures cannot be proven current, only inferred from deltas.")
    meta["data_warnings"] = warns
    meta["sentiment_meta"] = {k: sm.get(k) for k in
                              ("generated_at", "sources_present", "ticker_count",
                               "corroborated_count", "attention")}
    return scan_data, {"merged": merged, "no_data": none_found,
                       "uncorroborated": uncorroborated}


def _load(path):
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(BASE, path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Normalise retail sentiment payloads.")
    ap.add_argument("--apewisdom", help="raw ApeWisdom page-1 payload (page 1 ONLY)")
    ap.add_argument("--stocktwits-trending", dest="stt")
    ap.add_argument("--stocktwits-gauges", dest="stg")
    ap.add_argument("--stocktwits-symbols", dest="sts", metavar="GLOB", nargs="?",
                    const=ST_STREAM_FILE,
                    help=f"staged StockTwits symbol streams, one file per name "
                         f"(default pattern {ST_STREAM_FILE}, resolved under SCAN_DIR); "
                         "read for the bull share only, never for attention")
    ap.add_argument("--trends", help='{"NVDA": [{"date": "2026-09-01", "value": 63}, ...]} '
                                     "from Google Trends, when available")
    ap.add_argument("--attention-history", dest="hist", default=ATTENTION_HISTORY,
                    help="rolling per-symbol mention history the attention z is measured "
                         "against; read before, written after (default archive/"
                         "attention_history.json under SCAN_DIR). '' disables it.")
    ap.add_argument("--now", help="ISO timestamp override (UTC) for a deterministic run")
    ap.add_argument("--watchlists", help='{"Trending stocks": ["WMT", ...], ...}')
    ap.add_argument("--options-scan", dest="opts",
                    help="raw run_scan response for the Options activity scan")
    ap.add_argument("--earnings-days", dest="edays",
                    help='{"TSLA": 12} days to earnings, for the expected-move estimate')
    ap.add_argument("--quotes", help='{"WMT": {"price": 1.23}} for the liquidity filter')
    ap.add_argument("--out", default="sentiment.json")
    ap.add_argument("--merge-into", dest="merge_into",
                    help="scan_data.json to populate with retail blocks, written in place")
    a = ap.parse_args(argv)

    now = _parse_ts(a.now) if a.now else None
    history = load_attention_history(a.hist) if a.hist else None
    res = build(_load(a.apewisdom), _load(a.stt), _load(a.stg),
                _load(a.watchlists), _load(a.quotes),
                options_scan=_load(a.opts), earnings_days=_load(a.edays), now=now,
                st_symbol_streams=load_symbol_streams(a.sts) if a.sts else None,
                trends=_load(a.trends), attention_history=history)
    hist_next = res.pop("_attention_history", None)
    if a.hist and hist_next is not None and res["_meta"]["sources_present"].get("apewisdom"):
        write_attention_history(hist_next, a.hist)
    out = a.out if os.path.isabs(a.out) else os.path.join(BASE, a.out)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)

    m = res["_meta"]
    live = [k for k, v in m["sources_present"].items() if v]
    print(f"sentiment.json  <-  {', '.join(live) if live else 'NO SOURCES'}")
    print(f"{m['ticker_count']} tickers, {m['corroborated_count']} corroborated by 2+ feeds")
    att = m.get("attention") or {}
    print(f"attention: {att.get('scans_in_history', 0)} scan(s) of history, "
          f"{att.get('symbols_with_z', 0)} name(s) with a z-score"
          + (f", SPIKES: {', '.join(att['spikes'])}" if att.get("spikes") else "")
          + ("" if a.hist else "  (history disabled)"))
    for w in m["warnings"]:
        print("  ! " + w)
    if a.merge_into:
        sd = _load(a.merge_into)
        if sd is None:
            print(f"  ! --merge-into: {a.merge_into} unreadable, nothing merged")
        else:
            sd, rep = merge_into_scan_data(sd, res)
            p = a.merge_into if os.path.isabs(a.merge_into) else os.path.join(BASE, a.merge_into)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(sd, fh, indent=2)
            print(f"merged retail into {len(rep['merged'])} candidate(s); "
                  f"{len(rep['no_data'])} had no chatter"
                  + (f"; UNCORROBORATED: {', '.join(rep['uncorroborated'])}"
                     if rep.get("uncorroborated") else ""))
    if not live:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
