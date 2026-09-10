"""pm.py — Portfolio Manager decision engine for the Agentic account.

Scan Desk screens. This module DECIDES: it reconciles the book, protects open
positions, harvests targets, rebalances, and sizes new entries — then either
simulates the orders against a paper book or emits live order tickets.

It never invents a rule. Sizing, stops and every risk gate come from
portfolio.py, which mirrors the trading-system repo. What lives here is
execution policy: fills, order lifecycle, the broker policy (the day-trade /
margin regime — see broker_policy.py), the daily kill switch, and the decision
journal.

MODES
  paper  (default) — orders are simulated against paper_book.json. Nothing is
                     sent to a broker. This is the mode the system ships in.
  live             — orders are written to pm_orders.json for the caller to
                     place through the Robinhood MCP. The engine does not
                     mutate positions in live mode; broker state is the truth
                     and comes back in through --broker on the next run.

FILL MODEL (paper) — deliberately pessimistic, see PM.md
  * An entry NEVER fills in the run that places it. It rests as a day limit at
    the observed price and fills on a LATER slot only if price <= limit, at the
    limit. Real fills would often be faster and better; the book is biased
    against itself on purpose.
  * Exits are marketable and fill in the same run, at price less
    exit_slippage_pct. Protection must not sit unfilled.
  * Targets do NOT rest as sell limits (M2/M3, corrected 2026-09-02). When the slot
    price is at or through the 3R target the scale-out fires as a MARKETABLE sale at
    the slot price less exit_slippage_pct, exactly like any other exit. That is more
    conservative than a limit filled at the target, not less — but the docstring used
    to claim the limit, and code and doctrine disagreeing is how a future change goes
    wrong. PM.md section 3 says the same thing in the same words.
  * The book observes four prices a day, not a tape. It misses intraday
    touches in both directions. Never present paper results as backtest-grade.

AUDIT FIXES 2026-08-31 (the "Agentic Trading Audit" board)
  * TIMING-01 — scan_stale_minutes is now ENFORCED: a same-day scan older than
    the limit freezes entries instead of counting as fresh at any hour.
  * FILL-01  — the power-hour slot places no new entries: a day-limit entry
    placed at the last slot expires at the session roll before it can ever be
    evaluated for a fill.
  * SIZE-01  — names that already have a working buy order are excluded BEFORE
    sizing, so a resting order's cash cannot be spent from the budget twice.
  * STATE-02 — --broker takes the raw get_equity_positions payload and warns,
    per symbol, when the live account holds anything (paper mode expects it
    flat). Warn-only; it never mutates the book.

ADDED 2026-08-31 EVENING, SECOND PASS
  * --slot sentinel — the risk sentinel. Runs between the four decision slots,
    prices HOLDINGS and WORKING ORDERS only, and runs fills + the exit pass
    (stops, targets). No entries, no rebalance, no scan needed. A sentinel
    run that takes no decision and raises no warning is QUIET: it writes
    nothing, so the book's revision does not churn eleven times a day.
    Sentinel runs never add a point to the equity curve.
  * --desk NAME — strategy desks. desks.json maps a desk to its own book and
    journal, optional RULES overrides, and a candidate FILTER (setups, RSI
    band, score floor). Same engine, different ruleset, separate book — so
    the weekly review can attribute results cleanly. The default desk
    ("swing") is the unfiltered book and keeps the unsuffixed file names.
  * Spread gate — quotes_to_prices() now reads bid/ask; an entry whose spread
    is over PM_RULES["max_spread_pct"] of price is not placed. On a small
    book the spread is a bigger cost than anything the model reasons about.
  * CAP-01 (2026-09-01) — working buy orders are passed to build_proposals as
    pseudo-positions, so the sector cap, max-positions count, cumulative-risk budget,
    correlation multiplier and max-deployed room all see committed-but-unfilled names.
    Counting only filled positions let the swing book go 4-deep in one sector against a
    cap of 3.

ADDED 2026-09-02
  * HOUSE-01 — cross-desk exposure caps. Every cap in portfolio.py is enforced on
    ONE book at a time, and the three desks trade the same scan universe: on
    2026-09-02 the same four AI/semiconductor names appeared across all three books
    and no rule anywhere could see it. The manager now auto-discovers the other
    desks' books in $SCAN_DIR (from desks.json), computes house-level exposure per
    symbol and per GICS sector, and refuses an entry that would push either past its
    cap. Preventive, not corrective — exactly like CAP-01. portfolio.py is untouched.
  * COVER-01 — the sentinel heartbeat. A QUIET sentinel wrote nothing at all, by
    design, so "this desk was checked and nothing fired" and "this desk was never
    run" left byte-identical records: no journal entry, no book revision, nothing.
    Sentinel coverage was verifiable only by reading a session transcript. Every
    run — quiet or not — now writes pm_heartbeat<suffix>.json and prints a COVERAGE
    line for the caller to merge into claude/pm-coverage.json. The book and the
    journals are still untouched by a quiet run.

ADDED 2026-09-10
  * K-01 — the PDT guard became a BROKER POLICY. FINRA Regulatory Notice 26-10 amended
    Rule 4210 effective 2026-06-04 and replaced pattern-day-trader counting with an
    intraday margin requirement; Robinhood adopted it that day and the Agentic account is
    under it. broker_policy.py carries three regimes (legacy_pdt, intraday_margin — the
    default — and cash_settled), selected by --broker-policy, the book, desks.json or
    engine-config.json in that order. Every sale, entry gate and state line goes through
    the policy object; legacy_pdt reproduces the old guard byte-for-byte.
  * K-02 — the DRAWDOWN LADDER (ladder.py). The 3% daily kill was the only control against
    losing money and it is a cliff. The ladder sits under it and measures from the book's
    high-water mark (book["hwm"], never decreasing): −4% halves every new entry, −6% stops
    new entries, −8% halts the book through the kill switch's own path — flatten, cancel
    working buys — and cools off for five sessions, after which entries come back at half
    size until the HWM is regained. A −2% session is a soft daily level: no new entries for
    the rest of the day. Exits stay live at every rung. The 3% kill is untouched.
  * K-06 — SHADOW FILLS and the execution-cost budget (fills.py). The booked fill model above
    is unchanged and its P&L is byte-identical with the model off. Alongside every booked
    fill — entry, exit, trim, rebalance, flatten — the book now records what the same order
    would have paid against the quote (a buy at the ask plus k half-spreads plus a size
    add-on, a sale at the bid less the same), the signed gap in dollars (positive = the paper
    book flattered itself), and the implementation shortfall against the decision-time mid.
    The decision mid for an entry is captured when the order is PLACED (entry_pass) and
    carried on the order; for every same-run sale it is the mid of the quote the sale fired
    on. book["shadow"] accumulates the gap; state carries equity_shadow = equity − cum gap and
    the year-to-date cost against PM_RULES["cost_budget_bps_per_year"]. PM.md § "Shadow
    fills and execution cost".
  * K-03 — HOUSE EXPOSURE METRICS (house.py). HOUSE-01's two caps see one name and one
    sector at a time. house.py measures the combined book as a whole: the effective number
    of independent bets (N_eff, Herfindahl and correlation-aware — a sector proxy when no
    bars.json is staged, measured from daily returns when one is), beta-weighted exposure,
    momentum crowding, the largest sector and name, and how much of the book the desks hold
    in common. Computed once per run on the same combined book HOUSE-01 tallies, written
    to state["house"]["exposure"], the journal, the heartbeat and the coverage row.
    PM_RULES["house_exposure"] carries the thresholds; `enforce` is False by default, so
    the block is REPORTED and gates nothing. With enforce on a breach refuses NEW ENTRIES
    house-wide; exits are never touched in either mode.
  * K-04 — HISTORICAL VaR AND STRESS WINDOWS (var.py). The ladder reacts to a loss after
    it happens; this says how much THIS desk's book stands to lose on an ordinary bad day
    (1-day 99% historical-simulation VaR and CVaR over up to two years of the held names'
    shared daily returns) and what it would have done through 2020-03-16, 2022, 2024-08-05
    and 2025-04-03/04 — each name's own bars when they reach that far, else β_252 × the
    benchmark's window return, labelled proxy — plus the 2025-04-09 squeeze as the
    short-side number. Computed once per run on the post-exit marked positions when
    bars.json is staged, null with a reason otherwise; written to state["risk"], the
    journal's `risk` block, the heartbeat (var_pct, worst_stress) and so the coverage row.
    PM_RULES["var"] carries the thresholds; `enforce` is False by default. With enforce on
    a VaR over 3% of desk equity or a worst stress over 2 × the 8% halt refuses NEW
    ENTRIES on the desk; exits are never touched in either mode.
  * K-07 — STOPS BY DESK TYPE (stops.py) and the LIVE GUARDRAILS (guardrails.py). One stop
    rule served three desks that are not the same trade. desks.json `rules.stop_policy` now
    selects fixed_atr (today's rule, the default — every existing desk is unchanged),
    chandelier (a ratcheting ATR trail, a time stop) or time_catastrophe (a wide catastrophe
    stop, a time stop, an optional target — for mean reversion and rotation). The entry
    pass sets the initial levels from the desk's policy; the exit pass calls its update()
    every run, journals a raised stop as a "raise-stop" decision (never a lowered one) and
    maps its exit reasons onto the journal's words. The scale-out / breakeven logic runs
    only under fixed_atr. Two desks are shipped INACTIVE as templates (rotation, orb):
    --desk refuses them without --allow-inactive, and no book is created for them.
    guardrails.py is doctrine plus validators — there is still no live path — and the one
    thing the paper path does with it is journal, on every proposal, what the per-order and
    deny-list checks WOULD have refused live (jrn["live_would_refuse"]). PM.md § "Stops by
    desk type" and § "Live guardrails".
  * D-01 — THE SECTOR-ROTATION DESK (rotation.py, E26). A desk whose filter says
    {"universe": "sector-etfs"} takes its candidates from rotation.proposals() over a staged
    bars_etf.json (14 ETFs, >= 13 months of daily bars) and macro.json (tbill_3m_pct) instead
    of scan_results.json: the 12-1 month momentum rank, Antonacci's absolute filter against
    the bill, the top three equal sleeves or TLT, decided ONLY at the last power-hour slot
    of the month (plus a weekly check and --force-rebalance). Its entries are placed as
    MARKETABLE next-session orders (expires "next-session", fill_rule "marketable"): the
    FILL-01 rule that a day-limit placed at power-hour is dead on arrival is exactly right
    for a limit, and exactly wrong for a monthly rotation that decides at the close and
    fills at the next slot's quote plus slippage. Held ETFs that leave the target set are
    sold as `rotation-exit`; a re-affirmed holding gets `hold_from` reset so the 25-session
    time stop counts from the last decision. The ETFs are exempt from HOUSE-01's per-name
    and per-sector caps (sector-level by construction) and INCLUDED in K-03's exposure
    metrics. Every one of these branches is gated on the desk config: the three live desks
    write the same bytes as before (tests/test_stops.py pins that). Ships INACTIVE.

  * D-02 — the PAPER OPTIONS DESK (options_desk.py, E27). desks.json can now carry a desk
    with `"kind": "options"`; `--desk` on such a desk dispatches the decision run to
    options_desk.run_slot() and then applies exactly the protocol below — revision,
    --check, heartbeat, journal merge, pm_book_next / pm_state files. The equity engine
    (run()) is not touched and the three equity desks are unchanged. The desk ships
    INACTIVE; house_exposure() folds an options peer book in as a beta-weighted-delta
    equity equivalent (house.options_holdings). Paper only: options_desk imports nothing
    that can reach a broker.

  * D-03 — the ORB DESK (orb.py, E24). Still an INACTIVE template in desks.json; the wiring
    is in place so activating it is a config change. A desk whose rules carry an `orb` block
    is an intraday desk: the 09:35 sentinel (only that one) ranks the scan universe plus the
    held names by opening relative volume from bars_5m.json and places "stop-buy" working
    orders above the opening-range high; simulate_fills fills that type when a LATER 5-minute
    high reaches the trigger, at max(trigger, that bar's open) plus the shadow haircut, and
    cancels it unfilled after 10:30; the exit pass walks the 5-minute bars since entry for a
    stop hit on the lows and the 0.5×ATR chandelier trail under the highest 5-minute high; the standard
    entry pass, the thesis exits and the trims never touch it; the power-hour flatten
    (flatten_at_close) closes everything. No bars_5m.json: "no intraday bars — no ORB today".

Paths resolve from SCAN_DIR, else from this file's own directory.
"""
import json, os, sys, argparse, datetime as dt

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import archive
import broker_policy
import fills as fills_mod
import guardrails as guardrails_mod
import house as house_mod
import ladder as ladder_mod
import orb as orb_mod
import portfolio as pf_mod
import rotation as rotation_mod
import stops as stops_mod
import var as var_mod
from portfolio import RULES, build_proposals

# ---- execution policy. Risk limits live in portfolio.RULES; these are order mechanics ----
PM_RULES = {
    "exit_slippage_pct": 0.25,      # adverse fill assumption on a marketable exit
    "max_new_entries_per_run": 3,   # do not deploy the whole book in one slot
    "scale_out_pct": 50.0,          # sell half when the 3R target prints
    "trim_pct": 33.0,               # sell a third on a `trim` rating
    "max_trims_before_exit": 2,     # a name trimmed twice and still weak is closed, not nibbled
    # Rebalance discipline (2026-09-02). Trim has been capped at once per session since day
    # one, and PM.md gives the reason: "the manager nibbles the same losing position every
    # slot and calls it risk management." rebalance_pass had no equivalent guard and nibbled
    # the WINNING position instead — NVDA was shaved four times on 2026-09-02 for a combined
    # $0.86. Same reasoning, opposite sign. Two guards, because they stop different things:
    "max_rebalances_per_session": 1,  # count: stop the every-slot shave of one name
    "rebalance_deadband_pct": 1.5,    # size: a name oscillating around 15.0% is not a breach.
                                      # Trigger above cap + deadband; still trim back to the cap.
    # The legacy PDT numbers live in broker_policy.LEGACY_PDT_RULES; the keys stay here so a
    # desk's pm_rules override still reaches the legacy_pdt policy (it reads PM_RULES by
    # reference). Nothing else in this module reads them any more.
    **broker_policy.LEGACY_PDT_RULES,
    "scan_stale_minutes": 240,      # older than this and entries are frozen (ENFORCED — TIMING-01)
    "warn_price_drift_pct": 3.0,    # live quote vs the price the scan scored: note it
    "max_price_drift_pct": 5.0,     # beyond this the row no longer describes the price
    "max_spread_pct": 1.0,          # bid/ask wider than this % of price: do not enter
    "macro_gate_lookahead_min": 120,  # only a HIGH-IMPACT release this close ahead gates
    "equity_curve_max": 400,
    "closed_trades_max": 300,
    # HOUSE-01 — caps across ALL desk books combined, as a % of COMBINED equity.
    # The per-desk caps in portfolio.RULES are unchanged and still apply first; these
    # only ever refuse more, never allow more.
    "house_max_symbol_pct": 15.0,   # one ticker across every book
    "house_max_sector_pct": 40.0,   # one GICS sector across every book
    "house_caps_enabled": True,
    # K-03 — house exposure metrics (house.py). REPORTED by default: `enforce: False` means
    # the numbers and flags go to the state, journal and coverage row and gate nothing. Set
    # enforce True and a flagged breach refuses NEW entries house-wide; exits stay live.
    # Semantics and formulas in house.py and PM.md § "House exposure".
    "house_exposure": {
        "min_n_eff": 3.0,                # effective number of independent bets (Herfindahl / ρ)
        "max_beta_w": 0.8,               # Σ w·β as a fraction of combined equity, needs bars
        "max_momentum_crowd_pct": 60.0,  # equity share in names up >50% over 12-1m, needs bars
        "rho_default": 0.3,              # cross-sector ρ for the no-bars sector proxy
        "enforce": False,
    },
    # K-04 — historical-simulation VaR and the named stress windows (var.py), on THIS
    # desk's marked positions, once per run, only when bars.json is staged (null with a
    # reason otherwise). REPORTED by default: `enforce: False` means the numbers go to
    # state["risk"], the journal and the coverage row and gate nothing. Set enforce True
    # and a 1-day 99% VaR over max_var_pct_of_desk, or a worst long-side stress loss over
    # max_stress_multiple_of_halt × halt_pct, refuses NEW entries on the desk; exits stay
    # live. Semantics in var.py and PM.md § "VaR and stress".
    "var": {
        "alpha": 0.99,
        "max_var_pct_of_desk": 3.0,
        "max_stress_multiple_of_halt": 2.0,
        "halt_pct": 8.0,
        "enforce": False,
    },
    # K-02 — the drawdown ladder, measured from the book's high-water mark. Lives here
    # (not in portfolio.RULES) so the board and the alerts page read the same constants
    # the engine gates on. Semantics in ladder.py and PM.md § "Drawdown ladder".
    "ladder": {
        "soft_daily_pct": 2.0,          # session P&L at or below −2%: no NEW entries today
        "rungs": [
            {"dd_pct": 4.0, "entry_size_mult": 0.5},                    # halve new entries
            {"dd_pct": 6.0, "entry_size_mult": 0.0},                    # no new entries
            {"dd_pct": 8.0, "flatten": True, "cool_sessions": 5},       # halt, flatten, cool off
        ],
        "reentry_size_mult": 0.5,       # after the cool-off, until the HWM is regained
    },
    # K-06 — the shadow fill model. Records, never books: the booked price and P&L are
    # untouched whether this is on or off. Semantics in fills.shadow_fill() and PM.md
    # § "Shadow fills and execution cost". A desk's pm_rules may override the whole block.
    "shadow": {
        "enabled": True,
        "k_by_slot": {"pre-market": 1.0, "opening-range": 1.0, "midday": 0.0,
                      "power-hour": 1.0, "sentinel": 0.5, "ad-hoc": 0.5},
        "add_on_bps_large_cap": 2.0,
        "add_on_bps_small_cap": 15.0,
        "small_cap_adv_usd": 10e6,
        "gap_through_stops": True,
    },
    # K-06 — execution-cost budget per desk: implementation shortfall × notional, summed over
    # the year and divided by average equity, may use this many bps. Over 100% is a warning.
    "cost_budget_bps_per_year": 200,
    # K-07 — the live guardrails. DOCTRINE: nothing on the paper path gates on them, and
    # there is no live path for them to gate. They live here so the board, the docs and
    # the validators in guardrails.py read one set of numbers, and so a desk's pm_rules can
    # override the block. The paper path evaluates the per-order and deny-list checks on
    # every proposal and journals what WOULD have been refused (jrn["live_would_refuse"]).
    "live_guardrails": json.loads(json.dumps(guardrails_mod.LIVE_GUARDRAILS)),
}

SLOT_ORDER ={"pre-market": 0, "opening-range": 1, "midday": 2, "power-hour": 3, "ad-hoc": 4,
              "sentinel": 5}
SENTINEL = "sentinel"

# The active strategy desk. main() fills this from desks.json when --desk is given;
# the default is the unfiltered "swing" desk with the unsuffixed file names.
DESK = {"name": "swing", "filter": {}, "suffix": "", "kind": "equity"}

# HOUSE-01 — the other desks' books, loaded by main() from desks.json. Empty means the
# house caps were not evaluated this run, and that is journaled rather than assumed safe.
PEERS = {"books": {}, "loaded": [], "missing": []}

# K-03 — the optional bars file ($SCAN_DIR/bars.json, the get_equity_historicals payload
# technicals.py reads), loaded by main() through load_bars(). None means no bars were
# staged: N_eff falls back to the sector proxy and beta / momentum crowding report null.
BARS = {"rows": None, "path": None}

# K-01 — the broker policy for this run, built once by run() from the resolution order in
# broker_policy.get_policy(). None until then; _policy() resolves a default for any helper
# called outside run() so nothing ever gates on a missing object.
POLICY = None

# D-03 — the ORB desk's intraday inputs, loaded by main() through load_orb_inputs() (or set
# directly by a test). `bars_5m` is {SYMBOL: [normalised 5-minute bars]} or None when no
# bars_5m.json was staged; `universe` is the scan's rows (the sentinel loads no scan of its
# own, so the ORB entry pass reads the staged scan_results.json through here); `daily` is
# the bars.json map when one is staged, for ATR14 / ADV14.
ORB = {"bars_5m": None, "bars_5m_path": None, "universe": None, "daily": None, "notes": []}


def _orb_rules():
    """The ORB rules block when the active desk is an intraday ORB desk, else None. Every
    ORB branch in this module is gated on this, so the three live desks never enter one."""
    r = (DESK.get("rules") or {}).get("orb")
    return orb_mod.rules_for(r) if isinstance(r, dict) else None


def _policy():
    global POLICY
    if POLICY is None:
        POLICY = broker_policy.get_policy(None, {}, DESK, pm_rules=PM_RULES, risk_rules=RULES)
    return POLICY


class InactiveDesk(ValueError):
    """K-07 — the desk is defined in desks.json as an inactive template."""


