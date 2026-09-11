"""insiders.py — opportunistic insider buying from SEC Form 4 (P-01, experiment E15).

WHY. Cohen, Malloy & Pomorski (2012, "Decoding Inside Information", J. Finance) split
insider trades into ROUTINE — the insider traded in the same calendar month in each of the
three prior years — and OPPORTUNISTIC — everything else. Opportunistic buys earned about
82 bp/month of abnormal return in their sample; routine trades earned nothing. The
retail-friendly version of the same idea is the CLUSTER BUY: two or more distinct insiders
buying on the open market inside a 30-day window. The scan's existing insider panel is a
hand-collected list from a scrape (SCAN.md §9.3); it is a display, not a feature, and this
module does not touch it.

WHAT. Three pure pieces plus a staged-file reader and a CLI:

  parse_form4(text)                  one Form 4 XML (or the .txt complete-submission
                                     wrapper) -> list of transaction dicts (schema below).
                                     Tolerant: missing elements become None, the derivative
                                     table is ignored, and a document that is not an
                                     ownershipDocument yields [].
  classify_routine(history, date)    "routine" | "opportunistic" | "unknown" for one trade
                                     given the owner's prior transactions — the 3-year
                                     same-calendar-month rule. A prior year the history does
                                     not cover cannot be checked, so a miss there is
                                     "unknown", never "routine" and never "opportunistic".
  signal(txns_by_symbol, as_of)      per symbol: opportunistic buyers and dollars over 30
                                     days, routine dollars, net open-market dollars over 90
                                     days, the cluster flag, the last buy date.
  load_staged(run_dir)               insiders.json (parsed transactions the scheduled task
                                     stages) and/or form4/*.xml, deduplicated.

scanner.py reads the OUTPUT of this module — insiders_signal.json — and copies three
values onto each scored row's `features` dict (insider_cluster_buy,
insider_opportunistic_buy_usd_30d, insider_net_usd_90d). Logged, never scored: the E15
test in docs/BACKTEST.md is the only reader, through ic.py --by-feature. This module is a
data dependency of the scan, not an import, exactly like sentiment.py.

THE STAGED FILE — insiders.json. Either a bare list of transactions or
{"_meta": {...}, "transactions": [...]}. One transaction:

    {"symbol": "AAPL",                  issuerTradingSymbol, upper-cased
     "issuer_cik": "0000320193", "issuer_name": "Apple Inc.",
     "owner": "DOE JANE",               rptOwnerName as filed
     "owner_cik": "0001214128",         the stable identity; the name is a fallback
     "relationship": ["director"],      any of director, officer, ten_percent_owner, other
     "officer_title": null,
     "date": "2026-08-14",              transactionDate — the trade
     "filed": "2026-08-18" | null,      the filing date when the stager knows it (from
                                        form.idx or the accession header) — the moment the
                                        trade became PUBLIC, and the only honest signal time
     "code": "P",                       transactionCode: P purchase, S sale, A grant, M
                                        exercise, F tax withholding, G gift ...
     "acquired_disposed": "A" | "D",
     "shares": 1000.0, "price": 150.25, "value": 150250.0,   value = shares × price
     "shares_after": 5000.0 | null,
     "direct": true | false | null,     D vs I ownership
     "plan_10b5_1": false,              the aff10b5One flag (schema X0508+); older filings
                                        say it only in a footnote, so false means "not
                                        flagged", not "not a plan"
     "accession": "0001214128-26-000123" | null,
     "source": "form4/0001214128-26-000123.xml"}

Only open-market purchases (P) and sales (S) enter the signal. Grants, exercises, tax
withholdings and gifts are kept in the parsed list for the record and ignored by signal().

CLASSIFICATION WITH SHORT HISTORY. The rule needs the owner's trades in the same month of
each of the three prior years. A stager that pulls the last 90 days of Form 4s has no such
history, so almost every buy is "unknown". signal() counts unknown-history buyers with the
opportunistic ones (they are, by the paper's own definition, "everything that is not
routine") and reports how many of the window's buyers were unknown, so a reader can see
what the number rests on. A cluster of two "unknown" buyers is still a cluster.

POINT IN TIME. signal(as_of=...) uses the trade date for the windows and drops any
transaction whose `filed` date is after as_of when the stager supplied one: a trade filed
on Monday about Friday's purchase was not knowable on Friday. Without a `filed` date the
trade date is used and the window is optimistic by up to two business days (Form 4 is due
within two business days of the trade); the meta says which.

FETCHING (documented, run on the box, never from a scheduled session's sandbox).
EDGAR's quarterly full index, https://www.sec.gov/Archives/edgar/full-index/<YYYY>/QTR<n>/
form.idx, lists every filing with form type, company, CIK, date filed and the path of the
complete submission .txt. `--edgar-index form.idx --symbols A,B --company-tickers
company_tickers.json` maps tickers to issuer CIKs (https://www.sec.gov/files/
company_tickers.json) and prints the URL of each Form 4 submission filed BY THAT ISSUER's CIK
(a Form 4 is indexed under both the issuer and the reporting owner; the issuer row is the
one to filter on). `--fetch` pulls them with urllib, one at a time, under SEC's fair-access
rules: a descriptive User-Agent from $SEC_USER_AGENT ("Name contact@example.com" — SEC
rejects requests without one) and at most 10 requests per second. Nothing here is fetched
in a test; the parser is exercised on fixtures.

Usage
-----
    python3 insiders.py --form4-dir form4/ --as-of 2026-09-10 --out insiders_signal.json
    python3 insiders.py --run-dir . --as-of 2026-09-10 --out insiders_signal.json
    python3 insiders.py --edgar-index form.idx --symbols AAPL,MSFT \\
                        --company-tickers company_tickers.json            # URLs only
    SEC_USER_AGENT="Jane Doe jane@example.com" python3 insiders.py --edgar-index form.idx \\
                        --symbols AAPL --company-tickers company_tickers.json \\
                        --fetch --form4-dir form4/                       # on the box
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

STAGED_FILE = "insiders.json"
FORM4_DIR = "form4"
SIGNAL_FILE = "insiders_signal.json"
SCHEMA = 1

BUY_CODES = ("P",)
SELL_CODES = ("S",)
WINDOW_DAYS = 30
NET_WINDOW_DAYS = 90
ROUTINE_YEARS = 3

SEC_ARCHIVES = "https://www.sec.gov/Archives/"
SEC_FULL_INDEX = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/form.idx"
SEC_COMPANY_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT_ENV = "SEC_USER_AGENT"
SEC_MAX_REQ_PER_S = 10
SEC_MIN_INTERVAL_S = 1.0 / SEC_MAX_REQ_PER_S
TIMEOUT_S = 30

_XML_BLOCK = re.compile(r"<XML>(.*?)</XML>", re.S | re.I)
_ACCESSION = re.compile(r"(\d{10}-\d{2}-\d{6})")


# ---------------------------------------------------------------- small helpers
def isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _date(s):
    """date from 'YYYY-MM-DD' (extra text tolerated), else None."""
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    if isinstance(s, datetime):
        return s.date()
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(v):
    if isnum(v):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _flag(v):
    """'1' / 'true' / 'yes' -> True, '0' / 'false' / '' -> False, None -> None."""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False
    return None


def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def _text(el, *path):
    """Text of the first element at `path` under el; None when absent or blank.

    Every scalar in a Form 4 may or may not sit in a <value> child (the schema wraps
    footnoted fields), so a path is tried with and without a trailing <value>.
    """
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = _child(cur, name)
    if cur is None:
        return None
    v = _child(cur, "value")
    node = v if v is not None else cur
    t = (node.text or "").strip()
    return t or None


def _child(el, name):
    for c in el:
        if _strip_ns(c.tag) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _strip_ns(c.tag) == name]


# ---------------------------------------------------------------- Form 4 parser
def _extract_xml(text):
    """The ownershipDocument XML out of raw XML or a complete-submission .txt."""
    if not isinstance(text, str):
        text = text.decode("utf-8", errors="replace")
    if "<ownershipDocument" in text and "<XML>" not in text and "<xml>" not in text:
        return text.lstrip("\ufeff")
    for m in _XML_BLOCK.finditer(text):
        block = m.group(1).strip()
        if "<ownershipDocument" in block:
            return block
    return text.lstrip("\ufeff")


def _accession_of(text, source=None):
    for cand in (source or "", text[:4000] if isinstance(text, str) else ""):
        m = _ACCESSION.search(str(cand))
        if m:
            return m.group(1)
    return None


def _filed_of(text):
    """FILED AS OF DATE from a complete-submission header, as ISO; None on raw XML."""
    if not isinstance(text, str):
        return None
    m = re.search(r"FILED AS OF DATE:\s*(\d{8})", text[:4000])
    if not m:
        return None
    d = m.group(1)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def parse_form4(text, source=None, filed=None):
    """One Form 4 document -> [transaction, ...] in the insiders.json schema.

    `text` is the XML (str or bytes) or the EDGAR complete-submission .txt that wraps it.
    Non-derivative transactions only. A document with no parseable issuer or no
    non-derivative transactions yields []. Never raises on a malformed document: it
    returns [] and the caller decides whether that is a warning.
    """
    xml_text = _extract_xml(text)
    filed = filed or _filed_of(text if isinstance(text, str) else "")
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    if _strip_ns(root.tag) != "ownershipDocument":
        return []

    issuer = _child(root, "issuer")
    symbol = _text(issuer, "issuerTradingSymbol") if issuer is not None else None
    symbol = symbol.upper().strip() if symbol else None
    issuer_cik = _text(issuer, "issuerCik") if issuer is not None else None
    issuer_name = _text(issuer, "issuerName") if issuer is not None else None

    owners = []
    for ro in _children(root, "reportingOwner"):
        rid = _child(ro, "reportingOwnerId")
        rel = _child(ro, "reportingOwnerRelationship")
        roles = []
        if rel is not None:
            for tag, label in (("isDirector", "director"), ("isOfficer", "officer"),
                               ("isTenPercentOwner", "ten_percent_owner"), ("isOther", "other")):
                if _flag(_text(rel, tag)):
                    roles.append(label)
        owners.append({
            "owner": (_text(rid, "rptOwnerName") if rid is not None else None),
            "owner_cik": (_text(rid, "rptOwnerCik") if rid is not None else None),
            "relationship": roles,
            "officer_title": (_text(rel, "officerTitle") if rel is not None else None),
        })
    if not owners:
        owners = [{"owner": None, "owner_cik": None, "relationship": [], "officer_title": None}]

    plan = _flag(_text(root, "aff10b5One"))
    accession = _accession_of(xml_text, source)
    period = _text(root, "periodOfReport")

    out = []
    table = _child(root, "nonDerivativeTable")
    if table is None:
        return out
    for tx in _children(table, "nonDerivativeTransaction"):
        coding = _child(tx, "transactionCoding")
        amounts = _child(tx, "transactionAmounts")
        post = _child(tx, "postTransactionAmounts")
        nature = _child(tx, "ownershipNature")
        d = _date(_text(tx, "transactionDate")) or _date(period)
        code = (_text(coding, "transactionCode") if coding is not None else None)
        code = code.upper().strip() if code else None
        shares = _num(_text(amounts, "transactionShares")) if amounts is not None else None
        price = _num(_text(amounts, "transactionPricePerShare")) if amounts is not None else None
        ad = (_text(amounts, "transactionAcquiredDisposedCode") if amounts is not None else None)
        ad = ad.upper().strip() if ad else None
        after = _num(_text(post, "sharesOwnedFollowingTransaction")) if post is not None else None
        di = (_text(nature, "directOrIndirectOwnership") if nature is not None else None)
        direct = None if not di else (di.strip().upper() == "D")
        value = round(shares * price, 2) if shares is not None and price is not None else None
        # A joint filing lists several reporting owners; the transaction is one trade.
        # Attribute it to the first owner (the filer) — a cluster needs distinct filers,
        # and one Form 4 is one filer's report.
        o = owners[0]
        out.append({
            "symbol": symbol, "issuer_cik": issuer_cik, "issuer_name": issuer_name,
            "owner": o["owner"], "owner_cik": o["owner_cik"],
            "relationship": list(o["relationship"]), "officer_title": o["officer_title"],
            "date": d.isoformat() if d else None, "filed": filed,
            "code": code, "acquired_disposed": ad,
            "shares": shares, "price": price, "value": value,
            "shares_after": after, "direct": direct,
            "plan_10b5_1": bool(plan) if plan is not None else False,
            "accession": accession, "source": source,
        })
    return out


def parse_form4_file(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", errors="replace")
    return parse_form4(text, source=os.path.basename(path))


# ---------------------------------------------------------------- normalise / dedupe
def _owner_key(t):
    return (t.get("owner_cik") or "").strip() or (t.get("owner") or "").strip().upper() or None


def _txn_key(t):
    return (t.get("accession") or "", _owner_key(t) or "", t.get("symbol") or "",
            t.get("date") or "", t.get("code") or "",
            round(t.get("shares") or 0.0, 4), round(t.get("price") or 0.0, 4))


def normalise_txn(t):
    """A staged transaction dict, coerced to the schema; None when it has no symbol/date."""
    if not isinstance(t, dict):
        return None
    sym = t.get("symbol") or t.get("ticker")
    d = _date(t.get("date") or t.get("transaction_date"))
    if not sym or d is None:
        return None
    shares, price = _num(t.get("shares")), _num(t.get("price"))
    value = _num(t.get("value"))
    if value is None and shares is not None and price is not None:
        value = round(shares * price, 2)
    rel = t.get("relationship")
    if isinstance(rel, str):
        rel = [rel]
    code = t.get("code") or t.get("transaction_code")
    return {
        "symbol": str(sym).upper().strip(), "issuer_cik": t.get("issuer_cik"),
        "issuer_name": t.get("issuer_name"),
        "owner": t.get("owner") or t.get("insider"), "owner_cik": t.get("owner_cik"),
        "relationship": list(rel) if isinstance(rel, list) else [],
        "officer_title": t.get("officer_title") or t.get("title"),
        "date": d.isoformat(), "filed": (_date(t.get("filed")).isoformat()
                                        if _date(t.get("filed")) else None),
        "code": str(code).upper().strip() if code else None,
        "acquired_disposed": t.get("acquired_disposed"),
        "shares": shares, "price": price, "value": value,
        "shares_after": _num(t.get("shares_after")), "direct": t.get("direct"),
        "plan_10b5_1": bool(t.get("plan_10b5_1")),
        "accession": t.get("accession"), "source": t.get("source"),
    }


def dedupe(txns):
    seen, out = set(), []
    for t in txns:
        k = _txn_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def by_symbol(txns):
    out = {}
    for t in txns:
        if t.get("symbol"):
            out.setdefault(t["symbol"], []).append(t)
    for v in out.values():
        v.sort(key=lambda t: (t.get("date") or "", _owner_key(t) or ""))
    return out


# ---------------------------------------------------------------- routine vs opportunistic
def _same_side(code, ref):
    if ref is None:
        return True
    if ref in BUY_CODES:
        return code in BUY_CODES
    if ref in SELL_CODES:
        return code in SELL_CODES
    return code == ref


def classify_routine(owner_history, txn_date, code="P", history_since=None, years=ROUTINE_YEARS):
    """Cohen–Malloy–Pomorski: "routine" when the owner traded (same side) in the same
    calendar month in each of the `years` prior years; "opportunistic" when a covered prior
    year has no such trade; "unknown" when every checkable year matched but at least one
    prior year lies before the history starts, so the rule cannot be applied.

    `owner_history` is the owner's transactions (dicts with `date` and `code`, or bare
    dates). `history_since` is the date from which the history is known to be complete —
    the fetch's start date; without it the earliest transaction in the history is used,
    which is the conservative reading (nothing before the first observed trade is assumed).
    """
    d = _date(txn_date)
    if d is None:
        return "unknown"
    dates = []
    for h in owner_history or []:
        if isinstance(h, dict):
            hd = _date(h.get("date"))
            if hd is None or not _same_side((h.get("code") or "").upper(), code):
                continue
        else:
            hd = _date(h)
            if hd is None:
                continue
        dates.append(hd)
    since = _date(history_since)
    if since is None:
        all_dates = [_date(h.get("date")) if isinstance(h, dict) else _date(h)
                     for h in owner_history or []]
        all_dates = [x for x in all_dates if x is not None]
        since = min(all_dates) if all_dates else None
    months = {(x.year, x.month) for x in dates}
    unknown = False
    for k in range(1, years + 1):
        y = d.year - k
        if (y, d.month) in months:
            continue
        month_start = date(y, d.month, 1)
        if since is None or since > month_start:
            unknown = True          # this year cannot be checked
            continue
        return "opportunistic"      # covered, and the insider did not trade that month
    return "unknown" if unknown else "routine"


# ---------------------------------------------------------------- the signal
def _window(txns, as_of, days):
    lo = as_of - timedelta(days=days)
    out = []
    for t in txns:
        d = _date(t.get("date"))
        if d is None or d > as_of or d < lo:
            continue
        f = _date(t.get("filed"))
        if f is not None and f > as_of:
            continue                # not public yet on as_of
        out.append(t)
    return out


def signal(txns_by_symbol, as_of, window_days=WINDOW_DAYS, net_window_days=NET_WINDOW_DAYS,
           history_since=None):
    """{SYMBOL: {...}} per the module doc. Pure; `txns_by_symbol` is by_symbol()'s shape.

    Owner histories for the routine rule are taken from the same map — every transaction
    of that owner across every symbol in it — so the more history the stager supplies,
    the fewer "unknown" classifications come out.
    """
    as_of_d = _date(as_of)
    if as_of_d is None:
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    owner_hist = {}
    for txns in txns_by_symbol.values():
        for t in txns:
            k = _owner_key(t)
            if k:
                owner_hist.setdefault(k, []).append(t)

    out = {}
    for sym, txns in sorted(txns_by_symbol.items()):
        open_market = [t for t in txns if (t.get("code") in BUY_CODES + SELL_CODES)]
        w30 = _window(open_market, as_of_d, window_days)
        w90 = _window(open_market, as_of_d, net_window_days)
        buyers, unknown_buyers = set(), set()
        opp_usd = routine_usd = 0.0
        last_buy = None
        classes = {"routine": 0, "opportunistic": 0, "unknown": 0}
        for t in w30:
            if t.get("code") not in BUY_CODES:
                continue
            k = _owner_key(t) or f"?{sym}"
            cls = classify_routine(
                [h for h in owner_hist.get(k, []) if (h.get("date") or "") < (t.get("date") or "")],
                t["date"], code=t["code"], history_since=history_since)
            classes[cls] += 1
            v = t.get("value") or 0.0
            if cls == "routine":
                routine_usd += v
            else:
                opp_usd += v
                buyers.add(k)
                if cls == "unknown":
                    unknown_buyers.add(k)
            if last_buy is None or t["date"] > last_buy:
                last_buy = t["date"]
        # last buy over the longer window too, so a name whose last buy is 45 days old says so
        for t in w90:
            if t.get("code") in BUY_CODES and (last_buy is None or t["date"] > last_buy):
                last_buy = t["date"]
        buy90 = sum(((t.get("value") or 0.0) for t in w90 if t.get("code") in BUY_CODES), 0.0)
        sell90 = sum(((t.get("value") or 0.0) for t in w90 if t.get("code") in SELL_CODES), 0.0)
        out[sym] = {
            "opportunistic_buyers_30d": len(buyers),
            "unknown_history_buyers_30d": len(unknown_buyers),
            "opportunistic_buy_usd_30d": round(opp_usd, 2),
            "routine_buy_usd_30d": round(routine_usd, 2),
            "buy_usd_90d": round(buy90, 2),
            "sell_usd_90d": round(sell90, 2),
            "net_insider_usd_90d": round(buy90 - sell90, 2),
            "cluster_buy": len(buyers) >= 2,
            "last_buy_date": last_buy,
            "n_txns": len(txns),
            "n_open_market_30d": len(w30),
            "buy_classes_30d": classes,
        }
    return out


def build(txns, as_of, window_days=WINDOW_DAYS, net_window_days=NET_WINDOW_DAYS,
          history_since=None, sources=None, warnings=None):
    """The insiders_signal.json document: {_meta, symbols}."""
    txns = dedupe([t for t in (normalise_txn(t) for t in txns) if t])
    bysym = by_symbol(txns)
    sig = signal(bysym, as_of, window_days, net_window_days, history_since)
    dates = sorted(t["date"] for t in txns if t.get("date"))
    n_filed = sum(1 for t in txns if t.get("filed"))
    warns = list(warnings or [])
    if txns and n_filed < len(txns):
        warns.append(f"{len(txns) - n_filed} of {len(txns)} transactions carry no filing date — "
                     "windowed on the trade date, which is up to two business days before "
                     "the trade was public")
    meta = {
        "schema": SCHEMA, "as_of": _date(as_of).isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_days": window_days, "net_window_days": net_window_days,
        "routine_rule": f"same calendar month in each of the {ROUTINE_YEARS} prior years "
                        "(Cohen, Malloy & Pomorski 2012); unknown when the history does not "
                        "cover a prior year",
        "history_since": _date(history_since).isoformat() if _date(history_since) else None,
        "n_txns": len(txns), "n_symbols": len(sig),
        "n_with_filed_date": n_filed,
        "history_span": {"oldest": dates[0] if dates else None,
                         "newest": dates[-1] if dates else None},
        "n_cluster_buy": sum(1 for v in sig.values() if v["cluster_buy"]),
        "sources": list(sources or []),
        "warnings": warns,
    }
    return {"_meta": meta, "symbols": sig}


# ---------------------------------------------------------------- staged inputs
def _load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_staged(run_dir=None, form4_dir=None, staged_file=None):
    """(transactions, sources, warnings) from insiders.json and/or form4/*.xml.

    `run_dir` defaults to SCAN_DIR; `form4_dir` to <run_dir>/form4; `staged_file` to
    <run_dir>/insiders.json. Either input may be absent. Rows from both are merged and
    deduplicated on (accession, owner, symbol, date, code, shares, price).
    """
    run_dir = run_dir or BASE
    staged_file = staged_file or os.path.join(run_dir, STAGED_FILE)
    form4_dir = form4_dir or os.path.join(run_dir, FORM4_DIR)
    txns, sources, warnings = [], [], []

    payload = _load_json(staged_file)
    if payload is not None:
        rows = payload.get("transactions") if isinstance(payload, dict) else payload
        rows = rows if isinstance(rows, list) else []
        bad = 0
        for r in rows:
            n = normalise_txn(r)
            if n is None:
                bad += 1
                continue
            txns.append(n)
        sources.append(os.path.basename(staged_file))
        if bad:
            warnings.append(f"{bad} row(s) in {os.path.basename(staged_file)} had no symbol or "
                            "date and were skipped")
    elif os.path.exists(staged_file):
        warnings.append(f"{os.path.basename(staged_file)} is not valid JSON — ignored")

    if os.path.isdir(form4_dir):
        files = sorted(glob.glob(os.path.join(form4_dir, "*.xml"))
                       + glob.glob(os.path.join(form4_dir, "*.txt")))
        empty = []
        for p in files:
            rows = parse_form4_file(p)
            if not rows:
                empty.append(os.path.basename(p))
            txns.extend(rows)
        if files:
            sources.append(f"{os.path.basename(form4_dir.rstrip(os.sep))}/ ({len(files)} file(s))")
        if empty:
            warnings.append(f"{len(empty)} Form 4 file(s) yielded no non-derivative transaction: "
                            + ", ".join(empty[:6]) + ("..." if len(empty) > 6 else ""))
    txns = dedupe([t for t in (normalise_txn(t) for t in txns) if t])
    return txns, sources, warnings


def load_signal(run_dir=None, path=None):
    """The staged insiders_signal.json, or None when absent or unusable."""
    p = path or os.path.join(run_dir or BASE, SIGNAL_FILE)
    doc = _load_json(p)
    if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), dict):
        return None
    return doc


# ---------------------------------------------------------------- EDGAR index -> URLs
def full_index_url(d):
    d = _date(d) or date.today()
    return SEC_FULL_INDEX.format(year=d.year, qtr=(d.month - 1) // 3 + 1)


def symbols_to_ciks(symbols, company_tickers):
    """{SYMBOL: 10-digit CIK} from SEC's company_tickers.json (any of its shapes)."""
    want = {s.strip().upper() for s in symbols if s and s.strip()}
    rows = []
    if isinstance(company_tickers, dict):
        if "data" in company_tickers and "fields" in company_tickers:   # exchange file
            f = company_tickers["fields"]
            rows = [dict(zip(f, r)) for r in company_tickers["data"]]
        else:
            rows = [v for v in company_tickers.values() if isinstance(v, dict)]
    elif isinstance(company_tickers, list):
        rows = [r for r in company_tickers if isinstance(r, dict)]
    out = {}
    for r in rows:
        tk = str(r.get("ticker") or "").upper()
        cik = r.get("cik_str") or r.get("cik")
        if tk in want and cik is not None:
            out[tk] = str(cik).zfill(10)
    return out


def parse_form_idx(text):
    """Rows of EDGAR's form.idx: [{form, company, cik, filed, path}]. The file is
    fixed-width after a dashed header line; columns are located from the header."""
    lines = text.splitlines()
    rows, started, cols = [], False, None
    for ln in lines:
        if not started:
            if ln.startswith("Form Type"):
                cols = {"company": ln.index("Company Name"), "cik": ln.index("CIK"),
                        "filed": ln.index("Date Filed"), "path": ln.index("File Name")}
            elif ln.startswith("---") and cols:
                started = True
            continue
        if not ln.strip():
            continue
        try:
            form = ln[:cols["company"]].strip()
            company = ln[cols["company"]:cols["cik"]].strip()
            cik = ln[cols["cik"]:cols["filed"]].strip()
            filed = ln[cols["filed"]:cols["path"]].strip()
            path = ln[cols["path"]:].strip()
        except (KeyError, TypeError):
            continue
        if not cik or not path:
            continue
        rows.append({"form": form, "company": company, "cik": cik.zfill(10),
                     "filed": filed, "path": path})
    return rows


def form4_urls(idx_rows, ciks, forms=("4", "4/A"), since=None):
    """[{symbol, cik, filed, accession, url}] for Form 4 filings whose index row carries one
    of the wanted issuer CIKs. The URL is the complete-submission .txt, which embeds the
    ownershipDocument XML and is what parse_form4 reads."""
    want = {cik: sym for sym, cik in ciks.items()}
    since_d = _date(since)
    out = []
    for r in idx_rows:
        if r["form"] not in forms or r["cik"] not in want:
            continue
        if since_d and (_date(r["filed"]) or date.min) < since_d:
            continue
        m = _ACCESSION.search(r["path"])
        out.append({"symbol": want[r["cik"]], "cik": r["cik"], "filed": r["filed"],
                    "form": r["form"], "accession": m.group(1) if m else None,
                    "url": SEC_ARCHIVES + r["path"].lstrip("/")})
    return out


# ---------------------------------------------------------------- fetch (CLI only)
def fetch_form4s(urls, out_dir, user_agent, opener=None, sleep=time.sleep,
                 min_interval=SEC_MIN_INTERVAL_S):
    """Pull each URL into out_dir/<accession>.txt, at most SEC_MAX_REQ_PER_S per second.
    Returns (n_written, errors). Skips a file that already exists."""
    import urllib.error
    import urllib.request
    os.makedirs(out_dir, exist_ok=True)
    n, errors, last = 0, [], 0.0
    for u in urls:
        name = (u.get("accession") or re.sub(r"[^A-Za-z0-9._-]", "_", u["url"].rsplit("/", 1)[-1]))
        dest = os.path.join(out_dir, name + (".txt" if not name.endswith(".txt") else ""))
        if os.path.exists(dest):
            continue
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            sleep(wait)
        req = urllib.request.Request(u["url"], headers={"User-Agent": user_agent,
                                                        "Accept-Encoding": "identity",
                                                        "Host": "www.sec.gov"})
        last = time.monotonic()
        try:
            with (opener or urllib.request).urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            errors.append(f"HTTP {e.code} {u['url']}")
            continue
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append(f"{u['url']}: {e}")
            continue
        tmp = dest + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest)
        n += 1
    return n, errors


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Opportunistic insider buying from Form 4 (E15).")
    ap.add_argument("--run-dir", default=None, help="staged run dir (default $SCAN_DIR)")
    ap.add_argument("--form4-dir", default=None, help="directory of Form 4 .xml / .txt files")
    ap.add_argument("--transactions", default=None, help="insiders.json (parsed transactions)")
    ap.add_argument("--as-of", default=None, help="YYYY-MM-DD (default today)")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--net-window-days", type=int, default=NET_WINDOW_DAYS)
    ap.add_argument("--history-since", default=None,
                    help="date the staged history is complete from (sharpens routine/unknown)")
    ap.add_argument("--out", default=None, help=f"signal file (default <run-dir>/{SIGNAL_FILE})")
    ap.add_argument("--dump-transactions", default=None,
                    help="also write the parsed, deduplicated transactions here (insiders.json shape)")
    ap.add_argument("--edgar-index", default=None, help="a downloaded full-index form.idx")
    ap.add_argument("--symbols", default=None, help="A,B,C — tickers to list Form 4 URLs for")
    ap.add_argument("--company-tickers", default=None, help="SEC company_tickers.json for the CIK map")
    ap.add_argument("--ciks", default=None, help="A=0000320193,B=... explicit ticker=CIK pairs")
    ap.add_argument("--since", default=None, help="only index rows filed on/after this date")
    ap.add_argument("--fetch", action="store_true",
                    help=f"download the listed URLs into --form4-dir (needs ${SEC_USER_AGENT_ENV}; "
                         f"{SEC_MAX_REQ_PER_S} req/s max; run on the box, never in a sandbox)")
    a = ap.parse_args(argv)

    run_dir = a.run_dir or BASE
    if a.edgar_index:
        text = open(a.edgar_index, encoding="utf-8", errors="replace").read()
        ciks = {}
        if a.ciks:
            for pair in a.ciks.split(","):
                if "=" in pair:
                    s, c = pair.split("=", 1)
                    ciks[s.strip().upper()] = c.strip().zfill(10)
        if a.symbols and a.company_tickers:
            ciks.update(symbols_to_ciks(a.symbols.split(","), _load_json(a.company_tickers) or {}))
        elif a.symbols and not ciks:
            print(f"--symbols needs --company-tickers ({SEC_COMPANY_TICKERS}) or --ciks",
                  file=sys.stderr)
            return 2
        urls = form4_urls(parse_form_idx(text), ciks, since=a.since)
        missing = sorted(set(s.strip().upper() for s in (a.symbols or "").split(",") if s.strip())
                         - set(ciks))
        if missing:
            print(f"no CIK for: {', '.join(missing)}", file=sys.stderr)
        for u in urls:
            print(f"{u['symbol']}\t{u['filed']}\t{u['form']}\t{u['url']}")
        print(f"{len(urls)} Form 4 URL(s) for {len(ciks)} issuer CIK(s)", file=sys.stderr)
        if a.fetch:
            ua = os.environ.get(SEC_USER_AGENT_ENV, "").strip()
            if not ua or "@" not in ua:
                print(f"--fetch refused: set {SEC_USER_AGENT_ENV}=\"Name email@domain\" — SEC's "
                      "fair-access policy requires a contact in the User-Agent", file=sys.stderr)
                return 2
            dest = a.form4_dir or os.path.join(run_dir, FORM4_DIR)
            n, errs = fetch_form4s(urls, dest, ua)
            print(f"fetched {n} file(s) into {dest}", file=sys.stderr)
            for e in errs:
                print("  ! " + e, file=sys.stderr)
            if errs and not n:
                return 1
        if not (a.form4_dir or a.transactions or a.fetch):
            return 0

    as_of = a.as_of or date.today().isoformat()
    if _date(as_of) is None:
        print(f"--as-of must be YYYY-MM-DD, got {as_of!r}", file=sys.stderr)
        return 2
    txns, sources, warns = load_staged(run_dir, form4_dir=a.form4_dir, staged_file=a.transactions)
    doc = build(txns, as_of, a.window_days, a.net_window_days, a.history_since,
                sources=sources, warnings=warns)
    out = a.out or os.path.join(run_dir, SIGNAL_FILE)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    os.replace(tmp, out)
    if a.dump_transactions:
        with open(a.dump_transactions, "w", encoding="utf-8") as fh:
            json.dump({"_meta": {"schema": SCHEMA, "as_of": doc["_meta"]["as_of"],
                                 "sources": sources}, "transactions": txns}, fh, indent=2)
    m = doc["_meta"]
    print(f"{os.path.basename(out)}  <-  {', '.join(sources) if sources else 'NO INPUT'}")
    print(f"{m['n_txns']} transaction(s), {m['n_symbols']} symbol(s), "
          f"{m['n_cluster_buy']} cluster buy(s) as of {m['as_of']}")
    for sym, s in sorted(doc["symbols"].items(), key=lambda kv: -kv[1]["opportunistic_buy_usd_30d"]):
        flag = "CLUSTER" if s["cluster_buy"] else ""
        print(f"  {sym:<7}{s['opportunistic_buyers_30d']:>2} buyer(s) "
              f"${s['opportunistic_buy_usd_30d']:>12,.0f} opp  ${s['routine_buy_usd_30d']:>10,.0f} routine  "
              f"net90 ${s['net_insider_usd_90d']:>12,.0f}  {flag}")
    for w in m["warnings"]:
        print("  ! " + w)
    return 0 if txns else 2


if __name__ == "__main__":
    sys.exit(main())
