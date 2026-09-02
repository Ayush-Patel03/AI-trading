"""Render scan_results.json into the Scan Desk dashboard artifact."""
import json, html, os, sys, traceback

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

R = json.load(open(os.path.join(BASE, "scan_results.json")))
res, reg, meta = R["results"], R["regime"], R["meta"]
tape, notable = R.get("tape", []), R.get("notable", [])
ipo, insider = R.get("ipo") or {}, R.get("insider_panel") or {}

if not res:
    sys.exit("REFUSING TO RENDER: scan_results.json has no ranked rows. "
             "Fix the upstream collection rather than publishing an empty board.")

import render_portfolio
PORTF = ""
_pf_path = os.path.join(BASE, "portfolio_state.json")
if os.path.exists(_pf_path):
    # A broken portfolio panel used to vanish silently, which looks identical to
    # "no positions". Never swallow this one.
    try:
        PORTF = render_portfolio.build(json.load(open(_pf_path)))
    except Exception:
        traceback.print_exc()
        sys.exit("REFUSING TO RENDER: portfolio_state.json exists but the portfolio "
                 "panel failed to build. Publishing without it would hide your book.")
e = html.escape
DASH = "—"

# The pillar bar carries only the four QUANTITATIVE pillars — validated for CVD on
# adjacent pairs. Intelligence is faceted into its own meter rather than becoming a
# fifth hue, which fails colourblind separation at five categorical slots.
PILLARS = [("trend","Trend & Structure",25),("momentum","Momentum & Position",15),
           ("fundamentals","Fundamentals",20),("catalyst","Catalyst & Analyst",20)]
INTEL = ("intelligence","Market Intelligence",20)
VC = {"Strong Buy":"v-strong","Buy":"v-buy","Watch":"v-watch","Hold":"v-hold","Avoid":"v-avoid"}
SC = {"Momentum":"s-mom","Pullback in Uptrend":"s-pull","Early Recovery":"s-rec",
      "Broken Trend":"s-broken","Neutral":"s-neu","Unclassified":"s-neu"}
CONF = {"confirmed":("c-ok","2 sources"),"loose":("c-warn","approx"),
        "conflict":("c-bad","conflict"),"single":("c-single","1 source")}

def num(v,d=2,suf="",plus=False):
    if v is None: return '<span class="na">&mdash;</span>'
    return f'{v:+.{d}f}{suf}' if plus else f'{v:,.{d}f}{suf}'
def sgn(v): return "" if v is None else (" pos" if v>0 else (" neg" if v<0 else ""))

def pillar_bar(p):
    segs=""
    for k,lbl,mx in PILLARS:
        got=p.get(k,0)
        segs+=(f'<span class="pseg pseg-{k}" style="flex:{mx}" title="{lbl}: {got:.0f} of {mx}">'
               f'<span class="pfill" style="width:{got/mx*100:.1f}%"></span></span>')
    return f'<span class="pbar">{segs}</span>'

def intel_meter(p):
    k,lbl,mx=INTEL; got=p.get(k,0); pctv=got/mx*100
    lv = "hi" if pctv>=60 else ("mid" if pctv>=30 else "lo")
    return (f'<span class="intel intel-{lv}" title="{lbl}: {got:.0f} of {mx}">'
            f'<span class="ibar"><span class="ifill" style="height:{pctv:.0f}%"></span></span>'
            f'<span class="ival">{got:.0f}</span></span>')

def spark(trail, delta):
    if not trail: return '<span class="trailcol"></span>'
    if len(trail)==1:
        return ('<span class="trailcol"><svg class="spk" viewBox="0 0 56 20" aria-hidden="true">'
                '<circle cx="50" cy="10" r="3.2" class="spk-end"/></svg>'
                '<span class="delta first">1st</span></span>')
    lo,hi=min(trail),max(trail); rng=(hi-lo) or 1; n=len(trail)
    pts=[(6+i*(44/(n-1)), 16-(v-lo)/rng*12) for i,v in enumerate(trail)]
    d=" ".join(f"{x:.1f},{y:.1f}" for x,y in pts); ex,ey=pts[-1]
    cls="pos" if (delta or 0)>0 else ("neg" if (delta or 0)<0 else "flat")
    return (f'<span class="trailcol" title="Score across today\'s scans: {" &rarr; ".join(f"{v:.0f}" for v in trail)}">'
            f'<svg class="spk spk-{cls}" viewBox="0 0 56 20" aria-hidden="true">'
            f'<polyline points="{d}" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="3.2" class="spk-end"/></svg>'
            f'<span class="delta {cls}">{delta:+.0f}</span></span>')

def earn_card(r):
    """An earnings date is a date. An implied move is what the option market thinks the
    date is worth — that is the number that decides position size, so it gets its own card."""
    d, im = r.get("next_earnings"), r.get("implied_move_pct")
    if not d:
        return ""
    when = {"am": "before the open", "pm": "after the close"}.get(
        (r.get("earnings_timing") or "").lower(), "")
    px = r.get("price")
    band = ""
    if isnum(im) and isnum(px):
        band = (f'<p class="ebody">Options price a <b>&plusmn;{im:.1f}%</b> move '
                f'&mdash; roughly <b>${px*(1-im/100):,.2f} to ${px*(1+im/100):,.2f}</b>. '
                f'That is the size question, not the score question.</p>')
    elif isnum(im):
        band = f'<p class="ebody">Options price a <b>&plusmn;{im:.1f}%</b> move.</p>'
    lvl = "e-hot" if (isnum(im) and im >= 8) else "e-warn"
    return (f'<div class="ecard {lvl}"><h4>Earnings {e(str(d))}'
            + (f' <span class="ewhen">{when}</span>' if when else "") + '</h4>' + band
            + '<p class="enote">A multi-day swing screen cannot hold a binary event on '
              'full size. Verify the date before acting on it.</p></div>')

def covchip(r):
    """Only shown when the row was scored on less than the full evidence base."""
    cv = r.get("coverage_pct")
    if not isinstance(cv,(int,float)) or cv >= 100:
        return ""
    miss = ", ".join(r.get("missing_pillars") or []) or "some pillars"
    lvl = "cov-bad" if cv < 70 else "cov-warn"
    return (f'<span class="cov {lvl}" title="Scored on {cv:.0f}% of the evidence base '
            f'&mdash; no data for: {miss}">{cv:.0f}% cov</span>')

def range_bar(r):
    lo,hi,p=r.get("week52_low"),r.get("week52_high"),r["price"]
    if not(lo and hi and hi>lo): return ""
    pos=lambda v: max(0,min(100,(v-lo)/(hi-lo)*100))
    ticks="".join(f'<span class="tk tk-{c}" style="left:{pos(v):.2f}%" title="{n}: ${v:,.2f}"></span>'
                  for v,c,n in ((r.get("ma_200"),"200","200-day MA"),(r.get("ma_50"),"50","50-day MA")) if v)
    return (f'<div class="rangewrap"><div class="rangebar"><span class="rfill" style="width:{pos(p):.2f}%"></span>'
            f'{ticks}<span class="rdot" style="left:{pos(p):.2f}%" title="Current: ${p:,.2f}"></span></div>'
            f'<div class="rlabels"><span>${lo:,.2f}</span><span class="rmid">52-week range</span>'
            f'<span>${hi:,.2f}</span></div></div>')

