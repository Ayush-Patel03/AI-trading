"""Market scanner scoring engine.

Consumes a scan_data.json (regime + candidate fundamentals/technicals gathered
by the agent layer) and produces a ranked, explainable recommendation set.

Score is 0-100 across five weighted pillars, then adjusted by market regime:
    Trend & Structure   25
    Momentum & Position 15
    Fundamentals        20
    Catalyst & Analyst  20
    Market Intelligence 20

REAL TECHNICALS (2026-08-31). MA50/MA200, RSI, ATR, true relative volume and the
overnight gap now come from actual OHLC bars via technicals.py, fed by a single
Robinhood get_equity_historicals call covering up to ten symbols. Before this the
scan had no bars at all: no RSI, no ATR, and a relative volume scraped off a quote
page. RSI is scored against the SETUP, not in the abstract — 40 on a pullback is the
entry, 40 on a momentum name is a failing trend.

COVERAGE NORMALISATION. A pillar with no input data at all is excluded from the
denominator instead of being scored zero. Without this, a dead source silently
rescales the whole board: on 2026-08-28 the retail and insider feeds were down,
every name lost up to 20 of 100 points, "Strong Buy" (72+) became unreachable and
the 60-point proposal floor blocked every order. Scores are now comparable across
scans regardless of which sources answered. Rows carry `coverage_pct`, and a row
under 70% coverage cannot be called Strong Buy - thin evidence is not conviction.

LLM MEMOS (P-07, 2026-09-10). A scheduled session may leave `memos/<SYMBOL>.json` in the
run dir — a structured extraction from an ANONYMISED news payload (`news_payload.json`).
memo.py validates each one against that payload (latency, numerals copied not computed,
verbatim quote, temperature 0, post-cutoff) and the valid ones put `llm_*` keys on the
row's `features`, exactly like the S-04 technical features: logged, scored by nothing.
Every probability call goes to archive/calibration.jsonl for history.py --resolve-memos.
See docs/LLM.md.

Paths resolve from SCAN_DIR, else from this file's own directory, so the bundle
runs wherever it is copied.
"""
import json, os, sys
from datetime import date, datetime

import config
# The board's macro banner must describe the gate the MANAGER actually applies, so it
# imports that definition rather than restating it. Before 2026-09-09 this file bannered
# EVERY same-day release as freezing entries; pm.py gates on six, inside a 120-minute
# window. The board therefore contradicted the manager on every run that carried an
# ISM/JOLTS/ADP/Beige Book print — verified 2026-09-01 (five releases) and 2026-09-09
# (six). Importing pm is safe: it has no import-time side effects and does not import
# this module.
from pm import _is_high_impact as is_high_impact
# P-02 / E16: the 10-K/10-Q text-change signal ("Lazy Prices"). Pure functions over a staged
# file; no import-time side effects and it imports nothing from the engine.
import filings
# P-03: the deny-list override (short reports, negative news, halts). Applied AFTER scoring,
# only when the feed was staged; see veto.py and scan()'s `veto_feed` argument.
import veto as veto_mod

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

PILLAR_MAX = {"trend": 25, "momentum": 15, "fundamentals": 20,
              "catalyst": 20, "intelligence": 20}
# P-06: the earnings-quality feature keys (earnings_quality.FEATURE_KEYS, restated here so
# this module does not import that one — it is a data dependency, like sentiment.py).
EARNINGS_QUALITY_KEYS = ("sue", "ear_3d", "reg_residual", "earnings_agreement",
                         "days_since_earnings")
EARNINGS_QUALITY_FILE = "earnings_quality.json"
FULL_SCALE = sum(PILLAR_MAX.values())          # 100
MIN_COVERAGE_FOR_STRONG = 70.0                 # % of the evidence base

def dig(d, *path, default=None):
    """Nested .get that never raises on a missing or non-dict level."""
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur

def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)

def present(v):
    return v is not None and v != [] and v != {} and v != ""

# ---------------------------------------------------------------- helpers
def pct(a, b):
    """Percent difference of a vs b."""
    if a is None or b in (None, 0):
        return None
    return (a - b) / b * 100.0

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def band(value, thresholds):
    """thresholds: list of (min_value, points), descending. Returns points."""
    if value is None:
        return 0.0
    for cutoff, points in thresholds:
        if value >= cutoff:
            return points
    return 0.0

# ---------------------------------------------------------------- regime
def score_regime(r):
    """Return (multiplier, label, notes) describing the market backdrop."""
    notes, pts = [], 0

    spy_off = dig(r, "spy", "pct_off_52w_high")
    if not isnum(spy_off):
        notes.append("SPY 52-week position unavailable - regime read is partial")
    elif spy_off > -3:
        pts += 2; notes.append(f"SPY {spy_off:.1f}% off its 52-week high — index at/near highs")
    elif spy_off > -8:
        pts += 1; notes.append(f"SPY {spy_off:.1f}% off highs — mild consolidation")
    else:
        pts -= 1; notes.append(f"SPY {spy_off:.1f}% off highs — index in a drawdown")

    vix = r.get("vix")
    if isnum(vix):
        if vix < 16:
            pts += 2; notes.append(f"VIX {vix:.1f} — low volatility, risk-seeking conditions")
        elif vix < 22:
            pts += 1; notes.append(f"VIX {vix:.1f} — normal volatility")
        else:
            pts -= 2; notes.append(f"VIX {vix:.1f} — elevated fear, tighten risk")

    b = r.get("breadth") or {}
    a50 = b.get("pct_above_50dma")
    if isnum(a50):
        if a50 > 60:
            pts += 2; notes.append(f"{a50:.0f}% of stocks above their 50-day MA — broad participation")
        elif a50 > 48:
            pts += 0; notes.append(f"{a50:.0f}% of stocks above their 50-day MA — only a slim majority participating")
        else:
            pts -= 2; notes.append(f"{a50:.0f}% of stocks above their 50-day MA — narrow, weak breadth")

    ad = b.get("nyse_ad_ratio")
    if isnum(ad):
        hi_, lo_ = b.get("nyse_new_highs"), b.get("nyse_new_lows")
        extra = f", {hi_} new highs vs {lo_} new lows" if hi_ and lo_ else ""
        notes.append(f"NYSE advancers lead decliners {ad:.2f}-to-1{extra} — positive but not a breadth thrust")

    rets = {k.upper(): dig(r, k, "returns_1y") for k in ("spy","qqq","iwm")
            if isnum(dig(r, k, "returns_1y"))}
    if rets:
        lead_i = max(rets, key=rets.get)
        notes.append("Trailing 1-year: " + ", ".join(f"{k} {v:+.1f}%" for k, v in rets.items())
                     + f" — {lead_i} leading")

    all_etfs = r.get("sector_etfs") or {}
    etfs = {k: v for k, v in all_etfs.items()
            if isinstance(v, dict) and isnum(v.get("day"))}
    if etfs:
        green = sum(1 for v in etfs.values() if v["day"] > 0)
        tot = len(etfs)
        lead = max(etfs, key=lambda k: etfs[k]["day"])
        lag = min(etfs, key=lambda k: etfs[k]["day"])
        if green <= tot / 3:
            pts -= 1
            notes.append(f"Only {green} of {tot} GICS sectors green — leadership is very narrow, "
                         f"{lead} {etfs[lead]['day']:+.2f}% carrying it while {lag} {etfs[lag]['day']:+.2f}% lags")
        elif green <= tot / 2:
            notes.append(f"{green} of {tot} GICS sectors green — mixed, led by {lead} ({etfs[lead]['day']:+.2f}%)")
        else:
            pts += 1
            notes.append(f"{green} of {tot} GICS sectors green — broad advance led by {lead} ({etfs[lead]['day']:+.2f}%)")
        ytd = {k: v for k, v in all_etfs.items()
               if isinstance(v, dict) and isnum(v.get("ytd"))}
        if len(ytd) >= 2:
            yb = max(ytd, key=lambda k: ytd[k]["ytd"]); yw = min(ytd, key=lambda k: ytd[k]["ytd"])
            spread = ytd[yb]["ytd"] - ytd[yw]["ytd"]
            notes.append(f"YTD sector spread {spread:.0f} points: {yb} {ytd[yb]['ytd']:+.1f}% vs "
                         f"{yw} {ytd[yw]['ytd']:+.1f}% — dispersion is where the segmentation edge lives")
        elif all_etfs:
            notes.append("Sector YTD returns unavailable this run - the 11-ETF compare page did not answer")

    if pts >= 5:
        return 1.05, "Risk-On", notes
    if pts >= 2:
        return 1.00, "Constructive", notes
    if pts >= 0:
        return 0.93, "Mixed / Cautious", notes
    return 0.85, "Risk-Off", notes

