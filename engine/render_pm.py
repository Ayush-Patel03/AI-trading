"""render_pm.py — the Trade Desk board.

Renders pm_state.json as a self-contained HTML page for the Artifact tool.
The tool supplies the document skeleton, so this emits <title>, <style> and
body content only — no doctype, no <head>, no <body> wrapper.

Every field is treated as optional. A missing number renders as an em-dash;
a missing collection renders as an empty state with a reason. The 2026-08-31
Scan Desk audit exists because a renderer that raises publishes nothing at
all, and a board that silently stops updating is worse than an ugly one.

Paths resolve from SCAN_DIR, else from this file's own directory.
"""
import json, math, os, sys, html, datetime as dt

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
DASH = "—"

# ---------------------------------------------------------------- formatting
def esc(v):
    return html.escape(str(v), quote=True)

def money(v, dp=2):
    if v is None:
        return DASH
    try:
        return f"${float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return DASH

def signed_money(v, dp=2):
    if v is None:
        return DASH
    try:
        return f"{'+' if float(v) >= 0 else '-'}${abs(float(v)):,.{dp}f}"
    except (TypeError, ValueError):
        return DASH

def pct(v, dp=2, sign=False):
    if v is None:
        return DASH
    try:
        f = float(v)
        return f"{f:+.{dp}f}%" if sign else f"{f:.{dp}f}%"
    except (TypeError, ValueError):
        return DASH

def num(v, dp=4):
    if v is None:
        return DASH
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return DASH

def price(v):
    if v is None:
        return DASH
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return DASH

