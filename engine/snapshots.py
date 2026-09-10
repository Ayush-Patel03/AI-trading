"""snapshots.py — the raw per-slot inputs, archived as they were seen (S-01).

The compact scan record (archive.py) keeps scores, pillars and verdicts; the board keeps
the prose. Neither keeps the INPUTS — the bid, the ask, the option chain, the technicals
exactly as the scanner saw them — and without those nothing can be re-simulated later:
the slot-event simulator, the E10/E17 studies and E27 all need the fields the model
read, not the number it produced. This module writes two append-only archives, one file
per slot, into `<archive_dir>/`:

    scan_snapshot/<date>-<slot>.jsonl.gz    one row per candidate the scanner was given
    chain_snapshot/<date>-<slot>.jsonl.gz   one row per option contract, plus one derived
                                            row per symbol (ATM IV term structure, skew,
                                            call-put IV, options/share volume, 1σ move,
                                            ATM straddle)

Both are gzip'd JSON Lines: a header line `{"_meta": {...}}` then one JSON object per
row. Stdlib only, like everything under engine/ — no pyarrow, no pandas.

Nothing here changes a score, a size or a trade. A snapshot that cannot be written must
never fail the scan or the manager; the callers catch and record the failure.

Scan snapshot row
-----------------
Every key of the candidate dict from scan_data.json (fundamentals, technicals.py's
merged fields, sentiment) with the scored row from scan_results.json laid over it
(score, pillars, verdict, setup, coverage, ...). Prose fields (`reasons`, `*_note`)
are dropped — they are outputs, not inputs. Added on top:
    symbol, run_id, date, slot, as_of, rank, regime, regime_multiplier, dropped,
    bid, ask, last, quote_ts           from quotes.json / pm_quotes.json when staged,
                                       null otherwise — never 0.
A candidate the scanner dropped (no usable price) is still a row, with `dropped: true`
and `score: null`: the dataset is survivorship-free from the first slot.

Chain snapshot
--------------
Reads the option chain payload(s) staged in the run directory. `get_option_quotes`
returns 403 on this account (SCAN.md §9.1), so no chain file is produced today; the
reader accepts the shapes a session is likely to ferry once one is:
  * a list of contract rows, or {results: [...]} / {data: {results: [...]}}, each row
    flat or with `quote` / `instrument` / `greeks` sub-objects (Robinhood style:
    chain_symbol, expiration_date, strike_price, type, bid_price, ask_price,
    last_trade_price, volume, open_interest, implied_volatility, delta, gamma, theta,
    vega);
  * a Cboe delayed-quotes payload {data: {symbol, current_price, options: [{option:
    "AAPL260918C00150000", bid, ask, iv, open_interest, volume, delta, ...}]}}, or a
    list of them;
  * a map {SYMBOL: {spot, share_volume, contracts|options|results: [...]}} or
    {SYMBOL: [rows]}.
File names: option_chains.json, option_chain.json, options_chain.json,
option_quotes.json, and per-symbol option_chain-<SYM>.json / option_chain_<SYM>.json.
When none is present the snapshot is the header alone with n_rows 0.

Contract rows keep the 4 nearest expiries (DTE >= 0) and the 10 strikes either side of
spot, per expiry and type. The DERIVED row is computed from the whole chain the payload
carried, before trimming, so a 60-day point is interpolated whenever the data allows:
    atm_iv_30d / atm_iv_60d   linear in DTE between the two expiries bracketing 30 / 60
                              days; null when not bracketed (no extrapolation)
    skew25                    (IV at put Δ −0.25 − IV at call Δ +0.25) / ATM IV, at the
                              first expiry with 30 <= DTE <= 45
    cpiv                      open-interest-weighted mean of (IV_call − IV_put) over
                              strikes carrying both legs, at the same 30–45 DTE expiry
                              (the nearest expiry when there is none)
    os_ratio                  Σ contract volume × 100 / share volume
    em_1sd                    spot × ATM IV × sqrt(DTE / 365) at the nearest expiry
                              with DTE >= 1 (the overnight/next-expiry 1σ move, in $)
    straddle_price            ATM call mid + ATM put mid at that same expiry
A value that cannot be computed is null, never 0.

Usage
-----
    python3 snapshots.py --run-dir . --out <archive_dir> --slot <slot> --run-id <id>
                         [--as-of ISO] [--scan | --chain | --both]
"""
import argparse, glob, gzip, json, math, os, re, sys
from datetime import date, datetime