# ---------------------------------------------------------------- setup type
def classify_setup(c):
    p, m50, m200 = c.get("price"), c.get("ma_50"), c.get("ma_200")
    if not (isnum(p) and isnum(m50) and isnum(m200)):
        return "Unclassified", "Insufficient moving-average data"
    if p > m50 > m200:
        return "Momentum", "Price above both MAs with 50 over 200 — trend intact and extended"
    if p < m50 and m50 > m200 and p > m200:
        return "Pullback in Uptrend", "Long-term trend up but price has pulled back below the 50-day — mean-reversion entry"
    if p > m50 and m50 < m200:
        return "Early Recovery", "Reclaimed the 50-day while 50 is still under 200 — potential trend turn, unconfirmed"
    if p < m200:
        return "Broken Trend", "Price below the 200-day — structural downtrend, avoid until reclaimed"
    return "Neutral", "No clean structural signal"

# ---------------------------------------------------------------- pillars
def score_trend(c):
    """0-25. Rewards structure; treats healthy pullbacks as opportunity not weakness."""
    p, m50, m200 = c.get("price"), c.get("ma_50"), c.get("ma_200")
    if not (isnum(p) and isnum(m50) and isnum(m200)):
        return 0.0, ["No moving-average data"]
    s, why = 0.0, []

    # Long-term structure (most important)
    if p > m200:
        v = pct(p, m200)
        s += 10; why.append(f"Above the 200-day by {v:.1f}%")
    else:
        why.append(f"Below the 200-day by {abs(pct(p, m200)):.1f}% — structurally broken")

    # MA alignment
    if m50 > m200:
        s += 7; why.append("50-day above 200-day (golden-cross alignment)")
    else:
        why.append("50-day below 200-day (death-cross alignment)")

    # Position vs 50-day: both extremes are informative
    d50 = pct(p, m50)
    if d50 is not None:
        if -12 <= d50 <= -2:
            s += 8; why.append(f"{d50:.1f}% below the 50-day — constructive pullback zone")
        elif -2 < d50 <= 8:
            s += 7; why.append(f"{d50:+.1f}% vs the 50-day — riding the trend line")
        elif 8 < d50 <= 20:
            s += 4; why.append(f"{d50:+.1f}% above the 50-day — extended")
        elif d50 > 20:
            s += 2; why.append(f"{d50:+.1f}% above the 50-day — very extended, poor risk/reward on entry")
        else:
            s += 1; why.append(f"{d50:.1f}% below the 50-day — deep, momentum may be broken")
    return clamp(s, 0, 25), why

def score_rsi(rsi, setup):
    """0-3. RSI only means something relative to the setup it sits in. 40 on a pullback
    is the mean-reversion entry; 40 on a name that is supposed to be trending is a
    failing trend. Scoring it context-free would reward the wrong half of the board."""
    if not isnum(rsi):
        return 0.0, None
    if setup in ("Pullback in Uptrend", "Early Recovery"):
        if rsi < 30:
            return 1.5, f"RSI {rsi:.0f} — deeply oversold, but catching a falling knife"
        if rsi <= 50:
            return 3.0, f"RSI {rsi:.0f} — the mean-reversion entry zone for a pullback"
        if rsi <= 60:
            return 2.0, f"RSI {rsi:.0f} — pullback already bouncing, less edge left"
        return 1.0, f"RSI {rsi:.0f} — the pullback has been bought, entry is late"
    if setup == "Momentum":
        if rsi > 80:
            return 0.5, f"RSI {rsi:.0f} — very overbought, poor risk/reward on entry"
        if rsi > 70:
            return 1.5, f"RSI {rsi:.0f} — overbought, trend intact but extended"
        if rsi >= 55:
            return 3.0, f"RSI {rsi:.0f} — healthy trend strength"
        if rsi >= 45:
            return 2.0, f"RSI {rsi:.0f} — momentum cooling"
        return 1.0, f"RSI {rsi:.0f} — momentum is failing despite the MA structure"
    if rsi < 30:
        return 1.0, f"RSI {rsi:.0f} — oversold in a broken structure, not a setup"
    return 0.5, f"RSI {rsi:.0f}"