def use_desk(name, desks_path="desks.json", allow_inactive=False):
    """Select a strategy desk: DESK, the RULES / PM_RULES overrides, the book and journal
    names. Returns the desk's config dict (empty for the default swing desk).

    A desk carrying a top-level `"inactive": true` is a TEMPLATE — a mandate written down
    with its stop policy and filter so it can be wired later, not a desk that trades. It
    is refused here unless `allow_inactive` is set, its book is never created, and the peer
    loader and the paper mirror skip it. Raises InactiveDesk / KeyError; main() turns both
    into a FATAL exit.
    """
    if not name or name == "swing":
        desks = (_load(desks_path) or {}).get("desks") or {}
        cfg = desks.get("swing") or {}
        if cfg.get("inactive") and not allow_inactive:
            raise InactiveDesk("desk 'swing' is marked inactive in desks.json")
        DESK.update({"name": "swing", "filter": {}, "suffix": "", "rules": cfg.get("rules") or {},
                     "kind": cfg.get("kind") or "equity"})
        return cfg
    desks = (_load(desks_path) or {}).get("desks") or {}
    cfg = desks.get(name)
    if cfg is None:
        raise KeyError(f"desk {name!r} is not defined in {desks_path}")
    if cfg.get("inactive") and not allow_inactive:
        raise InactiveDesk(
            f"desk {name!r} is an INACTIVE template in {desks_path} — "
            f"{cfg.get('_note') or 'it has no book and is not scheduled'}. "
            "Pass --allow-inactive to run it deliberately against a staged book.")
    DESK.update({"name": name, "filter": cfg.get("filter") or {},
                 "suffix": f"-{name}", "rules": cfg.get("rules") or {},
                 "kind": cfg.get("kind") or "equity"})
    for k, v in (cfg.get("rules") or {}).items():
        if k in RULES:
            RULES[k] = v          # portfolio.RULES is the dict build_proposals defaults to
    for k, v in (cfg.get("pm_rules") or {}).items():
        if k in PM_RULES:
            PM_RULES[k] = v
    return cfg


def _stop_policy(name=None):
    """K-07 — the stop policy object. The desk's own policy by default; `name` selects the
    policy a POSITION was opened under (it may pre-date a desk config change), with the
    desk's parameters when the names agree and the policy's defaults when they do not."""
    desk_name, params = stops_mod.resolve(DESK.get("rules") or {})
    if name and name != desk_name:
        return stops_mod.get_policy(name, {})
    return stops_mod.get_policy(desk_name, params)


def rotation_desk():
    """D-01 — is the active desk the sector-rotation desk? Decided by its filter, so the
    wiring is a desks.json fact and not a name the engine hard-codes."""
    return (DESK.get("filter") or {}).get("universe") == "sector-etfs"


def desk_filter(rows, jrn):
    """Apply the active desk's candidate filter to the scan rows. Every row the desk
    declines is counted once in the journal, not listed — the scan already carries the
    reasons, and a desk that trades pullbacks is not 'skipping' momentum names."""
    f = DESK.get("filter") or {}
    if not f:
        return rows
    setups = set(f.get("setups") or [])
    lo, hi = (f.get("rsi_range") or [None, None])[:2]
    floor = f.get("min_score")
    universe = f.get("universe")
    kept, dropped = [], 0
    for r in rows:
        ok = True
        # D-01: a universe filter admits only rows the rotation module produced — a
        # scan_results.json row can never reach this desk's sizing by accident.
        if universe and r.get("_source") != rotation_mod.SOURCE:
            ok = False
        if setups and r.get("setup") not in setups:
            ok = False
        rsi = r.get("rsi_14")
        if ok and lo is not None and (not isinstance(rsi, (int, float)) or rsi < lo):
            ok = False
        if ok and hi is not None and (not isinstance(rsi, (int, float)) or rsi > hi):
            ok = False
        if ok and floor is not None and (r.get("score") is None or r["score"] < floor):
            ok = False
        if ok:
            kept.append(r)
        else:
            dropped += 1
    if dropped:
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"desk '{DESK['name']}': {dropped} scan row(s) outside this desk's "
                               f"mandate ({', '.join(f'{k}={v}' for k, v in f.items())})"})
    return kept


# ------------------------------------------------------------------ helpers
def _now(iso=None):
    if iso:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


def _scan_age_minutes(scan, now):
    """Age of the scan in minutes, from meta.scan_date + meta.time (an ET clock time).

    Returns None when the meta cannot be parsed — the calendar-date check still applies,
    and an unparseable time must not invent staleness. This is what makes
    PM_RULES["scan_stale_minutes"] a real control instead of dead config (TIMING-01):
    a same-day scan used to count as perfectly fresh at any hour, so the power-hour
    manager would happily size entries off the 08:00 scan seven hours later."""
    meta = (scan or {}).get("meta") or {}
    sd, tm = meta.get("scan_date"), str(meta.get("time") or "")
    if not sd or not tm:
        return None
    try:
        from zoneinfo import ZoneInfo
        t = dt.datetime.strptime(f"{sd} {tm[:5]}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("America/New_York"))
    except Exception:
        return None
    return (now - t.astimezone(dt.timezone.utc)).total_seconds() / 60.0


def _round_shares(v, rules=RULES):
    dec = rules.get("share_decimals", 6)
    return round(v, dec) if rules.get("fractional") else float(int(v))


_business_days_back = broker_policy.business_days_back


def day_trades_used(book, today):
    """Day trades in the legacy five-business-day window. Reporting only — whether it
    GATES anything is the policy's decision (K-01)."""
    return broker_policy.day_trades_in_window(book, today, PM_RULES["pdt_window_business_days"])


def pdt_applies(book, equity):
    """True only under the legacy_pdt policy and under its equity threshold."""
    pol = _policy()
    return isinstance(pol, broker_policy.LegacyPDT) and pol.applies(book, equity)


# ------------------------------------------------------------------ pricing
def quotes_to_prices(payload, now, max_age_min=30):
    """Turn a raw `get_equity_quotes` response into the manager's price map.

    The engine parses the broker payload itself rather than having a session retype
    numbers into a file — a mistyped price on this path sizes a position or fires a
    stop, and hand-transcription is exactly where that goes wrong.

    Follows the connector's own guidance: take whichever of last_trade_price /
    last_non_reg_trade_price carries the more recent venue timestamp, refuse a symbol
    that has not traded or is not in an active state, and refuse a quote older than
    `max_age_min` — a stale quote is not a live price just because it came from the
    broker."""
    out, rejected = {}, []
    results = (((payload or {}).get("data") or {}).get("results")
               or (payload or {}).get("results") or [])
    for row in results:
        q = row.get("quote") or {}
        sym = q.get("symbol")
        if not sym:
            continue
        if q.get("has_traded") is False or (q.get("state") or "active") != "active":
            rejected.append(f"{sym} (state {q.get('state')}, traded={q.get('has_traded')})")
            continue
        best_px, best_ts = None, None
        for pk, tk in (("last_trade_price", "venue_last_trade_time"),
                       ("last_non_reg_trade_price", "venue_last_non_reg_trade_time")):
            v, ts = q.get(pk), q.get(tk)
            if v in (None, "") or not ts:
                continue
            try:
                t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if best_ts is None or t > best_ts:
                best_px, best_ts = float(v), t
        if best_px is None or best_px <= 0:
            rejected.append(f"{sym} (no usable trade price)")
            continue
        age = (now - best_ts).total_seconds() / 60.0
        if age > max_age_min:
            rejected.append(f"{sym} (quote {age:.0f} min old)")
            continue
        # Bid/ask spread as a percentage of price. On a thin name this is the largest
        # cost the book pays; the entry pass refuses names over max_spread_pct.
        spread_pct = None
        bid = ask = None
        try:
            bid, ask = float(q.get("bid_price") or 0), float(q.get("ask_price") or 0)
            if bid > 0 and ask > bid:
                spread_pct = round((ask - bid) / ((ask + bid) / 2) * 100.0, 3)
        except (TypeError, ValueError):
            spread_pct = None
        # K-06: the raw touch travels with the price so the shadow fill can be priced
        # against the quote the decision saw. None unless both sides are usable.
        if not (isinstance(bid, float) and isinstance(ask, float) and bid > 0 and ask >= bid):
            bid = ask = None
        out[sym] = {"price": best_px, "as_of": best_ts.isoformat().replace("+00:00", "Z"),
                    "age_min": round(age, 1), "spread_pct": spread_pct,
                    "bid": bid, "ask": ask}
    return out, rejected


def broker_divergence(payload, book):
    """Warn when the LIVE agentic account holds anything the paper book does not expect.

    In paper mode the live account should be flat — the manager reads it every run
    precisely so a hand trade or a funding event gets NOTICED. PM.md promises that a
    divergence 'is reported, never quietly reconciled'; this is the mechanism behind
    the promise (STATE-02). Warn-only: it never mutates the book, and a malformed
    payload contributes nothing rather than crashing the run.

    The positions key is `data.positions`, NOT `data.results` (2026-09-02). This parser
    originally read `results` — the shape `get_equity_quotes` returns — which it appears to
    have inherited from the quote parser directly above. The effect was that a real live
    holding produced no warning at all and the run reported "no divergence": exactly the
    "checked and clean" / "never actually checked" collision COVER-01 removed for the
    sentinel. Both keys are accepted now so an older staged payload still parses."""
    d = payload or {}
    data = d.get("data") if isinstance(d.get("data"), dict) else {}
    rows = (data.get("positions") or data.get("results")
            or d.get("positions") or d.get("results") or [])
    warns = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = row.get("symbol") or (row.get("instrument") or {}).get("symbol")
        qty = None
        for k in ("quantity", "shares", "position_quantity"):
            v = row.get(k)
            if v in (None, ""):
                continue
            try:
                qty = float(v)
                break
            except (TypeError, ValueError):
                continue
        if sym and qty:
            warns.append(f"LIVE ACCOUNT DIVERGENCE: the live agentic account holds {qty:g} "
                         f"{sym}, which the paper book does not track. Vishal may have traded "
                         "or funded it by hand — report it; never reconcile silently.")
    return warns


def build_price_book(scan, prices_override, book, scan_stale):
    """One price per symbol, with its provenance and — the part that matters —
    whether it is FRESH. Order of preference: the PM's own fetch this run > today's
    scan > the last price the book saw.

    A stale price may be used to VALUE the book. It may never be used to trade it.
    Filling an order or firing a stop off yesterday's close is the exact failure the
    Scan Desk README calls the staleness trap, and it is worse here because the
    consequence is a position rather than a wrong-looking number."""
    pb = {}
    for r in (scan or {}).get("results", []):
        if isinstance(r.get("price"), (int, float)) and r["price"] > 0:
            # D-01: a rotation row's price is the last BAR CLOSE — get_equity_historicals
            # reaches only the last completed session — so it values, never trades. The
            # broker quote below outranks it, and the entry pass refuses an ETF without one.
            bar_close = r.get("_source") == rotation_mod.SOURCE
            pb[r["ticker"]] = {"price": float(r["price"]),
                               "source": "bars-close" if bar_close else "scan",
                               "as_of": (scan.get("meta") or {}).get("time"),
                               "fresh": (not scan_stale) and not bar_close,
                               "gics": r.get("gics"), "industry": r.get("industry")}
    for tk, v in (prices_override or {}).items():
        px = v.get("price") if isinstance(v, dict) else v
        if isinstance(px, (int, float)) and px > 0:
            pb[tk] = {"price": float(px), "source": "pm-fetch",
                      "as_of": (v.get("as_of") if isinstance(v, dict) else None),
                      "fresh": True,
                      "spread_pct": (v.get("spread_pct") if isinstance(v, dict) else None),
                      "bid": (v.get("bid") if isinstance(v, dict) else None),
                      "ask": (v.get("ask") if isinstance(v, dict) else None),
                      # K-07: the quote's age rides along for the live-guardrail audit.
                      "age_min": (v.get("age_min") if isinstance(v, dict) else None),
                      "gics": (pb.get(tk) or {}).get("gics"),
                      "industry": (pb.get(tk) or {}).get("industry")}
    for p in book.get("positions", []):
        if p["symbol"] not in pb and p.get("last_price"):
            pb[p["symbol"]] = {"price": float(p["last_price"]), "source": "stale-book",
                               "as_of": p.get("last_priced"), "fresh": False,
                               "gics": p.get("gics"), "industry": p.get("industry")}
    return pb


def tradeable(pb, sym):
    """The price to act on, or None. Valuation uses pb directly; every decision
    that moves shares comes through here."""
    q = pb.get(sym)
    return q["price"] if q and q.get("fresh") else None


def mark_book(book, pb):
    invested = 0.0
    unpriced = []
    for p in book.get("positions", []):
        q = pb.get(p["symbol"])
        if q:
            p["last_price"] = q["price"]
            p["price_source"] = q["source"]
            invested += q["price"] * p["shares"]
        else:
            unpriced.append(p["symbol"])
            invested += p["avg_cost"] * p["shares"]
    cash = float(book.get("cash", 0.0))
    equity = cash + invested
    return {"cash": round(cash, 2), "invested": round(invested, 2),
            "equity": round(equity, 2), "unpriced": unpriced}


def reserved_cash(book):
    return round(sum(o["limit_price"] * o["shares"]
                     for o in book.get("working_orders", [])
                     if o["side"] == "buy" and o.get("status") == "working"), 2)


# ------------------------------------------------------------------ HOUSE-01
def house_exposure(this_book, pb, peers=None):
    """Combined exposure across THIS desk's book and every peer desk book found.

    Every risk limit in portfolio.py is enforced on one book at a time. The three
    desks read the same scan universe and score it with the same model, so they
    converge on the same names by construction — on 2026-09-02 NVDA and HOOD were each
    held in two books and the four semiconductor names made up a third of combined
    equity, with no rule anywhere able to see it. PM.md §11 named this as a known gap;
    this is the mechanism that closes it.

    Positions and WORKING BUY ORDERS both count — committed capital is not free
    capital, the same argument CAP-01 made inside a single book. Marks come from this
    run's price book where a symbol is priced, and from the peer book's own last mark
    otherwise; a peer's stale mark is fine for a concentration measurement and is
    never used to trade.

    Returns a dict, or None when no peer book was found — the caller journals that
    rather than treating an unmeasured house as a safe one.

    D-01: the rotation desk's ETFs (rotation.UNIVERSE) are sector-level by construction —
    XLK IS the Information Technology sector — so they are left OUT of the per-name and
    per-sector cap tallies (by_symbol / by_sector) and kept IN `holdings`, where K-03
    measures their beta and their share of the house's independent bets.
    """
    peers = PEERS if peers is None else peers
    books = [("__this__", this_book)] + list((peers.get("books") or {}).items())
    if len(books) < 2:
        return None

    def _px(sym, fallback):
        q = pb.get(sym)
        if q and isinstance(q.get("price"), (int, float)) and q["price"] > 0:
            return q["price"]
        return fallback

    equity = 0.0
    by_symbol, by_sector, desks = {}, {}, {}
    holdings = []       # K-03: one line per (desk, symbol, kind) for house.metrics()
    for name, bk in books:
        if not isinstance(bk, dict):
            continue
        desk_label = DESK["name"] if name == "__this__" else name
        inv = 0.0
        if bk.get("kind") == "options":
            # D-02: a peer options book holds no shares. Its cash is house equity (less the
            # debit to close what it has sold), and each open structure enters the tally as
            # its beta-weighted-delta EQUITY EQUIVALENT — house.options_holdings(): net delta
            # × 100 × contracts × spot × beta — so a short put spread counts as the hidden
            # long it is, under the underlying's symbol and the "Index" sector.
            eq_o = house_mod.options_equity(bk)
            oh = house_mod.options_holdings(bk, desk_label)
            for h in oh:
                holdings.append(h)
                by_symbol[h["symbol"]] = round(by_symbol.get(h["symbol"], 0.0) + h["notional"], 2)
                by_sector[h["sector"]] = round(by_sector.get(h["sector"], 0.0) + h["notional"], 2)
            equity += eq_o
            desks[name] = {"equity": round(eq_o, 2), "invested": round(sum(h["notional"] for h in oh), 2),
                           "committed": 0.0, "kind": "options"}
            continue
        for p in bk.get("positions", []):
            px = _px(p["symbol"], p.get("last_price") or p.get("avg_cost") or 0.0)
            mv = px * p.get("shares", 0.0)
            inv += mv
            g = p.get("gics") or "Unclassified"
            if not rotation_mod.is_etf(p["symbol"]):
                by_symbol[p["symbol"]] = round(by_symbol.get(p["symbol"], 0.0) + mv, 2)
                by_sector[g] = round(by_sector.get(g, 0.0) + mv, 2)
            holdings.append({"desk": desk_label, "symbol": p["symbol"], "sector": g,
                             "notional": round(mv, 2), "kind": "position"})
        committed = 0.0
        for o in bk.get("working_orders", []):
            if o.get("side") != "buy" or o.get("status") != "working":
                continue
            mv = o.get("limit_price", 0.0) * o.get("shares", 0.0)
            committed += mv
            g = (o.get("meta") or {}).get("gics") or "Unclassified"
            if not rotation_mod.is_etf(o["symbol"]):
                by_symbol[o["symbol"]] = round(by_symbol.get(o["symbol"], 0.0) + mv, 2)
                by_sector[g] = round(by_sector.get(g, 0.0) + mv, 2)
            holdings.append({"desk": desk_label, "symbol": o["symbol"], "sector": g,
                             "notional": round(mv, 2), "kind": "order"})
        eq = float(bk.get("cash", 0.0)) + inv
        equity += eq
        desks[name] = {"equity": round(eq, 2), "invested": round(inv, 2),
                       "committed": round(committed, 2)}
    if equity <= 0:
        return None
    return {
        "desks": desks,
        "desk_count": len(desks),
        "equity": round(equity, 2),
        "holdings": holdings,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])),
        "by_sector": dict(sorted(by_sector.items(), key=lambda kv: -kv[1])),
        "symbol_pct": {k: round(v / equity * 100, 2) for k, v in by_symbol.items()},
        "sector_pct": {k: round(v / equity * 100, 2) for k, v in by_sector.items()},
        "caps": {"symbol_pct": PM_RULES["house_max_symbol_pct"],
                 "sector_pct": PM_RULES["house_max_sector_pct"]},
        "peers_loaded": list((peers.get("loaded") or [])),
        "peers_missing": list((peers.get("missing") or [])),
    }


def house_metrics(house, bars=None):
    """K-03 — the exposure block for the combined book HOUSE-01 just tallied.

    Runs ONCE per run on house["holdings"] (positions and working buys across every desk)
    and is attached as house["exposure"], so the state, the journal, the heartbeat and the
    coverage row all read one computation. `bars` is the staged bars.json content when
    the caller has one (BARS["rows"] by default); without it N_eff is the sector proxy and
    beta / momentum crowding are null, and the block says so.

    None when the house itself is unmeasured (no peer book) — the same rule HOUSE-01 keeps:
    an unmeasured house is not reported as a diversified one.
    """
    if not house:
        return None
    if bars is None:
        bars = BARS.get("rows")
    rules = PM_RULES.get("house_exposure") or {}
    try:
        return house_mod.metrics(house.get("holdings") or [], house.get("equity"),
                                 bars=bars, rules=rules)
    except Exception as exc:                      # noqa: BLE001 — never fail the manager
        return {"error": f"{type(exc).__name__}: {exc}", "flags": [], "reasons": [],
                "enforce": bool(rules.get("enforce")), "block_new_entries": False}


