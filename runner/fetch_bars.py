"""fetch_bars.py — daily bars for the backtest harness, from Alpaca Market Data v2.

The harness (`engine/backtest.py`, `engine/ic.py`, `engine/validate.py`) and
`engine/technicals.py` all read ONE bars shape — the Robinhood `get_equity_historicals`
response, or a list of them:

    {"data": {"results": [{"symbol": "AAPL",
                           "bars": [{"begins_at": "2023-08-01T04:00:00Z",
                                     "open_price": 196.24, "high_price": 196.73,
                                     "low_price": 195.28, "close_price": 195.61,
                                     "volume": 35281426}, ...]}, ...]}}

This program writes exactly that (one block, plus a top-level `meta` the loaders ignore),
so `--bars bars_all.json` works unchanged on every reader. `docs/BACKTEST.md` section 6b
names the inputs the E10 recipe needs: the universe, SPY, and the eleven SPDR sector ETFs
from 252 sessions before the first replay date.

    python3 runner/fetch_bars.py --universe experiments/universe_sp500.json --since 2024-08-21 \
        --symbols SPY,XLK,XLF,XLV,XLY,XLP,XLE,XLI,XLB,XLU,XLRE,XLC \
        --start 2023-08-01 --end today --keyfile C:\\ai-trading-runner\\alpaca.env \
        --out bars_all.json --missing-out bars_missing.json \
        --sector-map-out sector_map.json --sectors-csv sp500_sectors.csv --resume

Endpoint facts, verified against https://docs.alpaca.markets/reference/stockbars and
https://docs.alpaca.markets/docs/about-market-data-api on 2026-09-10:

- `GET https://data.alpaca.markets/v2/stocks/bars?symbols=A,B,C&timeframe=1Day&start=…&end=…
  &adjustment=all&feed=iex&limit=10000&sort=asc[&page_token=…]`
- `symbols` is "a comma-separated list of stock symbols" — the reference states NO cap on
  the count; the practical bounds are URL length and the page cap below. `--batch` (default
  20) keeps each request well inside both.
- `limit` is the page size and "applies to total data points, not per symbol": max 10,000,
  default 1,000. Twenty symbols × ~780 daily bars is ~15,600 points, so a batch spans two
  pages; the response carries `next_page_token` (null on the last page) which is passed
  back as `page_token`.
- `adjustment`: raw | split | dividend | spin-off | all. `all` here — the harness measures
  returns, so splits and dividends must not appear as price jumps. RECORD WHICH (DATA.md
  section 1 says so); `meta.adjustment` carries it.
- `feed=iex`: the Basic (free) plan's historical feed. `sip` on Basic is limited to data at
  least 15 minutes old and is not needed for daily bars. IEX history starts 2016.
- Rate limit on Basic: 200 requests per minute. `--sleep` (default 0.35 s → ~170/min) keeps
  under it; a 429 is retried with exponential backoff and honours `Retry-After`.
- Auth headers: `APCA-API-KEY-ID` and `APCA-API-SECRET-KEY`.
- Bars: `{"t", "o", "h", "l", "c", "v", "n", "vw"}` under `bars.<SYMBOL>`; daily `t` is the
  session's left edge in RFC-3339 UTC (04:00Z or 05:00Z of the New York day, so the first
  ten characters are the session date). A symbol with no bars in the window is simply
  absent from `bars` — treated here as "not covered", never as "did not trade".

THE KEY FILE. Two lines, `APCA_API_KEY_ID=…` and `APCA_API_SECRET_KEY=…`, at
`C:\\ai-trading-runner\\alpaca.env` — next to the venv, outside BOTH the engine clone and the
state repo, exactly like `engine-config.json` (runner/README.md step 4). This program refuses
a `--keyfile` that resolves inside the engine repo, and never writes the key anywhere.

Stdlib only.
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "engine"))
import universe_history  # noqa: E402

ALPACA_URL = "https://data.alpaca.markets/v2/stocks/bars"
PAGE_LIMIT = 10000          # the documented maximum; "total data points, not per symbol"
RATE_LIMIT_PER_MIN = 200    # Basic plan
DEFAULT_BATCH = 20
DEFAULT_SLEEP = 0.35        # ~170 requests/min, under the 200/min Basic limit
MAX_TRIES = 6
TIMEOUT_S = 60
KEY_ENV = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")
SECTOR_ETFS = ("XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC")

# GICS sector (as the Wikipedia constituents table writes it) → SPDR sector ETF.
GICS_TO_ETF = {
    "information technology": "XLK",
    "financials": "XLF",
    "health care": "XLV",
    "healthcare": "XLV",
    "consumer discretionary": "XLY",
    "consumer staples": "XLP",
    "energy": "XLE",
    "industrials": "XLI",
    "materials": "XLB",
    "utilities": "XLU",
    "real estate": "XLRE",
    "communication services": "XLC",
    "telecommunication services": "XLC",
}


class FetchError(Exception):
    """A request that could not be completed after every retry."""


# ------------------------------------------------------------------ credentials
def load_keys(keyfile=None, env=None):
    """(key_id, secret) from the environment, else from a two-line KEY=value file.

    The file must live outside the engine repo: a key committed by accident is the one
    failure this cannot recover from, so a path under the repo is refused outright.
    """
    env = os.environ if env is None else env
    if keyfile:
        p = Path(keyfile).expanduser().resolve()
        try:
            p.relative_to(REPO)
            inside = True
        except ValueError:
            inside = False
        if inside:
            raise SystemExit(f"REFUSED: --keyfile {p} is inside the engine repo {REPO}. Keep it "
                             "next to the venv (C:\\ai-trading-runner\\alpaca.env), never in a clone.")
        if not p.is_file():
            raise SystemExit(f"REFUSED: keyfile {p} not found")
        found = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            found[k.strip()] = v.strip().strip('"').strip("'")
        key, secret = found.get(KEY_ENV[0]), found.get(KEY_ENV[1])
    else:
        key, secret = env.get(KEY_ENV[0]), env.get(KEY_ENV[1])
    if not key or not secret:
        raise SystemExit(f"REFUSED: no Alpaca credentials — set {KEY_ENV[0]} and {KEY_ENV[1]} "
                         "in the environment or pass --keyfile")
    return key, secret


# ------------------------------------------------------------------ symbols
def universe_symbols(path, since=None):
    """Every ticker whose membership interval overlaps [since, ∞) — the names that were
    members at any point from `since` on, including the ones that have since left."""
    doc = universe_history.load(path)
    out = set()
    for t, ivs in (doc.get("membership") or {}).items():
        for frm, to in ivs:
            if since is None or to is None or to > since:
                out.add(t)
                break
    return doc, sorted(out)


def alt_symbol(sym):
    """The other spelling of a class-share ticker: BRK.B ↔ BRK-B. None if there is none."""
    if "." in sym:
        return sym.replace(".", "-")
    if "-" in sym:
        return sym.replace("-", ".")
    return None


# ------------------------------------------------------------------ the request
def _request(url, headers, log=print):
    """GET with retries on 429 and 5xx. Returns the parsed JSON body."""
    delay = 1.0
    for attempt in range(1, MAX_TRIES + 1):
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                body = resp.read()
            return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = exc.code
            retryable = code == 429 or 500 <= code < 600
            if not retryable or attempt == MAX_TRIES:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                raise FetchError(f"HTTP {code} after {attempt} attempt(s): {detail}") from None
            retry_after = None
            try:
                retry_after = float((exc.headers or {}).get("Retry-After") or 0) or None
            except (TypeError, ValueError):
                retry_after = None
            wait = retry_after or delay
            log(f"  HTTP {code}; retry {attempt}/{MAX_TRIES - 1} in {wait:.0f}s")
            time.sleep(wait)
            delay = min(delay * 2, 60)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == MAX_TRIES:
                raise FetchError(f"network: {exc} after {attempt} attempts") from None
            log(f"  {exc}; retry {attempt}/{MAX_TRIES - 1} in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    raise FetchError("unreachable")


def fetch_batch(symbols, start, end, key, secret, feed="iex", adjustment="all",
                sleep_s=DEFAULT_SLEEP, log=print, base_url=ALPACA_URL):
    """{SYMBOL: [alpaca bar, ...]} for one batch, following next_page_token to the end."""
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret, "Accept": "application/json"}
    bars = {}
    token = None
    pages = 0
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                  "adjustment": adjustment, "feed": feed, "limit": PAGE_LIMIT, "sort": "asc"}
        if token:
            params["page_token"] = token
        url = base_url + "?" + urllib.parse.urlencode(params)
        doc = _request(url, headers, log=log)
        pages += 1
        for sym, rows in (doc.get("bars") or {}).items():
            bars.setdefault(sym.upper(), []).extend(rows or [])
        token = doc.get("next_page_token")
        if not token:
            break
        time.sleep(sleep_s)
    time.sleep(sleep_s)
    return bars, pages


def to_rh_bars(rows):
    """Alpaca {t,o,h,l,c,v} → the Robinhood bar shape the engine reads. Oldest first,
    one bar per session (the last page wins on a duplicate timestamp)."""
    by_t = {}
    for r in rows or []:
        t = r.get("t")
        if not t:
            continue
        by_t[t] = {"begins_at": t, "open_price": r.get("o"), "high_price": r.get("h"),
                   "low_price": r.get("l"), "close_price": r.get("c"), "volume": r.get("v")}
    return [by_t[t] for t in sorted(by_t)]


# ------------------------------------------------------------------ files
def load_existing(path):
    """{SYMBOL: bars} from an existing output (any accepted shape), for --resume / --source file."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.load(open(p, encoding="utf-8"))
    blocks = raw if isinstance(raw, list) and raw and isinstance(raw[0], dict) \
        and "symbol" not in raw[0] else [raw]
    out = {}
    for blk in blocks:
        rows = blk
        if isinstance(blk, dict):
            rows = (blk.get("data") or {}).get("results") or blk.get("results") or []
        for res in rows or []:
            if not isinstance(res, dict) or not res.get("symbol"):
                continue
            b = [x for x in (res.get("bars") or []) if isinstance(x, dict)]
            if b:
                out.setdefault(res["symbol"].upper(), []).extend(b)
    for sym in out:
        out[sym].sort(key=lambda b: b.get("begins_at") or "")
    return out