def tone(v):
    """good / critical / neutral, by sign."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "flat"
    return "up" if f > 0 else ("down" if f < 0 else "flat")


CSS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');

:root {
  --sans: "IBM Plex Sans", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;

  --plane:      #f2f4f6;
  --surface:    #ffffff;
  --surface-2:  #f7f9fa;
  --ink:        #101418;
  --ink-2:      #555f6a;
  --ink-3:      #8a939d;
  --rule:       rgba(16,20,24,0.11);
  --rule-solid: #e3e7ea;

  --accent:     #2a78d6;
  --accent-dim: rgba(42,120,214,0.10);

  --good:     #0ca30c;
  --warning:  #fab219;
  --serious:  #ec835a;
  --critical: #d03b3b;
  --good-ink: #006300;
  --crit-ink: #b02f2f;
  --warn-ink: #8a6100;

  --grid:     #e6eaed;
  --shadow:   0 1px 2px rgba(16,20,24,0.05), 0 8px 24px -12px rgba(16,20,24,0.14);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --plane:      #0d0f12;
    --surface:    #171b1f;
    --surface-2:  #1d2227;
    --ink:        #f2f4f6;
    --ink-2:      #b5bec7;
    --ink-3:      #7d878f;
    --rule:       rgba(255,255,255,0.11);
    --rule-solid: #2a3036;
    --accent:     #3987e5;
    --accent-dim: rgba(57,135,229,0.14);
    --good-ink:   #0ca30c;
    --crit-ink:   #e06a6a;
    --warn-ink:   #fab219;
    --grid:       #262c32;
    --shadow:     0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.6);
  }
}
:root[data-theme="dark"] {
  --plane:      #0d0f12;
  --surface:    #171b1f;
  --surface-2:  #1d2227;
  --ink:        #f2f4f6;
  --ink-2:      #b5bec7;
  --ink-3:      #7d878f;
  --rule:       rgba(255,255,255,0.11);
  --rule-solid: #2a3036;
  --accent:     #3987e5;
  --accent-dim: rgba(57,135,229,0.14);
  --good-ink:   #0ca30c;
  --crit-ink:   #e06a6a;
  --warn-ink:   #fab219;
  --grid:       #262c32;
  --shadow:     0 1px 2px rgba(0,0,0,0.4), 0 8px 24px -12px rgba(0,0,0,0.6);
}

* { box-sizing: border-box; }
body {
  margin: 0; background: var(--plane); color: var(--ink);
  font-family: var(--sans); font-size: 14px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 1240px; margin: 0 auto; padding: 28px 20px 72px; }

/* ---------- masthead ---------- */
.mast { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 16px 24px; margin-bottom: 20px; }
.mast h1 {
  font-size: 19px; font-weight: 600; letter-spacing: -0.01em; margin: 0;
  text-wrap: balance;
}
.mast .sub { color: var(--ink-2); font-size: 13px; margin-top: 2px; }
.mast .spacer { flex: 1 1 40px; }

.badge {
  font-family: var(--mono); font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.09em; text-transform: uppercase;
  padding: 4px 9px; border-radius: 4px; white-space: nowrap;
  border: 1px solid var(--rule); color: var(--ink-2); background: var(--surface);
}
.badge.paper { border-color: var(--warning); color: var(--warn-ink);
               background: color-mix(in srgb, var(--warning) 12%, transparent); }
.badge.live  { border-color: var(--critical); color: var(--crit-ink);
               background: color-mix(in srgb, var(--critical) 12%, transparent); }

/* ---------- ribbon ---------- */
.ribbon {
  display: flex; flex-wrap: wrap; align-items: center; gap: 7px 13px;
  padding: 9px 14px; margin-bottom: 14px; border-radius: 8px;
  border: 1px solid var(--rule); background: var(--surface-2);
  color: var(--ink-2); font-size: 12.5px; line-height: 1.55;
}
.ribbon b { color: var(--ink); font-weight: 600; }
.ribbon a { color: var(--accent); text-decoration: none;
            border-bottom: 1px solid color-mix(in srgb, var(--accent) 40%, transparent); }
.ribbon a:hover { border-bottom-color: var(--accent); }
.ribbon .tag {
  font-family: var(--mono); font-size: 9.5px; font-weight: 600; letter-spacing: 0.09em;
  text-transform: uppercase; padding: 2px 8px; border-radius: 99px; flex: none;
  background: var(--accent); color: #fff;
}
.ribbon.live .tag { background: var(--good); }
.ribbon.snap { border-color: color-mix(in srgb, var(--accent) 35%, var(--rule)); }

/* ---------- banners ---------- */
.banner {
  display: flex; gap: 10px; align-items: flex-start;
  padding: 12px 14px; border-radius: 8px; margin-bottom: 14px;
  border: 1px solid var(--rule); background: var(--surface); box-shadow: var(--shadow);
  border-left-width: 3px;
}
.banner.critical { border-left-color: var(--critical); }
.banner.warning  { border-left-color: var(--warning); }
.banner.info     { border-left-color: var(--accent); }
.banner .icon { font-family: var(--mono); font-weight: 600; flex: none; }
.banner.critical .icon { color: var(--crit-ink); }
.banner.warning .icon  { color: var(--warn-ink); }
.banner.info .icon     { color: var(--accent); }
.banner b { font-weight: 600; }
.banner .body { font-size: 13px; color: var(--ink-2); }
.banner .body b { color: var(--ink); }

/* ---------- stat rail ---------- */
.rail {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr));
  gap: 1px; background: var(--rule-solid); border: 1px solid var(--rule-solid);
  border-radius: 10px; overflow: hidden; margin-bottom: 22px; box-shadow: var(--shadow);
}
.tile { background: var(--surface); padding: 14px 16px 15px; }
.tile .lab {
  font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 600; margin-bottom: 6px;
}
.tile .val { font-family: var(--mono); font-size: 20px; font-weight: 600; letter-spacing: -0.02em; }
.tile.hero { grid-column: span 2; }
.tile.hero .val { font-size: 48px; line-height: 1.05; letter-spacing: -0.035em; }
.tile .delta { font-family: var(--mono); font-size: 12px; margin-top: 4px; color: var(--ink-2); }
.up   { color: var(--good-ink); }
.down { color: var(--crit-ink); }
.flat { color: var(--ink-2); }

/* day-trade pips */
.pips { display: flex; gap: 4px; margin-top: 8px; }
.pip { width: 22px; height: 5px; border-radius: 2px; background: var(--rule-solid); }
.pip.used { background: var(--warning); }
.pip.res  { background: transparent; border: 1px solid var(--warning); }

/* ---------- cards ---------- */
.grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
@media (min-width: 1000px) { .grid.two { grid-template-columns: 1.15fr 1fr; } }
.card {
  background: var(--surface); border: 1px solid var(--rule); border-radius: 10px;
  box-shadow: var(--shadow); overflow: hidden; margin-bottom: 18px;
}
.card > h2 {
  font-size: 12px; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--ink-2); margin: 0; padding: 13px 16px;
  border-bottom: 1px solid var(--rule); display: flex; align-items: center; gap: 10px;
}
.card > h2 .count {
  font-family: var(--mono); font-size: 11px; color: var(--ink-3);
  letter-spacing: 0; text-transform: none;
}
.card > h2 .note {
  margin-left: auto; font-weight: 400; letter-spacing: 0; text-transform: none;
  font-size: 11.5px; color: var(--ink-3);
}
.pad { padding: 14px 16px; }
.empty { padding: 22px 16px; color: var(--ink-3); font-size: 13px; }

/* ---------- tables ---------- */
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th {
  text-align: left; font-size: 10.5px; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--ink-3); font-weight: 600; padding: 9px 12px;
  border-bottom: 1px solid var(--rule); white-space: nowrap; background: var(--surface-2);
}
td { padding: 10px 12px; border-bottom: 1px solid var(--rule); vertical-align: top; }
tr:last-child td { border-bottom: none; }
td.n, th.n { text-align: right; font-family: var(--mono);
             font-variant-numeric: tabular-nums; white-space: nowrap; }
th.n { text-align: right; }
.sym { font-family: var(--mono); font-weight: 600; letter-spacing: -0.01em; }
.sub2 { color: var(--ink-3); font-size: 11.5px; }
.sub2.nw { white-space: nowrap; }
td.st { white-space: nowrap; }
.card.decisions { align-self: start; }
tr.flagged td:first-child { box-shadow: inset 3px 0 0 var(--critical); }
tr.warn td:first-child { box-shadow: inset 3px 0 0 var(--warning); }

/* ---------- chips ---------- */
.chip {
  display: inline-flex; align-items: center; gap: 5px;
  font-family: var(--mono); font-size: 10.5px; font-weight: 500;
  padding: 2px 7px; border-radius: 4px; border: 1px solid var(--rule);
  color: var(--ink-2); background: var(--surface-2); white-space: nowrap;
}
.chip .dot { width: 6px; height: 6px; border-radius: 50%; flex: none; }
.chip.good     { color: var(--good-ink); border-color: color-mix(in srgb, var(--good) 45%, transparent); }
.chip.good .dot     { background: var(--good); }
.chip.critical { color: var(--crit-ink); border-color: color-mix(in srgb, var(--critical) 45%, transparent); }
.chip.critical .dot { background: var(--critical); }
.chip.warning  { color: var(--warn-ink); border-color: color-mix(in srgb, var(--warning) 45%, transparent); }
.chip.warning .dot  { background: var(--warning); }
.chip.accent   { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.chip.accent .dot   { background: var(--accent); }
.chip.mute .dot { background: var(--ink-3); }

/* ---------- decision log ---------- */
.log { list-style: none; margin: 0; padding: 0; }
.log li { padding: 12px 16px; border-bottom: 1px solid var(--rule); }
.log li:last-child { border-bottom: none; }
.log .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.log .why { color: var(--ink-2); font-size: 12.5px; margin-top: 5px; }
.log .why b { color: var(--ink); font-weight: 500; }
.log .n { font-family: var(--mono); font-variant-numeric: tabular-nums;
          font-size: 12px; color: var(--ink-2); margin-left: auto; }

/* ---------- chart ---------- */
.chart { position: relative; padding: 8px 4px 4px; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.axis { fill: var(--ink-3); font-family: var(--mono); font-size: 10px; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.baseline { stroke: var(--rule-solid); stroke-width: 1; }
.eqline { fill: none; stroke: var(--accent); stroke-width: 2;
          stroke-linejoin: round; stroke-linecap: round; }
.eqarea { fill: var(--accent); opacity: 0.10; }
.eqdot  { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
.hair   { stroke: var(--ink-3); stroke-width: 1; opacity: 0; }
.hair.on { opacity: 0.55; }
.hitdot { fill: var(--accent); stroke: var(--surface); stroke-width: 2; opacity: 0; }
.hitdot.on { opacity: 1; }
.tip {
  position: absolute; pointer-events: none; opacity: 0; transform: translate(-50%, -100%);
  background: var(--surface); border: 1px solid var(--rule); border-radius: 7px;
  box-shadow: var(--shadow); padding: 8px 10px; font-size: 12px; white-space: nowrap;
  transition: opacity .1s linear; z-index: 5;
}
.tip.on { opacity: 1; }
.tip .t-v { font-family: var(--mono); font-weight: 600; font-size: 14px; }
.tip .t-l { color: var(--ink-3); font-size: 11px; margin-bottom: 2px; }
.tip .t-r { color: var(--ink-2); font-family: var(--mono); font-size: 11.5px; margin-top: 3px; }
.tip .key { display: inline-block; width: 10px; height: 2px; background: var(--accent);
            vertical-align: middle; margin-right: 5px; }

/* ---------- footer ---------- */
.foot { margin-top: 26px; color: var(--ink-3); font-size: 12px; line-height: 1.65; }
.foot b { color: var(--ink-2); font-weight: 600; }
.foot p { margin: 0 0 8px; max-width: 74ch; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


# ---------------------------------------------------------------- components
def chip(label, kind="mute", dot=True):
    d = '<span class="dot"></span>' if dot else ""
    return f'<span class="chip {kind}">{d}{esc(label)}</span>'


def banner(kind, icon, title, body):
    return (f'<div class="banner {kind}"><span class="icon">{esc(icon)}</span>'
            f'<div class="body"><b>{esc(title)}</b> {body}</div></div>')


def equity_chart(curve, start_equity):
    """One series, so no legend — the card title names it. Crosshair + tooltip."""
    pts = [c for c in (curve or []) if isinstance(c.get("equity"), (int, float))]
    if len(pts) < 2:
        return ('<div class="empty">The curve needs at least two completed runs. '
                f'{len(pts)} recorded so far.</div>')
    W, H = 1000, 240
    PL, PR, PT, PB = 54, 16, 14, 26
    iw, ih = W - PL - PR, H - PT - PB
    vals = [p["equity"] for p in pts]
    if start_equity:
        vals = vals + [float(start_equity)]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        lo, hi = lo - 1, hi + 1
    pad = (hi - lo) * 0.12
    lo, hi = lo - pad, hi + pad

    def x(i):
        return PL + (iw * i / (len(pts) - 1))

    def y(v):
        return PT + ih - (ih * (v - lo) / (hi - lo))

    # clean y ticks — works at any magnitude, including the sub-dollar ranges a $50
    # book actually produces (the naive version collapsed to a single tick there)
    raw = (hi - lo) / 4 or 1
    mag = 10 ** math.floor(math.log10(abs(raw)))
    nice = next((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10 * mag)
    ticks, t = [], math.ceil(lo / nice) * nice
    while t < hi and len(ticks) < 7:
        ticks.append(round(t, 6))
        t += nice
    tick_dp = 0 if nice >= 1 else min(2, max(0, -math.floor(math.log10(nice))))

    grid = "".join(f'<line class="gridline" x1="{PL}" y1="{y(v):.1f}" x2="{W - PR}" '
                   f'y2="{y(v):.1f}"/>' for v in ticks)
    labs = "".join(f'<text class="axis" x="{PL - 8}" y="{y(v) + 3.5:.1f}" '
                   f'text-anchor="end">{money(v, tick_dp)}</text>' for v in ticks)
    base = ""
    if start_equity and lo < float(start_equity) < hi:
        yb = y(float(start_equity))
        base = (f'<line class="baseline" x1="{PL}" y1="{yb:.1f}" x2="{W - PR}" y2="{yb:.1f}"/>'
                f'<text class="axis" x="{W - PR}" y="{yb - 6:.1f}" text-anchor="end">'
                f'start {money(start_equity)}</text>')

    line = " ".join(f"{x(i):.1f},{y(p['equity']):.1f}" for i, p in enumerate(pts))
    area = (f"{PL},{PT + ih} " + line + f" {x(len(pts) - 1):.1f},{PT + ih}")
    last = pts[-1]
    firsts = [i for i, p in enumerate(pts) if i == 0 or p["date"] != pts[i - 1]["date"]]
    if len(firsts) > 8:                       # thin evenly rather than truncating the tail
        keep = {firsts[0], firsts[-1]}
        stride = max(1, round(len(firsts) / 7))
        keep |= {firsts[i] for i in range(0, len(firsts), stride)}
        firsts = sorted(keep)
    xt = [f'<text class="axis" x="{x(i):.1f}" y="{H - 8}" '
          f'text-anchor="{"start" if i == 0 else ("end" if i == len(pts) - 1 else "middle")}">'
          f'{esc(pts[i]["date"][5:])}</text>' for i in firsts]

    data = json.dumps([{"x": round(x(i), 1), "y": round(y(p["equity"]), 1),
                        "e": p["equity"], "c": p.get("cash"), "iv": p.get("invested"),
                        "d": p.get("date"), "s": p.get("slot")} for i, p in enumerate(pts)])
    return f"""<div class="chart" id="eqchart">
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Paper account equity by scan slot">
    {grid}{base}{labs}
    <polygon class="eqarea" points="{area}"/>
    <polyline class="eqline" points="{line}"/>
    <line class="hair" id="eqhair" x1="0" y1="{PT}" x2="0" y2="{PT + ih}"/>
    <circle class="hitdot" id="eqhit" r="4.5" cx="0" cy="0"/>
    <circle class="eqdot" cx="{x(len(pts) - 1):.1f}" cy="{y(last['equity']):.1f}" r="4.5"/>
    {''.join(xt)}
    <rect id="eqsurface" x="{PL}" y="{PT}" width="{iw}" height="{ih}" fill="transparent"/>
  </svg>
  <div class="tip" id="eqtip"></div>
  <script>(function(){{
    var pts={data},sv=document.getElementById('eqchart'),
        hair=document.getElementById('eqhair'),hit=document.getElementById('eqhit'),
        tip=document.getElementById('eqtip'),svg=sv.querySelector('svg'),
        surf=document.getElementById('eqsurface');
    function money(v){{return v==null?'\\u2014':'$'+Number(v).toLocaleString(undefined,
      {{minimumFractionDigits:2,maximumFractionDigits:2}});}}
    function move(ev){{
      var r=svg.getBoundingClientRect(),vx=(ev.clientX-r.left)/r.width*{W},best=0;
      for(var i=1;i<pts.length;i++){{if(Math.abs(pts[i].x-vx)<Math.abs(pts[best].x-vx))best=i;}}
      var p=pts[best];
      hair.setAttribute('x1',p.x);hair.setAttribute('x2',p.x);hair.classList.add('on');
      hit.setAttribute('cx',p.x);hit.setAttribute('cy',p.y);hit.classList.add('on');
      tip.textContent='';
      var l=document.createElement('div');l.className='t-l';
      l.textContent=(p.d||'')+'  '+(p.s||'');tip.appendChild(l);
      var v=document.createElement('div');v.className='t-v';
      var k=document.createElement('span');k.className='key';v.appendChild(k);
      v.appendChild(document.createTextNode(money(p.e)));tip.appendChild(v);
      var s=document.createElement('div');s.className='t-r';
      s.textContent='cash '+money(p.c)+'   in market '+money(p.iv);tip.appendChild(s);
      tip.style.left=(p.x/{W}*r.width)+'px';tip.style.top=(p.y/{H}*r.height-10)+'px';
      tip.classList.add('on');
    }}
    function out(){{hair.classList.remove('on');hit.classList.remove('on');
                   tip.classList.remove('on');}}
    surf.addEventListener('pointermove',move);
    sv.addEventListener('pointerleave',out);
  }})();</script>