def score_momentum(c, setup="Neutral"):
    """0-15. Trailing strength, position in range, real participation, and RSI in context."""
    s, why = 0.0, []
    ch = c.get("week52_change_pct")
    s += band(ch, [(200, 4), (100, 3.5), (50, 3), (25, 2.5), (10, 2), (0, 1)])
    if ch is not None:
        why.append(f"{ch:+.1f}% over the last 52 weeks")

    lo, hi, p = c.get("week52_low"), c.get("week52_high"), c.get("price")
    if isnum(lo) and isnum(hi) and isnum(p) and hi > lo:
        posn = (p - lo) / (hi - lo) * 100
        off_high = pct(p, hi)
        if posn >= 85:
            s += 2.5; why.append(f"{posn:.0f}% up its 52-week range ({off_high:.1f}% off the high) — near highs")
        elif posn >= 60:
            s += 4; why.append(f"{posn:.0f}% up its 52-week range ({off_high:.1f}% off the high) — strong but not stretched")
        elif posn >= 35:
            s += 2.5; why.append(f"{posn:.0f}% up its 52-week range ({off_high:.1f}% off the high) — mid-range")
        else:
            s += 1.5; why.append(f"{posn:.0f}% up its 52-week range — near the low end")

    # rel_volume comes pre-computed from bars when technicals.py ran; fall back to the
    # raw division only if it did not.
    rv = c.get("rel_volume")
    if not isnum(rv) and isnum(c.get("avg_volume_20d")) and isnum(c.get("volume")) \
            and c["avg_volume_20d"] > 0:
        rv = c["volume"] / c["avg_volume_20d"]
    if isnum(rv):
        if rv >= 1.5:
            s += 4; why.append(f"Relative volume {rv:.2f}x — heavy participation")
        elif rv >= 0.9:
            s += 2.5; why.append(f"Relative volume {rv:.2f}x — normal participation")
        else:
            s += 1; why.append(f"Relative volume {rv:.2f}x (intraday partial — indicative only)")

    r_pts, r_why = score_rsi(c.get("rsi_14"), setup)
    s += r_pts
    if r_why:
        why.append(r_why)

    gap = c.get("gap_pct")
    if isnum(gap) and abs(gap) >= 2:
        basis = c.get("gap_basis")
        how = {"extended_hours": " (extended-hours print vs prior close)",
               "prior_bar": " (last COMPLETED session's gap — no live quotes were passed)"}.get(basis, "")
        why.append(f"Gapped {gap:+.1f}% at the open against the prior close{how}")
    # Relative strength vs the benchmark: reported, deliberately NOT scored until
    # validate.py shows it ranks forward returns on this universe.
    rs = c.get("rs_20d_vs_SPY")
    if isnum(rs):
        why.append(f"Relative strength vs SPY {rs:+.1f} pts over 20 sessions"
                   + (" — leading the tape" if rs >= 5 else (" — lagging the tape" if rs <= -5 else "")))
    atrp = c.get("atr_pct")
    if isnum(atrp):
        why.append(f"ATR {atrp:.1f}% of price — {'high' if atrp >= 5 else 'moderate' if atrp >= 2.5 else 'low'} daily range")
    return clamp(s, 0, 15), why

def score_fundamentals(c):
    """0-20. Growth, profitability, and whether you're overpaying for it."""
    s, why = 0.0, []

    rg = c.get("revenue_growth_pct")
    s += band(rg, [(40, 5), (25, 4), (15, 3), (7, 2), (0, 1)])
    if rg is not None:
        why.append(f"Revenue growth {rg:+.1f}%")

    eg = c.get("eps_growth_pct")
    s += band(eg, [(100, 5), (40, 4), (15, 3), (5, 2), (0, 1)])
    if eg is not None:
        why.append(f"EPS growth {eg:+.1f}%")

    pm = c.get("profit_margin_pct")
    if pm is not None:
        if pm >= 30:
            s += 5; why.append(f"Profit margin {pm:.1f}% — highly profitable")
        elif pm >= 15:
            s += 4; why.append(f"Profit margin {pm:.1f}%")
        elif pm >= 5:
            s += 2; why.append(f"Profit margin {pm:.1f}% — thin")
        elif pm < 0:
            s -= 3; why.append(f"Profit margin {pm:.1f}% — currently unprofitable (RISK)")

    peg = c.get("peg")
    if peg is not None:
        if 0 < peg <= 1.0:
            s += 5; why.append(f"PEG {peg:.2f} — growth is cheap relative to price")
        elif peg <= 1.8:
            s += 3; why.append(f"PEG {peg:.2f} — fairly valued for its growth")
        elif peg <= 3.0:
            s += 1; why.append(f"PEG {peg:.2f} — paying up for growth")
        else:
            why.append(f"PEG {peg:.2f} — expensive relative to growth")

    de = c.get("debt_to_equity")
    if de is not None:
        if de <= 0.5:
            s += 2; why.append(f"Debt/equity {de:.2f} — clean balance sheet")
        elif de >= 1.5:
            s -= 2; why.append(f"Debt/equity {de:.2f} — leveraged")

    fpe = c.get("forward_pe")
    if fpe is not None and fpe > 60:
        s -= 2; why.append(f"Forward P/E {fpe:.1f} — priced for perfection")
    return clamp(s, 0, 20), why

def score_catalyst(c, today):
    """0-20. Analyst posture, upside to target, and near-term event risk."""
    s, why = 0.0, []

    rating = (c.get("analyst_rating") or "").lower()
    if "strong buy" in rating:
        s += 6; why.append("Analyst consensus: Strong Buy")
    elif "buy" in rating:
        s += 5; why.append("Analyst consensus: Buy")
    elif "hold" in rating:
        s += 1; why.append("Analyst consensus: Hold")
    elif rating:
        why.append(f"Analyst consensus: {c['analyst_rating']}")

    tgt = c.get("analyst_target")
    up = pct(tgt, c.get("price")) if isnum(tgt) else None
    if up is not None:
        if up >= 35:
            s += 6; why.append(f"{up:+.1f}% to the average analyst target")
        elif up >= 20:
            s += 5; why.append(f"{up:+.1f}% to the average analyst target")
        elif up >= 8:
            s += 4; why.append(f"{up:+.1f}% to the average analyst target")
        elif up >= 0:
            s += 1; why.append(f"Only {up:+.1f}% to target — most upside already priced in")
        else:
            s -= 2; why.append(f"{up:+.1f}% — trading ABOVE the average analyst target")

    n = len(c.get("catalysts") or [])
    s += band(n, [(5, 4), (3, 3), (1, 2)])
    if n:
        why.append(f"{n} active news catalysts in the last few weeks")

    ne = c.get("next_earnings")
    days = None
    if ne:
        try:
            days = (datetime.strptime(ne, "%Y-%m-%d").date() - today).days
        except ValueError:
            days = None
    timing = (c.get("earnings_timing") or "").lower()
    when = {"am": " before the open", "pm": " after the close"}.get(timing, "")
    im = c.get("implied_move_pct")
    im_txt = f", options price a ±{im:.1f}% move" if isnum(im) else ""
    if days is not None:
        if 0 <= days <= 7:
            s += 3; why.append(f"EARNINGS IN {days} DAY(S) ({ne}{when}){im_txt} — "
                               f"binary event risk, size accordingly")
        elif 0 < days <= 21:
            s += 2; why.append(f"Earnings on {ne}{when} ({days} days out){im_txt}")
        elif days < 0:
            why.append(f"Last reported {ne} — earnings just cleared")

    sf = c.get("short_float_pct")
    if sf is not None and sf >= 8:
        s += 2; why.append(f"Short float {sf:.1f}% — squeeze potential")
    return clamp(s, 0, 20), why


