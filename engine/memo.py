"""memo.py — the LLM-as-structured-extractor pattern, with calibration logging (P-07).

WHY THIS SHAPE (Appendix G of the 2026-09-10 research synthesis)
----------------------------------------------------------------
* LLM headline sentiment carries a real but small 1–2 day effect that decays as the trade
  gets crowded. It is evidence for the catalyst pillar's *record*, not a signal to size on.
* Anonymising the company before scoring improves the read (Glasserman & Lin): a model that
  recognises the name scores its prior about the company, not the news in front of it.
* Any evaluation on dates before the model's training cutoff is uninterpretable
  (Lopez-Lira, Tang & Zhu) — the model may simply remember what happened.
* Zero-shot LLM probabilities are poorly calibrated (Brier ≈ 0.21 against a crowd's 0.15),
  so a probability the model emits is a claim to be logged against its outcome, never a
  number to act on until the log says it beats the base rate.

So the division of labour is fixed: the LLM, in a scheduled session, writes a MEMO — one
small JSON object per (symbol, slot) — from an anonymised news payload. The deterministic
engine validates the memo against the payload the session was given, consumes ONLY the
numeric / enumerated fields as logged research features (`llm_*` on the row's `features`
dict), and appends every probability call to `calibration.jsonl`, where `history.py
--resolve-memos` later fills in what the price actually did. Nothing here touches a score,
a size or an order: `scanner.py` copies the features onto the row exactly as it does the
S-04 technical features, and no pillar reads them.

THE MEMO (schema 1, one object per symbol, written to `memos/<SYMBOL>.json` in the run dir)
-------------------------------------------------------------------------------------------
    schema            1
    as_of             ISO-8601 timestamp of the slot the memo was written for
    symbol_hash       the payload's opaque id for the company — the LLM never sees the
                      ticker; the engine maps the hash back (symbol_hash())
    sources           [{url, published_at, source}] — every item the memo relied on
    event_type        earnings | guidance | mna | regulatory | product | management |
                      litigation | short_report | macro | other | none
    direction         positive | negative | mixed | none
    magnitude_bucket  none | small | medium | large
    confidence        0..1 — how sure the extractor is of event_type/direction
    p_up_5d           0..1 or null — an OPTIONAL probability the name closes higher in
                      5 sessions; logged, never scored
    quote             verbatim, <= 300 chars, copied from one source text
    numerals          every number the memo cites, copied verbatim (never computed)
    model             {id, cutoff_date, prompt_hash, temperature}

VALIDATION — `validate(memo, payload, latency_min=5)` rejects, with reasons:
    * wrong schema / missing keys / bad enum / out-of-range confidence or probability
    * any source `published_at` later than `as_of - latency_min` (a memo cannot cite news
      the slot could not have had yet; five minutes is the feed's own latency)
    * any numeral that does not appear in the payload text (the model computed or
      hallucinated a number)
    * a quote that is not a substring of a source text, raw or anonymised
    * model.temperature != 0 (a sampled memo is not reproducible from its prompt_hash)
    * as_of before model.cutoff_date (uninterpretable — the model may remember the outcome)
    * a symbol_hash that does not match the payload's

THE PAYLOAD (`news_payload.json` in the run dir) is what the session staged for the
extractor, keyed by symbol:
    {as_of, salt, symbols: {SYM: {symbol_hash, company_name, aliases, executives,
                                  items: [{url, published_at, source, title, text}]}}}
A single-symbol payload (the inner object alone, plus `symbol`) is accepted too.

CALIBRATION — `calibration.jsonl` in the archive dir, one row per probability call:
    {as_of, symbol, slot, run_id, p_up_5d, direction, event_type, confidence, model_id,
     realised_up_5d: null, fwd_ret_5d: null, brier: null, resolved_on: null}
`resolve()` (history.py --resolve-memos) fills realised_up_5d / brier from bars once five
sessions have passed. `calibration_report()` returns Brier against the base-rate Brier and a
ten-bin reliability table, and the rule: until brier < base_rate_brier over n >= 100
resolved calls, the feature is `advisory: true`. The engine never sizes on it either way;
the flag is for whoever reads ic.py --by-feature.

Stdlib only; paths resolve from wherever the caller points.
"""
import hashlib
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