</div>"""


# ---------------------------------------------------------------- sections
ACTION_STYLE = {
    "place-buy": ("accent", "Order placed"),
    "fill-buy": ("accent", "Bought"),
    "fill-sell": ("mute", "Sold"),
    "cancel": ("mute", "Cancelled"),
    "expire": ("mute", "Expired"),
}
REASON_STYLE = {
    "stop": ("critical", "Stop"),
    "thesis": ("critical", "Thesis broke"),
    "target": ("good", "Target"),
    "trim": ("warning", "Trim"),
    "rebalance": ("warning", "Rebalance"),
}


def decision_log(jrn):
    rows = []
    for d in jrn.get("decisions", []):
        act = d.get("action", "")
        kind, label = ACTION_STYLE.get(act, ("mute", act))
        rk, rl = REASON_STYLE.get(d.get("reason"), (None, None))
        chips = chip(label, kind)
        if rk:
            chips += " " + chip(rl, rk)
        rows.append(
            f'<li><div class="head"><span class="sym">{esc(d.get("symbol", DASH))}</span>'
            f'{chips}<span class="n">{num(d.get("shares"))} @ {price(d.get("price"))}</span></div>'
            f'<div class="why">{esc(d.get("detail") or d.get("reason") or "")}</div></li>')
    for s in jrn.get("skipped", []):
        sym = s.get("symbol", "*")
        head = ("Book-wide" if sym == "*" else esc(sym))
        rows.append(f'<li><div class="head"><span class="sym">{head}</span>'
                    f'{chip("No action", "mute")}</div>'
                    f'<div class="why">{esc(s.get("reason", ""))}</div></li>')
    if not rows:
        return ('<div class="empty">Nothing to do this slot. The book was read, every '
                'stop was checked, and no rule fired.</div>')
    return f'<ul class="log">{"".join(rows)}</ul>'


def positions_table(state):
    ps = state.get("positions") or []
    if not ps:
        return ('<div class="empty">No open positions. Cash is fully reserved and '
                'every entry has to clear the risk gates before it is placed.</div>')
    unpriced = set(state.get("journal", {}).get("warnings") and [] or [])
    rows = []
    for p in ps:
        px, stop = p.get("price"), p.get("stop")
        flag = ""
        cls = ""
        if px is None:
            cls, flag = "flagged", chip("Unpriced", "critical")
        elif stop and px <= stop * 1.02:
            cls, flag = "warn", chip("Near stop", "warning")
        tc = p.get("trim_count") or 0
        trims = f' {chip(f"{tc} trim" + ("s" if tc > 1 else ""), "warning")}' if tc else ""
        dist = (f"{(px / stop - 1) * 100:.1f}% away" if px and stop else "")
        basis = esc(p.get("stop_basis_short") or "")
        stopsub = " &middot; ".join(x for x in [basis, dist] if x) or DASH
        psrc = p.get("price_source") or ""
        src = (f'<div class="sub2 nw">{esc(psrc)}</div>'
               if "stale" in psrc or psrc == "pm-fetch" else "")
        rows.append(f"""<tr class="{cls}">
  <td><span class="sym">{esc(p.get('symbol', DASH))}</span>
      <div class="sub2">{esc(p.get('setup') or p.get('thesis') or 'no current read')}</div></td>
  <td class="n">{num(p.get('shares'), 6)}</td>
  <td class="n">{price(p.get('avg_cost'))}</td>
  <td class="n">{price(px)}{src}</td>
  <td class="n">{money(p.get('market_value'))}</td>
  <td class="n {tone(p.get('unrealized'))}">{signed_money(p.get('unrealized'))}
      <div class="sub2 {tone(p.get('unrealized'))}">{pct(p.get('unrealized_pct'), 1, True)}</div></td>
  <td class="n">{price(stop)}<div class="sub2 nw">{stopsub}</div></td>
  <td class="n">{price(p.get('target'))}</td>
  <td class="st">{flag or chip('Holding', 'good')}{trims}</td>