def score_intelligence(c):
    """0-20. What the tape, the crowd, the call and the filings say — the signal
    that does not show up in price or on the balance sheet."""
    s, why = 0.0, []
    r = c.get("retail") or {}

    # --- retail attention momentum (7) ---
    m, m0 = r.get("wsb_mentions"), r.get("wsb_mentions_24h_ago")
    if m is not None:
        if m0 and m0 > 0:
            chg = (m - m0) / m0 * 100
            if chg >= 200:
                s += 5; why.append(f"Reddit mentions {m0}->{m} ({chg:+.0f}%) — attention spike")
            elif chg >= 50:
                s += 4; why.append(f"Reddit mentions {m0}->{m} ({chg:+.0f}%) — attention building")
            elif chg >= -25:
                s += 2; why.append(f"Reddit mentions {m0}->{m} ({chg:+.0f}%) — steady")
            else:
                s += 1; why.append(f"Reddit mentions {m0}->{m} ({chg:+.0f}%) — attention fading")
        else:
            s += 2; why.append(f"{m} Reddit mentions, no prior-day baseline")
        rank = r.get("wsb_rank")
        if rank and rank <= 10:
            s += 2; why.append(f"Rank #{rank} on r/wallstreetbets")
        elif rank and rank <= 50:
            s += 1; why.append(f"Rank #{rank} on r/wallstreetbets")
    else:
        why.append("No measurable retail chatter — an institutional name, not a crowd trade")

    st = r.get("stocktwits_score")
    if st is not None:
        lbl = r.get("stocktwits_label") or ""
        if st >= 75:
            s += 3; why.append(f"StockTwits {st} ({lbl})")
        elif st >= 55:
            s += 2; why.append(f"StockTwits {st} ({lbl})")
        elif st >= 45:
            s += 1; why.append(f"StockTwits {st} ({lbl}) — no directional edge")
        else:
            why.append(f"StockTwits {st} ({lbl}) — crowd leaning against it")
        if r.get("stocktwits_trending"):
            s += 1; why.append("Flagged Trending on StockTwits")
    # crowd-vs-tape divergence is itself information
    if m is not None and st is not None and m > 50 and st < 40:
        why.append("DIVERGENCE: heavy retail attention but bearish crowd tone")

    # --- earnings call signal (5) ---
    t = c.get("transcript")
    if t:
        sig = t.get("signal")
        if sig == "bullish":
            s += 4; why.append(f"{t.get('quarter','Latest')} call reads bullish: {t.get('headline','')}")
        elif sig == "bearish":
            why.append(f"{t.get('quarter','Latest')} call reads bearish: {t.get('headline','')}")
        else:
            s += 2; why.append(f"{t.get('quarter','Latest')} call reads neutral")
        if t.get("guidance_direction") == "raised":
            s += 1; why.append("Guidance raised")
        elif t.get("guidance_direction") == "cut":
            s -= 2; why.append("Guidance cut")
        if t.get("tone") == "confident":
            why.append(f'Management tone confident: "{t.get("tone_evidence","")[:90]}"')
        if t.get("risk"):
            why.append(f"Call risk: {t['risk']}")
    else:
        why.append("No recent earnings call analysed")

    # --- news flow (3) ---
    n = len(c.get("catalysts") or [])
    s += band(n, [(5, 3), (3, 2), (1, 1)])
    if n:
        why.append(f"{n} distinct news items on the tape")

    # --- insider activity (4) ---
    ins = c.get("insider")
    if ins:
        if ins.get("direction") == "buy":
            s += 4; why.append(f"Insider CLUSTER BUY: {ins.get('count')} insiders, ${abs(ins.get('value',0)):,.0f}")
        else:
            s -= 2; why.append(f"Insider cluster SELL: {ins.get('count')} insiders, ${abs(ins.get('value',0)):,.0f}")
    return clamp(s, 0, 20), why


def pillar_coverage(c):
    """Which pillars had ANY input data at all. A pillar with no inputs is not
    'a bad score', it is 'no evidence', and the two must not look the same."""
    retail = c.get("retail") or {}
    retail_live = any(present(retail.get(k)) for k in
                      ("wsb_mentions", "wsb_rank", "stocktwits_score", "stocktwits_label"))
    return {
        "trend": present(c.get("ma_50")) and present(c.get("ma_200")),
        "momentum": any(present(c.get(k)) for k in
                        ("week52_change_pct", "week52_high", "avg_volume_20d",
                         "rsi_14", "rel_volume")),
        "fundamentals": any(present(c.get(k)) for k in
                            ("revenue_growth_pct", "eps_growth_pct", "profit_margin_pct",
                             "peg", "debt_to_equity", "forward_pe")),
        "catalyst": any(present(c.get(k)) for k in
                        ("analyst_rating", "analyst_target", "catalysts",
                         "next_earnings", "short_float_pct")),
        "intelligence": retail_live or present(c.get("transcript"))
                        or present(c.get("insider")) or present(c.get("catalysts")),
    }

# ---------------------------------------------------------------- insider features (P-01 / E15)
# insiders.py (a data dependency, not an import — the sentiment.py rule) writes
# insiders_signal.json into the run directory when the scheduled task staged Form 4 data.
# When that file is present, every scored row's `features` dict gains the three keys below
# — null for a name the signal has no transactions for — and the archive record and the
# scan snapshot carry them like every other research feature. When it is absent the row
# is untouched: "we did not look" and "no insider bought" must stay distinguishable, and
# the golden output must not move. NOT an input to any pillar; the existing `insider`
# panel is a separate, hand-collected display and is not read here.
INSIDER_SIGNAL_FILE = "insiders_signal.json"
INSIDER_FEATURE_KEYS = ("insider_cluster_buy", "insider_opportunistic_buy_usd_30d",
                        "insider_net_usd_90d")

def load_insider_signal(base=None):
    """The staged insiders_signal.json ({_meta, symbols}), or None when not staged."""
    p = os.path.join(base or BASE, INSIDER_SIGNAL_FILE)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and isinstance(doc.get("symbols"), dict) else None

def insider_features(sig, ticker):
    """The three feature values for one ticker from a loaded signal; all None when the
    signal carries nothing for it."""
    row = (sig or {}).get("symbols", {}).get(str(ticker).upper())
    if not isinstance(row, dict):
        return {k: None for k in INSIDER_FEATURE_KEYS}
    cb = row.get("cluster_buy")
    return {
        "insider_cluster_buy": bool(cb) if cb is not None else None,
        "insider_opportunistic_buy_usd_30d": (row.get("opportunistic_buy_usd_30d")
                                              if isnum(row.get("opportunistic_buy_usd_30d")) else None),
        "insider_net_usd_90d": (row.get("net_insider_usd_90d")
                                if isnum(row.get("net_insider_usd_90d")) else None),
    }

def data_confidence(c):
    """Not scored — reported. How much should you trust this row's price?"""
    n = c.get("price_sources", 1)
    # M1 guard (2026-08-31 audit): a collection session once wrote price_sources as a
    # dict of {source: price} instead of a count, and the `n < 2` comparison below
    # raised a TypeError that killed the whole scan. A collection counts as its size;
    # anything else unparseable counts as a single unconfirmed source.
    if isinstance(n, (list, tuple, set, dict)):
        n = len(n)
    elif not isnum(n):
        n = 1
    dis = c.get("price_disagreement_pct")
    if not isnum(dis):
        dis = None
    if n < 2:
        return "single", "One source only — price unconfirmed"
    if dis is None:
        return "single", "One source only — price unconfirmed"
    if dis <= 1.0:
        return "confirmed", f"Two independent sources agree within {dis:.2f}%"
    if dis <= 3.0:
        return "loose", f"Two sources differ by {dis:.2f}% — treat the price as approximate"
    return "conflict", f"Two sources differ by {dis:.2f}% — price is NOT reliable, verify before trading"