# ---------- sector rotation ----------
def isnum(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool)
etfs = {k:v for k,v in (reg.get("sector_etfs") or {}).items()
        if isinstance(v,dict) and isnum(v.get("day"))}
srt = sorted(etfs.items(), key=lambda kv: kv[1]["day"], reverse=True)
mx_day = max((abs(v["day"]) for _,v in srt), default=0) or 1
sec_rows=""
for tk,v in srt:
    w = abs(v["day"])/mx_day*50
    side = "pos" if v["day"]>0 else "neg"
    off = 50 if v["day"]>0 else 50-w
    ytd = v.get("ytd")
    ytd_html = (f'<span class="sytd{sgn(ytd)}">{ytd:+.1f}%</span>' if isnum(ytd)
                else f'<span class="sytd"><span class="na">{DASH}</span></span>')
    sec_rows+=(f'<div class="secrow"><span class="sname">{e(str(v.get("name") or tk))}</span>'
               f'<span class="sbarwrap"><span class="szero"></span>'
               f'<span class="sbar {side}" style="left:{off:.2f}%;width:{w:.2f}%"></span></span>'
               f'<span class="sday {side}">{v["day"]:+.2f}%</span>{ytd_html}</div>')
if not sec_rows:
    sec_rows = ('<p class="empty">Sector rotation unavailable this run &mdash; the 11-ETF '
                'compare page did not answer.</p>')

# ---------- rows ----------
rows=[]
for i,r in enumerate(res,1):
    rid=f"row{i}"
    allp = PILLARS+[INTEL]
    reasons="".join(
        f'<div class="rz"><h4><span class="dot d-{k}"></span>{lbl}<em>{r["pillars"].get(k,0):.0f}<span>/{mx}</span></em></h4>'
        f'<ul>'+"".join(f"<li>{e(str(x))}</li>" for x in (r["reasons"].get(k) or []))+'</ul></div>'
        for k,lbl,mx in allp)
    cats="".join(f"<li>{e(str(c))}</li>" for c in (r.get("catalysts") or []))
    t=r.get("transcript")
    tr=""
    if t:
        ap="".join(f"<li>{e(str(x))}</li>" for x in (t.get("analyst_pressure") or []))
        tr=(f'<div class="tcard"><h4>Earnings call &middot; {e(t.get("quarter",""))} '
            f'<span class="tsig ts-{e(t.get("signal","neutral"))}">{e(t.get("signal","")).upper()}</span></h4>'
            f'<p class="thead">{e(t.get("headline",""))}</p>'
            f'<p class="tquote">&ldquo;{e(t.get("tone_evidence",""))}&rdquo;</p>'
            f'<p class="trisk"><strong>Risk:</strong> {e(t.get("risk",""))}</p>'
            + (f'<ul class="tap">{ap}</ul>' if ap else "") + '</div>')
    rt=r.get("retail") or {}
    rt_bits=[]
    if rt.get("wsb_mentions") is not None:
        rt_bits.append(f'<div class="kv"><dt>Reddit mentions</dt><dd>{rt["wsb_mentions"]:,}'
                       + (f' <small>from {rt["wsb_mentions_24h_ago"]:,}</small>' if rt.get("wsb_mentions_24h_ago") else "")+'</dd></div>')
    if rt.get("wsb_rank"):
        rt_bits.append(f'<div class="kv"><dt>r/wallstreetbets rank</dt><dd>#{rt["wsb_rank"]}</dd></div>')
    if rt.get("stocktwits_score") is not None:
        rt_bits.append(f'<div class="kv"><dt>StockTwits</dt><dd>{rt["stocktwits_score"]} '
                       f'<small>{e(rt.get("stocktwits_label",""))}</small></dd></div>')
    cc,ct = CONF.get(r.get("confidence","single"),("c-single","1 source"))
    earn=(f'<div class="kv"><dt>Next earnings</dt><dd class="warn">{e(r["next_earnings"])}</dd></div>'
          if r.get("next_earnings") else "")
    rows.append(f"""
<article class="row" data-setup="{e(r['setup'])}" data-gics="{e(str(r.get('gics') or ''))}">
 <button class="rowhead" aria-expanded="false" aria-controls="{rid}">
  <span class="rank">{i}</span>
  <span class="idcol"><span class="tkr">{e(r['ticker'])}</span>
   <span class="nm">{e(r['name'])}</span><span class="sect">{e(str(r.get('industry') or ''))}</span></span>
  <span class="pxcol"><span class="px">${r['price']:,.2f}</span>
   <span class="chg{sgn(r['day_change_pct'])}">{num(r['day_change_pct'],2,'%',True)}</span>
   <span class="conf {cc}" title="{e(r.get('confidence_note',''))}">{ct}</span></span>
  <span class="setup {SC.get(r['setup'],'s-neu')}">{e(r['setup'])}</span>
  {pillar_bar(r['pillars'])}
  {intel_meter(r['pillars'])}
  {spark(r.get('score_trail',[]), r.get('score_delta'))}
  <span class="scorecol"><span class="score">{r['score']:.0f}</span>
   <span class="verdict {VC.get(r['verdict'],'v-hold')}">{e(r['verdict'])}</span>
   {covchip(r)}</span>
  <span class="chev" aria-hidden="true"></span>
 </button>
 <div class="detail" id="{rid}" hidden>
  <p class="thesis">{e(r['setup_note'])}. <span class="sep">{e(r['verdict_note'])}.</span></p>
  <p class="confnote {cc}">{e(r.get('confidence_note',''))}</p>
  {range_bar(r)}
  <div class="metrics">
   <div class="kv"><dt>Sector / GICS</dt><dd class="sm">{e(str(r.get('sector') or DASH))} <small>{e(str(r.get('gics') or ''))}</small></dd></div>
   <div class="kv"><dt>SIC</dt><dd>{e(str(r.get('sic') or DASH))}</dd></div>
   <div class="kv"><dt>vs 50-day MA</dt><dd class="{'pos' if (r.get('vs_ma50_pct') or 0)>0 else 'neg'}">{num(r.get('vs_ma50_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>vs 200-day MA</dt><dd class="{'pos' if (r.get('vs_ma200_pct') or 0)>0 else 'neg'}">{num(r.get('vs_ma200_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>52-week change</dt><dd>{num(r.get('week52_change_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>Off 52-wk high</dt><dd>{num(r.get('off_high_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>Analyst target</dt><dd>{num(r.get('analyst_target'),2)} <small>({e(str(r.get('analyst_rating')))})</small></dd></div>
   <div class="kv"><dt>Upside to target</dt><dd class="{'pos' if (r.get('upside_pct') or 0)>0 else 'neg'}">{num(r.get('upside_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>Forward P/E</dt><dd>{num(r.get('forward_pe'),1)}</dd></div>
   <div class="kv"><dt>PEG</dt><dd>{num(r.get('peg'),2)}</dd></div>
   <div class="kv"><dt>Profit margin</dt><dd class="{'neg' if (r.get('profit_margin_pct') or 0)<0 else ''}">{num(r.get('profit_margin_pct'),1,'%')}</dd></div>
   <div class="kv"><dt>Return on equity</dt><dd>{num(r.get('roe_pct'),1,'%')}</dd></div>
   <div class="kv"><dt>Debt / equity</dt><dd>{num(r.get('debt_to_equity'),2)}</dd></div>
   <div class="kv"><dt>Short float</dt><dd>{num(r.get('short_float_pct'),1,'%')}</dd></div>
   <div class="kv"><dt>Beta</dt><dd>{num(r.get('beta'),2)}</dd></div>
   <div class="kv"><dt>Rel. volume</dt><dd>{num(r.get('rel_volume'),2,'x')}</dd></div>
   <div class="kv"><dt>RSI (14d)</dt><dd class="{'neg' if (r.get('rsi_14') or 50)>70 else ('pos' if 30<=(r.get('rsi_14') or 50)<=55 else '')}">{num(r.get('rsi_14'),0)}</dd></div>
   <div class="kv"><dt>ATR (14d)</dt><dd>{num(r.get('atr_14'),2)}{(' <small>' + f"{r['atr_pct']:.1f}% of price" + '</small>') if isnum(r.get('atr_pct')) else ''}</dd></div>
   <div class="kv"><dt>Gap at open</dt><dd class="{sgn(r.get('gap_pct')).strip()}">{num(r.get('gap_pct'),1,'%',True)}</dd></div>
   <div class="kv"><dt>Evidence coverage</dt><dd class="{'neg' if (r.get('coverage_pct') or 100)<70 else ''}">{num(r.get('coverage_pct'),0,'%')}{(' <small>no ' + e(', '.join(r.get('missing_pillars') or [])) + '</small>') if r.get('missing_pillars') else ''}</dd></div>
   {"".join(rt_bits)}{earn}
  </div>
  {earn_card(r)}
  {tr}
  <div class="reasons">{reasons}</div>
  <div class="cats"><h4>On the tape</h4><ul>{cats}</ul></div>
 </div>
</article>""")