def desk_risk(book, pb, equity, bars=None, benchmark="SPY"):
    """K-04 — VaR and stress for THIS desk's marked positions, once per run.

    Weights are position market value over desk equity (working buys are not exposure
    until they fill). With bars staged, var.hs_var() runs a historical simulation over up
    to two years of the held names' shared daily returns and var.stress() replays the
    five named windows — each name's own bars when they reach that far, else β_252 × the
    benchmark's window return, labelled proxy. Without bars both are null WITH A REASON:
    an unmeasured VaR is reported as unmeasured, never as zero.

    Returns {"var", "stress", "flags", "reasons", "enforce", "block_new_entries",
             "worst_stress", "worst_stress_window", "rules", "bars"}. Never raises: any
    failure lands in the block as an error string and gates nothing.
    """
    rules = dict(var_mod.DEFAULT_RULES, **(PM_RULES.get("var") or {}))
    if bars is None:
        bars = BARS.get("rows")
    weights = {}
    eq = float(equity) if equity and equity > 0 else 0.0
    for p in (book or {}).get("positions", []):
        q = (pb or {}).get(p.get("symbol")) or {}
        px = q.get("price") if isinstance(q.get("price"), (int, float)) else p.get("avg_cost")
        if eq > 0 and isinstance(px, (int, float)) and px > 0 and p.get("shares"):
            weights[p["symbol"]] = weights.get(p["symbol"], 0.0) + px * p["shares"] / eq
    base = {"rules": {k: rules.get(k) for k in var_mod.DEFAULT_RULES},
            "bars": "staged" if bars is not None else "absent",
            "weights_pct": {s: round(v * 100.0, 2) for s, v in
                            sorted(weights.items(), key=lambda kv: -kv[1])}}
    try:
        if bars is None:
            reason = "no bars.json staged this run"
            v = {"var_pct": None, "cvar_pct": None, "n_days": 0, "window": None,
                 "alpha": rules["alpha"], "horizon_days": 1, "reason": reason}
            st = {}
        elif not weights:
            v = var_mod.hs_var({}, {}, alpha=rules["alpha"])
            st = {}
        else:
            rets = house_mod.daily_returns(house_mod.close_series(bars))
            feats, _, _ = house_mod.features_from_bars(bars, symbols=list(weights),
                                                       benchmark=benchmark)
            betas = {s: f.get("beta_252") for s, f in feats.items()}
            v = var_mod.hs_var(weights, rets, alpha=rules["alpha"])
            st = var_mod.stress(weights, rets, betas=betas, benchmark=benchmark)
        verdict = var_mod.assess(v, st, rules)
        return dict(base, var=v, stress=st, **verdict)
    except Exception as exc:                      # noqa: BLE001 — never fail the manager
        return dict(base, var={"var_pct": None, "cvar_pct": None, "n_days": 0, "window": None,
                               "reason": f"{type(exc).__name__}: {exc}"},
                    stress={}, flags=[], reasons=[], enforce=bool(rules.get("enforce")),
                    block_new_entries=False, worst_stress=None, worst_stress_window=None,
                    error=f"{type(exc).__name__}: {exc}")


def risk_compact(risk):
    """The journal / heartbeat summary of desk_risk() — var.compact plus nothing."""
    if not risk:
        return None
    return var_mod.compact(risk.get("var"), risk.get("stress"), risk)


def load_bars(path="bars.json"):
    """K-03 — the optional bars file, resolved inside the run directory like every other
    input. Absent is normal: the sentinel and most slots stage no bars, and the metrics
    degrade to the proxy rather than to nothing."""
    rows = _load(path)
    BARS["rows"] = rows if rows else None
    BARS["path"] = path if rows else None
    return BARS


def house_block(house, symbol, gics, notional):
    """Would adding `notional` of `symbol` breach a house cap? Returns a reason or None.

    Measured against the CURRENT combined equity, which does not change when cash
    becomes a position — a buy moves money from the cash column to the invested column
    inside the same book. So the denominator is stable and the test is simply whether
    the resulting exposure clears the cap.

    Preventive only. It refuses a new entry; it never sells anything to get back under
    a cap, exactly as CAP-01 and rebalance_pass do not force-sell a sector breach. An
    existing house breach is a decision for Vishal, and the journal names it.
    """
    if not house or not PM_RULES.get("house_caps_enabled", True):
        return None
    eq = house["equity"]
    if eq <= 0:
        return None
    sym_after = house["by_symbol"].get(symbol, 0.0) + notional
    sym_cap = eq * PM_RULES["house_max_symbol_pct"] / 100.0
    if sym_after > sym_cap:
        return (f"House cap: {symbol} would be ${sym_after:,.0f} across "
                f"{house['desk_count']} desks, {sym_after / eq * 100:.1f}% of the "
                f"${eq:,.0f} combined book, over the "
                f"{PM_RULES['house_max_symbol_pct']:.0f}% single-name house limit "
                f"(already ${house['by_symbol'].get(symbol, 0.0):,.0f} held elsewhere)")
    g = gics or "Unclassified"
    sec_after = house["by_sector"].get(g, 0.0) + notional
    sec_cap = eq * PM_RULES["house_max_sector_pct"] / 100.0
    if sec_after > sec_cap:
        return (f"House cap: {g} would be ${sec_after:,.0f} across "
                f"{house['desk_count']} desks, {sec_after / eq * 100:.1f}% of the "
                f"${eq:,.0f} combined book, over the "
                f"{PM_RULES['house_max_sector_pct']:.0f}% sector house limit")
    return None


def house_apply(house, symbol, gics, notional):
    """Book an accepted entry into the house tally so the NEXT proposal in the same run
    sees it. Without this a single slot could place three entries that are each fine
    alone and collectively over the cap — the same mistake CAP-01 fixed for one book."""
    if not house:
        return
    house["by_symbol"][symbol] = round(house["by_symbol"].get(symbol, 0.0) + notional, 2)
    g = gics or "Unclassified"
    house["by_sector"][g] = round(house["by_sector"].get(g, 0.0) + notional, 2)
    eq = house["equity"] or 1.0
    house["symbol_pct"] = {k: round(v / eq * 100, 2) for k, v in house["by_symbol"].items()}
    house["sector_pct"] = {k: round(v / eq * 100, 2) for k, v in house["by_sector"].items()}


# ------------------------------------------------------------------ day roll
def roll_day(book, today, jrn):
    day = book.get("day") or {}
    if day.get("date") == today.isoformat():
        return
    # a new session: every day order dies, intraday share tags reset
    for o in book.get("working_orders", []):
        if o.get("status") == "working":
            # D-01: a rotation order decided at power-hour is meant to fill at the NEXT
            # session's first slot. It survives exactly one roll; a second roll expires it.
            if o.get("expires") == "next-session" and not o.get("rolled"):
                o["rolled"] = today.isoformat()
                continue
            o["status"] = "expired"
            o["closed"] = today.isoformat()
            jrn["decisions"].append({"action": "expire", "symbol": o["symbol"],
                                     "shares": o["shares"], "price": o["limit_price"],
                                     "reason": "day-order expired unfilled at the session roll",
                                     "detail": o.get("reason")})
    book["working_orders"] = [o for o in book.get("working_orders", []) if o.get("status") == "working"]
    for p in book.get("positions", []):
        p["intraday_shares"] = 0.0
    book["day_trades"] = [d for d in book.get("day_trades", [])
                          if d.get("date") in set(_business_days_back(today, 10))]
    book["day"] = {"date": today.isoformat(), "open_equity": None,
                   "halted": False, "halt_reason": None}


# ------------------------------------------------------------------ ledger ops
def short_basis(basis, kind=None):
    """A column-width label for a stop's rationale; the full sentence stays in the data.
    The KIND matters more than the wording — an ATR stop and a structural stop size a
    position very differently on a volatile name, and the board must not blur them."""
    b = (basis or "").lower()
    if "breakeven" in b:
        return "breakeven"
    # K-07 — the policy stops name themselves before the generic ATR test sees "ATR".
    if "chandelier trail" in b:
        return "trail"
    if "chandelier" in b:
        return "chandelier"
    if "catastrophe" in b:
        return "catastrophe"
    if "atr" in b and "no atr available" not in b:
        return "1.5x ATR"
    if "200-day" in b:
        return "200-day"
    if "50-day" in b:
        return "50-day"
    if "fixed" in b:
        return "8% fixed"
    return kind or "—"


# ------------------------------------------------------------------ K-06 shadow fills
def _shadow_rules():
    """The active shadow model, or None when it is switched off. Off means OFF: no key is
    written anywhere, so the book and the journal are byte-identical to the pre-K-06 engine."""
    r = PM_RULES.get("shadow")
    return r if isinstance(r, dict) and r.get("enabled") else None


def _shadow_record(book, pb, sym, side, shares, booked, today, jrn, mid=None, cap=None,
                   adv=None):
    """The per-fill shadow record for a fill that has just been booked, or None when the
    model is off. `mid` is the DECISION-TIME mid — for an entry it was captured at placement
    and travels on the order; for a same-run sale the mid of the quote the sale fired on is
    the decision mid, because the decision and the fill are the same run. `cap` bounds the
    touch (the exit path passes min(last, stop) for a broken stop). With no two-sided quote
    the shadow IS the booked price, basis "no-quote"."""
    rules = _shadow_rules()
    if rules is None:
        return None
    q = pb.get(sym) or {}
    bid, ask = q.get("bid"), q.get("ask")
    if mid is None:
        mid = fills_mod.decision_mid(bid, ask)
    shadow = fills_mod.shadow_fill(side, bid, ask, booked, jrn["slot"], rules, adv_usd=adv,
                                   cap=cap)
    return fills_mod.record_shadow(book, side, shares, booked, shadow, mid, today)


def _apply_buy(book, sym, shares, price, meta, today, jrn, reason, shadow=None, detail=None):
    cost = shares * price
    book["cash"] = round(book["cash"] - cost, 6)
    pos = next((p for p in book["positions"] if p["symbol"] == sym), None)
    if pos:
        tot = pos["shares"] + shares
        pos["avg_cost"] = round((pos["avg_cost"] * pos["shares"] + cost) / tot, 6)
        pos["shares"] = _round_shares(tot)
        pos["intraday_shares"] = _round_shares(pos.get("intraday_shares", 0.0) + shares)
    else:
        book["positions"].append({
            "symbol": sym, "shares": _round_shares(shares), "avg_cost": round(price, 6),
            "opened": today.isoformat(), "opened_slot": jrn["slot"],
            "intraday_shares": _round_shares(shares),
            "stop": meta.get("stop"), "target": meta.get("target"),
            "stop_basis": meta.get("stop_basis"),
            "stop_basis_kind": meta.get("stop_basis_kind"),
            "stop_basis_short": short_basis(meta.get("stop_basis"),
                                            meta.get("stop_basis_kind")),
            "atr_14": meta.get("atr_14"), "atr_pct": meta.get("atr_pct"),
            "stop_pct": meta.get("stop_pct"),
            "entry_score": meta.get("score"), "trim_count": 0, "last_trim_date": None,
            "rebalance_count": 0, "last_rebalance_date": None,
            "gics": meta.get("gics"), "industry": meta.get("industry"),
            "thesis": meta.get("thesis"), "scaled_out": False,
            "high_water": round(price, 6), "last_price": round(price, 6),
            "last_priced": jrn["ts"],
        })
        pos = book["positions"][-1]
        # K-07 — the stop-policy record. An order placed before K-07 carries no policy and
        # was sized under the fixed_atr rule, so that is what it is recorded as.
        stop = meta.get("stop")
        risk = meta.get("initial_risk")
        if not isinstance(risk, (int, float)) and isinstance(stop, (int, float)):
            risk = round(price - stop, 6)
        pos.update({
            "stop_policy": meta.get("stop_policy") or stops_mod.DEFAULT_POLICY,
            "initial_risk": risk,
            "initial_risk_usd": (round(risk * pos["shares"], 2)
                                 if isinstance(risk, (int, float)) else None),
            "sessions_held": 0,
            "highest_close": round(price, 6),
            "trail_level": None,
        })
        # D-01: the rotation record rides only on a rotation entry — no other desk's
        # position grows a key. `hold_from` is the date the rank last affirmed the name;
        # exit_pass counts the time stop from it.
        if meta.get("rotation"):
            pos["rotation"] = dict(meta["rotation"])
            pos["hold_from"] = today.isoformat()
    _policy().record_buy(book, pos, shares, today, price)
    rec = {"action": "fill-buy", "symbol": sym, "shares": round(shares, 6),
           "price": round(price, 4), "reason": reason,
           "detail": detail or f"${cost:,.2f} filled at the resting limit"}
    if shadow:
        rec.update(shadow)
    jrn["decisions"].append(rec)


def _apply_sell(book, sym, shares, price, today, jrn, reason, detail, shadow=None):
    """Book a sale. `shadow` is a callable (shares -> per-fill shadow record) or None — a
    callable because the shares actually sold are clamped to the position HERE, and the
    shadow gap must be measured on the shares that moved."""
    pos = next((p for p in book["positions"] if p["symbol"] == sym), None)
    if not pos:
        return
    shares = min(shares, pos["shares"])
    proceeds = shares * price
    pnl = (price - pos["avg_cost"]) * shares
    book["cash"] = round(book["cash"] + proceeds, 6)
    book["realized_pnl"] = round(book.get("realized_pnl", 0.0) + pnl, 6)
    # The policy books the sale (a day-trade record, settlement, a GFV) off the position's
    # intraday tag BEFORE it is decremented; the tag itself is the book's, not the policy's.
    _policy().record_sale(book, pos, shares, today, price, reason)
    intraday = pos.get("intraday_shares", 0.0)
    if intraday > 0 and shares > 0:
        pos["intraday_shares"] = _round_shares(intraday - min(shares, intraday))
    closed = {
        "symbol": sym, "shares": round(shares, 6), "entry": pos["avg_cost"],
        "exit": round(price, 4), "opened": pos.get("opened"), "closed": today.isoformat(),
        "closed_slot": jrn["slot"], "pnl": round(pnl, 2),
        "pnl_pct": round((price / pos["avg_cost"] - 1) * 100, 2) if pos["avg_cost"] else 0.0,
        "reason": reason, "detail": detail}
    # K-06: the shadow is measured AFTER realized_pnl is booked, so gap_share_of_realized
    # on the book reflects this sale; it is measured on the clamped share count.
    srec = shadow(shares) if shadow else None
    if srec:
        closed.update(srec)
    book.setdefault("closed_trades", []).append(closed)
    pos["shares"] = _round_shares(pos["shares"] - shares)
    if pos["shares"] <= 0 or pos["shares"] * price < 0.01:
        book["positions"] = [p for p in book["positions"] if p["symbol"] != sym]
    rec = {"action": "fill-sell", "symbol": sym, "shares": round(shares, 6),
           "price": round(price, 4), "reason": reason,
           "detail": f"{detail} — realised ${pnl:+,.2f}"}
    if srec:
        rec.update(srec)
    jrn["decisions"].append(rec)


# ------------------------------------------------------------------ 1. fills
def _fill_stop_buy(book, o, pb, today, jrn):
    """D-03 — one "stop-buy" order against the day's 5-minute bars (orb.check_stop_buy).

    Fills at max(trigger, the triggering bar's open) + the shadow haircut (k half-spreads
    from the quote when there is one, `fill_half_spread_bps` of price otherwise), cancels
    unfilled once the ET clock is past the order's `cancel_after_et`, and records the bar
    the fill came from on the position (`orb_managed_through`) so the exit pass walks only
    the bars after it. A quote is not required: the bars are the price source."""
    rules = _orb_rules() or orb_mod.rules_for(None)
    sym = o["symbol"]
    q = pb.get(sym) or {}
    bars = (ORB.get("bars_5m") or {}).get(sym)
    now_et = orb_mod.to_et(jrn["ts"])
    if bars:
        res = orb_mod.check_stop_buy(o, bars, now_et, rules, bid=q.get("bid"), ask=q.get("ask"))
        witness = "5-min bars"
    else:
        px = tradeable(pb, sym)
        trig = float(o.get("trigger_price") or o["limit_price"])
        if px is not None and px >= trig:
            base = max(px, trig)
            res = {"status": "fill", "price": round(base + orb_mod.haircut(base, q.get("bid"),
                                                                            q.get("ask"), rules), 4),
                   "base": round(base, 4), "bar": None,
                   "reason": f"quote {px:,.2f} at or through the {trig:,.2f} trigger — no "
                             "5-min bars staged, filled on the print + haircut"}
        elif now_et is not None and orb_mod._hhmm(now_et) >= orb_mod._hhmm(
                o.get("cancel_after_et") or rules["cancel_after_et"]):
            res = {"status": "cancel", "price": None, "bar": None,
                   "reason": f"unfilled by {o.get('cancel_after_et') or rules['cancel_after_et']} "
                             "ET — the opening-range breakout did not come"}
        else:
            res = {"status": "working", "price": None, "bar": None, "reason": None}
        witness = "quote (no 5-min bars staged)"
    if res["status"] == "cancel":
        o["status"] = "cancelled"
        o["closed"] = jrn["ts"]
        jrn["decisions"].append({"action": "cancel", "symbol": sym, "shares": o["shares"],
                                 "price": o["limit_price"], "reason": "ORB stop-buy expired",
                                 "detail": res["reason"]})
        return
    if res["status"] != "fill":
        return
    price = res["price"]
    o["status"] = "filled"
    o["fill_price"] = price
    o["fill_base"] = res.get("base")
    o["fill_bar"] = res.get("bar")
    o["closed"] = jrn["ts"]
    cash_short = price * o["shares"] - float(book.get("cash") or 0.0)
    if cash_short > 0:
        jrn["warnings"].append(f"{sym}: ORB stop-buy filled ${cash_short:,.2f} over the cash "
                               "line — the haircut and the gap above the trigger cost more "
                               "than the order reserved; the book carries the overdraft.")
    _apply_buy(book, sym, o["shares"], price, o.get("meta", {}), today, jrn,
               o.get("reason", "ORB entry"),
               shadow=_shadow_record(book, pb, sym, "buy", o["shares"], price, today, jrn,
                                     mid=o.get("decision_mid"),
                                     adv=(o.get("meta") or {}).get("adv_usd")),
               detail=f"${price * o['shares']:,.2f} — {res['reason']} [{witness}]")
    pos = next((p for p in book["positions"] if p["symbol"] == sym), None)
    if pos is not None:
        pos["orb_managed_through"] = res.get("bar")
        pos["orb_entry_bar"] = res.get("bar")
        pos["highest_close"] = round(price, 6)
        pos["highest_high"] = round(price, 6)      # the trail anchor: 5-min highs from here


def simulate_fills(book, pb, today, run_key, jrn, scan_by_tk):
    """Work the resting order book against this slot's prices."""
    for o in book.get("working_orders", []):
        if o.get("status") != "working":
            continue
        if o.get("type") == "stop-buy":
            # D-03: a buy-stop is worked against the 5-MINUTE BARS, not the slot's print —
            # the trigger lives between sentinels. With no bars staged the quote stands in
            # (a print at or through the trigger fills at the print plus the haircut), and
            # that is journaled as the weaker witness it is.
            if o.get("run_key") == run_key:
                continue                      # never fills in the run that placed it
            _fill_stop_buy(book, o, pb, today, jrn)
            continue
        px = tradeable(pb, o["symbol"])
        if px is None:
            src = (pb.get(o["symbol"]) or {}).get("source", "no quote")
            jrn["warnings"].append(f"{o['symbol']}: working order left untouched — no fresh "
                                   f"price this slot ({src}). It cannot be filled or cancelled "
                                   "on a stale quote.")
            continue
        if o["side"] == "buy":
            if o.get("run_key") == run_key:
                continue                      # never fills in the run that placed it
            r = scan_by_tk.get(o["symbol"])
            lapsed = r and (r.get("setup") == "Broken Trend" or
                            (r.get("score") is not None and r["score"] < RULES["min_score_to_propose"]))
            if lapsed:
                o["status"] = "cancelled"
                o["closed"] = jrn["ts"]
                jrn["decisions"].append({"action": "cancel", "symbol": o["symbol"],
                                         "shares": o["shares"], "price": o["limit_price"],
                                         "reason": "thesis lapsed before the fill",
                                         "detail": f"score {r.get('score')} / {r.get('setup')} "
                                                   "no longer clears the entry floor"})
                continue   # cash was never debited for a working order, only reserved
            if o.get("fill_rule") == "marketable":
                # D-01: a rotation order fills at THIS slot's price plus the exit-slippage
                # assumption, whatever the limit — the limit is the decision price, kept
                # on the order for the audit. Pessimistic in the same direction as every
                # other fill here. Shares are clamped to the cash actually on hand.
                fill_px = round(px * (1 + PM_RULES["exit_slippage_pct"] / 100), 4)
                shares = _round_shares(min(o["shares"], max(0.0, book["cash"]) / fill_px))
                if shares <= 0 or shares * fill_px < RULES["min_notional"]:
                    o["status"] = "cancelled"
                    o["closed"] = jrn["ts"]
                    jrn["decisions"].append({"action": "cancel", "symbol": o["symbol"],
                                             "shares": o["shares"], "price": o["limit_price"],
                                             "reason": "rotation entry unaffordable",
                                             "detail": f"cash ${book['cash']:,.2f} cannot cover "
                                                       "the sleeve at the fill price"})
                    continue
                o["status"] = "filled"
                o["fill_price"] = fill_px
                o["filled_shares"] = shares
                o["closed"] = jrn["ts"]
                _apply_buy(book, o["symbol"], shares, fill_px, o.get("meta", {}),
                           today, jrn, o.get("reason", "entry"),
                           shadow=_shadow_record(book, pb, o["symbol"], "buy", shares,
                                                 fill_px, today, jrn,
                                                 mid=o.get("decision_mid"),
                                                 adv=(o.get("meta") or {}).get("adv_usd")),
                           detail=(f"${shares * fill_px:,.2f} filled marketable at "
                                   f"{px:,.2f} + {PM_RULES['exit_slippage_pct']:.2f}% "
                                   f"slippage (decided at {o['limit_price']:,.2f})"))
                continue
            if px <= o["limit_price"]:
                o["status"] = "filled"
                o["fill_price"] = o["limit_price"]
                o["closed"] = jrn["ts"]
                # K-06: the decision mid was captured when the order was PLACED and rides
                # on the order; the shadow price is what a marketable buy pays against
                # THIS slot's quote. Both None-safe when the model is off.
                _apply_buy(book, o["symbol"], o["shares"], o["limit_price"], o.get("meta", {}),
                           today, jrn, o.get("reason", "entry"),
                           shadow=_shadow_record(book, pb, o["symbol"], "buy", o["shares"],
                                                 o["limit_price"], today, jrn,
                                                 mid=o.get("decision_mid"),
                                                 adv=(o.get("meta") or {}).get("adv_usd")))
        else:  # sell limit resting at a target
            if px >= o["limit_price"]:
                o["status"] = "filled"
                o["fill_price"] = o["limit_price"]
                o["closed"] = jrn["ts"]
                _apply_sell(book, o["symbol"], o["shares"], o["limit_price"], today, jrn,
                            o.get("kind", "target"), o.get("reason", "resting target"),
                            shadow=lambda n, _s=o["symbol"], _p=o["limit_price"]:
                            _shadow_record(book, pb, _s, "sell", n, _p, today, jrn))
    book["working_orders"] = [o for o in book.get("working_orders", []) if o.get("status") == "working"]