</tr>""")
    return f"""<div class="scroll"><table>
<thead><tr><th>Position</th><th class="n">Shares</th><th class="n">Cost</th>
<th class="n">Price</th><th class="n">Value</th><th class="n">Unrealised</th>
<th class="n">Stop</th><th class="n">Target</th><th>State</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


def orders_table(state):
    os_ = state.get("working_orders") or []
    if not os_:
        return '<div class="empty">No working orders.</div>'
    rows = []
    for o in os_:
        m = o.get("meta") or {}
        rows.append(f"""<tr>
  <td><span class="sym">{esc(o.get('symbol', DASH))}</span>
      <div class="sub2 nw">placed {esc(o.get('placed_slot', ''))}</div></td>
  <td>{chip('Buy limit' if o.get('side') == 'buy' else 'Sell limit',
            'accent' if o.get('side') == 'buy' else 'mute')}</td>
  <td class="n">{num(o.get('shares'), 6)}</td>
  <td class="n">{price(o.get('limit_price'))}</td>
  <td class="n">{money(o.get('notional'))}</td>
  <td class="n">{price(m.get('stop'))}</td>
  <td class="n">{price(m.get('target'))}</td>
  <td><div class="sub2">{esc(o.get('reason', ''))}</div></td>
</tr>""")
    return f"""<div class="scroll"><table>
<thead><tr><th>Order</th><th>Type</th><th class="n">Shares</th><th class="n">Limit</th>
<th class="n">Notional</th><th class="n">Stop</th><th class="n">Target</th>
<th>Why</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""


def closed_table(state):
    ts = list(reversed(state.get("closed_trades") or []))
    if not ts:
        return '<div class="empty">No closed trades yet.</div>'
    rows = []
    for t in ts[:25]:
        rk, rl = REASON_STYLE.get(t.get("reason"), ("mute", t.get("reason", DASH)))
        rows.append(f"""<tr>
  <td><span class="sym">{esc(t.get('symbol', DASH))}</span>
      <div class="sub2">{esc(t.get('opened', ''))} &rarr; {esc(t.get('closed', ''))}</div></td>
  <td class="n">{num(t.get('shares'), 6)}</td>
  <td class="n">{price(t.get('entry'))}</td>
  <td class="n">{price(t.get('exit'))}</td>
  <td class="n {tone(t.get('pnl'))}">{signed_money(t.get('pnl'))}</td>
  <td class="n {tone(t.get('pnl'))}">{pct(t.get('pnl_pct'), 1, True)}</td>
  <td>{chip(rl, rk)}</td>