SCHEMA = 1
EVENT_TYPES = ("earnings", "guidance", "mna", "regulatory", "product", "management",
               "litigation", "short_report", "macro", "other", "none")
DIRECTIONS = ("positive", "negative", "mixed", "none")
MAGNITUDES = ("none", "small", "medium", "large")
DIRECTION_SIGN = {"positive": 1, "negative": -1, "mixed": 0, "none": 0}
MAGNITUDE_LEVEL = {"none": 0, "small": 1, "medium": 2, "large": 3}
LATENCY_MIN = 5
MAX_QUOTE_CHARS = 300
REQUIRED = ("schema", "as_of", "symbol_hash", "sources", "event_type", "direction",
            "magnitude_bucket", "confidence", "quote", "numerals", "model")
MODEL_REQUIRED = ("id", "cutoff_date", "prompt_hash", "temperature")
FEATURE_KEYS = ("llm_event_type", "llm_direction", "llm_magnitude", "llm_confidence",
                "llm_p_up_5d")
CALIBRATION_FILE = "calibration.jsonl"
HORIZON_SESSIONS = 5
MIN_N_FOR_LIVE = 100
N_BINS = 10
COMPANY_PLACEHOLDER = "COMPANY_A"
EXEC_PLACEHOLDER = "EXEC_{n}"

_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