# ------------------------------------------------------------------ 2. guards
def kill_switch(book, marked, jrn):
    day = book["day"]
    if day.get("open_equity") is None:
        day["open_equity"] = marked["equity"]
    open_eq = day["open_equity"] or marked["equity"]
    pnl_pct = ((marked["equity"] - open_eq) / open_eq * 100) if open_eq else 0.0
    jrn["daily_pnl_pct"] = round(pnl_pct, 2)
    if not day.get("halted") and pnl_pct <= -RULES["max_daily_loss_pct"]:
        day["halted"] = True
        day["halt_reason"] = (f"Daily loss {pnl_pct:.2f}% breached the "
                              f"{RULES['max_daily_loss_pct']:.0f}% kill switch")
        for o in book.get("working_orders", []):
            if o["side"] == "buy" and o.get("status") == "working":
                o["status"] = "cancelled"
                o["closed"] = jrn["ts"]
                jrn["decisions"].append({"action": "cancel", "symbol": o["symbol"],
                                         "shares": o["shares"], "price": o["limit_price"],
                                         "reason": "kill switch", "detail": day["halt_reason"]})
        book["working_orders"] = [o for o in book["working_orders"] if o.get("status") == "working"]
        jrn["warnings"].append("KILL SWITCH TRIPPED — " + day["halt_reason"] +
                               ". No new entries for the rest of the session; exits stay live.")
    return pnl_pct


def _cancel_working_buys(book, jrn, reason, detail):
    """Cancel every resting buy — the kill switch's own cleanup, shared with the ladder."""
    for o in book.get("working_orders", []):
        if o["side"] == "buy" and o.get("status") == "working":
            o["status"] = "cancelled"
            o["closed"] = jrn["ts"]
            jrn["decisions"].append({"action": "cancel", "symbol": o["symbol"],
                                     "shares": o["shares"], "price": o["limit_price"],
                                     "reason": reason, "detail": detail})
    book["working_orders"] = [o for o in book["working_orders"] if o.get("status") == "working"]


def ladder_pass(book, pb, today, marked, jrn, day_pnl_pct):
    """K-02. Evaluate the drawdown ladder once per run, after the mark and the kill switch.

    Maintains book["hwm"] (never decreases). At rung 3 the book is HALTED through the same
    path as the kill switch — day.halted, working buys cancelled — and then FLATTENED:
    every position with a fresh price is sold at the slot price less exit slippage, through
    the broker policy like any other exit. A position that cannot be priced is reported
    UNPROTECTED exactly as the exit pass would; it is not sold on a stale mark. The
    cool-off record goes on the book (cool_until, ladder_halt) and the ladder re-arms
    from the post-flatten equity. Returns the ladder state dict; the entry pass reads it.
    """
    rules = PM_RULES.get("ladder") or ladder_mod.DEFAULT
    st = ladder_mod.state_for(book, marked["equity"], today, rules, day_pnl_pct)
    book["hwm"] = max(float(book.get("hwm") or 0.0), st["hwm"])
    day = book.setdefault("day", {})
    if st["soft_daily_hit"] and not day.get("ladder_soft_hit"):
        day["ladder_soft_hit"] = True
        jrn["warnings"].append("SOFT DAILY LEVEL — " + st["reason"] + ".")
    if st["regained"]:
        book["cool_until"] = None
        book["ladder_halt"] = None
        jrn["warnings"].append(f"Ladder cleared: equity ${marked['equity']:,.2f} regained the "
                               "high-water mark — entries back to full size, cool-off record "
                               "cleared.")
    if not st["halt"]:
        return st

    # ---- rung 3: halt, then flatten -------------------------------------------------
    reason = f"Drawdown ladder halt — {st['reason']}"
    if not day.get("halted"):
        day["halted"] = True
        day["halt_reason"] = reason
    _cancel_working_buys(book, jrn, "drawdown ladder halt", reason)
    jrn["warnings"].append("DRAWDOWN LADDER HALT — " + st["reason"] +
                           ". Every position with a fresh price is being closed; the book "
                           "cools off and re-enters at half size until the high-water mark "
                           "is back.")
    slip = 1 - PM_RULES["exit_slippage_pct"] / 100
    for pos in list(book.get("positions", [])):
        sym = pos["symbol"]
        px = tradeable(pb, sym)
        if px is None:
            src = (pb.get(sym) or {}).get("source")
            jrn["warnings"].append(
                f"UNPROTECTED: {sym} has no fresh price ({src or 'none'}) and cannot be "
                "flattened by the ladder halt on a stale mark — the position is carried as-is. "
                "Close it by hand.")
            jrn["skipped"].append({"symbol": sym, "reason": "ladder flatten wanted to fire — "
                                                             "no fresh price"})
            continue
        sellable, note = _sellable(pos, pos["shares"], "flatten", book, today,
                                   marked["equity"], jrn)
        if note:
            jrn["warnings"].append(f"{sym}: {note}")
        if sellable <= 0:
            jrn["skipped"].append({"symbol": sym, "reason": "ladder flatten wanted to fire — "
                                                             + (note or "blocked")})
            jrn["warnings"].append(
                f"UNPROTECTED: {sym} — the ladder halt wanted it flat and the {_policy().name} "
                "policy refused the sale. Close it by hand if you disagree.")
            continue
        booked = round(px * slip, 4)
        _apply_sell(book, sym, sellable, booked, today, jrn, "flatten",
                    f"Ladder halt: {st['dd_pct']:.2f}% under the ${st['hwm']:,.2f} high-water mark",
                    shadow=lambda n, _s=sym, _b=booked:
                    _shadow_record(book, pb, _s, "sell", n, _b, today, jrn))
    after = mark_book(book, pb)
    rec = ladder_mod.halt_record(st, today, after["equity"], rules)
    book["ladder_halt"] = rec
    book["cool_until"] = rec["cool_until"]
    st["cool_until"] = rec["cool_until"]
    st["reentry_base"] = rec["equity_after"]
    return st


# ------------------------------------------------------------------ 3. exits
def _sellable(pos, want_shares, reason, book, today, equity, jrn):
    """Ask the broker policy about a proposed sale. Returns (shares actually sellable, note).
    Under legacy_pdt this is the old PDT guard; under intraday_margin every sale goes."""
    return _policy().sellable(pos, want_shares, reason, book, today, equity)


def exit_pass(book, pb, scan_by_tk, today, equity, jrn):
    slip = 1 - PM_RULES["exit_slippage_pct"] / 100
    for pos in list(book.get("positions", [])):
        sym = pos["symbol"]
        px = tradeable(pb, sym)
        if px is None:
            src = (pb.get(sym) or {}).get("source")
            why = (f"last price is {src} and cannot be trusted to fire a stop"
                   if src else "no price at all this slot")
            jrn["warnings"].append(f"UNPROTECTED: {sym} has no fresh price — {why}. "
                                   "The stop cannot be evaluated and the position is carried "
                                   "as-is. Price it by hand if this persists.")
            jrn["skipped"].append({"symbol": sym, "reason": f"no fresh price — {why}"})
            continue
        pos["high_water"] = max(pos.get("high_water") or px, px)
        r = scan_by_tk.get(sym)
        # D-03 — an ORB position is exited by its stop, its trail and the close, never by a
        # scan score: the scan row is not consulted for it at all.
        orb_rules = _orb_rules()
        if orb_rules:
            r = None
        # D-03: the trail anchor is the highest 5-minute HIGH since entry (`highest_high`);
        # `highest_close` below is the generic "highest price the book has seen" field and
        # is bumped by the print like any other desk's.
        orb_prior_hh = pos.get("highest_high")

        # K-07 — the position's stop policy. Bookkeeping first, for EVERY position: a
        # position opened before K-07 is recorded as fixed_atr (the rule it was sized under)
        # and picks up the tracking fields on its next visit. Then, for a policy other than
        # fixed_atr, the policy's update(): a stop it raises is applied and journaled — it
        # can never lower one, stops.apply_update enforces that — and an exit it calls is
        # taken ahead of the thesis checks, exactly where the stop test sits today.
        pol_name = pos.get("stop_policy") or stops_mod.DEFAULT_POLICY
        pos["stop_policy"] = pol_name
        # D-01: a rotation holding counts from the date the rank last affirmed it
        # (`hold_from`), so a name re-selected at month-end is not closed by the 25-session
        # time stop four days into a month that just re-affirmed it. Only rotation entries
        # carry the key; every other position counts from `opened` as before.
        pos["sessions_held"] = stops_mod.sessions_between(pos.get("hold_from") or pos.get("opened"),
                                                          today)
        pos["highest_close"] = max(pos.get("highest_close") or px, px)
        pos.setdefault("trail_level", None)
        if "initial_risk" not in pos:
            st = pos.get("stop")
            pos["initial_risk"] = (round(pos["avg_cost"] - st, 6)
                                   if isinstance(st, (int, float))
                                   and pos.get("stop_basis_kind") != "breakeven" else None)
            pos["initial_risk_usd"] = (round(pos["initial_risk"] * pos["shares"], 2)
                                       if isinstance(pos["initial_risk"], (int, float)) else None)
        policy_exit = None
        orb_exit_px = None
        orb_bars = ((ORB.get("bars_5m") or {}).get(sym) if orb_rules else None)
        if orb_rules and orb_bars:
            # D-03 — the 5-minute bars since the last visit are the witness, not the print:
            # a low through the stop is a hit (at min(stop, that bar's open)); otherwise the
            # highest 5-minute HIGH ratchets the desk's 0.5×ATR chandelier trail up. The
            # generic policy update is skipped for this position — orb.manage_position did
            # the same job on better data — and the print still decides below when the bars
            # said nothing (the trail is enforced against `px` as well).
            since = orb_mod.bars_after(orb_bars, pos.get("orb_managed_through"),
                                       date=today.isoformat())
            m = orb_mod.manage_position(dict(pos, highest_high=orb_prior_hh), since, orb_rules,
                                        atr=pos.get("atr_14"))
            old = pos.get("stop")
            if m.get("managed_through"):
                pos["orb_managed_through"] = m["managed_through"]
            pos["highest_high"] = m["highest_high"]       # the bars' highs, never the print
            # keep the generic field at least as high, so a later bar-less visit (the quote
            # path through stops.py) trails from the same anchor and never lowers it
            pos["highest_close"] = max(pos.get("highest_close") or 0.0, m["highest_high"])
            pos["trail_level"] = m.get("trail_level")
            if isinstance(m.get("stop"), (int, float)) and (
                    not isinstance(old, (int, float)) or m["stop"] > old):
                pos["stop"] = m["stop"]
                pos["stop_basis"] = (f"chandelier trail: {orb_rules['k_trail']:g}x ATR "
                                     f"({pos.get('atr_14') or 0:.2f}) under the "
                                     f"${pos['highest_high']:,.2f} highest 5-min high — ratchets up only")
                pos["stop_basis_kind"] = "trail"
                pos["stop_basis_short"] = short_basis(pos["stop_basis"], "trail")
                if not m.get("exit_reason"):
                    jrn["decisions"].append({
                        "action": "raise-stop", "symbol": sym, "shares": pos["shares"],
                        "price": pos["stop"], "reason": "stop raised",
                        "detail": (f"orb: stop raised from {old:,.2f} to {pos['stop']:,.2f} — "
                                   f"{pos['stop_basis']}" if isinstance(old, (int, float)) else
                                   f"orb: stop set at {pos['stop']:,.2f} — {pos['stop_basis']}")})
            if m.get("exit_reason"):
                policy_exit = m["exit_reason"]
                orb_exit_px = m["exit_price"]
            elif isinstance(pos.get("stop"), (int, float)) and px <= pos["stop"]:
                policy_exit = "trail" if pos.get("stop_basis_kind") == "trail" else "stop"
        elif pol_name != stops_mod.DEFAULT_POLICY:
            res = stops_mod.apply_update(_stop_policy(pol_name), pos, px, pos["sessions_held"],
                                         {"atr": (r or {}).get("atr_14")})
            meta = res.get("meta") or {}
            pos["trail_level"] = meta.get("trail_level")
            if meta.get("raised") and isinstance(res.get("stop"), (int, float)):
                old = pos.get("stop")
                pos["stop"] = res["stop"]
                pos["stop_basis"] = meta.get("stop_basis") or pos.get("stop_basis")
                pos["stop_basis_kind"] = meta.get("stop_basis_kind") or pos.get("stop_basis_kind")
                pos["stop_basis_short"] = short_basis(pos["stop_basis"], pos["stop_basis_kind"])
            if meta.get("journal_raise"):
                jrn["decisions"].append({
                    "action": "raise-stop", "symbol": sym, "shares": pos["shares"],
                    "price": pos["stop"], "reason": "stop raised",
                    "detail": (f"{pol_name}: stop raised from "
                               f"{old:,.2f} to {pos['stop']:,.2f} — {pos['stop_basis']}"
                               if isinstance(old, (int, float)) else
                               f"{pol_name}: stop set at {pos['stop']:,.2f} — {pos['stop_basis']}")})
            policy_exit = res.get("exit_reason")
        # K-07 — an intraday desk (the orb template) never holds overnight: at the
        # power-hour slot every position is closed, whatever the policy says.
        flatten_close = (bool((DESK.get("rules") or {}).get("flatten_at_close"))
                         and jrn["slot"] == "power-hour" and not policy_exit)
        if flatten_close:
            policy_exit = "time"

        action = detail = None
        want = 0.0
        if flatten_close:
            action, want = "time", pos["shares"]
            detail = "Flatten at close: this desk holds nothing overnight (flatten_at_close)"
        elif policy_exit:
            # K-07 — the policy's exit, mapped onto the journal's words. "trail" is a stop
            # (a ratcheted one, and the detail says so); "time" is its own word.
            want = pos["shares"]
            if policy_exit in ("stop", "trail"):
                action = "stop"
                detail = (f"Price {px:,.2f} broke the {pos['stop']:,.2f} "
                          f"{'trailing ' if policy_exit == 'trail' else ''}stop "
                          f"({pos.get('stop_basis')})")
                if orb_exit_px is not None:
                    detail = (f"A 5-min low broke the {pos['stop']:,.2f} "
                              f"{'trailing ' if policy_exit == 'trail' else ''}stop — exit at "
                              f"{orb_exit_px:,.2f} ({'the open, gap through' if orb_exit_px < pos['stop'] else 'the stop'}; "
                              f"{pos.get('stop_basis')})")
            elif policy_exit == "target":
                action = "target"
                detail = (f"Target {pos['target']:,.2f} hit — closing whole "
                          f"({pol_name}: no scale-out under this policy)")
            else:
                action = "time"
                detail = (f"Time stop: {pos['sessions_held']} sessions held under {pol_name} "
                          f"— {'below 1R, ' if pol_name == 'chandelier' else ''}the holding "
                          "period is the exit")
        elif pos.get("stop") and px <= pos["stop"]:
            action, want = "stop", pos["shares"]
            detail = f"Price {px:,.2f} broke the {pos['stop']:,.2f} stop ({pos.get('stop_basis')})"
        elif r and r.get("setup") == "Broken Trend":
            action, want = "thesis", pos["shares"]
            detail = f"Setup is now Broken Trend — below the 200-day, score {r.get('score', 0):.0f}"
        elif r and r.get("score") is not None and r["score"] < RULES["exit_score_below"]:
            action, want = "thesis", pos["shares"]
            detail = f"Score fell to {r['score']:.0f} — the thesis that bought it is gone"
        elif (pol_name == stops_mod.DEFAULT_POLICY and pos.get("target") and px >= pos["target"]
              and not pos.get("scaled_out")):
            want = _round_shares(pos["shares"] * PM_RULES["scale_out_pct"] / 100)
            rest = pos["shares"] - want
            if rest * px < RULES["min_notional"]:
                want = pos["shares"]
                detail = (f"Target {pos['target']:,.2f} hit — closing whole; a half position "
                          f"would be under the ${RULES['min_notional']:.2f} minimum")
            else:
                detail = f"Target {pos['target']:,.2f} hit — taking half, stop to breakeven"
            action = "target"
        else:
            why = None
            if (r and r.get("score") is not None
                    and RULES["exit_score_below"] <= r["score"] < RULES["trim_score_below"]):
                why = f"Score {r['score']:.0f} — weakening"
            elif r and r.get("upside_pct") is not None and r["upside_pct"] < 0:
                why = f"Trading {r['upside_pct']:+.1f}% through the analyst target"
            if why:
                if pos.get("last_trim_date") == today.isoformat():
                    jrn["skipped"].append({"symbol": sym, "reason":
                                           f"{why}, but it was already trimmed today — "
                                           "one trim per name per session"})
                elif pos.get("trim_count", 0) >= PM_RULES["max_trims_before_exit"]:
                    action, want = "thesis", pos["shares"]
                    detail = (f"{why}. Already trimmed {pos['trim_count']} times — a name that "
                              "keeps weakening gets closed, not shaved again.")
                else:
                    action, want = "trim", _round_shares(pos["shares"] * PM_RULES["trim_pct"] / 100)
                    detail = f"{why} — cutting a third (trim {pos.get('trim_count', 0) + 1} of "
                    detail += f"{PM_RULES['max_trims_before_exit']} before a full exit)"

        if not action or want <= 0:
            if r is None and not jrn.get("sentinel") and not jrn.get("rotation") and not orb_rules:
                # A sentinel carries no scan by design; only a decision slot should note
                # that a holding fell out of the universe. The rotation desk judges its
                # holdings by rank at its own slot (rotation_pass), not by scan row.
                jrn["warnings"].append(f"{sym}: not in this scan's universe — priced but unjudged")
            continue

        sellable, note = _sellable(pos, want, action, book, today, equity, jrn)
        if note:
            jrn["warnings"].append(f"{sym}: {note}")
        if sellable <= 0:
            jrn["skipped"].append({"symbol": sym,
                                   "reason": f"{action} wanted to fire — " + (note or "blocked")})
            if action == "stop":
                jrn["warnings"].append(
                    f"UNPROTECTED: {sym} broke its stop and the {_policy().name} policy refused "
                    "the sale — the position is still open and cannot be closed today without "
                    "a violation. Close it by hand if you disagree.")
            continue
        if sellable * px < RULES["min_notional"] and sellable < pos["shares"]:
            jrn["skipped"].append({"symbol": sym,
                                   "reason": f"{action} sized to ${sellable * px:.2f}, under the "
                                             f"${RULES['min_notional']:.2f} broker minimum"})
            continue
        booked = round(px * slip, 4)
        if orb_exit_px is not None and action == "stop":
            # D-03: the bar said where the stop was hit; the exit slippage still applies.
            booked = round(orb_exit_px * slip, 4)
        # K-06: a broken stop's shadow touch is capped at min(last, stop) — never the
        # stop price, and never a stale bid sitting above the print. A stop is not
        # resting at the broker; the honest witness is the tape.
        cap = None
        if action == "stop" and (_shadow_rules() or {}).get("gap_through_stops", True):
            cap = min(px, float(pos["stop"]))
        _apply_sell(book, sym, sellable, booked, today, jrn, action, detail,
                    shadow=lambda n, _s=sym, _b=booked, _c=cap, _row=r:
                    _shadow_record(book, pb, _s, "sell", n, _b, today, jrn, cap=_c,
                                   adv=fills_mod.adv_usd(_row)))
        if action == "trim":
            live = next((p for p in book["positions"] if p["symbol"] == sym), None)
            if live:
                live["trim_count"] = live.get("trim_count", 0) + 1
                live["last_trim_date"] = today.isoformat()
        if action == "target" and pol_name == stops_mod.DEFAULT_POLICY:
            # K-07: the scale-out and the breakeven move belong to fixed_atr only. A
            # policy target closes the whole position and never leaves a runner.
            live = next((p for p in book["positions"] if p["symbol"] == sym), None)
            if live:                      # a runner survived, so this was a scale-out
                live["scaled_out"] = True
                live["stop"] = round(live["avg_cost"], 2)
                live["stop_basis"] = "breakeven — half the position was taken at the 3R target"
                live["stop_basis_kind"] = "breakeven"
                live["stop_basis_short"] = "breakeven"