legend="".join(f'<span class="lg"><span class="dot d-{k}"></span>{lbl}<em>/{mx}</em></span>'
               for k,lbl,mx in PILLARS)
legend+=f'<span class="lg"><span class="dot d-intelligence"></span>{INTEL[1]}<em>/{INTEL[2]}</em></span>'
cov_avg = meta.get("coverage_avg")
thin = [r for r in res if (r.get("coverage_pct") or 100) < 100]
late_banner = ""
if meta.get("stale"):
    late_banner = ('<div class="banner late"><b>Late scan.</b> This run finished well after its '
                   f'{e(str(meta.get("time","")))} ET slot, so the quotes below are from collection '
                   'time, not slot time.</div>')
cov_banner = ""
if thin:
    miss = {}
    for r in thin:
        for pl in r.get("missing_pillars", []):
            miss[pl] = miss.get(pl, 0) + 1
    detail = ", ".join(f"{k} on {v}" for k, v in sorted(miss.items(), key=lambda kv: -kv[1]))
    cov_banner = ('<div class="banner"><b>Partial evidence base.</b> '
                  f'{len(thin)} of {len(res)} rows are missing at least one pillar ({e(detail)}). '
                  'Scores are normalised to the pillars that had data, so they stay comparable '
                  'with a full scan &mdash; but a row under 70% coverage cannot be rated '
                  'Strong Buy.</div>')

def _stat(big, small):
    return f'<div class="rstat"><b>{big}</b><span>{small}</span></div>'
regime_stats = ""
for key in ("spy", "qqq", "iwm"):
    blk = reg.get(key) or {}
    px, dc = blk.get("price"), blk.get("day_change_pct")
    regime_stats += _stat(f"{px:,.2f}" if isnum(px) else DASH,
                          f"{key.upper()} {dc:+.2f}%" if isnum(dc) else key.upper())
_vix = reg.get("vix")
regime_stats += _stat(f"{_vix:.2f}" if isnum(_vix) else DASH, "VIX")
_a50 = (reg.get("breadth") or {}).get("pct_above_50dma")
regime_stats += _stat(f"{_a50:.0f}%" if isnum(_a50) else DASH, "above 50-DMA")
regime_stats += _stat(f"&times;{reg['multiplier']:.2f}", "score gate")
if isnum(cov_avg):
    regime_stats += _stat(f"{cov_avg:.0f}%", "evidence coverage")
notes="".join(f"<li>{e(str(n))}</li>" for n in (reg.get("notes") or []))
chips="".join(f'<button class="chip" data-filter="{e(s)}">{e(s)}</button>'
              for s in sorted({r["setup"] for r in res}))

SLOTS=[("Pre-market","08:00"),("Opening range","10:00"),("Midday","12:30"),("Power hour","15:00")]
by_slot={s.get("slot"):s for s in tape}; current=tape[-1]["slot"] if tape else None
slot_html=""
for lbl,tm in SLOTS:
    s=by_slot.get(lbl)
    if s:
        # Each slot keeps its own frozen board. Where we know its URL, the tape becomes the
        # index of the day: click 08:00 to see the 08:00 board as it actually stood.
        body=(f'<span class="st">{tm} ET</span>'
              f'<span class="sl">{e(lbl)}</span><span class="sv">{s["avg"]:.0f}</span>'
              f'<span class="sm">avg &middot; top {e(str(s.get("top") or DASH))}</span>'
              f'<span class="sm">{e(s.get("regime_label",""))}</span>')
        url=s.get("artifact_url")
        inner=(f'<a class="slotlink" href="{e(str(url))}" title="Open the {e(lbl)} board">{body}</a>'
               if url else body)
        slot_html+=f'<div class="slot{" now" if lbl==current else ""}">{inner}</div>'
    else:
        slot_html+=(f'<div class="slot pending"><span class="st">{tm} ET</span><span class="sl">{e(lbl)}</span>'
                    f'<span class="sv">&mdash;</span><span class="sm">not yet run</span></div>')
tape_foot=('<div class="notable">'+"".join(f'<span class="nb">{e(n)}</span>' for n in notable)+'</div>'
           if notable else '<p class="quiet">Nothing notable this scan.</p>')

ipo_up="".join(f'<tr><td class="mono">{e(str(x.get("symbol") or DASH))}</td><td>{e(str(x.get("company") or ""))}</td>'
               f'<td class="mono">{e(str(x.get("price_range") or DASH))}</td>'
               f'<td class="mono nw">{e(str(x.get("date") or DASH))}</td>'
               f'<td class="uw">{e(str(x.get("underwriters") or DASH))}</td></tr>' for x in (ipo.get("upcoming") or []))
def money(v):
    return f'${v:,.2f}' if isnum(v) else f'<span class="na">{DASH}</span>'
def pctv(v):
    return f'{v:+.1f}%' if isnum(v) else f'<span class="na">{DASH}</span>'
ipo_rec="".join(f'<tr><td class="mono">{e(str(x.get("symbol") or DASH))}</td>'
                f'<td>{e(str(x.get("company") or ""))}</td>'
                f'<td class="mono">{money(x.get("ipo_price"))}</td>'
                f'<td class="mono{sgn(x.get("return_pct"))}">{pctv(x.get("return_pct"))}</td></tr>'
                for x in (ipo.get("recent") or []))
ipo_notes="".join(f"<li>{e(str(n))}</li>" for n in (ipo.get("notes") or []))
def dollars0(v):
    return f'${abs(v):,.0f}' if isnum(v) else f'<span class="na">{DASH}</span>'