# ---------------------------------------------------------------- small helpers
def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def parse_ts(s):
    """ISO-8601 -> aware UTC datetime, or None. A naive stamp is taken as UTC."""
    if isinstance(s, datetime):
        d = s
    elif isinstance(s, str) and s.strip():
        txt = s.strip()
        if txt.endswith("Z") or txt.endswith("z"):
            txt = txt[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(txt)
        except ValueError:
            try:
                d = datetime.strptime(txt[:10], "%Y-%m-%d")
            except ValueError:
                return None
    else:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def symbol_hash(symbol, salt=""):
    """The opaque id a payload carries instead of the ticker. Deterministic per salt so the
    engine can map it back; a per-run salt means the id cannot be looked up across runs."""
    return hashlib.sha256(f"{salt}:{str(symbol).upper().strip()}".encode("utf-8")).hexdigest()[:16]


def prompt_hash(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


def _norm_number(tok):
    """'$1,250.5' / '12%' / 12.0 -> a canonical string, or None when not a number."""
    if isnum(tok):
        f = float(tok)
    else:
        s = str(tok).strip().replace(",", "").replace("$", "").rstrip("%")
        if s.startswith("+"):
            s = s[1:]
        try:
            f = float(s)
        except ValueError:
            return None
    if not math.isfinite(f):
        return None
    return str(int(f)) if f == int(f) else repr(f)


def numerals_in(text):
    """Every number-like token in a text, normalised."""
    out = set()
    for m in _NUMBER.findall(text or ""):
        n = _norm_number(m)
        if n is not None:
            out.add(n)
    return out


# ---------------------------------------------------------------- the payload
def payload_for(payload, symbol):
    """The per-symbol block of a staged news_payload.json, or None."""
    if not isinstance(payload, dict):
        return None
    syms = payload.get("symbols")
    if isinstance(syms, dict):
        blk = syms.get(str(symbol).upper())
        if isinstance(blk, dict):
            blk = dict(blk)
            blk.setdefault("as_of", payload.get("as_of"))
            blk.setdefault("salt", payload.get("salt"))
            blk.setdefault("symbol", str(symbol).upper())
            return blk
        return None
    if isinstance(payload.get("items"), list):
        if payload.get("symbol") and str(payload["symbol"]).upper() != str(symbol).upper():
            return None
        return payload
    return None


def _items(payload):
    items = payload.get("items") if isinstance(payload, dict) else None
    return [i for i in (items or []) if isinstance(i, dict)]


def _item_text(item):
    return " ".join(str(item.get(k) or "") for k in ("title", "text", "body", "summary")).strip()


def source_texts(payload):
    """Every source text the extractor could have quoted: raw and anonymised."""
    texts = [_item_text(i) for i in _items(payload)]
    texts = [t for t in texts if t]
    if isinstance(payload, dict) and (payload.get("company_name") or payload.get("symbol")):
        anon, _ = anonymise(texts, payload.get("symbol"), payload.get("company_name"),
                            payload.get("aliases"), payload.get("executives"))
        texts = texts + [t for t in anon if t]
    return texts


# ---------------------------------------------------------------- anonymisation
def _forms(symbol, company_name, aliases):
    forms = []
    for f in [company_name] + list(aliases or []) + [symbol]:
        if isinstance(f, str) and f.strip():
            forms.append(f.strip())
    # longest first so "NVIDIA Corporation" is replaced before "NVIDIA"
    seen, out = set(), []
    for f in sorted(forms, key=lambda s: -len(s)):
        if f.lower() not in seen:
            seen.add(f.lower())
            out.append(f)
    return out


def _pattern(form, ticker=False):
    esc = re.escape(form)
    if ticker:
        return re.compile(r"(?<![A-Za-z0-9])\$?" + esc + r"(?![A-Za-z0-9])")
    return re.compile(r"(?<![A-Za-z0-9])" + esc + r"(?![A-Za-z0-9])", re.IGNORECASE)


def anonymise(payload, symbol, company_name=None, aliases=None, executives=None):
    """Replace the ticker, company name, aliases and named executives with placeholders.

    `payload` may be a string, a list of strings, or a dict (every string value is
    rewritten, recursively; `company_name`, `aliases`, `executives` and `symbol` keys are
    dropped from the copy so the identity cannot leak through the metadata).
    Returns (anonymised, mapping) where mapping is what deanonymise() needs:
        {"COMPANY_A": {"symbol", "company_name", "aliases"}, "EXEC_1": "<name>", ...}
    """
    symbol = str(symbol or "").upper().strip()
    company = str(company_name).strip() if isinstance(company_name, str) and company_name.strip() else None
    aliases = [a for a in (aliases or []) if isinstance(a, str) and a.strip()]
    execs = [e.strip() for e in (executives or []) if isinstance(e, str) and e.strip()]
    mapping = {COMPANY_PLACEHOLDER: {"symbol": symbol or None, "company_name": company,
                                     "aliases": aliases}}
    subs = []
    for form in _forms(None, company, aliases):
        subs.append((_pattern(form), COMPANY_PLACEHOLDER))
    if symbol:
        subs.append((_pattern(symbol, ticker=True), COMPANY_PLACEHOLDER))
    # numbered in the order given (EXEC_1 is the first name listed), substituted longest
    # first so "Jensen Huang" is replaced before a bare "Huang" alias would be
    exec_subs = []
    for name in execs:
        if name in [e for e, _ in exec_subs]:
            continue
        ph = EXEC_PLACEHOLDER.format(n=len(exec_subs) + 1)
        mapping[ph] = name
        exec_subs.append((name, ph))
    for name, ph in sorted(exec_subs, key=lambda kv: -len(kv[0])):
        subs.append((_pattern(name), ph))

    def _text(s):
        for pat, ph in subs:
            s = pat.sub(ph, s)
        return s

    def _walk(v):
        if isinstance(v, str):
            return _text(v)
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()
                    if k not in ("company_name", "aliases", "executives", "symbol")}
        return v

    return _walk(payload), mapping


def deanonymise(text, mapping):
    """Put the names back, for logging. COMPANY_A becomes the company name (the ticker when
    there is none); EXEC_n the executive's name. Works on strings, lists and dicts."""
    mapping = mapping or {}
    repl = []
    for ph, val in mapping.items():
        if isinstance(val, dict):
            val = val.get("company_name") or val.get("symbol") or ph
        repl.append((ph, str(val)))
    repl.sort(key=lambda kv: -len(kv[0]))       # EXEC_10 before EXEC_1

    def _text(s):
        for ph, val in repl:
            s = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(ph) + r"(?![A-Za-z0-9_])", val, s)
        return s

    def _walk(v):
        if isinstance(v, str):
            return _text(v)
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v

    return _walk(text)