# ------------------------------------------------------------------ 3b. rotation (D-01)
def rotation_pass(book, pb, rot, today, equity, jrn):
    """The rotation desk's own exit: a held ETF that has left the target set is sold whole,
    marketable at the slot price less exit slippage, journaled as `rotation-exit`. A held
    ETF the rank re-affirmed gets `hold_from` reset to today so the time stop counts from
    this decision. Runs only when the desk's decision block says it acts; the ordinary exit
    pass (catastrophe stop, time stop) has already run and is untouched."""
    if not rot or not rot.get("acts"):
        return
    slip = 1 - PM_RULES["exit_slippage_pct"] / 100
    target = set(rot.get("target") or [])
    for pos in list(book.get("positions", [])):
        sym = pos["symbol"]
        if sym in target:
            if pos.get("hold_from") != today.isoformat():
                pos["hold_from"] = today.isoformat()
                jrn["skipped"].append({"symbol": sym, "reason":
                                       f"rotation: re-affirmed in the target set "
                                       f"({rot['mode']}) — held, time stop restarts today"})
            continue
        px = tradeable(pb, sym)
        if px is None:
            src = (pb.get(sym) or {}).get("source")
            jrn["warnings"].append(
                f"UNPROTECTED: {sym} left the rotation target set and has no fresh price "
                f"({src or 'none'}) — it cannot be sold on a stale mark and is carried as-is.")
            jrn["skipped"].append({"symbol": sym, "reason":
                                   "rotation-exit wanted to fire — no fresh price"})
            continue
        sellable, note = _sellable(pos, pos["shares"], "rotation-exit", book, today, equity, jrn)
        if note:
            jrn["warnings"].append(f"{sym}: {note}")
        if sellable <= 0:
            jrn["skipped"].append({"symbol": sym, "reason":
                                   "rotation-exit wanted to fire — " + (note or "blocked")})
            continue
        booked = round(px * slip, 4)
        rk = next((r for r in rot.get("ranks") or [] if r["symbol"] == sym), {})
        why = (f"rank {rk['rank']} of {len(rot.get('ranks') or [])}"
               if rk.get("rank") else "not in the ranking")
        if not rot.get("filter_on") and sym != rot.get("bond"):
            why = "absolute-momentum filter is OFF — the book moves to the bond leg"
        elif rot.get("filter_on") and sym == rot.get("bond"):
            why = "absolute-momentum filter is ON — the bond leg is replaced by sectors"
        _apply_sell(book, sym, sellable, booked, today, jrn, "rotation-exit",
                    f"Left the rotation target set ({rot['mode']}): {why}; "
                    f"target is {', '.join(rot.get('target') or [])}",
                    shadow=lambda n, _s=sym, _b=booked:
                    _shadow_record(book, pb, _s, "sell", n, _b, today, jrn))


# ------------------------------------------------------------------ 4. rebalance
def rebalance_pass(book, pb, today, equity, jrn, house=None):
    slip = 1 - PM_RULES["exit_slippage_pct"] / 100
    cap = equity * RULES["max_position_pct"] / 100
    # Trigger above the cap PLUS the deadband; trim back to the cap itself. The cap is
    # still the cap — the deadband only decides when a breach is worth acting on.
    trigger = equity * (RULES["max_position_pct"] + PM_RULES["rebalance_deadband_pct"]) / 100
    for pos in list(book.get("positions", [])):
        px = tradeable(pb, pos["symbol"])
        if px is None:
            continue
        val = px * pos["shares"]
        if val <= cap:
            continue
        pct = val / equity * 100 if equity else 0.0
        if val <= trigger:
            jrn["skipped"].append({"symbol": pos["symbol"], "reason":
                                   f"{pct:.1f}% of equity, over the "
                                   f"{RULES['max_position_pct']:.0f}% cap but inside the "
                                   f"{PM_RULES['rebalance_deadband_pct']:.1f}pt rebalance "
                                   "deadband — a price wobble is not a breach"})
            continue
        if pos.get("last_rebalance_date") == today.isoformat():
            jrn["skipped"].append({"symbol": pos["symbol"], "reason":
                                   f"{pct:.1f}% of equity and over the cap, but it was already "
                                   "rebalanced today — one rebalance per name per session"})
            continue
        excess = _round_shares((val - cap) / px)
        if excess <= 0 or excess * px < RULES["min_notional"]:
            continue
        sellable, note = _sellable(pos, excess, "rebalance", book, today, equity, jrn)
        if sellable <= 0:
            jrn["skipped"].append({"symbol": pos["symbol"],
                                   "reason": "Over the position cap but not trimmable — "
                                             + (note or "blocked")})
            continue
        booked = round(px * slip, 4)
        _apply_sell(book, pos["symbol"], sellable, booked, today, jrn, "rebalance",
                    f"Position was {pct:.1f}% of equity, over the "
                    f"{RULES['max_position_pct']:.0f}% cap",
                    shadow=lambda n, _s=pos["symbol"], _b=booked:
                    _shadow_record(book, pb, _s, "sell", n, _b, today, jrn))
        # Book the rebalance on the surviving position, not on the stale loop variable —
        # _apply_sell drops a position it closes out entirely.
        live = next((p for p in book["positions"] if p["symbol"] == pos["symbol"]), None)
        if live is not None:
            live["rebalance_count"] = live.get("rebalance_count", 0) + 1
            live["last_rebalance_date"] = today.isoformat()
    counts = {}
    for p in book.get("positions", []):
        counts[p.get("gics")] = counts.get(p.get("gics"), 0) + 1
    for g, n in counts.items():
        if g and n > RULES["max_per_sector"]:
            jrn["warnings"].append(f"Sector concentration: {n} positions in {g}, over the "
                                   f"{RULES['max_per_sector']} limit. Entries are gated; "
                                   "existing names are not force-sold — review by hand.")
    # HOUSE-01: the same warning, one level up. Reported every run whether or not it
    # binds, because the whole point is that nobody could see this number before.
    if house:
        for g, pct in house["sector_pct"].items():
            if pct > PM_RULES["house_max_sector_pct"]:
                jrn["warnings"].append(
                    f"HOUSE CONCENTRATION: {g} is {pct:.1f}% of the "
                    f"${house['equity']:,.0f} combined book across {house['desk_count']} desks, "
                    f"over the {PM_RULES['house_max_sector_pct']:.0f}% house limit. New entries "
                    "in it are gated on every desk; nothing is force-sold — review by hand.")
        for s, pct in house["symbol_pct"].items():
            if pct > PM_RULES["house_max_symbol_pct"]:
                jrn["warnings"].append(
                    f"HOUSE CONCENTRATION: {s} is {pct:.1f}% of the "
                    f"${house['equity']:,.0f} combined book across {house['desk_count']} desks, "
                    f"over the {PM_RULES['house_max_symbol_pct']:.0f}% single-name house limit. "
                    "Gated for new entries; not force-sold — review by hand.")


# Which scheduled releases actually move the whole tape. A gate that fires on every
# calendar item does not protect the book, it stops it trading: on 2026-09-01 the day's
# list was a Fed governor speech, JOLTS and ISM, and the next day's was ADP plus a 14:00
# Beige Book — a whole-day freeze on every one of them would have blocked all four slots.
# So only these gate, and only inside a lookahead window before the print.
MACRO_HIGH_IMPACT = (
    "fomc", "federal open market", "rate decision", "interest rate decision",
    "cpi", "consumer price", "pce", "core pce",
    "nonfarm", "non-farm", "payroll", "employment situation", "unemployment rate",
    "gdp", "powell",
)


def _is_high_impact(name):
    n = (name or "").lower()
    # "ADP National Employment" is not the employment situation report; exclude it
    # explicitly so the payroll keywords do not catch it.
    if "adp" in n:
        return False
    return any(k in n for k in MACRO_HIGH_IMPACT)


def macro_events_pending(scan, today, jrn):
    """High-impact releases dated today that have NOT yet printed and land within
    PM_RULES["macro_gate_lookahead_min"]. Those freeze new entries.

    Everything else on the calendar — a Fed speech, JOLTS, ISM, ADP, the Beige Book,
    weekly claims — is reported and never gates. A same-day high-impact release with no
    time given is treated as pending all day, because an unknown time cannot be cleared.
    Tomorrow's releases are noted for the overnight, never gated.
    """
    evs = ((scan or {}).get("meta") or {}).get("macro_events") or []
    try:
        from zoneinfo import ZoneInfo
        now_et = dt.datetime.fromisoformat(jrn["ts"].replace("Z", "+00:00")).astimezone(
            ZoneInfo("America/New_York"))
    except Exception:
        now_et = None
    look = PM_RULES.get("macro_gate_lookahead_min", 120)
    pending, noted, tomorrow = [], [], []
    for ev in evs:
        if not isinstance(ev, dict):
            continue
        try:
            d_ev = dt.date.fromisoformat(str(ev.get("date")))
        except (ValueError, TypeError):
            continue
        name = ev.get("name") or "scheduled release"
        t = ev.get("time_et")
        label = f"{name}{' at ' + str(t) + ' ET' if t else ''}"
        if d_ev == today + dt.timedelta(days=1):
            tomorrow.append(label)
            continue
        if d_ev != today:
            continue
        if not _is_high_impact(name):
            noted.append(label)
            continue
        if t and now_et:
            try:
                hh, mm = [int(x) for x in str(t).split(":")[:2]]
                when = now_et.replace(hour=hh, minute=mm, second=0, microsecond=0)
                mins = (when - now_et).total_seconds() / 60.0
                if mins <= 0:
                    noted.append(label + " (already printed)")
                    continue
                if mins > look:
                    noted.append(label + f" ({mins:.0f} min out — outside the "
                                         f"{look:.0f}-minute gate window)")
                    continue
            except (ValueError, TypeError):
                pass
        pending.append(label)
    if noted:
        jrn["warnings"].append("Macro calendar today (not gating): " + ", ".join(noted))
    if tomorrow:
        jrn["warnings"].append("MACRO TOMORROW: " + ", ".join(tomorrow) +
                               " — overnight holds carry tape-wide event risk.")
    return pending


# ------------------------------------------------------------------ K-07 live audit
def live_guardrail_audit(props, rows_by_tk, pb, jrn):
    """What the live guardrails WOULD have refused, per proposal, on the paper path.

    Runs guardrails.check_order (notional, distance from the last print, quote age, spread)
    and guardrails.check_symbol (the deny list) on every sized proposal — blocked or not —
    and returns [{symbol, notional, reasons}] for those that failed. Non-strict: a check
    whose input is not on the row is skipped rather than counted. Never raises and never
    changes a decision; this is the audit trail, not a gate.
    """
    rules = PM_RULES.get("live_guardrails") or guardrails_mod.LIVE_GUARDRAILS
    out = []
    for p in props or []:
        try:
            sym = p["ticker"]
            limit = round(p["entry"], 2)
            q = pb.get(sym) or {}
            age = q.get("age_min")
            ctx = {"rules": rules, "strict": False, "last": q.get("price"),
                   "quote_age_s": (age * 60.0 if isinstance(age, (int, float)) else None),
                   "spread_pct": q.get("spread_pct")}
            ok_o, r_o = guardrails_mod.check_order(
                {"symbol": sym, "shares": p["shares"], "limit_price": limit}, ctx)
            ok_s, r_s = guardrails_mod.check_symbol(
                rows_by_tk.get(sym) or {"ticker": sym, "price": p["entry"]},
                {"rules": rules, "strict": False, "now": jrn["ts"]})
            if not (ok_o and ok_s):
                out.append({"symbol": sym, "notional": round(p["shares"] * limit, 2),
                            "reasons": r_o + r_s})
        except Exception as exc:                  # noqa: BLE001 — never fail the manager
            out.append({"symbol": p.get("ticker"), "notional": None,
                        "reasons": [f"audit error: {type(exc).__name__}: {exc}"]})
    return out