import archive
import config
import history

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))
SCAN_KIND = "scan_snapshot"
CHAIN_KIND = "chain_snapshot"
SCHEMA = 1
QUOTE_FILES = ("quotes.json", "pm_quotes.json")
CHAIN_FILES = ("option_chains.json", "option_chain.json", "options_chain.json",
               "option_quotes.json")
CHAIN_GLOBS = ("option_chain-*.json", "option_chain_*.json", "options_chain-*.json")
N_EXPIRIES = 4
STRIKE_WINDOW = 10
SKEW_DTE = (30, 45)
DELTA_TOL = 0.10           # a "25-delta" contract must sit within this of ±0.25
PROSE_KEYS = ("reasons", "setup_note", "verdict_note", "confidence_note")
_OCC = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


# ---------------------------------------------------------------- small helpers
def _num(v):
    """float or None. Strings are parsed; bools, blanks and non-finite values are None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _load(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def _date_of(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _round(v, dp=8):
    return None if v is None else round(v, dp)


def snapshot_path(out_dir, kind, date_str, slot, ts=None):
    """`<out_dir>/<kind>/<date>-<slot-slug>[-HHMM].jsonl.gz`.

    The slug is archive.slot_slug so the scan ("Pre-market") and the manager ("pre-market")
    land on the same name. A sentinel or ad-hoc slot gets its clock time appended, exactly
    as archive.pm_run_id does, so hourly runs do not overwrite each other.
    """
    slug = archive.slot_slug(slot)
    name = f"{date_str}-{slug}"
    if not archive.is_scheduled(slot) and slug not in archive.SLOT_TIMES and ts:
        digits = re.sub(r"[^0-9]", "", str(ts))
        if len(digits) >= 12:
            name += f"-{digits[8:12]}"
    return os.path.join(out_dir, kind, f"{name}.jsonl.gz")


# ---------------------------------------------------------------- jsonl.gz io
def write_jsonl(path, meta, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    meta = dict(meta, n_rows=len(rows), schema=SCHEMA)
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, separators=(",", ":")) + "\n")
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
    os.replace(tmp, path)
    return path


def read_snapshot(path):
    """(meta, rows) from a snapshot file. Tolerates a plain .jsonl too."""
    opener = gzip.open if str(path).endswith(".gz") else open
    meta, rows = {}, []
    with opener(path, "rt", encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if i == 0 and isinstance(obj, dict) and "_meta" in obj:
                meta = obj["_meta"] or {}
                continue
            rows.append(obj)
    return meta, rows


# ---------------------------------------------------------------- quotes
def quote_fields(payload):
    """{SYMBOL: {bid, ask, last, quote_ts}} from a raw get_equity_quotes response.

    The same walk pm.quotes_to_prices does — `data.results[].quote`, the newer of the two
    venue-stamped trade prices — but it keeps every symbol and adds bid/ask: a snapshot
    records what the broker said, the manager decides what is usable.
    """
    out = {}
    rows = payload
    if isinstance(payload, dict):
        rows = ((payload.get("data") or {}).get("results") or payload.get("results") or [])
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        q = row.get("quote") if isinstance(row.get("quote"), dict) else row
        sym = (q.get("symbol") or "").upper()
        if not sym:
            continue
        last, ts = None, None
        for pk, tk in (("last_trade_price", "venue_last_trade_time"),
                       ("last_non_reg_trade_price", "venue_last_non_reg_trade_time")):
            v, t = _num(q.get(pk)), q.get(tk)
            if v is None or not t:
                continue
            if ts is None or str(t) > str(ts):
                last, ts = v, t
        if last is None:
            last = _num(q.get("last_trade_price")) or _num(q.get("last_non_reg_trade_price"))
        out[sym] = {"bid": _num(q.get("bid_price") or q.get("bid")),
                    "ask": _num(q.get("ask_price") or q.get("ask")),
                    "last": last, "quote_ts": ts}
    return out


def load_quotes(run_dir):
    """Merge the staged quote files; the first file listed wins per symbol."""
    merged = {}
    for name in QUOTE_FILES:
        payload = _load(os.path.join(run_dir, name))
        if not payload:
            continue
        for sym, q in quote_fields(payload).items():
            merged.setdefault(sym, q)
    return merged


# ================================================================ scan snapshot
def scan_rows(scan_data, results, quotes=None, meta=None):
    """Build the scan snapshot rows. Pure: no files."""
    quotes = quotes or {}
    meta = meta or {}
    cands = (scan_data or {}).get("candidates") or {}
    if isinstance(cands, list):
        cands = {str(c.get("symbol") or c.get("ticker")): c for c in cands if isinstance(c, dict)}
    scored = {r["ticker"]: (i + 1, r) for i, r in enumerate((results or {}).get("results") or [])
              if isinstance(r, dict) and r.get("ticker")}
    reg = (results or {}).get("regime") or {}
    rows = []
    for tk in list(cands) + [t for t in scored if t not in cands]:
        c = cands.get(tk) if isinstance(cands.get(tk), dict) else {}
        rank, r = scored.get(tk, (None, {}))
        row = {k: v for k, v in c.items()}
        row.update({k: v for k, v in r.items() if k not in PROSE_KEYS})
        sym = str(tk).upper()
        q = quotes.get(sym) or {}
        row.update({
            "symbol": sym, "ticker": tk,
            "run_id": meta.get("run_id"), "date": meta.get("date"), "slot": meta.get("slot"),
            "as_of": meta.get("as_of"), "rank": rank,
            "regime": reg.get("label"), "regime_multiplier": reg.get("multiplier"),
            "score": r.get("score"), "dropped": rank is None,
            "bid": q.get("bid"), "ask": q.get("ask"), "last": q.get("last"),
            "quote_ts": q.get("quote_ts"),
        })
        rows.append(row)
    rows.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, x["symbol"]))
    return rows


def _scan_meta(run_dir, meta):
    """Fill run_id / slot / date / as_of / engine_sha from scan_results.json when absent."""
    res = _load(os.path.join(run_dir, "scan_results.json")) or {}
    rm = res.get("meta") or {}
    m = dict(meta or {})
    slot = m.get("slot") or rm.get("slot") or rm.get("session") or "scan"
    m["slot"] = archive.slot_slug(slot)
    m["date"] = m.get("date") or rm.get("scan_date") or rm.get("date")
    m["run_id"] = m.get("run_id") or rm.get("run_id") or (archive.run_id(rm) if rm else None)
    if not m.get("as_of"):
        m["as_of"] = (f"{m['date']}T{rm.get('time')}" if m.get("date") and rm.get("time")
                      else m.get("date"))
    m.setdefault("engine_sha", rm.get("engine_sha") or config.engine_sha(run_dir))
    m["kind"] = SCAN_KIND
    return m, res


def write_scan_snapshot(run_dir=None, out_dir=None, meta=None):
    """Write `<out_dir>/scan_snapshot/<date>-<slot>.jsonl.gz`; returns (path, n_rows).

    Also folds every symbol into the followed set (history.update_followed) — the
    snapshot IS the point a name enters the record, so the two cannot disagree.
    """
    run_dir = run_dir or BASE
    out_dir = out_dir or os.path.join(run_dir, "archive")
    m, res = _scan_meta(run_dir, meta)
    data = _load(os.path.join(run_dir, "scan_data.json"))
    if data is None and m.get("run_id"):
        data = _load(os.path.join(run_dir, archive.files_for(m["run_id"])["data"]))
    if not data and not res:
        raise FileNotFoundError(f"neither scan_data.json nor scan_results.json in {run_dir}")
    quotes = load_quotes(run_dir)
    m["source_files"] = [n for n in ("scan_data.json", "scan_results.json") + QUOTE_FILES
                         if os.path.exists(os.path.join(run_dir, n))]
    rows = scan_rows(data, res, quotes, m)
    m["n_quoted"] = sum(1 for r in rows if r.get("last") is not None)
    path = snapshot_path(out_dir, SCAN_KIND, m.get("date") or "undated", m["slot"], m.get("as_of"))
    write_jsonl(path, m, rows)
    try:
        history.update_followed(out_dir, [r["symbol"] for r in rows], m.get("date"),
                                m["slot"], run_id=m.get("run_id"))
    except (OSError, ValueError) as exc:        # the snapshot is written; say so and go on
        print(f"WARNING: followed set not updated: {exc}", file=sys.stderr)
    return path, len(rows)


# ================================================================ chain snapshot
def _parse_occ(code):
    """'AAPL260918C00150000' -> (AAPL, 2026-09-18, call, 150.0), else None."""
    m = _OCC.match(str(code or "").replace(" ", "").upper())
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    try:
        exp = datetime.strptime(ymd, "%y%m%d").date().isoformat()
    except ValueError:
        return None
    return root, exp, ("call" if cp == "C" else "put"), int(strike) / 1000.0


def _flat_row(row):
    """Merge Robinhood-style sub-objects into one flat dict for key lookup."""
    flat = {}
    for sub in ("instrument", "quote", "greeks"):
        if isinstance(row.get(sub), dict):
            flat.update(row[sub])
    flat.update({k: v for k, v in row.items() if k not in ("instrument", "quote", "greeks")})
    return flat


def _iv(v):
    """Implied volatility as a decimal. A payload quoting 35 (percent) becomes 0.35.

    The cut is 5.0: a decimal IV above 500% is not a number anyone prices off, and a
    percentage under 5% is rarer still. Everything the connectors return is decimal."""
    f = _num(v)
    if f is None or f <= 0:
        return None
    return f / 100.0 if f > 5.0 else f


def _contract(row, default_symbol=None):
    """A normalised contract dict from one payload row, or None when it has no identity."""
    r = _flat_row(row)
    occ = _parse_occ(_first(r, "option", "occ_symbol", "occ", "contract_symbol"))
    if occ is None:
        s = _first(r, "symbol")
        occ = _parse_occ(s) if isinstance(s, str) and len(s) > 15 else None
    sym = _first(r, "chain_symbol", "underlying_symbol", "underlying", "root_symbol")
    if not sym and occ:
        sym = occ[0]
    if not sym:
        s = _first(r, "symbol")
        sym = s if isinstance(s, str) and len(s) <= 6 else default_symbol
    sym = (sym or default_symbol or "").upper()
    exp = _first(r, "expiration_date", "expiry", "expiration", "exp_date") or (occ[1] if occ else None)
    strike = _num(_first(r, "strike_price", "strike")) or (occ[3] if occ else None)
    typ = _first(r, "type", "option_type", "contract_type", "right", "put_call") or (occ[2] if occ else None)
    typ = str(typ or "").lower()
    typ = {"c": "call", "p": "put"}.get(typ, typ)
    exp_d = _date_of(exp)
    if not sym or exp_d is None or strike is None or typ not in ("call", "put"):
        return None
    bid, ask = _num(_first(r, "bid_price", "bid")), _num(_first(r, "ask_price", "ask"))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None and ask > 0 and ask >= bid else None
    return {
        "symbol": sym, "expiry": exp_d.isoformat(), "strike": strike, "type": typ,
        "bid": bid, "ask": ask, "mid": _round(mid),
        "last": _num(_first(r, "last_trade_price", "last", "last_price", "mark_price")),
        "volume": _num(_first(r, "volume", "day_volume")),
        "open_interest": _num(_first(r, "open_interest", "oi")),
        "iv": _iv(_first(r, "implied_volatility", "iv")),
        "delta": _num(_first(r, "delta")), "gamma": _num(_first(r, "gamma")),
        "theta": _num(_first(r, "theta")), "vega": _num(_first(r, "vega")),
        "spot": _num(_first(r, "underlying_price", "spot", "current_price")),
    }


def _rows_of(obj):
    """The contract row list inside one of the accepted payload shapes, or None."""
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        return None
    for k in ("options", "contracts", "results", "rows"):
        if isinstance(obj.get(k), list):
            return obj[k]
    d = obj.get("data")
    if isinstance(d, dict):
        return _rows_of(d)
    if isinstance(d, list):
        return d
    return None


def normalise_chain(payload, into=None):
    """payload (any accepted shape) -> {SYMBOL: {spot, share_volume, contracts: [...]}}."""
    out = into if into is not None else {}

    def add(sym, spot, share_vol, rows):
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            c = _contract(row, sym)
            if c is None:
                continue
            slot = out.setdefault(c["symbol"], {"spot": None, "share_volume": None, "contracts": []})
            if slot["spot"] is None:
                slot["spot"] = c["spot"] if c["spot"] is not None else _num(spot)
            if slot["share_volume"] is None and share_vol is not None:
                slot["share_volume"] = _num(share_vol)
            slot["contracts"].append(c)

    if isinstance(payload, list):
        if payload and all(isinstance(p, dict) and _rows_of(p) is not None
                           and not _contract(p) for p in payload):
            for p in payload:                      # a list of per-symbol payloads
                normalise_chain(p, out)
        else:
            add(None, None, None, payload)
        return out
    if not isinstance(payload, dict):
        return out
    d = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    rows = _rows_of(payload)
    if rows is not None:
        add(_first(d, "symbol", "chain_symbol", "underlying"),
            _first(d, "current_price", "spot", "underlying_price", "last"),
            _first(d, "share_volume", "underlying_volume", "stock_volume"), rows)
        return out
    for sym, sub in payload.items():                # {SYMBOL: ...}
        if not isinstance(sym, str) or sym.startswith("_"):
            continue
        sub_rows = _rows_of(sub)
        if sub_rows is None:
            continue
        s = sub if isinstance(sub, dict) else {}
        s = s.get("data") if isinstance(s.get("data"), dict) else s
        add(sym.upper(), _first(s, "spot", "current_price", "underlying_price", "last"),
            _first(s, "share_volume", "underlying_volume", "stock_volume"), sub_rows)
    return out


def chain_files(run_dir):
    names = [n for n in CHAIN_FILES if os.path.exists(os.path.join(run_dir, n))]
    for pat in CHAIN_GLOBS:
        names += [os.path.basename(p) for p in sorted(glob.glob(os.path.join(run_dir, pat)))]
    return sorted(set(names), key=names.index)


def load_chains(run_dir):
    chains = {}
    for name in chain_files(run_dir):
        payload = _load(os.path.join(run_dir, name))
        if payload:
            normalise_chain(payload, chains)
    return chains


# ---------------------------------------------------------------- derived
def _interp(points, target):
    """Linear interpolation of (dte, value) points at `target` DTE; None when not bracketed."""
    pts = sorted((d, v) for d, v in points if v is not None)
    if not pts:
        return None
    for d, v in pts:
        if d == target:
            return v
    lo = [p for p in pts if p[0] < target]
    hi = [p for p in pts if p[0] > target]
    if not lo or not hi:
        return None
    (d0, v0), (d1, v1) = lo[-1], hi[0]
    return v0 + (v1 - v0) * (target - d0) / (d1 - d0)


def _atm_strike(strikes, spot):
    return min(strikes, key=lambda k: (abs(k - spot), k)) if strikes and spot else None


def _by_expiry(contracts, as_of_d):
    """{expiry: {dte, calls: {strike: c}, puts: {strike: c}}} for expiries on/after as_of."""
    exps = {}
    for c in contracts:
        d = _date_of(c["expiry"])
        dte = (d - as_of_d).days
        if dte < 0:
            continue
        e = exps.setdefault(c["expiry"], {"dte": dte, "calls": {}, "puts": {}})
        e["calls" if c["type"] == "call" else "puts"][c["strike"]] = c
    return dict(sorted(exps.items()))


def _atm_iv(e, spot):
    strikes = sorted(set(e["calls"]) | set(e["puts"]))
    k = _atm_strike(strikes, spot)
    if k is None:
        return None, None
    ivs = [x["iv"] for x in (e["calls"].get(k), e["puts"].get(k)) if x and x["iv"] is not None]
    return (sum(ivs) / len(ivs) if ivs else None), k


def _nearest_delta(side, target):
    best = None
    for c in side.values():
        if c["delta"] is None or c["iv"] is None:
            continue
        gap = abs(c["delta"] - target)
        if gap <= DELTA_TOL and (best is None or gap < best[0]):
            best = (gap, c)
    return best[1] if best else None


def derived_row(symbol, chain, as_of_d, share_volume=None):
    """The per-symbol derived row (see the module doc for each definition)."""
    spot = chain.get("spot")
    exps = _by_expiry(chain.get("contracts") or [], as_of_d)
    # an expired contract is not part of the chain, whatever the payload carried
    contracts = [c for e in exps.values() for side in ("calls", "puts") for c in e[side].values()]
    share_vol = chain.get("share_volume") if chain.get("share_volume") is not None else share_volume
    row = {"_derived": True, "symbol": symbol, "spot": spot, "as_of": as_of_d.isoformat(),
           "n_contracts": len(contracts), "expiries": list(exps),
           "nearest_expiry": None, "nearest_dte": None, "expiry_30_45": None,
           "atm_iv_nearest": None, "atm_iv_30d": None, "atm_iv_60d": None,
           "skew25": None, "cpiv": None, "os_ratio": None, "share_volume": share_vol,
           "em_1sd": None, "em_1sd_pct": None, "straddle_price": None, "straddle_pct": None}
    if not exps or not spot:
        return row

    # term structure
    term = []
    for exp, e in exps.items():
        iv, _ = _atm_iv(e, spot)
        term.append((e["dte"], iv))
    row["atm_iv_30d"] = _round(_interp(term, 30))
    row["atm_iv_60d"] = _round(_interp(term, 60))

    # nearest expiry with at least one session to run: EM and the straddle
    near = next(((exp, e) for exp, e in exps.items() if e["dte"] >= 1), None)
    if near:
        exp, e = near
        iv, k = _atm_iv(e, spot)
        row.update({"nearest_expiry": exp, "nearest_dte": e["dte"], "atm_iv_nearest": _round(iv)})
        if iv:
            em = spot * iv * math.sqrt(e["dte"] / 365.0)
            row["em_1sd"] = _round(em, 6)
            row["em_1sd_pct"] = _round(em / spot * 100.0, 6)
        call, put = e["calls"].get(k), e["puts"].get(k)
        if call and put and call["mid"] is not None and put["mid"] is not None:
            row["straddle_price"] = _round(call["mid"] + put["mid"], 6)
            row["straddle_pct"] = _round(row["straddle_price"] / spot * 100.0, 6)

    # skew and call-put IV at the 30-45 DTE expiry (cpiv falls back to the nearest)
    skew_exp = next(((exp, e) for exp, e in exps.items()
                     if SKEW_DTE[0] <= e["dte"] <= SKEW_DTE[1]), None)
    if skew_exp:
        exp, e = skew_exp
        row["expiry_30_45"] = exp
        atm, _ = _atm_iv(e, spot)
        p25, c25 = _nearest_delta(e["puts"], -0.25), _nearest_delta(e["calls"], 0.25)
        if atm and p25 and c25:
            row["skew25"] = _round((p25["iv"] - c25["iv"]) / atm)
    cp_exp = skew_exp or near
    if cp_exp:
        _, e = cp_exp
        num, den, plain = 0.0, 0.0, []
        for k in set(e["calls"]) & set(e["puts"]):
            c, p = e["calls"][k], e["puts"][k]
            if c["iv"] is None or p["iv"] is None:
                continue
            diff = c["iv"] - p["iv"]
            w = (c["open_interest"] or 0.0) + (p["open_interest"] or 0.0)
            plain.append(diff)
            num += diff * w
            den += w
        if den > 0:
            row["cpiv"] = _round(num / den)
        elif plain:
            row["cpiv"] = _round(sum(plain) / len(plain))

    # options volume against share volume, over the whole chain
    vols = [c["volume"] for c in contracts if c["volume"] is not None]
    if vols and share_vol:
        row["os_ratio"] = _round(sum(vols) * 100.0 / share_vol)
    return row


def trim_contracts(contracts, spot, as_of_d, n_expiries=N_EXPIRIES, window=STRIKE_WINDOW):
    """The 4 nearest expiries, ±10 strikes around spot per expiry and type, with dte."""
    exps = _by_expiry(contracts, as_of_d)
    keep = []
    for exp, e in list(exps.items())[:n_expiries]:
        for side in ("calls", "puts"):
            strikes = sorted(e[side])
            if spot and strikes:
                i = strikes.index(_atm_strike(strikes, spot))
                strikes = strikes[max(0, i - window): i + window + 1]
            for k in strikes:
                keep.append(dict(e[side][k], dte=e["dte"], spot=spot))
    return keep


def chain_rows(chains, as_of_d, spots=None, share_volumes=None):
    """Contract rows (trimmed) followed by one derived row per symbol. Pure."""
    spots, share_volumes = spots or {}, share_volumes or {}
    rows = []
    for sym in sorted(chains):
        ch = dict(chains[sym])
        if ch.get("spot") is None:
            ch["spot"] = spots.get(sym)
        rows.extend(trim_contracts(ch["contracts"], ch["spot"], as_of_d))
        rows.append(derived_row(sym, ch, as_of_d, share_volumes.get(sym)))
    return rows


def _chain_meta(run_dir, meta):
    m = dict(meta or {})
    slot = m.get("slot") or "ad-hoc"
    m["slot"] = archive.slot_slug(slot)
    as_of = m.get("as_of")
    rid = str(m.get("run_id") or "")
    m["date"] = (m.get("date") or (str(as_of)[:10] if as_of else None)
                 or (rid[:10] if _date_of(rid[:10]) else None) or date.today().isoformat())
    m["as_of"] = as_of or m["date"]
    m["run_id"] = m.get("run_id") or f"{m['date']}-{m['slot']}"
    m.setdefault("engine_sha", config.engine_sha(run_dir))
    m["kind"] = CHAIN_KIND
    return m


def write_chain_snapshot(run_dir=None, out_dir=None, meta=None):
    """Write `<out_dir>/chain_snapshot/<date>-<slot>.jsonl.gz`; returns (path, n_rows).

    Header-only (n_rows 0) when no chain file is staged — the absence is itself a record.
    Spot falls back to the staged quotes, then to the scan price; share volume to the
    scan_data candidate's `volume`.
    """
    run_dir = run_dir or BASE
    out_dir = out_dir or os.path.join(run_dir, "archive")
    m = _chain_meta(run_dir, meta)
    files = chain_files(run_dir)
    m["source_files"] = files
    as_of_d = _date_of(m["date"]) or date.today()
    rows = []
    if files:
        chains = load_chains(run_dir)
        spots = {s: q["last"] for s, q in load_quotes(run_dir).items() if q.get("last")}
        res = _load(os.path.join(run_dir, "scan_results.json")) or {}
        for r in res.get("results") or []:
            if isinstance(r, dict) and r.get("ticker") and _num(r.get("price")):
                spots.setdefault(str(r["ticker"]).upper(), _num(r["price"]))
        vols = {}
        data = _load(os.path.join(run_dir, "scan_data.json")) or {}
        cands = data.get("candidates") or {}
        if isinstance(cands, dict):
            for tk, c in cands.items():
                if isinstance(c, dict) and _num(c.get("volume")):
                    vols[str(tk).upper()] = _num(c["volume"])
        rows = chain_rows(chains, as_of_d, spots, vols)
    m["n_symbols"] = sum(1 for r in rows if r.get("_derived"))
    path = snapshot_path(out_dir, CHAIN_KIND, m["date"], m["slot"], m["as_of"])
    write_jsonl(path, m, rows)
    return path, len(rows)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-dir", default=BASE)
    ap.add_argument("--out", default=None, help="archive dir (default <run-dir>/archive)")
    ap.add_argument("--slot", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--as-of", default=None, help="ISO timestamp or date")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--chain", action="store_true")
    ap.add_argument("--both", action="store_true")
    a = ap.parse_args(argv)
    if not (a.scan or a.chain or a.both):
        a.both = True
    out = a.out or os.path.join(a.run_dir, "archive")
    meta = {k: v for k, v in (("slot", a.slot), ("run_id", a.run_id), ("as_of", a.as_of)) if v}
    code = 0
    if a.scan or a.both:
        try:
            path, n = write_scan_snapshot(a.run_dir, out, dict(meta))
            print(f"scan snapshot  -> {path}  ({n} rows)")
        except (OSError, ValueError, KeyError) as exc:
            print(f"scan snapshot FAILED: {exc}", file=sys.stderr)
            code = 1
    if a.chain or a.both:
        try:
            path, n = write_chain_snapshot(a.run_dir, out, dict(meta))
            print(f"chain snapshot -> {path}  ({n} rows)")
        except (OSError, ValueError, KeyError) as exc:
            print(f"chain snapshot FAILED: {exc}", file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