# ---------------------------------------------------------------- validation
def validate(memo, payload, latency_min=LATENCY_MIN):
    """(ok, errors). Every reason is collected, not just the first."""
    errs = []
    if not isinstance(memo, dict):
        return False, ["memo is not a JSON object"]
    if not isinstance(payload, dict):
        payload = {}
    for k in REQUIRED:
        if k not in memo:
            errs.append(f"missing key: {k}")
    if errs:
        return False, errs
    if memo.get("schema") != SCHEMA:
        errs.append(f"schema {memo.get('schema')!r} != {SCHEMA}")

    as_of = parse_ts(memo.get("as_of"))
    if as_of is None:
        errs.append(f"as_of unparseable: {memo.get('as_of')!r}")

    # identity: the hash must be the payload's, never a ticker
    sh = memo.get("symbol_hash")
    if not isinstance(sh, str) or not sh.strip():
        errs.append("symbol_hash missing")
    else:
        want = payload.get("symbol_hash")
        if not want and payload.get("symbol") and payload.get("salt") is not None:
            want = symbol_hash(payload["symbol"], payload["salt"])
        if want and sh != want:
            errs.append("symbol_hash does not match the payload")
        sym = str(payload.get("symbol") or "").upper()
        if sym and sh.upper() == sym:
            errs.append("symbol_hash is the ticker itself")

    # enums and bounds
    for key, allowed in (("event_type", EVENT_TYPES), ("direction", DIRECTIONS),
                         ("magnitude_bucket", MAGNITUDES)):
        if memo.get(key) not in allowed:
            errs.append(f"{key} {memo.get(key)!r} not in {list(allowed)}")
    conf = memo.get("confidence")
    if not isnum(conf) or not 0.0 <= conf <= 1.0:
        errs.append(f"confidence {conf!r} not in [0, 1]")
    p = memo.get("p_up_5d")
    if p is not None and (not isnum(p) or not 0.0 <= p <= 1.0):
        errs.append(f"p_up_5d {p!r} not in [0, 1]")

    # sources: shape and latency
    sources = memo.get("sources")
    if not isinstance(sources, list):
        errs.append("sources is not a list")
        sources = []
    if memo.get("event_type") != "none" and not sources:
        errs.append("event_type is not 'none' but no sources are cited")
    latest_ok = (as_of - timedelta(minutes=float(latency_min))) if as_of else None
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            errs.append(f"sources[{i}] is not an object")
            continue
        for k in ("url", "published_at", "source"):
            if not s.get(k):
                errs.append(f"sources[{i}].{k} missing")
        pub = parse_ts(s.get("published_at"))
        if s.get("published_at") and pub is None:
            errs.append(f"sources[{i}].published_at unparseable")
        elif pub and latest_ok and pub > latest_ok:
            errs.append(f"sources[{i}].published_at {s['published_at']} is later than "
                        f"as_of - {latency_min} min (future or not-yet-available news)")

    # the quote must be copied, not paraphrased
    quote = memo.get("quote")
    texts = source_texts(payload)
    if not isinstance(quote, str):
        errs.append("quote is not a string")
    else:
        q = quote.strip()
        if len(q) > MAX_QUOTE_CHARS:
            errs.append(f"quote is {len(q)} chars, max {MAX_QUOTE_CHARS}")
        if q and not any(q in t for t in texts):
            errs.append("quote is not a verbatim substring of any source text")
        if not q and memo.get("event_type") != "none":
            errs.append("event_type is not 'none' but the quote is empty")

    # every numeral must exist in the payload — copied, never computed
    nums = memo.get("numerals")
    if not isinstance(nums, list):
        errs.append("numerals is not a list")
    else:
        have = numerals_in(" ".join(texts))
        for n in nums:
            key = _norm_number(n)
            if key is None:
                errs.append(f"numeral {n!r} is not a number")
            elif key not in have:
                errs.append(f"numeral {n!r} does not appear in the payload")

    # the model card
    model = memo.get("model")
    if not isinstance(model, dict):
        errs.append("model is not an object")
    else:
        for k in MODEL_REQUIRED:
            if k not in model:
                errs.append(f"model.{k} missing")
        t = model.get("temperature")
        if "temperature" in model and (not isnum(t) or t != 0):
            errs.append(f"model.temperature {t!r} != 0")
        cut = parse_ts(model.get("cutoff_date"))
        if model.get("cutoff_date") and cut is None:
            errs.append("model.cutoff_date unparseable")
        elif cut and as_of and as_of < cut:
            errs.append(f"as_of {memo.get('as_of')} is before model.cutoff_date "
                        f"{model.get('cutoff_date')} — uninterpretable (Lopez-Lira et al.)")
    return not errs, errs