# ------------------------------------------------------------------ 5. entries
def entry_pass(book, scan, pb, today, marked, jrn, scan_stale, house=None, ladder=None,
               risk=None):
    # FILL-01: an entry placed at the last slot of the day can never be evaluated for a
    # fill — the paper model fills entries only on a LATER slot, and roll_day() expires
    # every day order before the next session's fill pass runs. Placing entries here
    # manufactured orders that were structurally dead on arrival, so the slot runs
    # exits, trims and rebalancing only.
    # D-01: the rotation desk is the one exception, and only on a run its decision block
    # says acts — its orders are marketable next-session orders, which survive the roll.
    rot = ((scan or {}).get("rotation") or {}) if rotation_desk() else {}
    if rotation_desk() and not rot.get("acts"):
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"rotation: {rot.get('why') or 'no decision block this run'}"})
        return []
    if jrn["slot"] == "power-hour" and not rot.get("acts"):
        jrn["skipped"].append({"symbol": "*", "reason":
                               "power-hour places no new entries — a day-limit entry placed at "
                               "the last slot expires unfilled at the session roll before it can "
                               "ever fill. Exits, trims and rebalancing still ran."})
        return []
    if _orb_rules():
        # D-03: the ORB desk enters ONLY through the 09:35 sentinel's stop-buys
        # (orb_entry_pass). A decision slot manages its holdings and places nothing.
        jrn["skipped"].append({"symbol": "*", "reason":
                               "ORB desk: entries are the 09:35 sentinel's buy-stops above the "
                               "opening range — a decision slot places nothing on this desk"})
        return []
    if book["day"].get("halted"):
        jrn["skipped"].append({"symbol": "*", "reason": book["day"]["halt_reason"]})
        return []
    # K-02: the drawdown ladder. Rung 2, the cool-off after a rung-3 halt and the soft
    # daily level all refuse new entries here; rung 1 and re-entry only resize, below.
    if ladder and ladder.get("entries_blocked"):
        jrn["skipped"].append({"symbol": "*", "reason": ladder["reason"]})
        return []
    ladder_mult = float((ladder or {}).get("entry_size_mult", 1.0))
    if ladder and ladder.get("reason") and ladder_mult < 1.0:
        jrn["warnings"].append("LADDER: " + ladder["reason"] + ".")
    # K-03: the house exposure gate. Only ever refuses NEW entries, only when
    # PM_RULES["house_exposure"]["enforce"] is on, and only after the exit pass has
    # already run — a crowded house is a reason not to add, never a reason not to sell.
    exposure = (house or {}).get("exposure") or {}
    if exposure.get("block_new_entries"):
        why = "; ".join(exposure.get("reasons") or exposure.get("flags") or ["house exposure"])
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"house exposure (enforced): {why} — no new entries house-wide "
                               "until the combined book is less crowded; exits unaffected"})
        jrn["warnings"].append("HOUSE EXPOSURE: " + why + ". New entries blocked; exits live.")
        return []
    # K-04: the VaR / stress gate. Same shape as K-03's: only NEW entries, only when
    # PM_RULES["var"]["enforce"] is on, and only after the exit pass has already run.
    if (risk or {}).get("block_new_entries"):
        why = "; ".join(risk.get("reasons") or risk.get("flags") or ["VaR / stress"])
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"VaR / stress (enforced): {why} — no new entries on this desk "
                               "until the book's loss distribution is inside the limits; "
                               "exits unaffected"})
        jrn["warnings"].append("VAR / STRESS: " + why + ". New entries blocked; exits live.")
        return []
    if not scan or not scan.get("results"):
        jrn["skipped"].append({"symbol": "*", "reason": "no scan results available this run"})
        return []
    if scan_stale:
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"scan is {scan_stale} — entries frozen, holdings still managed"})
        return []
    # Macro gate: a scheduled tape-wide release later today (FOMC, CPI, payrolls) is a
    # binary event for every name at once. No new entries ahead of it; holdings are
    # managed normally. The scan's collection layer writes meta.macro_events.
    pending_macro = macro_events_pending(scan, today, jrn)
    if pending_macro:
        jrn["skipped"].append({"symbol": "*", "reason":
                               "macro gate: no new entries ahead of " + ", ".join(pending_macro)})
        jrn["warnings"].append("MACRO GATE: " + ", ".join(pending_macro) +
                               " later today — entries frozen until it is out; exits stay live.")
        return []
    equity = marked["equity"]
    # K-01: the broker policy's entry gate. legacy_pdt refuses at 2 of 3 day trades used;
    # intraday_margin refuses on a projected maintenance deficit or a freeze; cash_settled
    # never refuses here but caps the spendable cash below.
    ok, pnote = _policy().entries_allowed(book, today, equity)
    if not ok:
        jrn["skipped"].append({"symbol": "*", "reason": pnote})
        return []
    if pnote:
        jrn["warnings"].append(pnote)

    avail = max(0.0, _policy().buying_power(book, today, marked["cash"]) - reserved_cash(book))
    # CAP-01 (2026-09-01): a working buy order is capital and risk the book has already
    # COMMITTED — it is simply not filled yet. SIZE-01 stopped its cash being spent twice,
    # but every COUNT-based cap in portfolio.py still read `marked["positions"]` alone, so a
    # pending name was invisible to the sector cap, the max-positions slot count, the
    # cumulative-risk budget, the correlation multiplier and the max-deployed room. The
    # swing book ended 2026-09-01 four deep in Information Technology against a cap of 3:
    # NVDA was resting unfilled when CRDO was sized, so the cap counted 3 and allowed a
    # fourth. Pending buys are therefore passed to build_proposals as pseudo-positions.
    # They carry no meaningful shares or cost basis — nothing sizes off those — only the
    # identity and sector the caps need, plus their notional against the deployed room.
    # portfolio.py is untouched: the PM sanitises its own input, it does not fork the rules.
    pending_orders = [o for o in book.get("working_orders", [])
                      if o.get("side") == "buy" and o.get("status") == "working"]
    committed = reserved_cash(book)
    sizing_positions = [{"symbol": p["symbol"], "gics": p.get("gics"), "industry": p.get("industry"),
                         "market_value": (pb.get(p["symbol"], {}).get("price") or p["avg_cost"]) * p["shares"],
                         "shares": p["shares"], "avg_cost": p["avg_cost"]}
                        for p in book["positions"]]
    sizing_positions += [{"symbol": o["symbol"], "gics": (o.get("meta") or {}).get("gics"),
                          "industry": (o.get("meta") or {}).get("industry"),
                          "market_value": round(o["limit_price"] * o["shares"], 2),
                          "shares": o["shares"], "avg_cost": o["limit_price"], "pending": True}
                         for o in pending_orders]
    if pending_orders:
        jrn["skipped"].append({"symbol": "*", "reason":
                               f"{len(pending_orders)} working buy order(s) "
                               f"({', '.join(o['symbol'] for o in pending_orders)}) counted "
                               "against the sector, position, risk and deployment caps as if "
                               "already filled — committed capital is not free capital (CAP-01)"})
    marked_for_sizing = {
        "positions": sizing_positions,
        "invested": round(marked["invested"] + committed, 2), "cash": round(avail, 2),
        "equity": equity,
        "deployed_pct": round((marked["invested"] + committed) / equity * 100, 2) if equity else 0.0,
        "cash_pct": round(avail / equity * 100, 2) if equity else 0.0, "unrealized": 0.0,
    }
    # portfolio.py is the audited copy and is left byte-identical, so the PM sanitises
    # its own input rather than teaching that module to tolerate nulls. A row without a
    # usable score or price cannot be sized and is reported, never silently dropped.
    # SIZE-01: names that already have a working buy order are dropped HERE, before
    # build_proposals sees them. Sizing them anyway spent their notional from the cash
    # budget a second time (it was already subtracted as reserved cash above), silently
    # starving candidates later in the list.
    pending = {o["symbol"] for o in book["working_orders"]
               if o["side"] == "buy" and o.get("status") == "working"}
    clean, malformed, moved, drifted, wide = [], [], [], [], []
    for r in desk_filter(scan["results"], jrn):
        if not (isinstance(r.get("score"), (int, float)) and isinstance(r.get("price"), (int, float))
                and r["price"] > 0 and r.get("setup") and r.get("verdict")):
            malformed.append(r.get("ticker") or "?")
            continue
        if r.get("ticker") in pending:
            jrn["skipped"].append({"symbol": r["ticker"], "reason":
                                   "already has a working order — excluded before sizing so its "
                                   "cash cannot be counted against the budget twice"})
            continue
        # Spread gate: the broker quote carries bid/ask. A limit entry on a name whose
        # spread is wider than max_spread_pct pays more in the spread than the model's
        # edge is likely worth on a small book. Reported, never silently dropped.
        sp = (pb.get(r["ticker"]) or {}).get("spread_pct")
        if isinstance(sp, (int, float)) and sp > PM_RULES["max_spread_pct"]:
            wide.append(f"{r['ticker']} {sp:.2f}%")
            jrn["skipped"].append({"symbol": r["ticker"], "reason":
                                   f"bid/ask spread {sp:.2f}% of price, over the "
                                   f"{PM_RULES['max_spread_pct']:.1f}% entry limit — too "
                                   "expensive to trade at this size"})
            continue
        # Re-price the row against the live quote. The scan can be up to four hours old by
        # the time the manager runs; sizing and placing a limit off that price while holding
        # a live one from the broker is simply wrong.
        live = tradeable(pb, r["ticker"])
        if r.get("_source") == rotation_mod.SOURCE and live is None:
            # D-01: the row's price is a bar close. No broker quote, no entry.
            jrn["skipped"].append({"symbol": r["ticker"], "reason":
                                   "rotation: no fresh broker quote — not entered on a bar "
                                   "close; stage the 14 ETFs in pm_quotes.json"})
            continue
        if live and abs(live - r["price"]) / r["price"] > 1e-9:
            move = (live / r["price"] - 1) * 100
            if abs(move) > PM_RULES["max_price_drift_pct"]:
                drifted.append(f"{r['ticker']} {move:+.1f}%")
                continue
            if abs(move) > PM_RULES["warn_price_drift_pct"]:
                moved.append(f"{r['ticker']} {move:+.1f}%")
            r = dict(r, price=live, scan_price=r["price"], price_move_pct=round(move, 2))
        clean.append(r)
    if malformed:
        jrn["warnings"].append("Scan rows missing a score, price, setup or verdict and "
                               "therefore unsizeable: " + ", ".join(malformed))
    if drifted:
        jrn["warnings"].append(
            "Not entered — moved more than "
            f"{PM_RULES['max_price_drift_pct']:.0f}% since the scan scored them, so the setup "
            "and score no longer describe the price: " + ", ".join(drifted))
        for d in drifted:
            jrn["skipped"].append({"symbol": d.split()[0], "reason":
                                   f"price drifted {d.split()[1]} since the scan — rescored "
                                   "at the next scan before it can be entered"})
    if moved:
        jrn["warnings"].append(
            "Re-priced to the live quote and entered anyway, but the scan scored them at a "
            "different price: " + ", ".join(moved))
    if wide:
        jrn["warnings"].append("Not entered — bid/ask spread over the "
                               f"{PM_RULES['max_spread_pct']:.1f}% limit: " + ", ".join(wide))
    if not clean:
        jrn["skipped"].append({"symbol": "*", "reason":
                               "no scan row carried enough data to size a position"})
        return []
    props, blocks = build_proposals(clean, marked_for_sizing,
                                    daily_pnl_pct=jrn.get("daily_pnl_pct", 0.0))
    for b in blocks:
        jrn["skipped"].append({"symbol": "*", "reason": b})
    rows_by_tk = {row.get("ticker"): row for row in clean}

    # K-07 — the live-guardrail audit. Paper mode changes NOTHING here: every proposal is
    # run through the per-order and deny-list validators and what they WOULD have refused
    # live goes to the journal, so the paper record shows how often the guardrails would
    # bite. Non-strict — a check whose input the scan does not carry is skipped, not
    # counted as a refusal, because the point is to count the bites, not the blind spots.
    jrn["live_would_refuse"] = live_guardrail_audit(props, rows_by_tk, pb, jrn)

    placed = []
    spent = 0.0
    stop_policy = _stop_policy()
    for p in props:
        if len(placed) >= PM_RULES["max_new_entries_per_run"]:
            jrn["skipped"].append({"symbol": p["ticker"], "reason":
                                   f"run cap: {PM_RULES['max_new_entries_per_run']} new entries "
                                   "per slot, queued for the next one"})
            continue
        if p["ticker"] in pending:
            jrn["skipped"].append({"symbol": p["ticker"], "reason": "already has a working order"})
            continue
        if p["blocked"]:
            jrn["skipped"].append({"symbol": p["ticker"], "reason": "; ".join(p["warnings"])})
            continue
        limit = round(p["entry"], 2)
        # K-02: the ladder's entry-size multiplier is applied HERE, the one place a new
        # entry's share count is fixed, and nowhere inside portfolio.py — that module
        # mirrors the repo's sizing math and stays byte-identical. Dollar risk and
        # notional scale with it; the unscaled figure is kept on the order for the audit.
        full_shares = p["shares"]
        if ladder_mult < 1.0:
            p = dict(p, shares=_round_shares(p["shares"] * ladder_mult),
                     dollar_risk=round(p["dollar_risk"] * ladder_mult, 2))
            if p["shares"] <= 0 or p["shares"] * limit < RULES["min_notional"]:
                jrn["skipped"].append({"symbol": p["ticker"], "reason":
                                       f"ladder ×{ladder_mult:.2f} sized it to "
                                       f"${p['shares'] * limit:.2f}, under the "
                                       f"${RULES['min_notional']:.2f} broker minimum"})
                continue
        # K-07 — the desk's stop policy sets the initial levels. Under fixed_atr the
        # proposal's own stop and target ARE the policy (build_proposals called
        # portfolio.derive_levels) and nothing here moves. Under any other policy the
        # stop is re-derived and the position is RE-SIZED to the same dollar risk on the
        # new distance — a wider stop means fewer shares, never more risk — and capped at
        # the position cap and the cash left this run. The unscaled figure stays on the
        # order for the audit.
        initial_risk = round(limit - p["stop"], 6)
        if stop_policy.name != stops_mod.DEFAULT_POLICY:
            lv = stop_policy.initial(limit, p.get("atr_14"),
                                     {"row": rows_by_tk.get(p["ticker"]), "rules": RULES})
            new_stop, new_target = lv["stop"], lv["target"]
            new_risk = round(limit - new_stop, 6) if isinstance(new_stop, (int, float)) else 0.0
            if new_risk <= 0:
                jrn["skipped"].append({"symbol": p["ticker"], "reason":
                                       f"{stop_policy.name}: stop {new_stop} is not below the "
                                       f"{limit:,.2f} entry — cannot size a position"})
                continue
            cap_shares = (equity * RULES["max_position_pct"] / 100.0) / limit
            room_shares = max(0.0, avail - spent) / limit
            shares = _round_shares(min(p["dollar_risk"] / new_risk, cap_shares, room_shares))
            if shares <= 0 or shares * limit < RULES["min_notional"]:
                jrn["skipped"].append({"symbol": p["ticker"], "reason":
                                       f"{stop_policy.name}: ${shares * limit:.2f} at a "
                                       f"{new_risk / limit * 100:.1f}% stop distance is under "
                                       f"the ${RULES['min_notional']:.2f} broker minimum"})
                continue
            p = dict(p, stop=new_stop, target=new_target, stop_basis=lv["meta"]["stop_basis"],
                     stop_basis_kind=lv["meta"]["stop_basis_kind"], shares=shares,
                     dollar_risk=round(shares * new_risk, 2),
                     stop_pct=round(new_risk / limit * 100, 2),
                     risk_pct_of_equity=round(shares * new_risk / equity * 100, 2) if equity else 0.0)
            initial_risk = new_risk
        # D-01: the rotation desk sizes by SLEEVE, not by risk — an equal share of desk
        # equity per target name (the bond leg takes bond_sleeves), times the desk vol
        # scalar when vol_target is on, capped at the cash left this run. The risk-based
        # figure the pipeline just produced stays on the order as `unscaled_shares`; the
        # catastrophe stop and initial_risk are the policy's and are not touched.
        rot_sleeve = None
        if rot.get("acts") and rows_by_tk.get(p["ticker"], {}).get("_source") == rotation_mod.SOURCE:
            n_sl = (rot.get("sleeves_by_symbol") or {}).get(p["ticker"], 1)
            sleeves = max(1, int(rot.get("sleeves") or 1))
            target_notional = equity * (n_sl / sleeves) * float(rot.get("vol_scalar") or 1.0)
            room = max(0.0, avail - spent)
            shares = _round_shares(min(target_notional, room) / limit)
            if shares <= 0 or shares * limit < RULES["min_notional"]:
                jrn["skipped"].append({"symbol": p["ticker"], "reason":
                                       f"rotation: sleeve ${min(target_notional, room):,.2f} "
                                       f"is under the ${RULES['min_notional']:.2f} broker minimum"})
                continue
            rot_sleeve = {"sleeves": n_sl, "of": sleeves, "vol_scalar": rot.get("vol_scalar"),
                          "rank": rows_by_tk[p["ticker"]].get("rotation_rank"),
                          "ret_12_1": rows_by_tk[p["ticker"]].get("rotation_ret_12_1"),
                          "mode": rot.get("mode"), "decided": jrn["date"]}
            p = dict(p, shares=shares, dollar_risk=round(shares * initial_risk, 2),
                     risk_pct_of_equity=round(shares * initial_risk / equity * 100, 2)
                     if equity else 0.0)
        notional = round(p["shares"] * limit, 2)
        # HOUSE-01. This is the LAST gate, after portfolio.py has approved the trade for
        # this desk in isolation: it can only ever refuse, never resize or allow. Applied
        # here rather than inside build_proposals because portfolio.py mirrors the repo's
        # per-book rules and must stay byte-identical — the house is a PM concept.
        # D-01: the sector ETFs are exempt (sector-level by construction, see house_exposure).
        hb = None if rotation_mod.is_etf(p["ticker"]) else house_block(house, p["ticker"],
                                                                        p["gics"], notional)
        if hb:
            jrn["skipped"].append({"symbol": p["ticker"], "reason": hb})
            continue
        order = {
            "id": f"{jrn['date']}-{jrn['slot']}-{p['ticker']}",
            "symbol": p["ticker"], "side": "buy", "kind": "entry", "type": "limit",
            "limit_price": limit, "shares": p["shares"], "notional": notional,
            "placed": jrn["ts"], "placed_slot": jrn["slot"], "run_key": jrn["run_key"],
            "expires": "day", "status": "working",
            "reason": f"Score {p['score']:.0f} {p['setup']} — {p['verdict']}",
            "meta": {"stop": p["stop"], "target": p["target"], "stop_basis": p["stop_basis"],
                     "stop_basis_kind": p.get("stop_basis_kind"),
                     "atr_14": p.get("atr_14"), "atr_pct": p.get("atr_pct"),
                     "stop_pct": p.get("stop_pct"),
                     "score": p["score"], "gics": p["gics"], "industry": p["industry"],
                     "thesis": p.get("thesis"), "risk_pct": p["risk_pct_of_equity"],
                     "dollar_risk": p["dollar_risk"],
                     "ladder_mult": ladder_mult, "unscaled_shares": full_shares,
                     # K-07: the policy the levels came from and the risk per share at
                     # placement — the position records both when the order fills.
                     "stop_policy": stop_policy.name,
                     "initial_risk": initial_risk,
                     **({"stop_params": dict(stop_policy.params)} if stop_policy.params else {})},
            "warnings": p["warnings"],
        }
        if rot_sleeve is not None:
            # D-01: decided at the close, filled at the next slot's quote plus slippage.
            order.update({"expires": "next-session", "fill_rule": "marketable",
                          "rotation": True})
            order["meta"]["rotation"] = rot_sleeve
            order["reason"] = (f"rotation ({rot.get('mode')}): Sector Rotation — rank "
                               f"{rot_sleeve['rank'] or '—'}, sleeve {rot_sleeve['sleeves']}/"
                               f"{rot_sleeve['of']}")
        spent += notional
        # K-06: THE DECISION-TIME MID. An entry is decided here and filled on a later slot,
        # so the mid of the quote this decision was taken on is captured now, carried on
        # the order, and read back by simulate_fills() when the resting limit fills. None
        # when the price came from the scan rather than a two-sided broker quote. Written
        # only with the model on, so the off path stays byte-identical.
        if _shadow_rules() is not None:
            q = pb.get(p["ticker"]) or {}
            order["decision_mid"] = fills_mod.decision_mid(q.get("bid"), q.get("ask"))
            order["meta"]["adv_usd"] = fills_mod.adv_usd(next(
                (row for row in clean if row.get("ticker") == p["ticker"]), None))
        book["working_orders"].append(order)
        placed.append(order)
        if not rotation_mod.is_etf(p["ticker"]):
            house_apply(house, p["ticker"], p["gics"], notional)
        jrn["decisions"].append({"action": "place-buy", "symbol": p["ticker"],
                                 "shares": p["shares"], "price": limit,
                                 **({"decision_mid": order["decision_mid"]}
                                    if "decision_mid" in order else {}),
                                 "reason": order["reason"],
                                 "detail": f"${order['notional']:,.2f}, stop {p['stop']:,.2f}, "
                                           + (f"target {p['target']:,.2f}, "
                                              if isinstance(p["target"], (int, float))
                                              else f"no fixed target ({stop_policy.name}), ")
                                           + f"risking ${p['dollar_risk']:,.2f}"
                                           + (f" (ladder ×{ladder_mult:.2f}: {full_shares:g} "
                                              f"shares unscaled)" if ladder_mult < 1.0 else "")})
    return placed


def rotation_compact(rot):
    """D-01 — the rotation decision block without its candidate rows, for the journal and
    the state. None when no bars were staged (the journal says so through `error`)."""
    if not isinstance(rot, dict):
        return None
    keep = ("acts", "mode", "why", "as_of", "filter_on", "filter", "tbill_3m_pct",
            "tbill_default_used", "target", "held", "exits", "affirmed", "entries", "sleeves",
            "sleeves_by_symbol", "vol_scalar", "spy_rv_20d", "next_rebalance", "top_n",
            "drop_below_rank", "bond", "notes", "error")
    out = {k: rot.get(k) for k in keep if k in rot}
    out["ranks"] = [{"symbol": r["symbol"], "rank": r.get("rank"),
                     "ret_12_1": (round(r["ret_12_1"], 6)
                                  if isinstance(r.get("ret_12_1"), (int, float)) else None)}
                    for r in rot.get("ranks") or []]
    return out


def rotation_scan(bars_raw, macro, book, slot, now, force=False, calendar=None):
    """D-01 — the rotation desk's stand-in for scan_results.json: a scan-shaped dict whose
    `results` are rotation.proposals() rows and whose `rotation` block is the decision.
    Dated and timed NOW so the freshness gates read it as this run's own work. With no
    bars staged it still returns a scan (empty, acts False) that says why."""
    from zoneinfo import ZoneInfo
    now_et = now.astimezone(ZoneInfo("America/New_York"))
    meta = {"scan_date": now.date().isoformat(), "time": now_et.strftime("%H:%M"),
            "slot": slot, "source": rotation_mod.SOURCE, "macro_events": [],
            "sources": ["rotation: bars_etf.json"]}
    held = [p["symbol"] for p in (book or {}).get("positions", [])]
    if not bars_raw:
        return {"meta": meta, "results": [],
                "rotation": {"acts": False, "mode": None, "as_of": meta["scan_date"],
                             "why": "bars_etf.json is not staged — the desk cannot rank, "
                                    "enter or exit; holdings are still managed by their stops",
                             "error": "bars_etf.json not staged", "held": held, "target": [],
                             "ranks": [], "notes": []}}
    bars = rotation_mod.load_bars(bars_raw)
    rot = rotation_mod.proposals(bars, now.date(), held, slot, macro=macro, force=force,
                                 cfg=rotation_mod.config(DESK.get("rules")),
                                 calendar=calendar, vol_rules=RULES)
    return {"meta": meta, "results": list(rot.pop("rows") or []), "rotation": rot}


# ------------------------------------------------------------------ D-03 ORB entries
def load_orb_inputs(bars_5m_path="bars_5m.json", scan_path="scan_results.json",
                    bars_path="bars.json"):
    """The ORB desk's inputs from the run directory: the 5-minute bars (None when the file
    is not staged — the desk then does nothing and says so), the scan rows as the universe,
    and the daily bars for ATR14 / ADV14 when bars.json is staged. Only called for an ORB
    desk; the three live desks never read bars_5m.json."""
    ORB.update({"bars_5m": None, "bars_5m_path": None, "universe": None, "daily": None,
                "notes": []})
    raw = _load(bars_5m_path)
    if raw is not None:
        by_sym, notes = orb_mod.bars_by_symbol(raw)
        ORB["bars_5m"] = by_sym
        ORB["bars_5m_path"] = bars_5m_path
        ORB["notes"] = notes
    scan = _load(scan_path)
    if isinstance(scan, dict):
        ORB["universe"] = [r for r in (scan.get("results") or []) if isinstance(r, dict)]
    p = _in_base(bars_path)
    if p and os.path.exists(p):
        try:
            import backtest
            ORB["daily"] = backtest.load_bars(p)
        except Exception as exc:                 # noqa: BLE001 — daily bars are optional
            ORB["notes"].append(f"bars.json unreadable ({type(exc).__name__}: {exc}); "
                                "ATR14 / ADV14 fall back to the scan rows")
    return ORB


def orb_entry_pass(book, pb, today, now, marked, jrn, house=None, ladder=None,
                   risk=None):
    """D-03 — the 09:35 sentinel's entry pass for an ORB desk.

    Ranks the scan universe plus the held names by opening RVOL (orb.screen), sizes the
    long names for 1% risk under the 25% notional cap (orb.size_entries) and rests the
    buy-stops as working orders. Every gate a standard entry honours is honoured here —
    the kill switch, the ladder, the broker policy's entry gate, the house caps — and every
    name that did not make it is journaled with its reason. Runs only inside the entry
    window (orb.in_entry_window); any other sentinel places nothing."""
    rules = _orb_rules()
    if not rules:
        return []
    if not orb_mod.in_entry_window(now, rules):
        return []
    if ORB.get("bars_5m") is None:
        jrn["skipped"].append({"symbol": "*", "reason":
                               "no intraday bars — no ORB today (bars_5m.json was not staged)"})
        jrn["warnings"].append("ORB: no intraday bars — no ORB today (bars_5m.json was not "
                               "staged). The desk enters nothing today.")
        print("ORB: no intraday bars — no ORB today")
        return []
    for n in ORB.get("notes") or []:
        jrn["warnings"].append("ORB bars: " + n)
    if book["day"].get("halted"):
        jrn["skipped"].append({"symbol": "*", "reason": book["day"]["halt_reason"]})
        return []
    if ladder and ladder.get("entries_blocked"):
        jrn["skipped"].append({"symbol": "*", "reason": ladder["reason"]})
        return []
    equity = marked["equity"]
    ok, pnote = _policy().entries_allowed(book, today, equity)
    if not ok:
        jrn["skipped"].append({"symbol": "*", "reason": pnote})
        return []
    if pnote:
        jrn["warnings"].append(pnote)
    exposure = (house or {}).get("exposure") or {}
    if exposure.get("block_new_entries"):
        why = "; ".join(exposure.get("reasons") or exposure.get("flags") or ["house exposure"])
        jrn["skipped"].append({"symbol": "*", "reason": f"house exposure (enforced): {why}"})
        return []
    # K-04: the VaR / stress gate, the same way entry_pass honours it — only new entries,
    # only when PM_RULES["var"]["enforce"] is on; the exit pass has already run.
    if (risk or {}).get("block_new_entries"):
        why = "; ".join(risk.get("reasons") or risk.get("flags") or ["VaR / stress"])
        jrn["skipped"].append({"symbol": "*", "reason": f"VaR / stress (enforced): {why}"})
        return []
    ladder_mult = float((ladder or {}).get("entry_size_mult", 1.0))
    universe = ORB.get("universe") or []
    if not universe:
        jrn["skipped"].append({"symbol": "*", "reason":
                               "ORB: no scan universe staged (scan_results.json) — only the "
                               "held names could be ranked"})
    held = [p["symbol"] for p in book["positions"]]
    working = [o["symbol"] for o in book["working_orders"]
               if o.get("side") == "buy" and o.get("status") == "working"]
    in_play, rejected = orb_mod.screen(universe, ORB["bars_5m"], ORB.get("daily"), rules,
                                       today=today.isoformat(), held=held)
    jrn["orb"] = {"date": today.isoformat(), "n_universe": len(universe) + len(held),
                  "n_in_play": len(in_play), "n_rejected": len(rejected),
                  "in_play": [{k: s.get(k) for k in ("rank", "symbol", "rvol", "direction",
                                                     "or_high", "or_low", "atr14", "adv14",
                                                     "stats_source")} for s in in_play]}
    for rj in rejected:
        jrn["skipped"].append({"symbol": rj["symbol"], "reason": "ORB screen: " + rj["reason"]})
    if not in_play:
        jrn["skipped"].append({"symbol": "*", "reason": "ORB: no stock in play this morning"})
        return []
    avail = max(0.0, _policy().buying_power(book, today, marked["cash"]) - reserved_cash(book))
    orders, notes = orb_mod.size_entries(
        in_play, equity * ladder_mult if ladder_mult < 1.0 else equity, rules,
        avail_cash=avail, open_count=len(held) + len(working),
        open_symbols=held + working, today=today.isoformat(), ts=jrn["ts"],
        run_key=jrn["run_key"], slot=jrn["slot"],
        max_new=PM_RULES.get("max_new_entries_per_run"))
    for n in notes:
        jrn["skipped"].append(n)
    if ladder_mult < 1.0 and orders:
        jrn["warnings"].append(f"LADDER: ORB sized against ×{ladder_mult:.2f} equity — "
                               + str((ladder or {}).get("reason") or ""))
    placed = []
    for o in orders:
        hb = house_block(house, o["symbol"], (o.get("meta") or {}).get("gics"), o["notional"])
        if hb:
            jrn["skipped"].append({"symbol": o["symbol"], "reason": hb})
            continue
        if _shadow_rules() is not None:
            q = pb.get(o["symbol"]) or {}
            o["decision_mid"] = fills_mod.decision_mid(q.get("bid"), q.get("ask"))
        o["meta"]["ladder_mult"] = ladder_mult
        book["working_orders"].append(o)
        placed.append(o)
        house_apply(house, o["symbol"], (o.get("meta") or {}).get("gics"), o["notional"])
        jrn["decisions"].append({"action": "place-buy", "symbol": o["symbol"],
                                 "shares": o["shares"], "price": o["trigger_price"],
                                 "reason": o["reason"],
                                 "detail": (f"stop-buy ${o['notional']:,.2f} above the OR high, "
                                            f"stop {o['meta']['stop']:,.2f}, risking "
                                            f"${o['meta']['dollar_risk']:,.2f}, live until "
                                            f"{o['cancel_after_et']} ET"
                                            + (" (notional cap binds)"
                                               if o["meta"]["orb"]["notional_cap_bound"] else ""))})
    return placed