def write_bars(path, bars, meta):
    """Atomic write of the one-block shape every loader accepts."""
    results = [{"symbol": sym, "bars": bars[sym]} for sym in sorted(bars)]
    doc = {"meta": meta, "data": {"results": results}}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, p)


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------------ sector map
def _norm_header(h):
    return (h or "").strip().lower().replace("_", " ")


def sectors_from_csv(path):
    """{TICKER: gics sector} from a CSV with a Symbol/Ticker column and a GICS Sector/Sector
    column — the Wikipedia constituents table saved as CSV, or any equivalent."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        heads = {_norm_header(h): h for h in (reader.fieldnames or [])}
        sym_col = next((heads[k] for k in ("symbol", "ticker") if k in heads), None)
        sec_col = next((heads[k] for k in ("gics sector", "sector") if k in heads), None)
        if not sym_col or not sec_col:
            raise SystemExit(f"REFUSED: {path} needs a Symbol/Ticker column and a GICS Sector/"
                             f"Sector column; found {reader.fieldnames}")
        for row in reader:
            s = (row.get(sym_col) or "").strip().upper()
            if s:
                out[s] = (row.get(sec_col) or "").strip()
    return out


def build_sector_map(symbols, sectors):
    """{SYMBOL: ETF} for every symbol whose sector is known and maps to a SPDR ETF; the
    ETFs themselves and SPY are left out. Returns (map, unmapped)."""
    out, unmapped = {}, []
    for s in symbols:
        if s == "SPY" or s in SECTOR_ETFS:
            continue
        sec = sectors.get(s) or sectors.get(alt_symbol(s) or "")
        etf = GICS_TO_ETF.get((sec or "").strip().lower())
        if etf:
            out[s] = etf
        else:
            unmapped.append(s)
    return out, unmapped


# ------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--universe", help="a universe_history.py JSON; every name that was a member "
                                       "at any point from --since on is fetched")
    ap.add_argument("--since", help="ISO date: membership intervals ending before this are skipped "
                                    "(default: every name in the file)")
    ap.add_argument("--symbols", default="", help="extra symbols, comma-separated (SPY and the "
                                                  "sector ETFs for the E10 recipe)")
    ap.add_argument("--start", required=True, help="first bar date, YYYY-MM-DD")
    ap.add_argument("--end", default="today", help="last bar date, YYYY-MM-DD, or 'today'")
    ap.add_argument("--out", required=True, help="bars_all.json — the shape backtest.py/technicals.py read")
    ap.add_argument("--missing-out", default=None,
                    help="where to write the symbols that returned no bars (default: "
                         "bars_missing.json next to --out)")
    ap.add_argument("--resume", action="store_true", help="skip symbols already in --out")
    ap.add_argument("--source", choices=["alpaca", "file"], default="alpaca",
                    help="alpaca: fetch. file: re-emit --file (any accepted bars shape) in the "
                         "output shape — for conversions and offline runs")
    ap.add_argument("--file", help="with --source file: the bars file to read")
    ap.add_argument("--keyfile", help="two-line APCA_API_KEY_ID=… / APCA_API_SECRET_KEY=… file, "
                                      "OUTSIDE the repo (C:\\ai-trading-runner\\alpaca.env)")
    ap.add_argument("--feed", default="iex", choices=["iex", "sip"])
    ap.add_argument("--adjustment", default="all", choices=["raw", "split", "dividend", "all"])
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="symbols per request")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="seconds between requests")
    ap.add_argument("--checkpoint-every", type=int, default=5,
                    help="rewrite --out after this many batches (so a killed run resumes)")
    ap.add_argument("--sector-map-out", help="write {SYMBOL: ETF} here (GICS sector → SPDR ETF)")
    ap.add_argument("--sectors-csv", help="CSV with Symbol and GICS Sector columns, used when the "
                                          "universe file carries no sectors")
    ap.add_argument("--base-url", default=ALPACA_URL, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    log = lambda s: print(s, file=sys.stderr, flush=True)  # noqa: E731
    end = dt.date.today().isoformat() if a.end == "today" else a.end
    start = a.start

    # ---- the symbol list
    wanted, doc, sectors = [], None, {}
    if a.universe:
        doc, uni = universe_symbols(a.universe, a.since)
        wanted += uni
        if isinstance(doc.get("sectors"), dict):
            sectors.update({str(k).upper(): str(v) for k, v in doc["sectors"].items()})
    for s in a.symbols.split(","):
        s = s.strip().upper()
        if s and s not in wanted:
            wanted.append(s)
    if not wanted and a.source == "alpaca":
        raise SystemExit("REFUSED: nothing to fetch — pass --universe and/or --symbols")
    if a.sectors_csv:
        sectors.update(sectors_from_csv(a.sectors_csv))

    # ---- what we already have
    have = load_existing(a.out) if a.resume else {}
    if a.source == "file":
        if not a.file:
            raise SystemExit("REFUSED: --source file needs --file")
        have.update(load_existing(a.file))
        if not wanted:
            wanted = sorted(have)
    todo = [s for s in wanted if s not in have and (alt_symbol(s) or s) not in have]
    log(f"{len(wanted)} symbols wanted, {len(wanted) - len(todo)} already present, "
        f"{len(todo)} to fetch ({start} → {end}, feed {a.feed}, adjustment {a.adjustment})")

    meta = {"source": a.source, "fetched_at": dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "feed": a.feed if a.source == "alpaca" else None,
            "adjustment": a.adjustment if a.source == "alpaca" else None,
            "timeframe": "1Day", "start": start, "end": end,
            "universe": (a.universe and {"file": str(a.universe), "since": a.since,
                                         "name": (doc or {}).get("name")}) or None,
            "bar_shape": "robinhood get_equity_historicals: data.results[].bars[]"
                         "{begins_at, open_price, high_price, low_price, close_price, volume}"}

    missing = []
    requests_made = 0
    if a.source == "alpaca" and todo:
        key, secret = load_keys(a.keyfile)
        batches = [todo[i:i + a.batch] for i in range(0, len(todo), a.batch)]
        for i, batch in enumerate(batches, 1):
            try:
                got, pages = fetch_batch(batch, start, end, key, secret, feed=a.feed,
                                         adjustment=a.adjustment, sleep_s=a.sleep, log=log,
                                         base_url=a.base_url)
            except FetchError as exc:
                log(f"batch {i}/{len(batches)} FAILED: {exc} — writing what we have; re-run with --resume")
                write_bars(a.out, have, dict(meta, partial=True, error=str(exc)))
                return 2
            requests_made += pages
            for sym in batch:
                rows = got.get(sym)
                if rows:
                    have[sym] = to_rh_bars(rows)
                else:
                    missing.append(sym)
            log(f"batch {i}/{len(batches)}: {len(batch)} symbols, {pages} page(s), "
                f"{sum(1 for s in batch if got.get(s))} with bars")
            if i % max(a.checkpoint_every, 1) == 0:
                write_bars(a.out, have, dict(meta, partial=True))
        # Second pass: the other spelling of class-share tickers (BRK.B ↔ BRK-B). The output
        # stays keyed by the universe file's spelling; the backtest matches either.
        retry = [(s, alt_symbol(s)) for s in missing if alt_symbol(s)]
        if retry:
            try:
                got, pages = fetch_batch([alt for _, alt in retry], start, end, key, secret,
                                         feed=a.feed, adjustment=a.adjustment, sleep_s=a.sleep,
                                         log=log, base_url=a.base_url)
                requests_made += pages
                for s, alt in retry:
                    if got.get(alt):
                        have[s] = to_rh_bars(got[alt])
                        missing.remove(s)
                        log(f"  {s}: served as {alt}")
            except FetchError as exc:
                log(f"alternate-spelling pass failed: {exc}")

    meta["n_symbols"] = len(have)
    meta["n_missing"] = len(missing)
    meta["requests"] = requests_made
    write_bars(a.out, have, meta)
    log(f"wrote {a.out}: {len(have)} symbols with bars")

    # ---- the residual survivorship statement
    missing_out = a.missing_out or str(Path(a.out).with_name("bars_missing.json"))
    if a.source == "alpaca" and (todo or missing):
        gone = set()
        if doc:
            gone = set(universe_history.ever_members(doc.get("membership")) -
                       universe_history.current_members(doc.get("membership")))
        write_json(missing_out, {
            "_what": "Symbols requested from the bars source that returned NO bars in the window. "
                     "Treat each as NOT COVERED, never as 'did not trade'. A member with no bars is "
                     "silently absent from every replay — this list is the residual survivorship "
                     "bias the membership file cannot remove (docs/DATA.md section 3).",
            "source": a.source, "feed": a.feed, "start": start, "end": end,
            "fetched_at": meta["fetched_at"],
            "n_requested": len(todo), "n_missing": len(missing),
            "missing": sorted(missing),
            "missing_former_members": sorted(s for s in missing if s in gone),
            "missing_current_members": sorted(s for s in missing if s not in gone),
        })
        log(f"{len(missing)} symbol(s) returned no bars → {missing_out}")

    # ---- the sector map
    if a.sector_map_out:
        smap, unmapped = build_sector_map(wanted, sectors)
        write_json(a.sector_map_out, smap)
        log(f"sector map: {len(smap)} symbols mapped, {len(unmapped)} without a known GICS "
            f"sector → {a.sector_map_out}"
            + ("" if sectors else " (no sectors known: pass --sectors-csv or a universe file "
                                  "that carries `sectors`)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