# ---------------------------------------------------------------- features
def to_features(memo):
    """The only fields the engine consumes: enumerations and numbers. Null when no memo."""
    if not isinstance(memo, dict):
        return {k: None for k in FEATURE_KEYS}
    p = memo.get("p_up_5d")
    conf = memo.get("confidence")
    return {
        "llm_event_type": memo.get("event_type"),
        "llm_direction": DIRECTION_SIGN.get(memo.get("direction")),
        "llm_magnitude": MAGNITUDE_LEVEL.get(memo.get("magnitude_bucket")),
        "llm_confidence": float(conf) if isnum(conf) else None,
        "llm_p_up_5d": float(p) if isnum(p) else None,
    }


# ---------------------------------------------------------------- calibration log
def read_calibration(path):
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _write_rows(path, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def log_calibration(path, memo, symbol, slot=None, run_id=None):
    """Append one pending row for a memo that made a probability call. Returns the row, or
    None when the memo made no call. A (as_of, symbol) already logged is not logged twice."""
    p = memo.get("p_up_5d") if isinstance(memo, dict) else None
    if not isnum(p):
        return None
    row = {"as_of": memo.get("as_of"), "symbol": str(symbol).upper(), "slot": slot,
           "run_id": run_id, "p_up_5d": float(p), "direction": memo.get("direction"),
           "event_type": memo.get("event_type"), "confidence": memo.get("confidence"),
           "model_id": (memo.get("model") or {}).get("id"),
           "prompt_hash": (memo.get("model") or {}).get("prompt_hash"),
           "realised_up_5d": None, "fwd_ret_5d": None, "brier": None, "resolved_on": None}
    for r in read_calibration(path):
        if r.get("as_of") == row["as_of"] and r.get("symbol") == row["symbol"]:
            return r
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    return row


def load_bars(path_or_payload):
    """{SYMBOL: [(date, close), ...]} oldest first. Accepts a raw get_equity_historicals
    response, a list of them, or a plain {SYM: [{date, close}]} map."""
    raw = path_or_payload
    if isinstance(raw, str):
        with open(raw, encoding="utf-8") as fh:
            raw = json.load(fh)
    series = {}

    def add(sym, pts):
        sym = str(sym or "").upper()
        if sym and pts:
            merged = dict(series.get(sym, []))
            merged.update(dict(pts))
            series[sym] = sorted(merged.items())

    def bar_pt(b):
        if not isinstance(b, dict) or b.get("interpolated"):
            return None
        c = b.get("close_price", b.get("close", b.get("c")))
        try:
            c = float(c)
        except (TypeError, ValueError):
            return None
        d = str(b.get("begins_at") or b.get("date") or b.get("t") or "")[:10]
        return (d, c) if d and math.isfinite(c) else None

    def walk(obj):
        if isinstance(obj, list):
            for o in obj:
                walk(o)
            return
        if not isinstance(obj, dict):
            return
        rows = (obj.get("data") or {}).get("results") if isinstance(obj.get("data"), dict) else None
        rows = rows or obj.get("results")
        if isinstance(rows, list):
            for res in rows:
                if isinstance(res, dict) and res.get("symbol"):
                    add(res["symbol"], [p for p in map(bar_pt, res.get("bars") or []) if p])
            return
        if obj.get("symbol") and isinstance(obj.get("bars"), list):
            add(obj["symbol"], [p for p in map(bar_pt, obj["bars"]) if p])
            return
        for sym, bars in obj.items():                       # {SYM: [bars]}
            if isinstance(bars, list) and not str(sym).startswith("_"):
                add(sym, [p for p in map(bar_pt, bars) if p])
    walk(raw)
    return series


def forward_outcome(series, as_of, horizon=HORIZON_SESSIONS):
    """(realised_up, fwd_ret) from the last close on/before as_of to `horizon` sessions
    later; (None, None) until those bars exist."""
    d0 = str(as_of or "")[:10]
    if not d0 or not series:
        return None, None
    base = None
    for i, (d, c) in enumerate(series):
        if d <= d0:
            base = i
        else:
            break
    if base is None or base + horizon >= len(series):
        return None, None
    c0, c1 = series[base][1], series[base + horizon][1]
    if not c0:
        return None, None
    ret = c1 / c0 - 1.0
    return (1 if ret > 0 else 0), ret


def resolve(path, bars, horizon=HORIZON_SESSIONS, today=None):
    """Fill realised outcomes into the pending rows of calibration.jsonl. Returns
    (rows, n_resolved_now, n_still_pending). Rewrites the file only when something changed."""
    rows = read_calibration(path)
    parsed = isinstance(bars, dict) and all(
        isinstance(v, list) and (not v or (isinstance(v[0], (tuple, list)) and len(v[0]) == 2
                                           and isinstance(v[0][0], str)))
        for v in bars.values())
    series = {k: [tuple(p) for p in v] for k, v in bars.items()} if parsed else load_bars(bars)
    stamp = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    done = pending = 0
    for r in rows:
        if r.get("realised_up_5d") is not None:
            continue
        y, ret = forward_outcome(series.get(str(r.get("symbol") or "").upper()),
                                 r.get("as_of"), horizon)
        if y is None:
            pending += 1
            continue
        p = r.get("p_up_5d")
        r["realised_up_5d"] = y
        r["fwd_ret_5d"] = round(ret, 6)
        r["brier"] = round((float(p) - y) ** 2, 6) if isnum(p) else None
        r["resolved_on"] = stamp
        done += 1
    if done:
        _write_rows(path, rows)
    return rows, done, pending


def calibration_report(path_or_rows, min_n=MIN_N_FOR_LIVE, n_bins=N_BINS):
    """Brier score of the resolved calls against the base-rate Brier, a reliability table,
    and the advisory rule. `advisory` is True until brier < base_rate_brier over n >= min_n."""
    rows = read_calibration(path_or_rows) if isinstance(path_or_rows, str) else list(path_or_rows or [])
    res = [(float(r["p_up_5d"]), int(r["realised_up_5d"])) for r in rows
           if isinstance(r, dict) and isnum(r.get("p_up_5d")) and r.get("realised_up_5d") in (0, 1)]
    n = len(res)
    pending = sum(1 for r in rows if isinstance(r, dict) and isnum(r.get("p_up_5d"))
                  and r.get("realised_up_5d") is None)
    out = {"n": n, "pending": pending, "brier": None, "base_rate": None,
           "base_rate_brier": None, "reliability_bins": [], "min_n": min_n,
           "advisory": True, "advisory_reason": None}
    if n == 0:
        out["advisory_reason"] = "no resolved probability calls yet"
        return out
    brier = sum((p - y) ** 2 for p, y in res) / n
    base = sum(y for _, y in res) / n
    base_brier = sum((base - y) ** 2 for _, y in res) / n
    bins = []
    for b in range(n_bins):
        lo, hi = b / n_bins, (b + 1) / n_bins
        members = [(p, y) for p, y in res if (lo <= p < hi) or (b == n_bins - 1 and p == 1.0)]
        if members:
            bins.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(members),
                         "mean_p": round(sum(p for p, _ in members) / len(members), 4),
                         "mean_outcome": round(sum(y for _, y in members) / len(members), 4)})
    out.update({"brier": round(brier, 6), "base_rate": round(base, 4),
                "base_rate_brier": round(base_brier, 6), "reliability_bins": bins})
    beats = brier < base_brier
    if n < min_n and beats:
        out["advisory_reason"] = f"beats the base rate but only {n} of {min_n} calls resolved"
    elif n < min_n:
        out["advisory_reason"] = f"does not beat the base rate ({n} of {min_n} calls resolved)"
    elif not beats:
        out["advisory_reason"] = (f"Brier {brier:.4f} is not below the base-rate Brier "
                                  f"{base_brier:.4f} over {n} calls")
    else:
        out["advisory"] = False
        out["advisory_reason"] = (f"Brier {brier:.4f} beats the base-rate Brier "
                                  f"{base_brier:.4f} over {n} calls")
    return out