# ------------------------------------------------------------------ orchestration
def run(book, scan, prices_override, slot, now_iso, mode, policy_name=None):
    global POLICY
    now = _now(now_iso)
    today = now.date()
    POLICY = broker_policy.get_policy(policy_name, book, DESK, pm_rules=PM_RULES, risk_rules=RULES)
    sentinel = (slot == SENTINEL)
    # A sentinel fires many times a day, so its run key carries the clock time: each run
    # is its own journal entry rather than replacing the previous sentinel's.
    run_key = (f"{today.isoformat()}#{slot}-{now.strftime('%H%M')}" if sentinel
               else f"{today.isoformat()}#{slot}")
    jrn = {"ts": now.isoformat().replace("+00:00", "Z"), "date": today.isoformat(),
           "slot": slot, "run_key": run_key, "mode": mode, "desk": DESK["name"],
           "broker_policy": POLICY.name,
           "sentinel": sentinel, "decisions": [], "skipped": [], "warnings": [],
           "daily_pnl_pct": 0.0,
           # K-07: the desk's stop policy, and the live-guardrail audit (filled by the
           # entry pass; an empty list on a run that sized no proposal).
           "stop_policy": _stop_policy().name, "live_would_refuse": []}
    # D-01: the rotation desk's decision block, compacted, on its journal entry only.
    rot = ((scan or {}).get("rotation") or None) if rotation_desk() else None
    if rotation_desk():
        jrn["rotation"] = rotation_compact(rot)

    book.setdefault("positions", [])
    book.setdefault("working_orders", [])
    book.setdefault("closed_trades", [])
    book.setdefault("day_trades", [])
    book.setdefault("equity_curve", [])
    book.setdefault("realized_pnl", 0.0)
    roll_day(book, today, jrn)

    scan_by_tk = {r["ticker"]: r for r in (scan or {}).get("results", [])}
    scan_stale = None
    if scan:
        sd = (scan.get("meta") or {}).get("scan_date")
        if sd and sd != today.isoformat():
            scan_stale = f"dated {sd}, not today"
        else:
            # TIMING-01: the calendar-date check alone let an 08:00 scan count as fresh
            # at 15:15. Enforce the minutes limit the config always claimed to have.
            age = _scan_age_minutes(scan, now)
            if age is not None and age > PM_RULES["scan_stale_minutes"]:
                scan_stale = (f"{age:.0f} minutes old — over the "
                              f"{PM_RULES['scan_stale_minutes']:.0f}-minute freshness limit")
        jrn["scan_as_of"] = f"{sd} {(scan.get('meta') or {}).get('time', '')}".strip()
    else:
        jrn["scan_as_of"] = None

    pb = build_price_book(scan, prices_override, book, scan_stale)
    if scan_stale:
        fresh_n = len([1 for q in pb.values() if q.get("fresh")])
        jrn["warnings"].append(
            f"The scan is {scan_stale}. Its prices are valuation-only this run: no order "
            f"fills and no stop fires on them. {fresh_n} symbol(s) have a price the manager "
            "fetched itself and are still fully managed.")
    simulate_fills(book, pb, today, run_key, jrn, scan_by_tk)

    marked = mark_book(book, pb)
    day_pnl = kill_switch(book, marked, jrn)
    # K-02: the ladder is judged ONCE per run, on the same mark the kill switch saw and
    # before the exit pass — a rung-3 flatten is itself an exit and runs first.
    ladder = ladder_pass(book, pb, today, marked, jrn, day_pnl)
    if ladder["halt"]:
        marked = mark_book(book, pb)
    exit_pass(book, pb, scan_by_tk, today, marked["equity"], jrn)
    # D-01: the rank is the exit. After the policy stops, before the house tally, and only
    # on a decision slot: holdings that left the target set are sold as `rotation-exit`.
    if rot is not None and not sentinel:
        # The notes (the bill-rate default, a short history) are a WARNING on the run that
        # acts on them and a journal line otherwise — a standing warning every slot would
        # publish a board four times a day for a desk that decides once a month.
        for note in rot.get("notes") or []:
            if rot.get("acts"):
                jrn["warnings"].append(f"rotation: {note}")
            else:
                jrn["skipped"].append({"symbol": "*", "reason": f"rotation: {note}"})
        rotation_pass(book, pb, rot, today, marked["equity"], jrn)

    # HOUSE-01. Computed AFTER the exit pass so a stop that just fired is already out of
    # the tally, and before rebalancing and entries, which are the two passes that use it.
    house = house_exposure(book, pb)
    if house is None and PM_RULES.get("house_caps_enabled", True) and not sentinel:
        jrn["warnings"].append(
            "House caps NOT evaluated this run — no peer desk book was found in the run "
            "directory, so cross-desk exposure is unmeasured. Stage every desk's book "
            "(paper_book.json, paper_book_pullback.json, paper_book_momentum.json) before "
            "the engine runs. An unmeasured house is not a safe one.")
    # K-03. Once per run, on the post-exit combined book, for every slot including the
    # sentinel (which reports it and takes no entry). Reported unless enforce is on; the
    # entry pass reads house["exposure"]["block_new_entries"] and nothing else does.
    # A reported-only flag raises NO journal warning on purpose: archive.should_publish
    # treats any warning as a reason to publish a board, and a standing N_eff flag would
    # publish one every slot. The flag lives in the state, the journal's house block, the
    # heartbeat and the console line instead.
    if house is not None:
        house["exposure"] = house_metrics(house)
    # K-04. Once per run, on THIS desk's post-exit positions, every slot including the
    # sentinel. Null with a reason when no bars are staged. Reported unless enforce is on;
    # the entry passes (entry_pass, orb_entry_pass) read risk["block_new_entries"] and
    # nothing else does. As with K-03,
    # a reported-only flag raises no journal warning (archive.should_publish would publish
    # a board every slot on a standing flag); it lives in the state, the journal's risk
    # block, the heartbeat and the console line.
    marked = mark_book(book, pb)
    risk = desk_risk(book, pb, marked["equity"])

    if sentinel:
        # Exits only. Rebalancing and entries are slot decisions; the sentinel exists so
        # a stop is honoured within the hour rather than at the next slot.
        placed = []
        if _orb_rules():
            # D-03: the one exception — the ORB desk's entries ARE a sentinel decision,
            # at 09:35 and at no other hour (orb_entry_pass gates on the clock).
            marked = mark_book(book, pb)
            placed = orb_entry_pass(book, pb, today, now, marked, jrn, house, ladder, risk)
    else:
        marked = mark_book(book, pb)
        rebalance_pass(book, pb, today, marked["equity"], jrn, house)

        marked = mark_book(book, pb)
        placed = entry_pass(book, scan, pb, today, marked, jrn, scan_stale, house, ladder,
                            risk)

    marked = mark_book(book, pb)
    open_eq = book["day"].get("open_equity") or marked["equity"]
    jrn["daily_pnl_pct"] = round(((marked["equity"] - open_eq) / open_eq * 100)
                                 if open_eq else 0.0, 2)
    # The HWM is kept on the book, not derived from the capped equity curve, and it only
    # ever rises. The run's ladder verdict was taken on the pre-exit mark; the closing
    # mark can only bump the mark, never lower it.
    book["hwm"] = round(max(float(book.get("hwm") or 0.0), marked["equity"]), 2)
    ladder = dict(ladder, hwm=book["hwm"])
    for p in book["positions"]:
        p["last_priced"] = jrn["ts"]
    book["cash"] = round(book["cash"], 2)
    # One point per slot, not per invocation. A slot that re-runs (a retried or resumed
    # session) replaces its own point, exactly as the journal replaces its own entry by
    # run_key — otherwise the curve and the journal disagree about how many runs happened,
    # and a retry leaves a duplicate stub on the chart forever.
    if not sentinel:        # the curve is one point per decision slot, never per sentinel
        book["equity_curve"] = [c for c in book["equity_curve"]
                                if not (c.get("date") == jrn["date"] and c.get("slot") == slot)]
        book["equity_curve"].append({"ts": jrn["ts"], "date": jrn["date"], "slot": slot,
                                     "equity": marked["equity"], "cash": marked["cash"],
                                     "invested": marked["invested"],
                                     "realized": round(book["realized_pnl"], 2)})
        book["equity_curve"].sort(key=lambda c: (c.get("date", ""),
                                                 SLOT_ORDER.get(c.get("slot"), 9),
                                                 c.get("ts", "")))
    book["equity_curve"] = book["equity_curve"][-PM_RULES["equity_curve_max"]:]
    book["closed_trades"] = book["closed_trades"][-PM_RULES["closed_trades_max"]:]
    book["based_on_revision"] = book.get("revision", 0)
    book["revision"] = book.get("revision", 0) + 1
    book["last_run"] = jrn["ts"]
    book["mode"] = mode
    # This run's identity, and a hash of what the book IS rather than what it is worth.
    # main() compares the fingerprint against the previous entry's to decide whether this
    # run earned a frozen board, so it has to be computed after the book is final.
    jrn["run_id"] = archive.pm_run_id(jrn["date"], slot, jrn["ts"]) + DESK["suffix"]
    jrn["book_revision"] = book["revision"]
    jrn["book_fingerprint"] = archive.book_fingerprint(book)
    # Which commit of the engine took this decision. Written by the clone step as
    # $SCAN_DIR/engine_sha; None when the engine was not run from a repo.
    import config
    jrn["engine_sha"] = config.engine_sha()
    # HOUSE-01 — a compact record on every entry, so the weekly review can read combined
    # exposure out of the journal without reconstructing three books.
    jrn["house"] = ({"equity": house["equity"], "desks": house["desk_count"],
                     "top_symbol_pct": house["symbol_pct"],
                     "sector_pct": house["sector_pct"],
                     "caps": house["caps"], "peers": house["peers_loaded"],
                     # K-03 — the compact exposure summary, same fields as the coverage row.
                     "exposure": house_mod.compact(house.get("exposure"))}
                    if house else None)
    # K-04 — the compact VaR / stress summary, same fields as the heartbeat carries.
    jrn["risk"] = risk_compact(risk)

    if marked["unpriced"]:
        jrn["warnings"].append("Unpriced positions this run: " + ", ".join(marked["unpriced"]) +
                               " — they carry no live stop until a price is available.")

    # K-06: the shadow ledger and the cost budget. Only with the model on — the off path
    # writes no key at all. book["shadow"] is refreshed here so gap_share_of_realized_pct
    # reflects this run's realised P&L even on a run that booked no fill.
    shadow_state = None
    if _shadow_rules() is not None:
        book.setdefault("shadow", {"cum_gap_usd": 0.0, "n_fills": 0,
                                   "gap_share_of_realized_pct": None, "by_year": {}})
        book["shadow"].update(fills_mod.shadow_summary(book))
        budget = fills_mod.cost_budget_status(book, today, PM_RULES)
        shadow_state = dict(fills_mod.shadow_summary(book), cost_budget=budget,
                            equity_shadow=round(marked["equity"] - book["shadow"]["cum_gap_usd"], 2))
        if (budget.get("share_used_pct") or 0) > 100:
            jrn["warnings"].append(
                f"EXECUTION COST BUDGET EXCEEDED: {budget['ytd_cost_bps']:.1f} bp of average "
                f"equity spent on implementation shortfall in {budget['year']}, against a "
                f"{budget['budget_bps']:.0f} bp/year budget ({budget['share_used_pct']:.0f}% "
                "used). The desk is paying more to trade than its budget allows — trade less, "
                "or trade wider names at quieter slots.")
        jrn["shadow"] = shadow_state

    jrn.update({"equity": marked["equity"], "cash": marked["cash"],
                "invested": marked["invested"], "realized_pnl": round(book["realized_pnl"], 2),
                "halted": bool(book["day"].get("halted")),
                "ladder": ladder,
                "day_trades_used": day_trades_used(book, today),
                "scan_stale": scan_stale, "positions": len(book["positions"]),
                "working_orders": len(book["working_orders"])})

    _basis = pf_mod.capital_basis(book)
    state = {
        "generated": jrn["ts"], "mode": mode, "slot": slot, "date": jrn["date"],
        "engine_sha": jrn["engine_sha"],
        "desk": DESK["name"], "sentinel": sentinel,
        "account": book.get("mirrors", {}),
        "book": {"equity": marked["equity"], "cash": marked["cash"],
                 "invested": marked["invested"],
                 "deployed_pct": round(marked["invested"] / marked["equity"] * 100, 2)
                 if marked["equity"] else 0.0,
                 "realized_pnl": round(book["realized_pnl"], 2),
                 # M4: the return is measured against the capital actually put in — the
                 # seed PLUS every recorded deposit — not against a seed that stopped
                 # describing the book the moment it was funded.
                 "starting_equity": _basis or book.get("starting_equity", marked["equity"]),
                 "seed_equity": book.get("starting_equity"),
                 "deposits_total": round((_basis or 0.0) - (book.get("starting_equity") or 0.0), 2)
                 if _basis else 0.0,
                 "total_return_pct": round((marked["equity"] / _basis - 1) * 100, 2)
                 if _basis else 0.0,
                 "open_equity": book["day"].get("open_equity"),
                 "daily_pnl_pct": jrn["daily_pnl_pct"],
                 "halted": bool(book["day"].get("halted")),
                 "halt_reason": book["day"].get("halt_reason"),
                 # K-02: hwm, dd_pct, rung, entry_size_mult, entries_blocked, reason,
                 # soft_daily_hit, cool_until, reentry_active — see ladder.state_for().
                 "hwm": book["hwm"],
                 "cool_until": book.get("cool_until"),
                 "ladder": ladder,
                 # K-01: broker_policy, day_trades_used, day_trade_limit, pdt_applies and
                 # whatever else the regime reports (deficits, settlement, GFVs).
                 **POLICY.state(book, today, marked["equity"]),
                 # K-06: equity_shadow = equity − cumulative shadow gap, the shadow summary
                 # and the year-to-date cost budget. Absent entirely with the model off.
                 **({"equity_shadow": shadow_state["equity_shadow"],
                     "shadow": {k: shadow_state[k] for k in
                                ("cum_gap_usd", "n_fills", "gap_share_of_realized_pct")},
                     "cost_budget": shadow_state["cost_budget"]}
                    if shadow_state else {})},
        "positions": [dict(p, market_value=round((pb.get(p["symbol"], {}).get("price")
                                                  or p["avg_cost"]) * p["shares"], 2),
                           price=pb.get(p["symbol"], {}).get("price"),
                           price_source=(pb.get(p["symbol"], {}).get("source")
                                         + ("" if pb.get(p["symbol"], {}).get("fresh")
                                            else " (stale)")
                                         if pb.get(p["symbol"]) else None),
                           unrealized=round(((pb.get(p["symbol"], {}).get("price") or p["avg_cost"])
                                             - p["avg_cost"]) * p["shares"], 2),
                           unrealized_pct=round((((pb.get(p["symbol"], {}).get("price")
                                                   or p["avg_cost"]) / p["avg_cost"]) - 1) * 100, 2)
                           if p["avg_cost"] else 0.0,
                           score=(scan_by_tk.get(p["symbol"]) or {}).get("score"),
                           setup=(scan_by_tk.get(p["symbol"]) or {}).get("setup"))
                      for p in book["positions"]],
        "working_orders": book["working_orders"],
        "closed_trades": book["closed_trades"][-40:],
        "equity_curve": book["equity_curve"],
        "journal": jrn, "rules": RULES, "pm_rules": PM_RULES,
        "broker_policy": {"name": POLICY.name, "describe": POLICY.describe()},
        "house": house,
        # K-04: {"var": hs_var block, "stress": {window: ...}, flags, reasons, enforce,
        # block_new_entries, worst_stress, worst_stress_window, rules, bars, weights_pct}.
        "risk": risk,
        "scan_as_of": jrn["scan_as_of"], "scan_stale": scan_stale,
        "orders_to_place": placed if mode == "live" else [],
    }
    if rotation_desk():
        state["rotation"] = jrn.get("rotation")
    return book, jrn, state


# ------------------------------------------------------------------ CLI
def _in_base(path):
    """Resolve a caller-supplied filename inside the run directory, or None (M5).

    Every input a run reads is staged into $SCAN_DIR by the caller, so a path that
    resolves outside it is a caller mistake, not a feature. `os.path.join` silently
    honours an absolute path and `..` walks out, so the old code would happily read —
    or, more often, crash on — a file the run never staged. None means "treat it as
    absent", which every caller already handles."""
    if not isinstance(path, str) or not path:
        return None
    base = os.path.realpath(BASE)
    p = os.path.realpath(os.path.join(base, path))
    if p == base or p.startswith(base + os.sep):
        return p
    print(f"NOTE: refusing {path!r} — it resolves outside the run directory; treated as absent.",
          file=sys.stderr)
    return None


def _load(path, default=None):
    p = _in_base(path)
    if p is None or not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_peers(desks_path, this_desk, this_book_file):
    """HOUSE-01 — find the OTHER desks' books in the run directory.

    Auto-discovery rather than a new CLI argument, deliberately: the three desks already
    run in one directory with all three books staged (PM.md §13), and a flag the four task
    prompts would each have to pass is a flag that will be missing from one of them. A
    book that is not there is recorded in `missing` and reported, never silently skipped.
    """
    desks = (_load(desks_path) or {}).get("desks") or {}
    for name, cfg in desks.items():
        if name == this_desk:
            continue
        if cfg.get("inactive"):
            continue              # K-07: a template has no book and is not a peer
        fn = cfg.get("book") or f"paper_book-{name}.json"
        if os.path.basename(fn) == os.path.basename(this_book_file or ""):
            continue
        bk = _load(fn)
        if bk is None:
            PEERS["missing"].append(f"{name} ({fn})")
            continue
        PEERS["books"][name] = bk
        PEERS["loaded"].append(name)
    return PEERS