ins_cl="".join(f'<tr><td class="mono">{e(str(x.get("ticker") or DASH))}</td>'
               f'<td>{x.get("count") if x.get("count") is not None else DASH} insiders</td>'
               f'<td class="mono {"neg" if x.get("direction")=="sell" else "pos"}">'
               f'{"SELL" if x.get("direction")=="sell" else "BUY"}</td>'
               f'<td class="mono {"neg" if x.get("direction")=="sell" else "pos"}">{dollars0(x.get("value"))}</td>'
               f'<td class="uw">{e(str(x.get("note") or ""))}</td></tr>' for x in (insider.get("clusters") or []))
ins_buy="".join(f'<tr><td class="mono">{e(str(x.get("ticker") or DASH))}</td>'
                f'<td>{e(str(x.get("insider") or DASH))}<small> {e(str(x.get("title") or ""))}</small></td>'
                f'<td class="mono pos">{dollars0(x.get("value"))}</td>'
                f'<td class="uw">{e(str(x.get("note") or ""))}</td></tr>'
                for x in (insider.get("purchases") or []))
ins_notes="".join(f"<li>{e(str(n))}</li>" for n in (insider.get("notes") or []))
warn="".join(f"<li>{e(str(w))}</li>" for w in (meta.get("data_warnings") or []))
srcs=" &middot; ".join(e(str(x)) for x in (meta.get("sources") or []))
conc=R.get("sector_concentration",{})
conc_html=" &middot; ".join(f"{e(k)} {v}" for k,v in sorted(conc.items(), key=lambda kv:-kv[1]))
avg=sum(r["score"] for r in res)/len(res)