</tr>""")
    return f"""<div class="scroll"><table>
<thead><tr><th>Trade</th><th class="n">Shares</th><th class="n">In</th><th class="n">Out</th>
<th class="n">P&amp;L</th><th class="n">Return</th><th>Closed by</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>"""


# ---------------------------------------------------------------- page
def stop_basis_mix(state):
    """Say which stop basis is actually in force. ATR became primary on 2026-08-31;
    a structural stop now means the bars were missing for that name, which is worth
    seeing rather than averaging away."""
    kinds = [p.get("stop_basis_kind") for p in (state.get("positions") or [])]
    atr = len([k for k in kinds if k == "atr"])
    struct = len([k for k in kinds if k and k != "atr"])
    if not kinds:
        return "stops: 1.5&times; ATR, structural fallback"
    if atr and not struct:
        return f"all {atr} stops from 1.5&times; ATR"
    if struct and not atr:
        return f"all {struct} stops structural &mdash; no ATR available"
    return f"{atr} ATR &middot; {struct} structural"


def render(state):
    b = state.get("book") or {}
    jrn = state.get("journal") or {}
    mode = state.get("mode", "paper")
    equity = b.get("equity")
    start = b.get("starting_equity")

    banners = []
    if mode == "paper":
        banners.append(banner(
            "warning", "!", "Paper book.",
            "Every order on this page was simulated. Nothing was sent to Robinhood and "
            "no real money moved. The live account is read each run to keep the lock "
            "honest, never to trade."))
    else:
        banners.append(banner(
            "critical", "!", "Live execution.",
            "Orders on this page are placed against the Agentic account with real money."))
    if b.get("halted"):
        banners.append(banner("critical", "■", "Kill switch tripped.",
                              f"{esc(b.get('halt_reason', ''))}. No new entries for the rest of "
                              "the session. Exits stay live."))
    if state.get("scan_stale"):
        banners.append(banner("warning", "▲", "Scan is stale.",
                              f"The scan feeding this run is {esc(state['scan_stale'])}. "
                              "Entries are frozen; open positions are still priced and "
                              "protected from the manager's own quotes."))
    for w in jrn.get("warnings", []):
        kind = "critical" if str(w).startswith("UNPROTECTED") or "KILL SWITCH" in str(w) else "warning"
        banners.append(banner(kind, "▲", "Attention.", esc(w)))

    dt_used = b.get("day_trades_used") or 0
    dt_lim = b.get("day_trade_limit") or 3
    pips = "".join(f'<span class="pip {"used" if i < dt_used else ("res" if i == dt_lim - 1 else "")}"></span>'
                   for i in range(dt_lim))

    rail = f"""<div class="rail">
  <div class="tile hero">
    <div class="lab">Paper equity</div>
    <div class="val">{money(equity)}</div>
    <div class="delta {tone(b.get('total_return_pct'))}">{pct(b.get('total_return_pct'), 2, True)}
      since {money(start)} start</div>
  </div>
  <div class="tile"><div class="lab">Session P&amp;L</div>
    <div class="val {tone(b.get('daily_pnl_pct'))}">{pct(b.get('daily_pnl_pct'), 2, True)}</div>
    <div class="delta">halt at &minus;{state.get('rules', {}).get('max_daily_loss_pct', 3):.0f}%</div></div>
  <div class="tile"><div class="lab">Cash</div><div class="val">{money(b.get('cash'))}</div>
    <div class="delta">{pct(100 - (b.get('deployed_pct') or 0), 0)} of equity</div></div>
  <div class="tile"><div class="lab">In market</div><div class="val">{money(b.get('invested'))}</div>
    <div class="delta">{pct(b.get('deployed_pct'), 0)} deployed, cap
      {pct(state.get('rules', {}).get('max_deployed_pct', 85), 0)}</div></div>
  <div class="tile"><div class="lab">Realised</div>
    <div class="val {tone(b.get('realized_pnl'))}">{signed_money(b.get('realized_pnl'))}</div>
    <div class="delta">{len(state.get('closed_trades') or [])} closed</div></div>
  <div class="tile"><div class="lab">Day trades</div>
    <div class="val">{dt_used}<span style="color:var(--ink-3);font-size:14px">/{dt_lim}</span></div>
    <div class="pips">{pips}</div></div>