def write_heartbeat(base, sfx, desk, slot, ts, book, quiet, decisions=0, warnings=0,
                    reason=None, exposure=None, risk=None):
    """COVER-01 — proof that this desk was looked at.

    A QUIET sentinel writes no book revision and no journal entry, by design (PM.md §12):
    eleven quiet writes a day is how a real UNPROTECTED banner gets scrolled past. The
    cost was that "checked, nothing fired" and "never ran" left byte-identical records —
    nothing outside a session transcript could tell them apart, so the daily health check
    could not verify sentinel coverage at all. On 2026-09-02 the 09:47 sentinel left
    entries for the swing and momentum desks and none for pullback, and reconstructing
    which of the two had happened took reading the engine source.

    This file is that proof and nothing more: a few hundred bytes, no book state, no
    decision content. The caller merges it into claude/pm-coverage.json. The book and the
    journals stay exactly as untouched by a quiet run as they were before.
    """
    hb = {
        "ts": ts, "date": ts[:10], "desk": desk, "slot": slot,
        "quiet": bool(quiet), "reason": reason,
        # D-02: an options book carries `structures` instead of `positions`; both count.
        "positions": len((book.get("positions") or book.get("structures") or [])
                         if isinstance(book, dict) else []),
        "working_orders": len(book.get("working_orders", []) if isinstance(book, dict) else []),
        "decisions": decisions, "warnings": warnings,
        # The revision that is actually STORED after this run. run() increments
        # book["revision"] before anything is written, and a quiet run writes nothing —
        # so reporting book["revision"] here would claim a revision that does not exist.
        "book_revision": ((book or {}).get("based_on_revision") if quiet
                          else (book or {}).get("revision")),
        # K-03 — the compact house exposure summary (house.compact). Additive: the runner
        # copies it onto the coverage row; a consumer that ignores it loses nothing. None
        # when the house was not measured this run (flat book short-circuit, no peers).
        "exposure": exposure,
        # K-04 — this desk's 1-day VaR (% of desk equity) and its worst long-side stress
        # window P&L (%), from risk_compact(). Both null, never 0, when no bars were
        # staged or the book was flat. The runner copies them onto the desk's coverage row.
        "var_pct": (risk or {}).get("var_pct"),
        "worst_stress": (risk or {}).get("worst_stress"),
        "worst_stress_window": (risk or {}).get("worst_stress_window"),
    }
    path = os.path.join(base, f"pm_heartbeat{sfx}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hb, f, indent=2)
    print(f"COVERAGE {desk} {slot} {ts} — {'quiet' if quiet else 'acted'}, "
          f"{hb['positions']} position(s), {hb['warnings']} warning(s)  ->  "
          f"pm_heartbeat{sfx}.json")
    return hb


def main():
    ap = argparse.ArgumentParser(description="Portfolio Manager decision engine")
    ap.add_argument("--slot", default="ad-hoc",
                    help="pre-market | opening-range | midday | power-hour | sentinel | ad-hoc")
    ap.add_argument("--now", default=None, help="ISO timestamp override (UTC)")
    ap.add_argument("--mode", default=None, choices=["paper", "live"])
    ap.add_argument("--desk", default=None,
                    help="strategy desk name from --desks (default: the unfiltered 'swing' "
                         "desk on paper_book.json)")
    ap.add_argument("--desks", default="desks.json",
                    help="desk definitions: {desks: {name: {book, journal, rules, filter}}}")
    ap.add_argument("--book", default=None)
    ap.add_argument("--scan", default="scan_results.json")
    ap.add_argument("--prices", default="pm_prices.json",
                    help="optional {TICKER: {price, as_of}} override map")
    ap.add_argument("--quotes", default="pm_quotes.json",
                    help="raw get_equity_quotes response; parsed directly, preferred over "
                         "--prices because nothing has to be retyped")
    ap.add_argument("--broker", default="pm_broker.json",
                    help="raw get_equity_positions response for the live agentic account; "
                         "any live holding raises a divergence warning (STATE-02). Optional.")
    ap.add_argument("--quote-max-age-min", type=float, default=30.0)
    ap.add_argument("--bars", default="bars.json",
                    help="optional get_equity_historicals payload (the file technicals.py "
                         "reads). When staged, K-03 measures N_eff from daily-return "
                         "correlations and reports beta and momentum crowding; absent, the "
                         "sector proxy is used and those two are n/a.")
    ap.add_argument("--bars-5m", default="bars_5m.json",
                    help="D-03: 5-minute get_equity_historicals payload for an ORB desk "
                         "(today + the 14-session lookback). Read only when the active desk "
                         "carries rules.orb; absent, the desk logs 'no intraday bars — no ORB "
                         "today' and enters nothing.")
    ap.add_argument("--broker-policy", default=None, choices=list(broker_policy.VALID),
                    help="day-trade / margin regime (K-01). Overrides the book, desks.json and "
                         "engine-config.json; default intraday_margin (FINRA Reg. Notice 26-10)")
    ap.add_argument("--journal", default=None)
    ap.add_argument("--no-house-caps", action="store_true",
                    help="HOUSE-01 escape hatch: measure and report cross-desk exposure but "
                         "do not let it refuse an entry. For diagnosis only.")
    ap.add_argument("--check", default=None,
                    help="compare a freshly-read book against the one this run was based on; "
                         "exit 2 on a concurrent write")
    ap.add_argument("--allow-inactive", action="store_true",
                    help="K-07: run a desk that desks.json marks `inactive` (a template with "
                         "no book of its own). Refused otherwise. Its book must be staged.")
    ap.add_argument("--bars-etf", dest="bars_etf", default="bars_etf.json",
                    help="D-01: the rotation desk's input — get_equity_historicals for the 14 "
                         "ETFs in rotation.UNIVERSE, >= 13 months of daily bars. Replaces "
                         "--scan for a desk whose filter is {universe: sector-etfs}.")
    ap.add_argument("--macro", default="macro.json",
                    help="D-01: optional {tbill_3m_pct: x} for the absolute-momentum filter; "
                         "absent, the bill rate is 0 and the journal says so")
    ap.add_argument("--force-rebalance", dest="force_rebalance", action="store_true",
                    help="D-01: apply the rotation rule at this slot instead of waiting for "
                         "the last power-hour of the month")
    args = ap.parse_args()

    # ---- strategy desk: which book, which journal, which rules, which candidates
    if args.desk and args.desk != "swing":
        try:
            cfg = use_desk(args.desk, args.desks, allow_inactive=args.allow_inactive)
        except KeyError:
            print(f"FATAL: desk {args.desk!r} is not defined in {args.desks}", file=sys.stderr)
            sys.exit(2)
        except InactiveDesk as e:
            print(f"FATAL: {e}", file=sys.stderr)
            sys.exit(2)
        args.book = args.book or cfg.get("book") or f"paper_book{DESK['suffix']}.json"
        args.journal = args.journal or cfg.get("journal") or f"pm_journal_current{DESK['suffix']}.json"
    else:
        args.book = args.book or "paper_book.json"
        args.journal = args.journal or "pm_journal_current.json"
    sfx = DESK["suffix"]
    if args.no_house_caps:
        PM_RULES["house_caps_enabled"] = False

    book = _load(args.book)
    if book is None:
        print(f"FATAL: {args.book} not found in {BASE}", file=sys.stderr)
        sys.exit(2)

    if args.check:
        fresh = _load(args.check)
        if fresh is None:
            print("FATAL: --check file not found", file=sys.stderr)
            sys.exit(2)
        if fresh.get("revision") != book.get("based_on_revision"):
            print(f"REFUSED: the stored book is at revision {fresh.get('revision')}, this run was "
                  f"based on {book.get('based_on_revision')}. Another session wrote while this one "
                  "was working. Do not write. Journal the run as abandoned.", file=sys.stderr)
            sys.exit(2)
        print(f"OK: no concurrent write (revision {fresh.get('revision')} -> {book.get('revision')})")
        return

    # HOUSE-01 — the peer books, before any decision is taken. Loaded even for a sentinel,
    # which takes no entries, so the heartbeat and the report still carry house exposure.
    load_peers(args.desks, DESK["name"], args.book)
    if PEERS["missing"]:
        print(f"NOTE: peer desk book(s) not staged: {', '.join(PEERS['missing'])} — "
              "house caps will be measured against what IS here.", file=sys.stderr)
    # K-03 — optional. Present: N_eff is measured from returns and beta / momentum crowding
    # exist. Absent: the sector proxy, and those two report n/a.
    load_bars(args.bars)
    # D-03 — an ORB desk reads its 5-minute bars, the staged scan as its universe and the
    # daily bars for ATR14 / ADV14. Any other desk never opens bars_5m.json.
    orb_active = bool(_orb_rules())
    if orb_active:
        load_orb_inputs(args.bars_5m, args.scan, args.bars)
        if ORB["bars_5m"] is None:
            print("ORB: no intraday bars — no ORB today (bars_5m.json not staged)", file=sys.stderr)

    mode = args.mode or book.get("mode", "paper")
    ts_now = _now(args.now).isoformat().replace("+00:00", "Z")
    # D-03: a flat ORB book at the 09:35 sentinel is not quiet — that IS its entry slot.
    orb_entry_slot = (orb_active and args.slot == SENTINEL
                      and orb_mod.in_entry_window(_now(args.now), _orb_rules()))
    if (args.slot == SENTINEL and not (book.get("positions") or book.get("working_orders")
                                       or book.get("structures"))
            and not orb_entry_slot):
        write_heartbeat(BASE, sfx, DESK["name"], args.slot, ts_now, book, quiet=True,
                        reason="flat — no positions and no working orders")
        print("SENTINEL QUIET — the book holds no positions and no working orders. "
              "Nothing to protect; nothing written.")
        return
    scan = _load(args.scan) if args.slot != SENTINEL else None
    if rotation_desk() and args.slot != SENTINEL:
        # D-01: this desk's candidates come from the ETF bars, never from the scan. The
        # ETF bars are also folded into the K-03 bars so the house can measure their beta.
        bars_etf = _load(args.bars_etf)
        if not bars_etf:
            print(f"NOTE: {args.bars_etf} not staged — the rotation desk takes no decision "
                  "this run; holdings are managed by their stops only.", file=sys.stderr)
        scan = rotation_scan(bars_etf, _load(args.macro), book, args.slot, _now(args.now),
                             force=args.force_rebalance)
        if bars_etf:
            etf_rows = house_mod.bars_rows(bars_etf)
            have = house_mod.bars_rows(BARS["rows"]) if BARS["rows"] else []
            known = {str(r.get("symbol") or "").upper() for r in have if isinstance(r, dict)}
            BARS["rows"] = list(have) + [r for r in etf_rows if isinstance(r, dict)
                                         and str(r.get("symbol") or "").upper() not in known]
    prices = dict(_load(args.prices, {}) or {})
    quote_rejects = []
    raw_quotes = _load(args.quotes)
    if raw_quotes:
        parsed, quote_rejects = quotes_to_prices(raw_quotes, _now(args.now),
                                                 args.quote_max_age_min)
        prices.update(parsed)      # broker quotes outrank a hand-written override

    try:
        if DESK.get("kind") == "options":
            # D-02 — the paper options desk. Same protocol from here on; different engine.
            import options_desk
            book, jrn, state = options_desk.run_slot(
                BASE, book, args.slot, args.now, rules=DESK.get("rules"), peers=PEERS,
                mode=mode, prices=prices, desk_name=DESK["name"],
                policy_name=args.broker_policy)
        else:
            book, jrn, state = run(book, scan, prices, args.slot, args.now, mode,
                                   policy_name=args.broker_policy)
    except ValueError as e:          # an unknown broker policy name in the book or config
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(2)
    if quote_rejects:
        msg = ("Broker quotes refused as unusable: " + ", ".join(quote_rejects) +
               ". Those symbols fall back to the scan price, or to no price at all.")
        jrn.setdefault("warnings", []).append(msg)
        state["journal"] = jrn

    raw_broker = _load(args.broker)
    if raw_broker:
        for w in broker_divergence(raw_broker, book):
            jrn.setdefault("warnings", []).append(w)
        state["journal"] = jrn

    # S-01: the option chain as priced this slot, into $SCAN_DIR/archive/chain_snapshot/,
    # only when a chain file was staged. Non-fatal and OUTSIDE the book and the journal:
    # a snapshot failure is recorded on pm_state.json, never on the decision record, so
    # the book the runner writes stays byte-identical to a direct run whatever happens
    # here. Every desk of a slot writes the same file; the last writer wins and they agree.
    try:
        import snapshots
        if snapshots.chain_files(BASE):
            cpath, cn = snapshots.write_chain_snapshot(
                BASE, os.path.join(BASE, "archive"),
                {"slot": args.slot, "as_of": jrn["ts"], "date": jrn["date"],
                 "run_id": archive.pm_run_id(jrn["date"], args.slot, jrn["ts"])})
            state["chain_snapshot"] = {"path": os.path.relpath(cpath, BASE), "rows": cn}
            print(f"chain snapshot -> {state['chain_snapshot']['path']} ({cn} rows)")
    except Exception as exc:                      # noqa: BLE001 — never fail the manager
        state["chain_snapshot"] = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"WARNING: chain snapshot NOT written: {state['chain_snapshot']['error']}",
              file=sys.stderr)

    # K-03 — the compact exposure summary rides on every heartbeat, quiet or not, so the
    # coverage row carries it even on a run that wrote nothing else.
    exposure = house_mod.compact((state.get("house") or {}).get("exposure"))
    # K-04 — the desk's VaR / stress summary rides on the heartbeat the same way.
    risk_summary = risk_compact(state.get("risk"))
    if args.slot == SENTINEL and not jrn["decisions"] and not jrn["warnings"]:
        b = state["book"]
        write_heartbeat(BASE, sfx, DESK["name"], args.slot, jrn["ts"], book, quiet=True,
                        reason="every stop checked, nothing fired", exposure=exposure,
                        risk=risk_summary)
        # D-02: an options book carries `structures`, not `positions` / `working_orders`.
        n_pos = len(book.get("positions") or book.get("structures") or [])
        noun = "structure(s)" if DESK.get("kind") == "options" else "position(s)"
        print(f"SENTINEL QUIET — {jrn['date']} {jrn['ts'][11:16]}Z  equity ${b['equity']:,.2f}  "
              f"{n_pos} {noun}, {len(book.get('working_orders') or [])} working "
              "order(s), every stop checked, nothing fired. Nothing written — do not "
              "project_write the book or the journal, do not publish.")
        print(house_mod.console_line((state.get("house") or {}).get("exposure")))
        print(var_mod.console_line(risk_summary))
        return

    write_heartbeat(BASE, sfx, DESK["name"], args.slot, jrn["ts"], book, quiet=False,
                    decisions=len(jrn["decisions"]), warnings=len(jrn["warnings"]),
                    exposure=exposure, risk=risk_summary)

    # The journal is loaded BEFORE the writes now, because the archive decision needs the
    # previous run's fingerprint and render_pm.py reads that decision out of pm_state.json.
    journal = _load(args.journal, {"entries": []}) or {"entries": []}

    def _order(e):
        return (e.get("date", ""), SLOT_ORDER.get(e.get("slot"), 9), e.get("ts", ""))

    earlier = [e for e in (journal.get("entries") or []) if _order(e) < _order(jrn)]
    prev = earlier[-1] if earlier else None
    # The entry this run is about to replace, if the slot has already been attempted today.
    superseded = next((e for e in (journal.get("entries") or [])
                       if e.get("run_key") == jrn["run_key"]), None)

    publish, why = archive.should_publish(jrn, book, prev)
    jrn["changed"] = bool(why) and why != [archive.CLOSE_REASON]
    jrn["change_reasons"] = why
    jrn["attempt"] = ((superseded.get("attempt") or 1) + 1) if superseded else 1
    jrn["artifact_url"] = (superseded or {}).get("artifact_url")

    # A re-run of a slot REPLACES its journal entry, and a quiet re-run would otherwise
    # report publish=False over the top of an attempt that really did publish a board —
    # the artifact would still exist with nothing in the journal pointing at it. So a slot
    # that has published once keeps publishing: the snapshot is re-rendered from the
    # re-run's book and republished to the URL already carried here.
    if superseded and (superseded.get("publish_snapshot")
                       or superseded.get("artifact_url")) and not publish:
        publish = True
        why = [archive.RERUN_REASON]
        jrn["change_reasons"] = why
    jrn["publish_snapshot"] = publish

    rid = jrn["run_id"]
    fn = archive.pm_files_for(rid)
    # The book snapshot is tied to a CHANGE, not to a publish: a close-of-day board of an
    # unchanged book is worth having, a duplicate copy of an unchanged book is not. A
    # pointer an earlier attempt wrote is carried forward rather than dropped.
    jrn["book_snapshot"] = (fn["doc_book"] if jrn["changed"]
                            else (superseded or {}).get("book_snapshot"))
    state["journal"] = jrn
    state["run_id"] = rid
    state["run_label"] = archive.pm_run_label(jrn["date"], jrn["slot"])
    state["publish_snapshot"] = publish
    state["publish_reasons"] = why
    state["republish_url"] = jrn["artifact_url"]
    state["attempt"] = jrn["attempt"]
    state["changed"] = jrn["changed"]
    import config
    state["live_board_url"] = (book.get("board_url")
                               or config.board_url("trade_desk")
                               or archive.PM_BOARD_URL)

    with open(os.path.join(BASE, f"pm_book_next{sfx}.json"), "w", encoding="utf-8") as f:
        json.dump(book, f, indent=2)
    with open(os.path.join(BASE, f"pm_state{sfx}.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    # Per-run copies. The two unstamped files above are overwritten by the next slot;
    # these are not, so a run stays reconstructable after the fact.
    with open(os.path.join(BASE, fn["book"]), "w", encoding="utf-8") as f:
        json.dump(book, f, indent=2)
    with open(os.path.join(BASE, fn["state"]), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    entries = [e for e in journal.get("entries", []) if e.get("run_key") != jrn["run_key"]]
    entries.append(jrn)
    entries.sort(key=lambda e: (e.get("date", ""), SLOT_ORDER.get(e.get("slot"), 9),
                                e.get("ts", "")))
    journal["entries"] = entries[-500:]
    journal["updated"] = jrn["ts"]
    with open(os.path.join(BASE, f"pm_journal_next{sfx}.json"), "w", encoding="utf-8") as f:
        json.dump(journal, f, indent=2)

    if mode == "live":
        with open(os.path.join(BASE, f"pm_orders{sfx}.json"), "w", encoding="utf-8") as f:
            json.dump({"generated": jrn["ts"], "slot": args.slot,
                       "orders": state["orders_to_place"]}, f, indent=2)

    b = state["book"]
    print(f"[{mode.upper()}] {jrn['date']} {args.slot}   equity ${b['equity']:,.2f}  "
          f"cash ${b['cash']:,.2f}  invested ${b['invested']:,.2f} ({b['deployed_pct']:.1f}%)")
    regime = (f"day trades {b['day_trades_used']}/{b['day_trade_limit']}"
              if b.get("broker_policy") == "legacy_pdt" else f"policy {b.get('broker_policy')}")
    print(f"day P&L {b['daily_pnl_pct']:+.2f}%   realised ${b['realized_pnl']:+,.2f}   "
          f"total {b['total_return_pct']:+.2f}%   {regime}"
          + ("   *HALTED*" if b["halted"] else ""))
    if b.get("shadow") is not None:
        sh, cb = b["shadow"], b.get("cost_budget") or {}
        share = sh.get("gap_share_of_realized_pct")
        print(f"shadow gap ${sh['cum_gap_usd']:,.2f} "
              + (f"({share:.1f}% of realized)" if share is not None
                 else "(n/a % of realized — nothing realised yet)"), end="")
        print(f"   equity_shadow ${b['equity_shadow']:,.2f}   {sh['n_fills']} shadow fill(s)   "
              f"exec cost {cb.get('ytd_cost_bps', 0):.1f}/{cb.get('budget_bps', 0):.0f} bp "
              f"({cb.get('share_used_pct') or 0:.0f}% of budget)")
    lad = b.get("ladder") or {}
    if lad.get("rung", 0) > 0 or lad.get("entries_blocked") or lad.get("reentry_active"):
        print(f"LADDER rung {lad.get('rung', 0)}   drawdown {lad.get('dd_pct', 0):.2f}% from "
              f"HWM ${lad.get('hwm', 0):,.2f}   entry size x{lad.get('entry_size_mult', 1.0):.2f}"
              + ("   entries BLOCKED" if lad.get("entries_blocked") else "")
              + (f"   cool-off through {lad['cool_until']}" if lad.get("cool_active") else "")
              + ("   re-entry" if lad.get("reentry_active") else "")
              + (f"\n  {lad['reason']}" if lad.get("reason") else ""))
    h = state.get("house")
    if h:
        top = sorted(h["sector_pct"].items(), key=lambda kv: -kv[1])[:3]
        print(f"HOUSE  ${h['equity']:,.2f} across {h['desk_count']} desks "
              f"({', '.join(h['peers_loaded'])} + this)   "
              + "  ".join(f"{g.split()[0]} {p:.0f}%" for g, p in top)
              + f"   caps {h['caps']['symbol_pct']:.0f}%/name, "
                f"{h['caps']['sector_pct']:.0f}%/sector"
              + ("" if PM_RULES.get("house_caps_enabled", True) else "  [ADVISORY ONLY]"))
        print(house_mod.console_line(h.get("exposure")))
    print(var_mod.console_line(risk_summary))
    if not jrn["decisions"]:
        print("\nNo action this slot.")
    else:
        print(f"\n{'ACTION':<12}{'SYM':<7}{'SHARES':>11}{'PRICE':>10}  REASON")
        print("-" * 92)
        for d in jrn["decisions"]:
            print(f"{d['action']:<12}{d['symbol']:<7}{d['shares']:>11,.4f}{d['price']:>10,.2f}  "
                  f"{d['reason']}")
            if d.get("detail"):
                print(f"{'':<40}{d['detail']}")
    for s in jrn["skipped"]:
        print(f"  skip {s['symbol']:<7} {s['reason']}")
    for w in jrn["warnings"]:
        print(f"  ! {w}")

    print(f"\nRUN {rid}   book revision {book['revision']}   ->  {fn['book']}, {fn['state']}")
    if jrn["changed"]:
        print(f"BOOK CHANGED ({'; '.join(why)})")
        print(f"  project_write {fn['book']}  ->  {fn['doc_book']}")
    else:
        print("BOOK UNCHANGED - marks moved, no decision was taken. No book snapshot.")
    if publish and jrn["artifact_url"]:
        print(f"REPUBLISH this slot's board ({'; '.join(why)}) to the URL it already has:")
        print(f"  {jrn['artifact_url']}")
    elif publish:
        print(f"PUBLISH a frozen board: {'; '.join(why)}")
    else:
        print("NO SNAPSHOT BOARD - refresh the rolling Trade Desk only.")
    if jrn["attempt"] > 1:
        print(f"(attempt {jrn['attempt']} at this slot - it replaces its own journal entry)")


if __name__ == "__main__":
    main()