# ---------------------------------------------------------------- verdict
def verdict(score, setup):
    if setup == "Broken Trend":
        return "Avoid", "Structure is broken — no long setup regardless of score"
    if score >= 72:
        return "Strong Buy", "High conviction across trend, fundamentals and catalyst"
    if score >= 60:
        return "Buy", "Solid setup with a clear edge"
    if score >= 48:
        return "Watch", "Constructive but wants confirmation before committing capital"
    if score >= 35:
        return "Hold", "No clear edge here right now"
    return "Avoid", "Fails on multiple criteria"

def scan_date_of(meta):
    """The run's date. SCAN.md's documented scan_data.json contract names `date`, while
    this module has always read `scan_date`; a caller that followed the doc got
    KeyError: 'scan_date' on the first call. Accept either, prefer the explicit one, and
    keep the key normalised so everything downstream (tape rows, archive records) still
    sees `scan_date`.
    """
    d = meta.get("scan_date") or meta.get("date")
    if not d:
        raise KeyError("meta.scan_date (or meta.date) is required")
    meta["scan_date"] = str(d)
    return str(d)


def attach_filings(rows, staged, today):
    """P-02 / E16: lay the 10-K/10-Q text-change features onto every row's `features`
    block when `filings_signal.json` was staged. Logged for ic.py --by-feature; scored by
    nothing — the intended use, once E16 has a result, is a slow negative screen.

    `staged` is filings.load_staged()'s dict, or None when the file is not there, in which
    case nothing is touched (a row with no feature block stays without one, exactly as
    before). With the file present every row carries the three keys, null for a symbol the
    file does not name or whose filing post-dates the scan — "not covered" must never read
    as "unchanged". Returns {"n_symbols", "n_matched", "n_changers"} for the meta block."""
    if not isinstance(staged, dict):
        return None
    n_matched = n_changers = 0
    for r in rows:
        f = filings.features_for(staged.get(r["ticker"]), today)
        feats = r.get("features") if isinstance(r.get("features"), dict) else {}
        feats.update(f)
        r["features"] = feats
        if f["filing_change_score"] is not None:
            n_matched += 1
            n_changers += 1 if f["filing_changer"] else 0
    return {"n_symbols": sum(1 for k in staged if not str(k).startswith("_")),
            "n_matched": n_matched, "n_changers": n_changers,
            "threshold": (staged.get("_meta") or {}).get("threshold", filings.CHANGER_THRESHOLD)}


def attach_memos(data, run_dir, archive_dir=None):
    """P-07: LLM memos as logged features. `memos/<SYMBOL>.json` in the run dir, validated
    by memo.py against `news_payload.json` (the staged raw news the session was given);
    valid ones put `llm_*` keys on the candidate's `features`, rejections land in
    `meta.memo_rejections`, probability calls go to `<archive>/calibration.jsonl`. No pillar
    reads any of it — the row's score is the same with or without a memo. Never raises."""
    try:
        import memo
        return memo.apply_memos(data, run_dir, archive_dir)
    except Exception as exc:                      # noqa: BLE001 — a memo never kills a scan
        data.setdefault("meta", {}).setdefault("data_warnings", []).append(
            f"MEMO: processing failed and was skipped: {type(exc).__name__}: {exc}")
        return {"accepted": [], "rejected": {}, "logged": [], "error": str(exc)}