HTML=f"""<title>@@TITLE@@</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{{
 --ground:#FAFAF8;--surface:#FFFFFF;--raise:#F4F4F1;
 --ink:#12171E;--ink-2:#4A5462;--ink-3:#79838F;--line:#E4E4E0;--line-2:#EFEFEB;
 --accent:#8A5B14;--accent-on:#FFFFFF;
 --pos:#2C7A55;--neg:#B23A32;--warn:#9A6B12;
 --p-trend:#007E9E;--p-momentum:#A8701A;--p-fundamentals:#6257B0;--p-catalyst:#BC4F63;
 --p-intelligence:#8A5B14;--track:#E9E9E4;
 --shadow:0 1px 2px rgba(18,23,30,.05),0 8px 24px -12px rgba(18,23,30,.14);
}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{
 --ground:#12171E;--surface:#181E27;--raise:#1E2530;
 --ink:#E8EAED;--ink-2:#A5AFBC;--ink-3:#7A8695;--line:#252D38;--line-2:#1F2731;
 --accent:#D9A24E;--accent-on:#12171E;
 --pos:#4FA97D;--neg:#DE7168;--warn:#D4A03F;
 --p-trend:#2E9EBF;--p-momentum:#C08630;--p-fundamentals:#8B83D8;--p-catalyst:#CF6E7D;
 --p-intelligence:#D9A24E;--track:#232B36;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 28px -14px rgba(0,0,0,.6);
}}}}
:root[data-theme="dark"]{{
 --ground:#12171E;--surface:#181E27;--raise:#1E2530;
 --ink:#E8EAED;--ink-2:#A5AFBC;--ink-3:#7A8695;--line:#252D38;--line-2:#1F2731;
 --accent:#D9A24E;--accent-on:#12171E;
 --pos:#4FA97D;--neg:#DE7168;--warn:#D4A03F;
 --p-trend:#2E9EBF;--p-momentum:#C08630;--p-fundamentals:#8B83D8;--p-catalyst:#CF6E7D;
 --p-intelligence:#D9A24E;--track:#232B36;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 28px -14px rgba(0,0,0,.6);
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--ground);color:var(--ink);
 font:400 15px/1.6 "IBM Plex Sans",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1140px;margin:0 auto;padding:44px 22px 80px;display:flex;flex-direction:column;gap:24px}}
h1,h2,h3,h4{{margin:0;text-wrap:balance}}
.mono,.px,.score,.sv{{font-family:"IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}}
.nw{{white-space:nowrap}}
header.mast{{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;
 padding-bottom:20px;border-bottom:1px solid var(--line)}}
.mast h1{{font-family:"Fraunces",Georgia,serif;font-weight:600;font-size:clamp(30px,4.4vw,44px);letter-spacing:-.02em;line-height:1.02}}
.mast .sub{{color:var(--ink-2);font-size:14px;margin-top:6px;max-width:58ch}}
.stamp{{font-family:"IBM Plex Mono",monospace;font-size:11.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--ink-3);text-align:right;line-height:1.9}}
.stamp b{{color:var(--ink-2);font-weight:500}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}}
.regime-top{{display:flex;flex-wrap:wrap;align-items:center;gap:14px 22px;padding:18px 22px;border-bottom:1px solid var(--line-2);background:var(--raise)}}
.rlabel{{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3)}}
.rverdict{{font-family:"Fraunces",Georgia,serif;font-size:25px;font-weight:600}}
.rstats{{display:flex;gap:20px;margin-left:auto;flex-wrap:wrap}}
.rstat{{display:flex;flex-direction:column;gap:1px}}
.rstat b{{font-family:"IBM Plex Mono",monospace;font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}}
.rstat span{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3)}}
.narr{{padding:15px 22px 0;font-size:14px;color:var(--ink-2);max-width:88ch;line-height:1.65}}
.regime ul{{margin:12px 0 0;padding:0 22px 18px 40px;columns:2;column-gap:34px;font-size:13.5px;color:var(--ink-2)}}
.regime li{{margin-bottom:7px;break-inside:avoid}}
@media(max-width:760px){{.regime ul{{columns:1}}}}
.sec{{padding:16px 22px 18px}}
.sec h2,.panel h2{{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3);font-weight:400;margin-bottom:12px}}
.sechead{{display:grid;grid-template-columns:132px 1fr 64px 64px;gap:12px;font-size:10px;letter-spacing:.07em;
 text-transform:uppercase;color:var(--ink-3);padding-bottom:6px;border-bottom:1px solid var(--line-2);margin-bottom:8px}}
.sechead span:nth-child(3),.sechead span:nth-child(4){{text-align:right}}
.secrow{{display:grid;grid-template-columns:132px 1fr 64px 64px;gap:12px;align-items:center;padding:3px 0}}
.sname{{font-size:12.5px;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.sbarwrap{{position:relative;height:14px}}
.szero{{position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--line)}}
.sbar{{position:absolute;top:2px;height:10px;border-radius:2px}}
.sbar.pos{{background:var(--pos)}} .sbar.neg{{background:var(--neg)}}
.sday,.sytd{{font-family:"IBM Plex Mono",monospace;font-size:12px;text-align:right;font-variant-numeric:tabular-nums}}
.sday.pos,.sytd.pos{{color:var(--pos)}} .sday.neg,.sytd.neg{{color:var(--neg)}}
.sytd{{color:var(--ink-3)}}
.slots{{display:flex;gap:0;overflow-x:auto}}
.slot{{flex:1 0 auto;min-width:130px;padding:0 16px;border-left:1px solid var(--line-2);display:flex;flex-direction:column;gap:3px}}
.slot:first-child{{border-left:0;padding-left:0}}
.slot .st{{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600}}
.slot .sl{{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3)}}
.slot .sv{{font-size:15px;margin-top:3px}}
.slot .sm{{font-size:11px;color:var(--ink-2)}}
.slot.now .st{{color:var(--accent)}} .pending{{opacity:.42}}
.notable{{display:flex;flex-wrap:wrap;gap:7px;padding-top:12px;margin-top:12px;border-top:1px dashed var(--line)}}
.nb{{font-size:12px;padding:5px 10px;border-radius:99px;background:color-mix(in srgb,var(--accent) 12%,transparent);
 color:var(--accent);border:1px solid color-mix(in srgb,var(--accent) 30%,transparent)}}
.quiet{{font-size:12.5px;color:var(--ink-3);padding-top:12px;margin-top:12px;border-top:1px dashed var(--line)}}
.controls{{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;justify-content:space-between}}
.legend{{display:flex;flex-wrap:wrap;gap:6px 15px;font-size:12px;color:var(--ink-2)}}
.lg{{display:inline-flex;align-items:center;gap:6px}}
.lg em{{color:var(--ink-3);font-style:normal;font-family:"IBM Plex Mono",monospace;font-size:11px;margin-left:2px}}
.dot{{width:9px;height:9px;border-radius:2px;display:inline-block;flex:none}}
.d-trend{{background:var(--p-trend)}} .d-momentum{{background:var(--p-momentum)}}
.d-fundamentals{{background:var(--p-fundamentals)}} .d-catalyst{{background:var(--p-catalyst)}}
.d-intelligence{{background:var(--p-intelligence);border-radius:50%}}
.filters{{display:flex;flex-wrap:wrap;gap:7px}}
.chip{{font:500 12px/1 "IBM Plex Sans",sans-serif;padding:7px 12px;border-radius:99px;cursor:pointer;
 background:var(--surface);color:var(--ink-2);border:1px solid var(--line);transition:.15s}}
.chip:hover{{border-color:var(--accent);color:var(--ink)}}
.chip[aria-pressed="true"]{{background:var(--accent);border-color:var(--accent);color:var(--accent-on)}}
.rows{{display:flex;flex-direction:column;gap:9px}}
.row{{background:var(--surface);border:1px solid var(--line);border-radius:11px;overflow:hidden;box-shadow:var(--shadow);transition:border-color .15s}}
.row:hover{{border-color:var(--ink-3)}} .row[hidden]{{display:none}}
.rowhead{{width:100%;display:grid;align-items:center;gap:13px;padding:15px 18px;cursor:pointer;background:none;border:0;
 text-align:left;color:inherit;font:inherit;
 grid-template-columns:24px minmax(130px,1.1fr) 92px 140px minmax(96px,1fr) 40px 56px 82px 14px}}
.rowhead:focus-visible{{outline:2px solid var(--accent);outline-offset:-3px}}
.rank{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink-3)}}
.idcol{{display:flex;flex-direction:column;gap:1px;min-width:0}}
.tkr{{font-family:"IBM Plex Mono",monospace;font-weight:600;font-size:15px}}
.nm{{font-family:"Fraunces",Georgia,serif;font-size:13px;color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.sect{{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.pxcol{{display:flex;flex-direction:column;gap:2px;text-align:right;align-items:flex-end}}
.px{{font-size:14px;font-weight:500}}
.chg{{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink-3);font-variant-numeric:tabular-nums}}
.chg.pos{{color:var(--pos)}} .chg.neg{{color:var(--neg)}}
.conf{{font-size:9px;letter-spacing:.05em;text-transform:uppercase;padding:1px 4px;border-radius:3px;white-space:nowrap}}
.c-ok{{background:color-mix(in srgb,var(--pos) 14%,transparent);color:var(--pos)}}
.c-warn{{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}}
.c-bad{{background:color-mix(in srgb,var(--neg) 16%,transparent);color:var(--neg)}}
.c-single{{background:var(--raise);color:var(--ink-3)}}
.setup{{font-size:11px;font-weight:500;padding:5px 9px;border-radius:5px;text-align:center;border:1px solid transparent;line-height:1.3}}
.s-mom{{color:var(--p-trend);border-color:color-mix(in srgb,var(--p-trend) 32%,transparent);background:color-mix(in srgb,var(--p-trend) 9%,transparent)}}
.s-pull{{color:var(--p-momentum);border-color:color-mix(in srgb,var(--p-momentum) 34%,transparent);background:color-mix(in srgb,var(--p-momentum) 10%,transparent)}}
.s-rec{{color:var(--p-fundamentals);border-color:color-mix(in srgb,var(--p-fundamentals) 32%,transparent);background:color-mix(in srgb,var(--p-fundamentals) 9%,transparent)}}
.s-broken{{color:var(--neg);border-color:color-mix(in srgb,var(--neg) 34%,transparent);background:color-mix(in srgb,var(--neg) 9%,transparent)}}
.s-neu{{color:var(--ink-3);border-color:var(--line)}}
.pbar{{display:flex;gap:2px;height:13px;width:100%}}
.pseg{{background:var(--track);border-radius:2px;overflow:hidden;display:block}}
.pfill{{display:block;height:100%;border-radius:2px}}
.pseg-trend .pfill{{background:var(--p-trend)}} .pseg-momentum .pfill{{background:var(--p-momentum)}}
.pseg-fundamentals .pfill{{background:var(--p-fundamentals)}} .pseg-catalyst .pfill{{background:var(--p-catalyst)}}
.intel{{display:flex;align-items:center;gap:4px;justify-content:flex-end}}
.ibar{{width:7px;height:26px;background:var(--track);border-radius:2px;display:flex;align-items:flex-end;overflow:hidden}}
.ifill{{width:100%;background:var(--p-intelligence);border-radius:2px}}
.ival{{font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--ink-3);font-variant-numeric:tabular-nums}}
.intel-hi .ival{{color:var(--p-intelligence);font-weight:600}}
.trailcol{{display:flex;flex-direction:column;align-items:center;gap:1px}}
.spk{{width:56px;height:20px;overflow:visible}}
.spk polyline{{stroke:var(--ink-3)}} .spk-end{{fill:var(--ink-3)}}
.spk-pos polyline{{stroke:var(--pos)}} .spk-pos .spk-end{{fill:var(--pos)}}
.spk-neg polyline{{stroke:var(--neg)}} .spk-neg .spk-end{{fill:var(--neg)}}
.delta{{font-family:"IBM Plex Mono",monospace;font-size:10.5px;color:var(--ink-3);font-variant-numeric:tabular-nums}}
.delta.pos{{color:var(--pos)}} .delta.neg{{color:var(--neg)}}
.delta.first{{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase}}
.scorecol{{display:flex;flex-direction:column;align-items:flex-end;gap:3px}}
.cov{{font-family:"IBM Plex Mono",monospace;font-size:9px;letter-spacing:.04em;padding:1px 4px;
 border-radius:3px;white-space:nowrap}}
.cov-warn{{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}}
.cov-bad{{background:color-mix(in srgb,var(--neg) 16%,transparent);color:var(--neg)}}
.banner{{background:color-mix(in srgb,var(--warn) 12%,var(--surface));border:1px solid
 color-mix(in srgb,var(--warn) 40%,var(--line));border-left:3px solid var(--warn);
 border-radius:10px;padding:12px 15px;font-size:13.5px;line-height:1.55;color:var(--ink-2)}}
.banner b{{color:var(--ink)}}
.banner.late{{background:color-mix(in srgb,var(--neg) 10%,var(--surface));
 border-color:color-mix(in srgb,var(--neg) 40%,var(--line));border-left-color:var(--neg)}}
.ribbon{{display:flex;flex-wrap:wrap;align-items:center;gap:7px 13px;padding:9px 14px;
 border:1px solid var(--line);border-radius:9px;background:var(--raise);
 color:var(--ink-2);font-size:12.5px;line-height:1.55}}
.ribbon b{{color:var(--ink);font-weight:600}}
.ribbon a{{color:var(--accent);text-decoration:none;
 border-bottom:1px solid color-mix(in srgb,var(--accent) 40%,transparent)}}
.ribbon a:hover{{border-bottom-color:var(--accent)}}
.ribbon .tag{{font-family:"IBM Plex Mono",monospace;font-size:9.5px;letter-spacing:.09em;
 text-transform:uppercase;padding:2px 8px;border-radius:99px;flex:none;
 background:var(--accent);color:var(--accent-on)}}
.ribbon.live .tag{{background:var(--pos);color:var(--surface)}}
.ribbon.snap{{border-color:color-mix(in srgb,var(--accent) 35%,var(--line))}}
.slot a.slotlink{{color:inherit;text-decoration:none;display:flex;flex-direction:column;gap:3px}}
.slot a.slotlink .sl{{width:fit-content;border-bottom:1px dotted color-mix(in srgb,var(--ink-3) 55%,transparent)}}
.slot a.slotlink .sl::after{{content:" ↗";color:var(--accent);font-size:10px;font-weight:600}}
.slot a.slotlink:hover .sl{{color:var(--accent);border-bottom-color:var(--accent)}}
.slot a.slotlink:focus-visible{{outline:2px solid var(--accent);outline-offset:3px;border-radius:5px}}
.empty{{color:var(--ink-3);font-size:13.5px;padding:10px 0;margin:0}}
.ecard{{border:1px solid var(--line);border-left:3px solid var(--warn);border-radius:9px;
 padding:12px 14px;margin:14px 0;background:color-mix(in srgb,var(--warn) 7%,var(--surface))}}
.ecard.e-hot{{border-left-color:var(--neg);background:color-mix(in srgb,var(--neg) 8%,var(--surface))}}
.ecard h4{{font-size:12.5px;letter-spacing:.03em;text-transform:uppercase;color:var(--ink-2);margin-bottom:6px}}
.ecard .ewhen{{text-transform:none;letter-spacing:0;color:var(--ink-3);font-weight:400}}
.ecard .ebody{{margin:0 0 5px;font-size:14px;color:var(--ink)}}
.ecard .enote{{margin:0;font-size:12.5px;color:var(--ink-3)}}
.score{{font-size:22px;font-weight:600;line-height:1}}
.verdict{{font-size:10.5px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;padding:3px 7px;border-radius:4px;white-space:nowrap}}
.v-strong{{background:var(--pos);color:var(--surface)}}
.v-buy{{background:color-mix(in srgb,var(--pos) 16%,transparent);color:var(--pos);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--pos) 38%,transparent)}}
.v-watch{{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--warn) 38%,transparent)}}
.v-hold{{background:var(--raise);color:var(--ink-3);box-shadow:inset 0 0 0 1px var(--line)}}
.v-avoid{{background:color-mix(in srgb,var(--neg) 16%,transparent);color:var(--neg);box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--neg) 38%,transparent)}}
.chev{{width:8px;height:8px;border-right:1.6px solid var(--ink-3);border-bottom:1.6px solid var(--ink-3);transform:rotate(45deg);transition:transform .2s;justify-self:end;margin-top:-4px}}
.rowhead[aria-expanded="true"] .chev{{transform:rotate(225deg);margin-top:2px}}
@media(max-width:1000px){{
 .rowhead{{grid-template-columns:22px 1fr auto;grid-template-areas:"rank id score" "px px score" "setup setup setup" "bar bar intel" "trail trail trail";gap:9px 12px}}
 .rank{{grid-area:rank}} .idcol{{grid-area:id}} .pxcol{{grid-area:px;flex-direction:row;align-items:center;gap:10px}}
 .setup{{grid-area:setup;justify-self:start}} .pbar{{grid-area:bar}} .intel{{grid-area:intel}}
 .trailcol{{grid-area:trail;flex-direction:row;gap:8px}} .scorecol{{grid-area:score}} .chev{{display:none}}
}}
.detail{{padding:4px 18px 20px;border-top:1px solid var(--line-2);display:flex;flex-direction:column;gap:16px}}
.detail[hidden]{{display:none}}
.thesis{{margin:16px 0 0;font-family:"Fraunces",Georgia,serif;font-size:16px;line-height:1.55;max-width:74ch}}
.thesis .sep{{color:var(--ink-2)}}
.confnote{{margin:0;font-size:12px;padding:6px 10px;border-radius:6px;display:inline-block;width:fit-content}}
.rangewrap{{max-width:560px;width:100%}}
.rangebar{{position:relative;height:7px;background:var(--track);border-radius:99px}}
.rfill{{position:absolute;left:0;top:0;height:100%;border-radius:99px;background:color-mix(in srgb,var(--accent) 55%,transparent)}}
.tk{{position:absolute;top:-4px;width:1.5px;height:15px;border-radius:1px}}
.tk-50{{background:var(--p-momentum)}} .tk-200{{background:var(--p-fundamentals)}}
.rdot{{position:absolute;top:50%;width:11px;height:11px;border-radius:50%;background:var(--ink);transform:translate(-50%,-50%);box-shadow:0 0 0 2.5px var(--surface)}}
.rlabels{{display:flex;justify-content:space-between;margin-top:7px;font-family:"IBM Plex Mono",monospace;font-size:11px;color:var(--ink-3)}}
.rmid{{letter-spacing:.08em;text-transform:uppercase;font-size:10px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fill,minmax(158px,1fr));gap:1px;background:var(--line-2);border:1px solid var(--line-2);border-radius:8px;overflow:hidden}}
.kv{{background:var(--surface);padding:10px 12px;display:flex;flex-direction:column;gap:2px}}
.kv dt{{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3)}}
.kv dd{{margin:0;font-family:"IBM Plex Mono",monospace;font-size:14px;font-weight:500;font-variant-numeric:tabular-nums}}
.kv dd.sm{{font-size:12px}}
.kv dd small{{font-family:"IBM Plex Sans",sans-serif;font-size:11px;color:var(--ink-3);font-weight:400}}
.kv dd.pos{{color:var(--pos)}} .kv dd.neg{{color:var(--neg)}} .kv dd.warn{{color:var(--warn)}}
.na{{color:var(--ink-3)}}
.tcard{{background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:14px 16px}}
.tcard h4{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-2);display:flex;align-items:center;gap:9px;margin-bottom:9px}}
.tsig{{font-size:9.5px;padding:2px 6px;border-radius:3px;letter-spacing:.06em}}
.ts-bullish{{background:color-mix(in srgb,var(--pos) 18%,transparent);color:var(--pos)}}
.ts-bearish{{background:color-mix(in srgb,var(--neg) 18%,transparent);color:var(--neg)}}
.ts-neutral{{background:var(--track);color:var(--ink-3)}}
.thead{{margin:0 0 8px;font-size:13.5px;color:var(--ink)}}
.tquote{{margin:0 0 8px;font-family:"Fraunces",Georgia,serif;font-size:14px;font-style:italic;color:var(--ink-2);border-left:2px solid var(--accent);padding-left:11px}}
.trisk{{margin:0 0 8px;font-size:12.5px;color:var(--ink-2)}}
.tap{{margin:0;padding-left:16px;font-size:12.5px;color:var(--ink-2)}}
.reasons{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}}
.rz h4{{display:flex;align-items:center;gap:7px;font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-2);font-weight:600;margin-bottom:7px}}
.rz h4 em{{margin-left:auto;font-style:normal;font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink)}}
.rz h4 em span{{color:var(--ink-3)}}
.rz ul,.cats ul{{margin:0;padding-left:16px;font-size:12.5px;color:var(--ink-2);line-height:1.55}}
.rz li,.cats li{{margin-bottom:4px}}
.cats{{border-top:1px dashed var(--line);padding-top:14px}}
.cats h4{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-2);margin-bottom:7px}}
.panels{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}}
.panel{{padding:16px 20px 18px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{text-align:left;font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-3);font-weight:500;padding:0 8px 6px 0;border-bottom:1px solid var(--line-2)}}
td{{padding:6px 8px 6px 0;border-bottom:1px solid var(--line-2);color:var(--ink-2);vertical-align:top}}
td.mono{{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums;color:var(--ink)}}
td.pos{{color:var(--pos)}} td.neg{{color:var(--neg)}}
td.uw{{font-size:11px;color:var(--ink-3)}}
td small{{display:block;font-size:10.5px;color:var(--ink-3)}}
.tablewrap{{overflow-x:auto}}
.pnotes{{margin:12px 0 0;padding-left:16px;font-size:11.5px;color:var(--ink-3);line-height:1.55}}
.pnotes li{{margin-bottom:4px}}
.subh{{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-2);margin:16px 0 8px;font-weight:600}}
.pfsec .rstats.pfstats{{margin-left:0;gap:26px;padding-bottom:4px}}
.empty{{margin:0;font-size:13px;color:var(--ink-3);background:var(--raise);border:1px dashed var(--line);
 border-radius:8px;padding:14px 16px;max-width:78ch;line-height:1.6}}
.hstat{{font-size:10px;font-weight:600;letter-spacing:.05em;text-transform:uppercase;padding:2px 6px;border-radius:3px}}
.h-hold{{background:color-mix(in srgb,var(--pos) 15%,transparent);color:var(--pos)}}
.h-trim{{background:color-mix(in srgb,var(--warn) 16%,transparent);color:var(--warn)}}
.h-exit{{background:color-mix(in srgb,var(--neg) 16%,transparent);color:var(--neg)}}
.h-unk{{background:var(--raise);color:var(--ink-3)}}
.tickets{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
.ticket{{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:var(--surface)}}
.tk-ready{{border-left:3px solid var(--pos)}}
.tk-blocked{{border-left:3px solid var(--neg);opacity:.72}}
.tkhead{{display:flex;align-items:baseline;gap:8px;margin-bottom:11px}}
.tktkr{{font-family:"IBM Plex Mono",monospace;font-weight:600;font-size:16px}}
.tkname{{font-family:"Fraunces",Georgia,serif;font-size:12.5px;color:var(--ink-3);flex:1;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.tkbadge{{font-size:9.5px;font-weight:600;letter-spacing:.06em;padding:2px 6px;border-radius:3px}}
.tk-ready .tkbadge{{background:color-mix(in srgb,var(--pos) 16%,transparent);color:var(--pos)}}
.tk-blocked .tkbadge{{background:color-mix(in srgb,var(--neg) 16%,transparent);color:var(--neg)}}
.tkgrid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(88px,1fr));gap:10px 12px;margin-bottom:11px}}
.tkv dt{{font-size:9.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin-bottom:2px}}
.tkv dd{{margin:0;font-family:"IBM Plex Mono",monospace;font-size:13px;font-weight:500;font-variant-numeric:tabular-nums}}
.tkv dd.big{{font-size:19px;font-weight:600}}
.tkv dd.pos{{color:var(--pos)}} .tkv dd.neg{{color:var(--neg)}} .tkv dd.warn{{color:var(--warn)}}
.tkv dd small{{display:block;font-family:"IBM Plex Sans",sans-serif;font-size:10px;color:var(--ink-3);font-weight:400}}
.tkstop{{margin:0 0 5px;font-size:11.5px;color:var(--ink-2);line-height:1.5}}
.tkstop strong{{color:var(--ink)}}
.tkwarn{{margin:9px 0 0;padding-left:15px;font-size:11.5px;color:var(--neg);line-height:1.5}}
.pfnote{{margin:16px 0 0;font-size:11.5px;color:var(--ink-3);line-height:1.65;max-width:98ch}}
.pfnote strong{{color:var(--ink-2)}}
footer{{border-top:1px solid var(--line);padding-top:20px;font-size:12.5px;color:var(--ink-3);display:flex;flex-direction:column;gap:10px;line-height:1.65}}
footer strong{{color:var(--ink-2)}}
footer code{{font-family:"IBM Plex Mono",monospace;font-size:11.5px;background:var(--raise);padding:1px 5px;border-radius:3px;color:var(--ink-2)}}
footer ul{{margin:0;padding-left:18px}}
.srcline{{font-size:11.5px;color:var(--ink-3);font-family:"IBM Plex Mono",monospace}}
@media (prefers-reduced-motion:reduce){{*{{transition:none!important;animation:none!important}}}}
</style>

<div class="wrap">
<header class="mast">
 <div><h1>Scan Desk</h1>
  <p class="sub">Multi-source equity scan &mdash; trend, momentum, fundamentals, catalysts and market
   intelligence, mapped to GICS sectors and gated on the market regime.</p></div>
 <div class="stamp">Scan <b>{e(meta['scan_date'])}</b><br>{e(str(meta.get('slot','')))} &middot; {len(res)} names<br>Avg <b>{avg:.0f}</b>{f" &middot; cov <b>{cov_avg:.0f}%</b>" if isnum(cov_avg) else ""}</div>
</header>

@@RIBBON@@
{late_banner}{cov_banner}
<section class="card regime">
 <div class="regime-top">
  <div><div class="rlabel">Market regime</div><div class="rverdict">{e(reg['label'])}</div></div>
  <div class="rstats">{regime_stats}</div>
 </div>
 <p class="narr">{e(reg.get('narrative',''))}</p>
 <ul>{notes}</ul>
</section>

<section class="card sec">
 <h2>Sector rotation &middot; all 11 GICS sectors</h2>
 <div class="sechead"><span>Sector</span><span>Today</span><span>Day</span><span>YTD</span></div>
 {sec_rows}
 <p class="pnotes" style="padding-left:0;list-style:none">Concentration in the top 10: {conc_html}</p>
</section>

<section class="card sec">
 <h2>Intraday tape &middot; {e(meta['scan_date'])}</h2>
 <div class="slots">{slot_html}</div>
 {tape_foot}
</section>

<div class="controls">
 <div class="legend">{legend}</div>
 <div class="filters"><button class="chip" data-filter="all" aria-pressed="true">All setups</button>{chips}</div>
</div>

<main class="rows">{''.join(rows)}</main>

{PORTF}

<div class="panels">
 <section class="card panel">
  <h2>IPO watch</h2>
  <div class="subh">Upcoming</div>
  <div class="tablewrap"><table><thead><tr><th>Sym</th><th>Company</th><th>Range</th><th>Date</th><th>Underwriters</th></tr></thead><tbody>{ipo_up}</tbody></table></div>
  <div class="subh">Recent pricings &mdash; return from offer</div>
  <div class="tablewrap"><table><thead><tr><th>Sym</th><th>Company</th><th>Offer</th><th>Return</th></tr></thead><tbody>{ipo_rec}</tbody></table></div>
  <ul class="pnotes">{ipo_notes}</ul>
 </section>
 <section class="card panel">
  <h2>Insider activity</h2>
  <div class="subh">Clusters &mdash; multiple insiders, same name</div>
  <div class="tablewrap"><table><thead><tr><th>Sym</th><th>Filers</th><th>Side</th><th>Value</th><th></th></tr></thead><tbody>{ins_cl}</tbody></table></div>
  <div class="subh">Notable purchases</div>
  <div class="tablewrap"><table><thead><tr><th>Sym</th><th>Insider</th><th>Value</th><th></th></tr></thead><tbody>{ins_buy}</tbody></table></div>
  <ul class="pnotes">{ins_notes}</ul>
 </section>
</div>

<footer>
 <p><strong>How the score works.</strong> Five pillars &mdash; Trend &amp; Structure (25), Momentum &amp; Position (15),
  Fundamentals (20), Catalyst &amp; Analyst (20), Market Intelligence (20) &mdash; multiplied by the regime gate
  (<code>&times;{reg['multiplier']:.2f}</code> today). The four-segment bar carries the quantitative pillars, each segment
  sized to its maximum and filled to what the name earned. <strong>Market Intelligence sits in its own meter</strong>, not
  as a fifth segment: five categorical hues fail colourblind separation, and it is a different kind of evidence &mdash;
  crowd attention, news flow, earnings-call language and insider filings rather than price and balance-sheet maths.
  A <em>Pullback in Uptrend</em> scores well by design; it is the mean-reversion entry, not a weakness signal.</p>
 <p><strong>Coverage normalisation.</strong> A pillar with no input data at all is dropped from the denominator
  rather than scored zero, so the score is always out of the evidence that actually existed. Without this a dead
  source quietly rescales the whole board &mdash; when the retail and insider feeds went down on 2026-08-28 every
  name lost up to 20 points and nothing could reach Strong Buy. The <em>% cov</em> chip appears on any row scored
  on a partial base, and a row under 70% coverage is capped at Buy: thin evidence is not conviction.</p>
 <p><strong>Price confidence.</strong> Each row is tagged by how many independent sources confirmed its price.
  <em>2 sources</em> means two agreed within 1%. <em>conflict</em> means they differed by more than 3% and the price
  should be verified before trading. This matters: in this run one aggregator's quote pages were stale by a different
  amount for every ticker, from one session to fourteen days.</p>
 <p><strong>Known data limits.</strong></p>
 <ul>{warn}</ul>
 <p><strong>Sources.</strong> <span class="srcline">{srcs}</span></p>
 <p><strong>This is a screen, not advice.</strong> It surfaces names matching a rule set. It does not size positions,
  set stops, or know your existing exposure. Verify every name against live data before trading it.</p>
</footer>
</div>

<script>
document.querySelectorAll(".rowhead").forEach(function(b){{
 b.addEventListener("click",function(){{
  var o=b.getAttribute("aria-expanded")==="true";
  b.setAttribute("aria-expanded",String(!o));
  document.getElementById(b.getAttribute("aria-controls")).hidden=o;
 }});
}});
var chips=document.querySelectorAll(".chip");
chips.forEach(function(c){{
 c.addEventListener("click",function(){{
  chips.forEach(function(x){{x.setAttribute("aria-pressed","false")}});
  c.setAttribute("aria-pressed","true");
  var f=c.dataset.filter;
  document.querySelectorAll(".row").forEach(function(r){{
   r.hidden=!(f==="all"||r.dataset.setup===f);
  }});
 }});
}});
</script>"""
# ---------------------------------------------------------------- publish
# Two files, deliberately. The stamped one is this run's permanent record and is never
# written again; the unstamped one is the rolling board that keeps the bookmarked URL
# current. Before this, the 08:00 board simply ceased to exist at 10:00.
import archive