</div>"""

    stop_basis_mix_s = stop_basis_mix(state)
    acct = state.get("account") or {}
    acct_line = " ".join(x for x in [acct.get("broker"), acct.get("display"),
                                     acct.get("nickname") and f'"{acct["nickname"]}"'] if x)
    gen = state.get("generated", "")
    slot = (state.get("slot") or "").replace("-", " ")

    return f"""<title>@@TITLE@@</title>
<style>{CSS}</style>
<div class="wrap">
  <div class="mast">
    <div>
      <h1>Trade Desk</h1>
      <div class="sub">{esc(state.get('date', ''))} &middot; {esc(slot)} &middot;
        mirroring {esc(acct_line or 'the Agentic account')}</div>
    </div>
    <div class="spacer"></div>
    <span class="badge {'paper' if mode == 'paper' else 'live'}">
      {'Paper &mdash; no orders sent' if mode == 'paper' else 'Live orders'}</span>
    <span class="badge">Scan {esc(state.get('scan_as_of') or 'unavailable')}</span>
    <span class="badge">Run {esc(gen[11:16] if len(gen) > 16 else gen)} UTC</span>
  </div>

  @@RIBBON@@
  {''.join(banners)}
  {rail}

  <div class="grid two">
    <div>
      <div class="card">
        <h2>The book <span class="count">{len(state.get('positions') or [])} open</span>
          <span class="note">{stop_basis_mix_s}</span></h2>
        {positions_table(state)}
      </div>
      <div class="card">
        <h2>Working orders <span class="count">{len(state.get('working_orders') or [])}</span>
          <span class="note">day limits &middot; never fill in the slot that places them</span></h2>
        {orders_table(state)}
      </div>
    </div>
    <div class="card decisions">
      <h2>This slot's decisions
        <span class="count">{len(jrn.get('decisions') or [])} acted &middot;
          {len(jrn.get('skipped') or [])} declined</span></h2>
      {decision_log(jrn)}
    </div>
  </div>

  <div class="card">
    <h2>Paper equity <span class="note">one point per completed run</span></h2>
    {equity_chart(state.get('equity_curve'), start)}
  </div>

  <div class="card">
    <h2>Closed trades <span class="count">{len(state.get('closed_trades') or [])}</span></h2>
    {closed_table(state)}
  </div>

  <div class="foot">
    <p><b>How to read the fills.</b> An entry rests as a day limit at the price the scan
    observed and never fills in the run that placed it &mdash; it fills on a later slot only
    if the price comes back to the limit, and then at the limit. Exits are marketable and
    fill in the same run, {pct(state.get('pm_rules', {}).get('exit_slippage_pct', 0.25))}
    against the book. The manager sees four prices a day, not a tape, so it misses intraday
    touches in both directions. This is a decision log with a P&amp;L attached, not a backtest.</p>
    <p><b>What it is allowed to do.</b> Open positions that clear every gate, sell on a broken
    stop or a broken thesis, take half at the 3R target and move the stop to breakeven, trim a
    third on a weakening score, and cut a position back under the
    {pct(state.get('rules', {}).get('max_position_pct', 15), 0)} cap. Sizing, stops and every
    risk limit come from the Scan Desk portfolio layer unchanged.</p>
    <p><b>Where the stops come from.</b> 1.5&times; the 14-day ATR below the entry, clamped between
    3% and 12% of price, and snapped to a moving average when one sits within a whisker of it.
    A name whose bars are missing falls back to a structural stop and is labelled as such on
    its row &mdash; the two size a position very differently.</p>
    <p><b>The gaps it cannot close.</b> Robinhood does not accept stop orders on fractional
    shares, so there is no resting stop in the market &mdash; a position is only protected
    when the manager runs. A position it cannot price is flagged and left alone rather than
    guessed at.</p>
  </div>