def scan(data, *, insider_signal=None, filings_signal=None, veto_feed=None,
         earnings_quality=None, run_dir=None):
    """Score one scan_data.json document.

    Every optional input is a staged file the scheduled task may or may not have put in
    the run directory; each attaches *logged* features (scored by nothing) and a `meta`
    block only when present, so the output of a scan with none of them is byte-identical
    to one that never had the arguments.

    `insider_signal` (P-01): the insiders_signal.json document to attach as features; when
    None (the normal CLI path) it is read from $SCAN_DIR if staged there, and when nothing
    is staged no insider feature is attached at all.

    `filings_signal` (P-02): the parsed filings_signal.json (filings.load_staged), or None.
    It is an explicit argument rather than a file read here so that a backtest replay —
    which calls scan() per historical date — cannot pick up today's staged file by
    accident; __main__ loads it for the live path.

    `veto_feed` (P-03): what veto.load() returned, or None. With a feed, vetoed rows are
    overridden to Avoid AFTER scoring — scores and pillars untouched, the override logged
    in NOTABLE and meta.veto. None means no override anywhere; the output is byte-identical
    to a scan that never had the argument. The veto pass runs last, after every feature
    block is on the row, so it sees the final row.

    `earnings_quality` (P-06): {TICKER: {sue, ear_3d, reg_residual, earnings_agreement,
    days_since_earnings}} from earnings_quality.py, or None. Merged into each row's
    `features` dict — logged, scored by nothing. A ticker absent from the map gets every
    key null when the map was supplied, and nothing at all when it was not.

    `run_dir` (P-07): the run directory holding `memos/<SYMBOL>.json` and
    `news_payload.json`, or None. Valid memos put `llm_*` keys on the candidate's
    `features` before scoring (so they ride into the row with the S-04 technicals),
    rejections land in `meta.memo_rejections`, and probability calls are logged to
    `<run_dir>/archive/calibration.jsonl`. With no memos/ directory nothing is touched.
    """
    today = datetime.strptime(scan_date_of(data["meta"]), "%Y-%m-%d").date()
    if run_dir:
        attach_memos(data, run_dir, os.path.join(run_dir, "archive"))
    mult, regime_label, regime_notes = score_regime(data["regime"])
    insider_sig = insider_signal if insider_signal is not None else load_insider_signal()

    # Prior scans from earlier slots today: [{slot, time, regime_label, avg, scores:{TKR:score}}]
    history = [h for h in data.get("history", [])
               if h.get("date", data["meta"]["scan_date"]) == data["meta"]["scan_date"]]
    history.sort(key=lambda h: h.get("time", ""))

    rows, skipped = [], []
    for tk, c in data["candidates"].items():
        if not isnum(c.get("price")) or c["price"] <= 0:
            # No usable price means no setup, no upside, no sizing. Drop it and say so
            # rather than rendering a row full of em-dashes that looks like a finding.
            skipped.append(tk)
            continue
        setup, setup_note = classify_setup(c)
        t, twhy = score_trend(c)
        m, mwhy = score_momentum(c, setup)
        f, fwhy = score_fundamentals(c)
        k, kwhy = score_catalyst(c, today)
        x, xwhy = score_intelligence(c)

        cov = pillar_coverage(c)
        earned = {"trend": t, "momentum": m, "fundamentals": f,
                  "catalyst": k, "intelligence": x}
        for pillar, ok in cov.items():
            if not ok:
                earned[pillar] = 0.0
        available = sum(PILLAR_MAX[pl] for pl, ok in cov.items() if ok)
        raw = sum(earned[pl] for pl, ok in cov.items() if ok)
        coverage_pct = available / FULL_SCALE * 100.0
        norm = (raw / available * 100.0) if available else 0.0
        adj = clamp(norm * mult, 0, 100)
        missing = sorted(pl for pl, ok in cov.items() if not ok)

        v, vnote = verdict(adj, setup)
        if v == "Strong Buy" and coverage_pct < MIN_COVERAGE_FOR_STRONG:
            v = "Buy"
            vnote = (f"Capped from Strong Buy — only {coverage_pct:.0f}% of the evidence "
                     f"base was available ({', '.join(missing)} missing)")
        conf_level, conf_note = data_confidence(c)
        tgt = c.get("analyst_target")
        rows.append({
            "ticker": tk, "name": c.get("name") or tk, "sector": c.get("sector"),
            "price": c["price"], "day_change_pct": c.get("day_change_pct"),
            "market_cap": c.get("market_cap"), "setup": setup, "setup_note": setup_note,
            "pillars": {"trend": round(t,1), "momentum": round(m,1),
                        "fundamentals": round(f,1), "catalyst": round(k,1),
                        "intelligence": round(x,1)},
            "raw_score": round(raw,1), "normalized_score": round(norm,1),
            "score": round(adj,1), "coverage_pct": round(coverage_pct,0),
            "missing_pillars": missing,
            "verdict": v, "verdict_note": vnote,
            "upside_pct": round(pct(tgt, c["price"]),1) if tgt else None,
            "analyst_target": tgt, "analyst_rating": c.get("analyst_rating"),
            "ma_50": c.get("ma_50"), "ma_200": c.get("ma_200"),
            "vs_ma50_pct": round(pct(c["price"], c.get("ma_50")),1) if c.get("ma_50") else None,
            "vs_ma200_pct": round(pct(c["price"], c.get("ma_200")),1) if c.get("ma_200") else None,
            "week52_change_pct": c.get("week52_change_pct"),
            "week52_low": c.get("week52_low"), "week52_high": c.get("week52_high"),
            "off_high_pct": round(pct(c["price"], c.get("week52_high")),1) if c.get("week52_high") else None,
            "rel_volume": (round(c["volume"]/c["avg_volume_20d"], 2)
                           if isnum(c.get("avg_volume_20d")) and isnum(c.get("volume"))
                           and c["avg_volume_20d"] > 0 else None),
            "forward_pe": c.get("forward_pe"), "peg": c.get("peg"),
            "profit_margin_pct": c.get("profit_margin_pct"), "roe_pct": c.get("roe_pct"),
            "revenue_growth_pct": c.get("revenue_growth_pct"), "eps_growth_pct": c.get("eps_growth_pct"),
            "debt_to_equity": c.get("debt_to_equity"), "short_float_pct": c.get("short_float_pct"),
            "beta": c.get("beta"), "next_earnings": c.get("next_earnings"),
            "earnings_timing": c.get("earnings_timing"),
            "implied_move_pct": c.get("implied_move_pct"),
            "rsi_14": c.get("rsi_14"), "atr_14": c.get("atr_14"),
            "atr_pct": c.get("atr_pct"), "gap_pct": c.get("gap_pct"),
            "gap_basis": c.get("gap_basis"),
            "ret_20d_pct": c.get("ret_20d_pct"), "ret_60d_pct": c.get("ret_60d_pct"),
            "rs_20d_vs_spy": c.get("rs_20d_vs_SPY"), "rs_60d_vs_spy": c.get("rs_60d_vs_SPY"),
            "bars_used": c.get("bars_used"),
            "catalysts": c.get("catalysts") or [],
            "reasons": {"trend": twhy, "momentum": mwhy, "fundamentals": fwhy,
                        "catalyst": kwhy, "intelligence": xwhy},
            "industry": c.get("industry"), "gics": c.get("gics"), "sic": c.get("sic"),
            "retail": c.get("retail") or {}, "transcript": c.get("transcript"),
            "confidence": conf_level, "confidence_note": conf_note,
            "price_disagreement_pct": c.get("price_disagreement_pct"),
        })
        # S-04: the research features technicals.features() computed (merged into the
        # candidate from technicals.json, or set directly by backtest.py). Logged so the
        # archive record and the snapshot carry them for ic.py --by-feature; NOT an input
        # to any pillar above, and absent rather than null when nothing computed them.
        # The P-07 memo keys (llm_*) arrive here too: attach_memos() put them on the
        # candidate before scoring, and every staged source below adds disjoint keys.
        if isinstance(c.get("features"), dict):
            rows[-1]["features"] = dict(c["features"])
        # P-01: the insider signal, when staged. Same rule — logged, scored by nothing.
        if insider_sig is not None:
            feats = dict(rows[-1].get("features") or {})
            feats.update(insider_features(insider_sig, tk))
            rows[-1]["features"] = feats
        # P-06: the earnings-quality features ride in the same dict, same rule — logged on
        # the row, read by ic.py --by-feature, scored by nothing. Only when the map exists.
        if isinstance(earnings_quality, dict):
            eq = earnings_quality.get(tk) or {}
            feats = dict(rows[-1].get("features") or {})
            feats.update({k: eq.get(k) for k in EARNINGS_QUALITY_KEYS})
            rows[-1]["features"] = feats

    # P-02: filing text-change features, when the staged file is present. Rows only;
    # nothing above reads them.
    filings_meta = attach_filings(rows, filings_signal, today)

    # P-03: the veto override. A separate pass, after every pillar is scored, every verdict
    # struck and every feature block laid on the row, and only with a staged feed. It
    # changes a verdict, never a score, so a vetoed name still ranks where it scored — with
    # "Avoid" written across it and the reason on the row, in NOTABLE and in meta.veto.
    # Without the feed nothing here runs. Keep this the LAST pass over the rows: it must
    # see the final row.
    vetoed = []
    if veto_feed is not None:
        vetoed = veto_mod.apply_to_rows(rows, today, veto_feed)

    # Score trail across today's slots, with this scan appended as the final point
    prior_top5 = set()
    if history:
        last = history[-1]
        prior_top5 = set(sorted(last.get("scores", {}), key=lambda k: last["scores"][k],
                                reverse=True)[:5])
    for r in rows:
        trail = [h["scores"][r["ticker"]] for h in history if r["ticker"] in h.get("scores", {})]
        r["score_trail"] = trail + [r["score"]]
        r["score_delta"] = round(r["score"] - trail[-1], 1) if trail else None
        r["is_new"] = bool(history) and not trail

    rows.sort(key=lambda r: r["score"], reverse=True)
    for i, r in enumerate(rows):
        r["entered_top5"] = bool(history) and i < 5 and r["ticker"] not in prior_top5
    tape = history + [{
        "date": data["meta"]["scan_date"],
        "slot": data["meta"].get("slot", data["meta"].get("session", "scan")),
        "time": data["meta"].get("time", ""),
        "regime_label": regime_label,
        "avg": round(sum(r["score"] for r in rows) / len(rows), 1) if rows else 0,
        "top": rows[0]["ticker"] if rows else None,
        "scores": {r["ticker"]: r["score"] for r in rows},
    }]

    notable = []
    for r in rows:
        if r.get("veto"):
            notable.append(f'VETO: {r["ticker"]} — ' + "; ".join(r["veto_reasons"]) +
                           f' — verdict overridden to Avoid (scored {r["score"]:.0f}, '
                           f'{r["pre_veto_verdict"]}); the manager refuses the entry')
    for r in rows:
        if r["score"] >= 75:
            notable.append(f'{r["ticker"]} scores {r["score"]:.0f} ({r["verdict"]}, {r["setup"]})')
        if r.get("entered_top5"):
            notable.append(f'{r["ticker"]} entered the top 5')
        if r.get("score_delta") is not None and abs(r["score_delta"]) >= 8:
            notable.append(f'{r["ticker"]} score moved {r["score_delta"]:+.0f} since the last scan')
    if history and history[-1].get("regime_label") != regime_label:
        notable.insert(0, f'Regime flipped: {history[-1]["regime_label"]} to {regime_label}')

    for r in rows:
        im_, d_ = r.get("implied_move_pct"), r.get("next_earnings")
        if isnum(im_) and im_ >= 8 and d_:
            notable.append(f'{r["ticker"]} reports {d_} with a ±{im_:.1f}% implied move — '
                           f'that is the position size question, not the score')
        g_ = r.get("gap_pct")
        if isnum(g_) and abs(g_) >= 4:
            notable.append(f'{r["ticker"]} gapped {g_:+.1f}% at the open')

    # Scheduled macro releases (FOMC decision, CPI, payrolls...) are binary events for the
    # whole tape, exactly as earnings are for one name. The collection layer writes
    # meta.macro_events = [{date, name, time_et?}]; the manager refuses new entries ahead
    # of one dated today (pm.py macro gate) and this board says so.
    for ev in (data["meta"].get("macro_events") or []):
        try:
            d_ev = datetime.strptime(str(ev.get("date")), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        dd = (d_ev - today).days
        if dd == 0:
            label = (f"{ev.get('name', 'scheduled release')}"
                     f"{' at ' + str(ev['time_et']) + ' ET' if ev.get('time_et') else ''}")
            if is_high_impact(ev.get("name")):
                notable.insert(0, f"MACRO TODAY: {label}"
                                  " — the manager places no new entries inside its"
                                  " lookahead window; existing positions are managed"
                                  " normally")
            else:
                notable.append(f"MACRO TODAY: {label} — reported, NOT gating:"
                               " the manager's entry gate covers only FOMC, CPI, PCE,"
                               " payrolls, GDP and Powell")
        elif dd == 1:
            notable.append(f"MACRO TOMORROW: {ev.get('name', 'scheduled release')} on {ev['date']}"
                           " — overnight holds carry tape-wide event risk")

    thin = [r for r in rows if r["coverage_pct"] < 100]
    if thin:
        gaps = {}
        for r in thin:
            for pl in r["missing_pillars"]:
                gaps[pl] = gaps.get(pl, 0) + 1
        worst = ", ".join(f"{pl} missing on {n}" for pl, n in
                          sorted(gaps.items(), key=lambda kv: -kv[1]))
        notable.insert(0, f"DEGRADED: {len(thin)} of {len(rows)} rows scored on a partial "
                          f"evidence base ({worst}). Scores are normalised to what was "
                          f"available, so they stay comparable, but conviction is capped.")

    # sector concentration across the top 10 — mirrors check_sector_limit in the trading system
    from collections import Counter
    top = rows[:10]
    conc = Counter(r.get("gics") or "other" for r in top)
    for g, n_ in conc.items():
        if n_ >= 5:
            notable.insert(0, f"CONCENTRATION: {n_} of the top 10 are {g} — this scan is one sector bet")

    meta = dict(data["meta"])
    warns = list(meta.get("data_warnings", []))
    if skipped:
        warns.append("No usable price, dropped from the ranking: " + ", ".join(sorted(skipped)))
    late = meta.get("minutes_late")
    if isnum(late) and late > 45:
        warns.insert(0, f"LATE SCAN: this run finished {late:.0f} minutes after its "
                        f"{meta.get('time','slot')} ET slot. Quotes are from collection time, "
                        f"not the slot time — treat the board as a snapshot of when it ran.")
        meta["stale"] = True
    meta["data_warnings"] = warns
    meta["coverage_avg"] = (round(sum(r["coverage_pct"] for r in rows) / len(rows), 0)
                            if rows else 0)
    meta["dropped"] = sorted(skipped)
    if insider_sig is not None:
        sm = insider_sig.get("_meta") or {}
        meta["insider_signal_meta"] = {
            "as_of": sm.get("as_of"), "n_txns": sm.get("n_txns"),
            "n_symbols": sm.get("n_symbols"), "n_cluster_buy": sm.get("n_cluster_buy"),
            "covered": sorted(tk for tk in data["candidates"]
                              if str(tk).upper() in insider_sig["symbols"]),
        }
        for w in (sm.get("warnings") or []):
            warns.append("INSIDERS: " + str(w))
    if veto_feed is not None:
        vm = veto_feed.get("_meta") or {}
        meta["veto"] = {"applied": vetoed, "checked": len(rows),
                        "feed_counts": vm.get("counts"), "feed_path": vm.get("path"),
                        "feed_as_of": veto_feed.get("as_of")}
    if isinstance(earnings_quality, dict):
        meta["earnings_quality"] = {
            "symbols_with_data": sorted(t for t in earnings_quality
                                        if t in data["candidates"]),
            "keys": list(EARNINGS_QUALITY_KEYS)}
    # Which commit of the engine produced this scan. Written by the clone step as
    # $SCAN_DIR/engine_sha; None when the engine was not run from a repo.
    meta["engine_sha"] = config.engine_sha()
    # P-02: only when the staged file was there — an absent key is "not staged", and the
    # golden output of a run without it must not move.
    if filings_meta is not None:
        meta["filings_signal"] = filings_meta

    return {"meta": meta, "ipo": data.get("ipo"), "insider_panel": data.get("insider"),
            "sector_concentration": dict(conc),
            "regime": {"label": regime_label, "multiplier": mult, "notes": regime_notes,
                       **data["regime"]},
            "results": rows, "tape": tape, "notable": notable}

if __name__ == "__main__":
    import shutil
    import archive
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "scan_data.json")
    # Every optional staged file lives in the run directory ($SCAN_DIR); each is None when
    # the scheduled task did not stage it, and the rows then carry none of its features.
    # Absent means "not run", not "clean".
    # P-01: insiders_signal.json.  P-02: filings_signal.json.
    staged_insiders = load_insider_signal(BASE)
    staged_filings = filings.load_staged(BASE)
    # P-03: veto.json.  P-06: earnings_quality.json.
    feed = veto_mod.load(BASE)
    eq_path = os.path.join(BASE, EARNINGS_QUALITY_FILE)
    eq_map = None
    if os.path.exists(eq_path):
        try:
            eq_map = json.load(open(eq_path, encoding="utf-8"))
            eq_map = {str(k).upper(): v for k, v in eq_map.items()
                      if isinstance(v, dict)} if isinstance(eq_map, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            print(f"note: {EARNINGS_QUALITY_FILE} unreadable ({exc}) — earnings-quality "
                  "features not attached", file=sys.stderr)
    data = json.load(open(src, encoding="utf-8"))
    # Every run owns its own file names. The unstamped scan_results.json stays as the
    # "latest" copy the rest of the pipeline reads; the stamped copy is the one that is
    # still here after the next slot runs. The input is snapshotted too, so a board can
    # be re-derived from exactly what it was scored on. The id is fixed before scoring so
    # the memo calibration rows (P-07) carry it.
    scan_date_of(data["meta"])
    rid = archive.run_id(data["meta"])
    data["meta"]["run_id"] = rid
    # BASE is the run dir: memos/<SYMBOL>.json + news_payload.json are read from it (P-07).
    out = scan(data, insider_signal=staged_insiders, filings_signal=staged_filings,
               veto_feed=feed, earnings_quality=eq_map, run_dir=BASE)
    out["meta"]["run_id"] = rid
    fn = archive.files_for(rid)
    json.dump(out, open(os.path.join(BASE, "scan_results.json"), "w", encoding="utf-8"), indent=2)
    json.dump(out, open(os.path.join(BASE, fn["results"]), "w", encoding="utf-8"), indent=2)
    if os.path.abspath(src) != os.path.abspath(os.path.join(BASE, fn["data"])):
        shutil.copyfile(src, os.path.join(BASE, fn["data"]))
    # S-01: the raw inputs, as scored, into $SCAN_DIR/archive/scan_snapshot/. It reads the
    # scan_results.json just written (rows, time, engine_sha), so it runs after that dump
    # and the results are re-written once more to carry the snapshot's path — or the
    # warning. Non-fatal by rule: a board is worth more than its archive, but a failure is
    # recorded in the results meta so the record says the snapshot is missing rather than
    # nothing at all.
    try:
        import snapshots
        snap_path, snap_n = snapshots.write_scan_snapshot(
            BASE, os.path.join(BASE, "archive"),
            {"run_id": rid, "slot": out["meta"].get("slot") or out["meta"].get("session")})
        out["meta"]["scan_snapshot"] = os.path.relpath(snap_path, BASE)
        snap_note = f"snapshot {out['meta']['scan_snapshot']} ({snap_n} rows)"
    except Exception as exc:                      # noqa: BLE001 — never fail the scan
        warn = f"scan snapshot NOT written: {type(exc).__name__}: {exc}"
        out["meta"].setdefault("data_warnings", []).append(warn)
        snap_note = warn
    for name in ("scan_results.json", fn["results"]):
        json.dump(out, open(os.path.join(BASE, name), "w", encoding="utf-8"), indent=2)
    print(f"RUN {rid}  ->  {fn['results']} + {fn['data']}")
    print(snap_note)
    # One line per optional source, staged or not, so the run log says what was looked at.
    im = out["meta"].get("insider_signal_meta")
    if im:
        print(f"INSIDERS (E15, not scored): {len(im['covered'])} of {len(out['results'])} rows covered, "
              f"{im.get('n_cluster_buy')} cluster buy(s) across {im.get('n_symbols')} symbol(s) as of {im.get('as_of')}")
    else:
        print("INSIDERS: insiders_signal.json not staged — no insider features attached")
    fm = out["meta"].get("filings_signal")
    if fm:
        print(f"FILINGS (E16, not scored): {fm['n_matched']} of {len(out['results'])} rows carry a "
              f"10-K/10-Q change score, {fm['n_changers']} changer(s) at threshold {fm['threshold']}")
    else:
        print("FILINGS: filings_signal.json not staged — no filing features attached")
    em = out["meta"].get("earnings_quality")
    if em:
        print(f"EARNINGS QUALITY (E22/E23, not scored): {len(em['symbols_with_data'])} of "
              f"{len(out['results'])} rows carry {', '.join(em['keys'])}")
    else:
        print(f"EARNINGS QUALITY: {EARNINGS_QUALITY_FILE} not staged — no earnings-quality features attached")
    if feed is not None:
        print(f"VETO FEED: {feed['_meta']['counts']} — overrode "
              f"{len(out['meta']['veto']['applied'])} row(s): "
              f"{', '.join(out['meta']['veto']['applied']) or 'none'}")
    else:
        print("VETO FEED: not staged — no override applied")
    mm = out["meta"].get("memos")
    if mm:
        print(f"MEMOS (P-07, not scored): {len(mm['accepted'])} accepted ({', '.join(mm['accepted']) or '-'}), "
              f"{mm['rejected']} rejected; calibration advisory={mm.get('advisory', True)}")
        for sym, errs in sorted((out["meta"].get("memo_rejections") or {}).items()):
            print(f"  ! {sym}: " + "; ".join(errs))
    else:
        print("MEMOS: memos/ not staged — no llm_* features attached")
    print()
    print(f"REGIME: {out['regime']['label']} (x{out['regime']['multiplier']})   "
          f"coverage avg {out['meta']['coverage_avg']:.0f}%\n")
    print(f"{'#':<3}{'TKR':<7}{'SCORE':>6}  {'VERDICT':<12}{'SETUP':<22}{'T/M/F/C/I':<16}"
          f"{'COV':>5}  {'CONF':<11}{'UPSIDE':>8}")
    print("-"*90)
    for i, r in enumerate(out["results"], 1):
        p = r["pillars"]
        pil = f"{p['trend']:.0f}/{p['momentum']:.0f}/{p['fundamentals']:.0f}/{p['catalyst']:.0f}/{p['intelligence']:.0f}"
        up = f"{r['upside_pct']:+.1f}%" if r["upside_pct"] is not None else "  n/a"
        print(f"{i:<3}{r['ticker']:<7}{r['score']:>6.1f}  {r['verdict']:<12}{r['setup']:<22}"
              f"{pil:<16}{r['coverage_pct']:>4.0f}%  {r['confidence']:<11}{up:>8}")
    if out["meta"].get("dropped"):
        print("\nDROPPED (no usable price):", ", ".join(out["meta"]["dropped"]))
    print()
    if out["notable"]:
        print("NOTABLE (push-worthy):")
        for n in out["notable"]:
            print("  \u2022", n)
    else:
        print("NOTABLE: nothing \u2014 quiet scan, no push warranted.")
