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

BANNED INPUT. api.stocktwits.com/api/2/streams/symbol/<T>.json is cached with a
variable TTL and was measured 50 hours stale for NVDA, 31h for TSLA and 10h for a
small cap while the trending endpoint was live to the minute. This module refuses
that payload shape outright rather than trusting a caller to remember. The
stocktwits.com/symbol/<T> HTML gauge is a different thing and is fine.

Reddit post/comment text arrives from Arctic Shift (the Pushshift successor), which
returns raw bodies rather than a vendor's aggregate. Its mention counts and lexicon
tone are REPORTED, NOT SCORED, until validate.py shows they rank forward returns —
the same rule relative strength is under.

Paths resolve from SCAN_DIR, else this file's directory. No path is hardcoded.
"""
import argparse, json, os, re, sys
from datetime import datetime, timezone

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------- stop-list
# Tickers that are also ordinary English words, conjunctions or units. A mention
# counter with no stop-list scores prose. Everything here was observed in a live
# ApeWisdom top-150 on 2026-09-01 except where noted as pre-emptive.
STOP_TICKERS = {
    "A","ALL","AM","AN","AND","ANY","ARE","AS","AT","BE","BIG","BY","CAN","CC","DAY",
    "DC","DD","DE","DK","DO","EOD","ES","EU","EV","FOR","GO","GP","HAS","HE","IG","IN",
    "IP","IQ","IS","IT","ITS","JUST","KEY","LOVE","MY","NEW","NOW","ON","ONE","OR",
    "OUT","PM","PR","PT","REAL","SD","SF","SO","TA","TD","TWO","UP","US","USA","VS",
    "WE","WELL","YOU","AI","CEO","CFO","ETF","FDA","GDP","IMO","IPO","IRA","LOL","NFA",
    "OTM","ITM","PE","RH","ROI","SEC","TLDR","YOLO","DTE","EPS","ATH","FOMO","HODL",
}
# Names that are legitimately traded AND common words. Keep them, but only when the
# mention arrives cashtagged ($DTE) or the ticker is corroborated by another feed.
AMBIGUOUS_BUT_REAL = {"DTE","ALL","KEY","IT","ON","PR","ES","IP","DAY","LOVE","GO","UP"}

MAX_UPVOTES_PER_MENTION = 500   # HIMS reported 9001 upvotes on 3 mentions
CASHTAG = re.compile(r"\$([A-Z]{1,5})\b")
BARE_TICKER = re.compile(r"\b([A-Z]{2,5})\b")

BULL_WORDS = {"calls","long","buy","bought","bullish","moon","rip","squeeze","breakout",
              "up","green","rally","beat","upgrade","strong","pump","yolo","hold","hodl"}
BEAR_WORDS = {"puts","short","sell","sold","bearish","crash","dump","drop","down","red",
              "miss","downgrade","weak","bag","bagholder","rug","tank","bubble"}


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


# --------------------------------------------------------------- Arctic Shift
def parse_reddit_posts(payload, warnings, max_age_hours=36, now=None):
    """Raw Reddit posts/comments -> per-ticker mention counts and a lexicon tone.

    REPORTED, NOT SCORED. The tone is a bag-of-words count, not a model, and it is
    carried so validate.py can test whether it ranks forward returns before anything
    depends on it.
    """
    rows = _rows(payload, "data", "posts", "results")
    if not rows:
        return {}, None
    now = now or datetime.now(timezone.utc)
    counts, bull, bear, newest = {}, {}, {}, None
    stale = 0
    for r in rows:
        ts = r.get("created_utc")
        if isnum(ts):
            when = datetime.fromtimestamp(ts, timezone.utc)
            newest = when if newest is None else max(newest, when)
            if (now - when).total_seconds() > max_age_hours * 3600:
                stale += 1
                continue
        text = " ".join(str(r.get(k) or "") for k in ("title", "selftext", "body"))
        if not text.strip():
            continue
        words = set(w.lower() for w in re.findall(r"[a-zA-Z]+", text))
        b_, s_ = len(words & BULL_WORDS), len(words & BEAR_WORDS)
        # Cashtags are unambiguous; bare uppercase words need the stop-list.
        tickers = set(CASHTAG.findall(text))
        for t in BARE_TICKER.findall(text):
            if t not in STOP_TICKERS:
                tickers.add(t)
        for t in tickers:
            if t in STOP_TICKERS:
                continue
            counts[t] = counts.get(t, 0) + 1
            bull[t] = bull.get(t, 0) + b_
            bear[t] = bear.get(t, 0) + s_
    if stale:
        warnings.append(f"Dropped {stale} Reddit item(s) older than {max_age_hours}h. "
                        "If most items were dropped, the `sort=desc` parameter is missing.")
    if newest is not None:
        age_min = (now - newest).total_seconds() / 60.0
        if age_min > 180:
            warnings.append(f"Newest Reddit item is {age_min:.0f} minutes old — the feed "
                            "may be stalled or `sort=desc` was omitted.")
    out = {}
    for t, n in counts.items():
        b_, s_ = bull.get(t, 0), bear.get(t, 0)
        tone = None
        if b_ + s_ >= 3:
            tone = round((b_ - s_) / (b_ + s_), 2)
        out[t] = {"reddit_raw_mentions": n, "reddit_tone": tone,
                  "reddit_bull_hits": b_, "reddit_bear_hits": s_}
    return out, newest


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
SOURCE_KEYS = ("apewisdom", "stocktwits_trending", "reddit_raw", "rh_watchlist")

def build(apewisdom=None, st_trending=None, st_gauges=None, reddit=None,
          watchlists=None, quotes=None, options_scan=None, earnings_days=None, now=None):
    warnings = []
    aw = parse_apewisdom(apewisdom, warnings)
    stt = parse_stocktwits_trending(st_trending, warnings)
    stg = parse_stocktwits_gauges(st_gauges, warnings)
    rd, reddit_newest = parse_reddit_posts(reddit, warnings, now=now)
    wl, wl_dropped = parse_watchlists(watchlists, warnings, quotes)
    opt = parse_options_scan(dig_result(options_scan) if options_scan else None, warnings)
    earnings_days = earnings_days if isinstance(earnings_days, dict) else {}

    tickers = sorted(set(aw) | set(stt) | set(stg) | set(rd) | set(wl) | set(opt))
    out = {}
    for tk in tickers:
        a, t, g, r, w = aw.get(tk), stt.get(tk), stg.get(tk), rd.get(tk), wl.get(tk)
        o = opt.get(tk)
        present = [name for name, v in
                   (("apewisdom", a), ("stocktwits_trending", t),
                    ("reddit_raw", r), ("rh_watchlist", w), ("options_scan", o)) if v]
        # An ambiguous English-word ticker only counts when a second, independent
        # feed also names it. One vendor's prose match is not a finding.
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
        if r:
            row.update(r)
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
        out[tk] = row

    ranked = sorted(out.items(),
                    key=lambda kv: (kv[1]["sources_count"],
                                    kv[1].get("wsb_mentions") or 0), reverse=True)
    meta = {
        "generated_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "sources_present": {k: bool(v) for k, v in
                            (("apewisdom", aw), ("stocktwits_trending", stt),
                             ("stocktwits_gauges", stg), ("reddit_raw", rd),
                             ("rh_watchlists", wl), ("options_scan", opt))},
        "ticker_count": len(out),
        "corroborated_count": sum(1 for v in out.values() if v["corroborated"]),
        "reddit_newest_utc": reddit_newest.isoformat(timespec="seconds") if reddit_newest else None,
        "watchlist_excluded": wl_dropped,
        # Neither ApeWisdom nor StockTwits stamps its payload. This is the honest
        # statement of that, carried into the board rather than assumed away.
        "vendor_timestamps_available": False,
        "warnings": warnings,
    }
    return {"_meta": meta, "tickers": out,
            "top_by_corroboration": [k for k, _ in ranked[:25]],
            "universe": sorted(wl)}


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
REPORTED_KEYS = ("sources", "sources_count", "corroborated", "wsb_upvotes",
                 "wsb_rank_24h_ago", "wsb_rank_delta", "wsb_mention_change_pct",
                 "wsb_new_entrant", "stocktwits_trending_position",
                 "stocktwits_watchlist_count", "reddit_raw_mentions", "reddit_tone",
                 "reddit_bull_hits", "reddit_bear_hits", "rh_lists", "rh_best_position",
                 "iv", "hv", "iv_hv_ratio", "put_call_ratio", "put_call_ratio_illiquid",
                 "put_call_basis", "options_volume", "options_open_interest",
                 "relative_options_volume", "call_volume", "put_volume",
                 "expected_move_pct", "expected_move_basis")


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
                               "corroborated_count", "reddit_newest_utc")}
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
    ap.add_argument("--reddit", help="raw Arctic Shift posts payload (sort=desc)")
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

    res = build(_load(a.apewisdom), _load(a.stt), _load(a.stg),
                _load(a.reddit), _load(a.watchlists), _load(a.quotes),
                options_scan=_load(a.opts), earnings_days=_load(a.edays))
    out = a.out if os.path.isabs(a.out) else os.path.join(BASE, a.out)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=2)

    m = res["_meta"]
    live = [k for k, v in m["sources_present"].items() if v]
    print(f"sentiment.json  <-  {', '.join(live) if live else 'NO SOURCES'}")
    print(f"{m['ticker_count']} tickers, {m['corroborated_count']} corroborated by 2+ feeds")
    if m["reddit_newest_utc"]:
        print(f"newest Reddit item: {m['reddit_newest_utc']}")
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