</div>"""


if __name__ == "__main__":
    src = os.path.join(BASE, "pm_state.json")
    if not os.path.exists(src):
        print(f"FATAL: {src} not found", file=sys.stderr)
        sys.exit(2)
    state = json.load(open(src, encoding="utf-8"))
    if not isinstance(state.get("book"), dict):
        print("FATAL: pm_state.json has no book — refusing to publish an empty board",
              file=sys.stderr)
        sys.exit(2)
    # ------------------------------------------------------------------ publish
    # The rolling board is rewritten every run so the bookmarked URL is always current.
    # A frozen copy is published only when pm.py decided the run earned one — the book
    # changed, or it is the close-of-day record of an open book. Four identical boards a
    # day of a book that did nothing is how a real UNPROTECTED banner gets scrolled past.
    import archive

    page = render(state)
    rid = state.get("run_id") or archive.pm_run_id(state.get("date", ""),
                                                  state.get("slot", "ad-hoc"),
                                                  state.get("generated"))
    label = state.get("run_label") or archive.pm_run_label(state.get("date", ""),
                                                           state.get("slot", ""))
    title = archive.pm_title_for(state.get("date", ""), state.get("slot", ""))
    live = state.get("live_board_url") or archive.PM_BOARD_URL
    reasons = "; ".join(state.get("publish_reasons") or []) or "no change"

    ribbon_live = (f'<div class="ribbon live"><span class="tag">Live board</span><span>'
                   f'Rewritten every run &mdash; currently showing <b>{esc(label)}</b>. '
                   + (f'This run also published a frozen copy, <b>{esc(title)}</b> '
                      f'({esc(reasons)}).'
                      if state.get("publish_snapshot") else
                      'No frozen copy this run: the marks moved but the book did not '
                      'change, so there was nothing new to freeze.')
                   + '</span></div>')
    ribbon_snap = (f'<div class="ribbon snap"><span class="tag">Snapshot</span><span>'
                   f'The book exactly as it stood at <b>{esc(label)}</b>, frozen because '
                   f'{esc(reasons)}. This board will never update. '
                   f'<a href="{esc(live)}">Open the live Trade Desk &rarr;</a>'
                   f'</span></div>')

    def emit(path, t, ribbon):
        doc = page.replace("@@TITLE@@", esc(t)).replace("@@RIBBON@@", ribbon)
        if "@@" in doc:
            print(f"REFUSING TO WRITE {path}: an unsubstituted @@ placeholder survived. "
                  "The template and the publish step have drifted apart.", file=sys.stderr)
            sys.exit(2)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
        return len(doc)

    rolling = os.path.join(BASE, "trade-desk.html")
    n = emit(rolling, "Trade Desk", ribbon_live)
    print(f"wrote {n:,} bytes -> {rolling}  [rolling board, publish with url=]")

    if state.get("publish_snapshot"):
        snap = os.path.join(BASE, archive.pm_files_for(rid)["board"])
        again = state.get("republish_url")
        n2 = emit(snap, title, ribbon_snap)
        print(f"wrote {n2:,} bytes -> {snap}  ["
              + ("frozen snapshot, UPDATE the board this slot already has"
                 if again else "frozen snapshot, publish as a NEW artifact") + "]")
        print()
        print("PUBLISH, in this order:")
        if again:
            print(f"  1. Artifact(file_path={snap!r}, url={again!r})")
            print("     <- this slot already has a board; update it in place, do NOT "
                  "publish a second one")
        else:
            print(f"  1. Artifact(file_path={snap!r},")
            print(f"              title={title!r}, favicon='\U0001F4D2')   <- new artifact")
            print("  2. put that URL on this run's journal entry as `artifact_url`, "
                  "then project_write the journal")
        print(f"  3. Artifact(file_path={rolling!r}, url={live!r})")
    else:
        print()
        print(f"No snapshot this run ({reasons}). Publish only the rolling board:")
        print(f"  Artifact(file_path={rolling!r}, url={live!r})")