# ---------------------------------------------------------------- the run-dir hook
def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def apply_memos(data, run_dir, archive_dir=None, latency_min=LATENCY_MIN):
    """Read memos/<SYMBOL>.json for every candidate, validate each against
    news_payload.json, attach the llm_* features to valid ones, record rejections in
    meta.memo_rejections and log probability calls. Mutates `data`; never raises.

    When there is no memos/ directory nothing is touched, so a run without memos is
    byte-identical to one before this module existed. When there is one, every candidate
    carries the llm_* keys (null where no valid memo) so the dataset stays rectangular.
    """
    report = {"accepted": [], "rejected": {}, "logged": []}
    try:
        memo_dir = os.path.join(run_dir, "memos")
        if not os.path.isdir(memo_dir):
            return report
        cands = data.get("candidates")
        if not isinstance(cands, dict):
            return report
        meta = data.setdefault("meta", {})
        payload, perr = _load_json(os.path.join(run_dir, "news_payload.json"))
        cal_path = os.path.join(archive_dir, CALIBRATION_FILE) if archive_dir else None
        for tk, c in cands.items():
            if not isinstance(c, dict):
                continue
            sym = str(tk).upper()
            feats = c.get("features") if isinstance(c.get("features"), dict) else {}
            path = os.path.join(memo_dir, f"{sym}.json")
            memo = None
            if os.path.exists(path):
                memo, merr = _load_json(path)
                if memo is None:
                    report["rejected"][sym] = [f"memo unreadable: {merr}"]
                else:
                    blk = payload_for(payload, sym)
                    errs = []
                    if blk is None:
                        errs.append("no news_payload.json entry for this symbol"
                                    + (f" ({perr})" if perr else ""))
                    ok, verrs = validate(memo, blk or {}, latency_min)
                    errs += verrs
                    if errs:
                        report["rejected"][sym] = errs
                        memo = None
            feats = dict(feats)
            feats.update(to_features(memo))
            c["features"] = feats
            if memo is not None:
                report["accepted"].append(sym)
                if cal_path:
                    try:
                        row = log_calibration(cal_path, memo, sym,
                                              slot=meta.get("slot") or meta.get("session"),
                                              run_id=meta.get("run_id"))
                        if row:
                            report["logged"].append(sym)
                    except OSError as exc:
                        meta.setdefault("data_warnings", []).append(
                            f"MEMO: calibration row for {sym} not written: {exc}")
        meta["memo_rejections"] = report["rejected"]
        meta["memos"] = {"accepted": sorted(report["accepted"]),
                         "rejected": len(report["rejected"]),
                         "calibration_logged": sorted(report["logged"])}
        if cal_path:
            rep = calibration_report(cal_path)
            meta["memos"]["advisory"] = rep["advisory"]
            meta["memos"]["calibration"] = {k: rep[k] for k in ("n", "pending", "brier",
                                                                 "base_rate_brier")}
        if report["rejected"]:
            meta.setdefault("data_warnings", []).append(
                "MEMO: rejected " + ", ".join(f"{s} ({len(e)} reason(s))"
                                             for s, e in sorted(report["rejected"].items()))
                + " — see meta.memo_rejections")
    except Exception as exc:                          # noqa: BLE001 — a memo never kills a scan
        try:
            data.setdefault("meta", {}).setdefault("data_warnings", []).append(
                f"MEMO: processing failed and was skipped: {type(exc).__name__}: {exc}")
        except Exception:                             # noqa: BLE001
            pass
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report