RID   = meta.get("run_id") or archive.run_id(meta)
LABEL = archive.run_label(meta)
TITLE = archive.title_for(meta)
LIVE  = meta.get("live_board_url") or archive.LIVE_BOARD_URL
TM    = meta.get("time") or ""
_when = f' &middot; {e(str(TM))} ET' if TM else ""

RIBBON_LIVE = (f'<div class="ribbon live"><span class="tag">Live board</span><span>'
               f'Refreshed at every scan &mdash; currently showing <b>{e(LABEL)}</b>{_when}. '
               f'This run also published a frozen copy, <b>{e(TITLE)}</b>, which will not '
               f'change. Completed slots in the tape above link to their own boards.'
               f'</span></div>')
RIBBON_SNAP = (f'<div class="ribbon snap"><span class="tag">Snapshot</span><span>'
               f'Frozen as it stood at <b>{e(LABEL)}</b>{_when}. This board will never '
               f'update. <a href="{e(LIVE)}">Open the live Scan Desk &rarr;</a>'
               f'</span></div>')

def _emit(path, title, ribbon):
    doc = HTML.replace("@@TITLE@@", e(title)).replace("@@RIBBON@@", ribbon)
    if "@@" in doc:
        sys.exit(f"REFUSING TO WRITE {path}: an unsubstituted @@ placeholder survived. "
                 "The template and the publish step have drifted apart.")
    open(path, "w").write(doc)
    return len(doc)

SNAP_PATH   = os.path.join(BASE, archive.files_for(RID)["board"])
LATEST_PATH = os.path.join(BASE, "scan-desk.html")
n1 = _emit(SNAP_PATH, TITLE, RIBBON_SNAP)
n2 = _emit(LATEST_PATH, "Scan Desk", RIBBON_LIVE)

print(f"wrote {n1:,} bytes -> {SNAP_PATH}   [snapshot, publish as a NEW artifact]")
print(f"wrote {n2:,} bytes -> {LATEST_PATH} [rolling latest, publish with url=LIVE BOARD]")
print()
print("PUBLISH, in this order:")
print(f"  1. Artifact(file_path={SNAP_PATH!r},")
print(f"              title={TITLE!r}, favicon='\U0001F4E1')      <- new artifact, keep its URL")
print(f"  2. archive.py --record --history-entry ... --artifact-url <that URL>")
print(f"  3. Artifact(file_path={LATEST_PATH!r}, url={LIVE!r})   <- updates in place")
