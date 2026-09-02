"""Builds the Portfolio + Proposals section for the Scan Desk dashboard."""
import html
e = html.escape

STAT = {"hold": ("h-hold", "Hold"), "trim": ("h-trim", "Trim"),
        "exit": ("h-exit", "Exit"), "unscanned": ("h-unk", "No read")}

def _sgn(v):
    return "" if v is None else (" pos" if v > 0 else (" neg" if v < 0 else ""))

def _shares(v):
    """Robinhood trades fractional and this account is small, so whole-share
    formatting renders every real ticket as 0. Show what was actually sized."""
    if not isinstance(v, (int, float)):
        return "&mdash;"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.6f}".rstrip("0").rstrip(".")

def build(PF):
    if not PF:
        return ""
    m, acct = PF["marked"], PF.get("account", {})
    rules, props, hold = PF["rules"], PF["proposals"], PF["holdings"]
    ready = [p for p in props if not p["blocked"]]
    risk_used = sum(p["risk_pct_of_equity"] for p in ready)
    deployed_after = m["invested"] + sum(p["notional"] for p in ready)
    eq = m["equity"] or 1

    if hold:
        rows = []
        for h in hold:
            cls, lbl = STAT.get(h["status"], ("h-unk", "?"))
            rows.append(
                '<tr><td class="mono">%s</td><td class="mono">%s</td><td class="mono">$%s</td>'
                '<td class="mono">$%s</td><td class="mono">$%s</td>'
                '<td class="mono%s">%s</td><td class="mono%s">%+.1f%%</td>'
                '<td><span class="hstat %s">%s</span></td><td class="uw">%s</td></tr>'
                % (e(h["symbol"]), _shares(h["shares"]), f'{h["avg_cost"]:,.2f}',
                   f'{h["price"]:,.2f}', f'{h["market_value"]:,.2f}',
                   _sgn(h["unrealized"]), f'{h["unrealized"]:+,.2f}',
                   _sgn(h["unrealized_pct"]), h["unrealized_pct"], cls, lbl, e(h["note"])))
        htable = ('<div class="tablewrap"><table><thead><tr><th>Sym</th><th>Shares</th><th>Cost</th>'
                  '<th>Price</th><th>Value</th><th>P&amp;L</th><th>%</th><th>Action</th><th></th>'
                  '</tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>')
    else:
        htable = ('<p class="empty">No positions on file yet. The broker is the source of truth &mdash; '
                  'give me your Robinhood holdings (symbol, shares, average cost) and cash balance, and every '
                  'scan from then on will mark them to market, re-judge each thesis against the current scan, '
                  'and size new proposals around what you already own.</p>')

    tickets = []
    for p in props:
        cls = "tk-blocked" if p["blocked"] else "tk-ready"
        warns = "".join("<li>%s</li>" % e(w) for w in p["warnings"])
        earn = ('<div class="tkv"><dt>Earnings</dt><dd class="warn">%s</dd></div>'
                % e(str(p["next_earnings"]))) if p.get("next_earnings") else ""
        tickets.append(
            '<div class="ticket %s">'
            '<div class="tkhead"><span class="tktkr">%s</span><span class="tkname">%s</span>'
            '<span class="tkbadge">%s</span></div>'
            '<div class="tkgrid">'
            '<div class="tkv"><dt>Shares</dt><dd class="big">%s</dd></div>'
            '<div class="tkv"><dt>Entry</dt><dd>$%s</dd></div>'
            '<div class="tkv"><dt>Stop</dt><dd class="neg">$%s</dd></div>'
            '<div class="tkv"><dt>Target</dt><dd class="pos">$%s</dd></div>'
            '<div class="tkv"><dt>Notional</dt><dd>$%s <small>%.1f%% of equity</small></dd></div>'
            '<div class="tkv"><dt>Risk</dt><dd>$%s <small>%.2f%% &middot; R:R 1:3</small></dd></div>'
            '%s</div>'
            '<p class="tkstop"><strong>Stop basis:</strong> %s</p>'
            '<p class="tkstop"><strong>Sizing:</strong> conviction %.2f from score %.0f &rarr; %.2f%% risk. %s</p>'
            '%s</div>'
            % (cls, e(p["ticker"]), e(p["name"]), "BLOCKED" if p["blocked"] else "READY",
               _shares(p["shares"]), f'{p["entry"]:,.2f}', f'{p["stop"]:,.2f}', f'{p["target"]:,.2f}',
               f'{p["notional"]:,.2f}', p["pct_of_equity"], f'{p["dollar_risk"]:,.2f}',
               p["risk_pct_of_equity"], earn, e(p["stop_basis"]), p["conviction"], p["score"],
               p["risk_pct_of_equity"], e(p["correlation_note"]),
               ('<ul class="tkwarn">' + warns + "</ul>") if warns else ""))

    blocks_html = "".join("<li>%s</li>" % e(b) for b in PF["blocks"])
    return (
      '<section class="card sec pfsec">'
      '<h2>Portfolio &middot; %s &middot; every order needs your approval</h2>'
      '<div class="rstats pfstats">'
      '<div class="rstat"><b>$%s</b><span>equity</span></div>'
      '<div class="rstat"><b>$%s</b><span>cash %.0f%%</span></div>'
      '<div class="rstat"><b>%.0f%%</b><span>deployed / %.0f%% cap</span></div>'
      '<div class="rstat"><b>%d</b><span>of %d positions</span></div>'
      '<div class="rstat"><b>%.1f%%</b><span>new risk / %.0f%% budget</span></div>'
      '<div class="rstat"><b>%.0f%%</b><span>deployed if all filled</span></div>'
      '</div>'
      '<div class="subh">Holdings</div>%s'
      '<div class="subh">Proposed orders &mdash; %d ready, %d blocked</div>'
      '<div class="tickets">%s</div>%s'
      '<p class="pfnote">Sizing mirrors <code>src/execution/position_sizing.py</code> in the trading-system repo: '
      'shares = (equity &times; risk%%) &divide; (entry &minus; stop), conviction-scaled between %.1f%% and %.1f%%, '
      'reduced for correlation, then capped at %.0f%% per name, %d per GICS sector and %.0f%% deployed. '
      'Stops are <strong>structural, not ATR</strong> &mdash; ATR needs OHLC bars this data path does not have. '
      '<strong>Nothing on this page is an order.</strong> Every ticket is a proposal for you to place yourself.</p>'
      '</section>'
      % (e(str(acct.get("broker", "Broker"))), f'{m["equity"]:,.0f}', f'{m["cash"]:,.0f}',
         m["cash_pct"], m["deployed_pct"], rules["max_deployed_pct"], len(m["positions"]),
         rules["max_positions"], risk_used, rules["cumulative_risk_cap_pct"],
         deployed_after / eq * 100, htable, len(ready), len(props) - len(ready),
         "".join(tickets), ('<ul class="pnotes">' + blocks_html + "</ul>") if blocks_html else "",
         rules["min_risk_per_trade_pct"], rules["max_risk_per_trade_pct"],
         rules["max_position_pct"], rules["max_per_sector"], rules["max_deployed_pct"]))
