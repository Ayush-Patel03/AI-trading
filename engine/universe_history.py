"""universe_history.py — a free, survivorship-aware index membership history for the backtest.

WHY
---
`backtest.py` scores whatever symbols are in the bars file, and a bars file is a list of names
that exist TODAY. `universe.py` can take point-in-time membership snapshots, but nothing
produced them. This module produces them, for free, from the one public source that keeps a
dated record of index additions and removals: Wikipedia's "List of S&P 500 companies" (and the
S&P 400 page), which carries a current-constituents table and a "Selected changes" table with
an effective date, the ticker added and the ticker removed.

Walked backwards from today's list, the changes table gives an interval of membership for
every ticker that was in the index at any point the table covers — including names that were
later removed, acquired, or delisted. A replay on date D can then score only the names that
were members on D, and the names a current-constituent list would have silently dropped are
back in for the dates they were there.

WHAT IT DOES NOT FIX — read this before quoting a number
--------------------------------------------------------
* The Wikipedia table is titled "SELECTED changes" and it means it. It is close to complete
  since about 2019 and thin before that. `bias_statement` reports what could and could not be
  parsed; docs/DATA.md says what to state in every write-up.
* Membership is not price history. A removed name is only scored if the bars file has its
  bars, and a bars source that no longer serves delisted symbols reintroduces exactly the bias
  this removes. Fetch bars for `--members-on <start>` plus every ticker in the file, from a
  source that still serves them (docs/DATA.md, "Universe and survivorship").
* Ticker renames (FB -> META, FISV -> FI) appear in the table as a removal and an addition on
  the same date, or not at all. The membership is correct either way; the bars file has to be
  keyed the way the bars source keys it.

CONVENTIONS
-----------
* Dates are ISO strings. An interval is `[from_date, to_date]` where `to_date` is EXCLUSIVE:
  the effective date of a change is the first session the added name is in the index and the
  first session the removed name is out of it. `to_date = None` means "still a member" and
  `from_date = None` means "since before the history starts".
* Tickers are kept as Wikipedia writes them (`BRK.B`, `BF.B`). `normalize_ticker` maps the
  share-class dot to a hyphen (`BRK-B`), which is how Robinhood's and Alpaca's bars are keyed.
  `members_on` returns the raw form; the backtest matches on both.
* stdlib only (urllib + html.parser). One request per page, with a User-Agent that names the
  project, which is what Wikipedia's robot policy asks for.

Usage
-----
    python3 universe_history.py --index sp500 --out universe_sp500.json [--since 2019-01-01]
    python3 universe_history.py --in universe_sp500.json --members-on 2025-03-14
    python3 universe_history.py --index sp500 --html saved_page.html --out ...   # offline parse
    python3 universe_history.py --changes-csv changes.csv --current-csv sp500.csv --out ...

Network failure -> a clear message on stderr, exit 2, and no file: `save` writes to a
temporary path and renames, so a partial file is never left behind.
"""
import argparse
import csv
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser

PROJECT_URL = "https://github.com/Ayush-Patel03/AI-trading"
USER_AGENT = f"ai-trading-engine/universe_history (+{PROJECT_URL}; stdlib urllib)"
PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
}
TIMEOUT_S = 30
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}([.\-][A-Z0-9]{1,2})?$")
DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y")


class FetchError(Exception):
    """The page could not be fetched. The CLI turns this into exit 2 and no file."""


# ---------------------------------------------------------------- fetch
def fetch_html(url, opener=None):
    """One polite GET. Raises FetchError on any transport or HTTP failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "text/html"})
    try:
        with (opener or urllib.request).urlopen(req, timeout=TIMEOUT_S) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise FetchError(f"HTTP {status} from {url}")
            raw = resp.read()
            charset = "utf-8"
            try:
                charset = resp.headers.get_content_charset() or "utf-8"
            except AttributeError:
                pass
            return raw.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"could not fetch {url}: {e}") from e


# ---------------------------------------------------------------- html -> tables
class _TableParser(HTMLParser):
    """Every <table> on the page as a list of rows of cells, text only.

    Footnote superscripts (`<sup class="reference">`) and anything inside <style>/<script>
    are dropped; a <br> becomes a space so two tickers stacked in one cell stay separable.
    Cells keep their rowspan/colspan so `expand_grid` can lay them out the way a browser
    would — the changes table spans a shared date and reason across several rows."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []          # [{"attrs": {...}, "rows": [[cell, ...], ...]}]
        self._stack = []          # nested tables, if any
        self._row = None
        self._cell = None
        self._skip = 0            # depth inside sup.reference / style / script

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self._skip:
            if tag in ("sup", "style", "script"):
                self._skip += 1
            return
        if tag in ("style", "script") or (tag == "sup" and "reference" in (a.get("class") or "")):
            self._skip += 1
            return
        if tag == "table":
            self._stack.append({"attrs": a, "rows": []})
        elif tag == "tr" and self._stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "header": tag == "th",
                          "rowspan": _int(a.get("rowspan"), 1),
                          "colspan": _int(a.get("colspan"), 1)}
        elif tag == "br" and self._cell is not None:
            self._cell["text"].append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and self._cell is not None and not self._skip:
            self._cell["text"].append(" ")

    def handle_endtag(self, tag):
        if self._skip:
            if tag in ("sup", "style", "script"):
                self._skip -= 1
            return
        if tag in ("td", "th") and self._cell is not None:
            self._cell["text"] = _clean(" ".join(self._cell["text"]))
            if self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:          # unclosed cell
                self.handle_endtag("td")
            if self._stack and self._row:
                self._stack[-1]["rows"].append(self._row)
            self._row = None
        elif tag == "table" and self._stack:
            if self._row is not None:
                self.handle_endtag("tr")
            self.tables.append(self._stack.pop())

    def handle_data(self, data):
        if self._skip or self._cell is None:
            return
        self._cell["text"].append(data)


def _int(v, default):
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return default


def _clean(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"\[\s*\d+\s*\]", " ", s)          # a footnote that survived as text
    s = re.sub(r"\[\s*(note|citation needed|[a-z])\s*\d*\s*\]", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def parse_tables(html):
    p = _TableParser()
    p.feed(html)
    p.close()
    return p.tables


def expand_grid(rows):
    """Lay cells out on a grid honouring rowspan and colspan, the way a browser renders
    them. Returns rows of {"text", "header"} with every column filled."""
    grid = []
    pending = {}                                  # col -> (cell, rows_left)
    for raw in rows:
        out, col, i = [], 0, 0
        while i < len(raw) or any(c >= col for c in pending):
            if col in pending:
                cell, left = pending[col]
                out.append({"text": cell["text"], "header": cell["header"]})
                if left - 1 > 0:
                    pending[col] = (cell, left - 1)
                else:
                    del pending[col]
                col += 1
                continue
            if i >= len(raw):
                if any(c > col for c in pending):
                    out.append({"text": "", "header": False})
                    col += 1
                    continue
                break
            cell = raw[i]
            i += 1
            for k in range(cell["colspan"]):
                out.append({"text": cell["text"], "header": cell["header"]})
                if cell["rowspan"] > 1:
                    pending[col + k] = (cell, cell["rowspan"] - 1)
            col += cell["colspan"]
        grid.append(out)
    return grid


# ---------------------------------------------------------------- the two tables
def parse_date(text):
    """'June 30, 2026' -> '2026-06-30'; None when it is not a date."""
    t = _clean(text or "")
    t = re.sub(r"^(effective|on)\s+", "", t, flags=re.I)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def parse_tickers(text):
    """Every ticker in a cell. Tolerates 'A, B', 'A B' and footnote residue; anything that
    does not look like a ticker is dropped (the caller counts the row as unparseable when
    nothing survives and the security cell was not empty)."""
    out = []
    for tok in re.split(r"[,\s/;]+", (text or "").strip()):
        tok = tok.strip().strip("()").upper()
        if tok and TICKER_RE.match(tok) and tok not in out:
            out.append(tok)
    return out


def normalize_ticker(t):
    """Wikipedia writes share classes with a dot (BRK.B, BF.B); Robinhood and Alpaca key
    their bars with a hyphen (BRK-B). Only the class separator changes, nothing else."""
    return (t or "").strip().upper().replace(".", "-")


def _find_table(tables, want_id, header_words):
    for t in tables:
        if (t["attrs"].get("id") or "") == want_id:
            return t
    for t in tables:
        head = " ".join(c["text"].lower() for r in t["rows"][:2] for c in r if c["header"])
        if all(w in head for w in header_words):
            return t
    return None


def parse_constituents(table):
    grid = expand_grid(table["rows"])
    hdr_i, sym_col = None, None
    for i, row in enumerate(grid[:3]):
        for j, c in enumerate(row):
            if c["header"] and c["text"].lower() in ("symbol", "ticker", "ticker symbol"):
                hdr_i, sym_col = i, j
                break
        if hdr_i is not None:
            break
    if hdr_i is None:
        raise ValueError("no Symbol/Ticker column in the constituents table")
    out = []
    for row in grid[hdr_i + 1:]:
        if sym_col < len(row) and not row[sym_col]["header"]:
            for tk in parse_tickers(row[sym_col]["text"])[:1]:
                if tk not in out:
                    out.append(tk)
    return out


def _changes_columns(grid):
    """(date, added_ticker, removed_ticker, reason) column indexes from the two header
    rows: 'Effective Date | Added (Ticker, Security) | Removed (Ticker, Security) | Reason'.
    Verified against both pages on 2026-09-10; falls back to the positional layout
    0/1/3/5 when the headers cannot be read, and says so."""
    hdr = [r for r in grid[:3] if any(c["header"] for c in r)]
    if len(hdr) >= 2:
        top, sub = hdr[0], hdr[1]
        n = min(len(top), len(sub))
        cols = {"date": None, "added": None, "removed": None, "reason": None}
        for j in range(n):
            a, b = top[j]["text"].lower(), sub[j]["text"].lower()
            if "date" in a and cols["date"] is None:
                cols["date"] = j
            elif a.startswith("added") and b.startswith("ticker") and cols["added"] is None:
                cols["added"] = j
            elif a.startswith("removed") and b.startswith("ticker") and cols["removed"] is None:
                cols["removed"] = j
            elif "reason" in a and cols["reason"] is None:
                cols["reason"] = j
        if None not in (cols["date"], cols["added"], cols["removed"]):
            return cols, len(hdr), None
    return ({"date": 0, "added": 1, "removed": 3, "reason": 5}, max(1, len(hdr)),
            "changes table headers not recognised; used the positional layout 0/1/3/5")


def parse_changes(table):
    """[{date, added: [...], removed: [...], reason, raw_date}] plus a count of rows that
    could not be used. A row is unparseable when its date does not parse or when neither
    ticker cell yields a ticker although one of the security cells names a company."""
    grid = expand_grid(table["rows"])
    cols, n_hdr, note = _changes_columns(grid)
    notes = [note] if note else []
    out, bad = [], 0
    for row in grid[n_hdr:]:
        if all(c["header"] for c in row):
            continue
        txt = [c["text"] for c in row]
        get = lambda k: txt[cols[k]] if cols[k] is not None and cols[k] < len(txt) else ""
        d = parse_date(get("date"))
        added, removed = parse_tickers(get("added")), parse_tickers(get("removed"))
        sec_named = any(txt[j].strip() for j in (cols["added"] + 1, cols["removed"] + 1)
                        if j < len(txt))
        if d is None or (not added and not removed):
            if d is None or sec_named:
                bad += 1
            continue
        out.append({"date": d, "added": added, "removed": removed,
                    "reason": get("reason"), "raw_date": get("date")})
    return out, bad, notes


def parse_page(html, index="sp500"):
    """{"current": [...], "changes": [...], "unparseable_rows": n, "notes": [...]}"""
    tables = parse_tables(html)
    cons = _find_table(tables, "constituents", ("symbol", "security"))
    chg = _find_table(tables, "changes", ("added", "removed"))
    if cons is None or chg is None:
        raise ValueError(f"the {index} page did not contain both the constituents and the "
                         f"changes table ({len(tables)} tables found)")
    current = parse_constituents(cons)
    changes, bad, notes = parse_changes(chg)
    return {"current": current, "changes": changes, "unparseable_rows": bad, "notes": notes}


def fetch_wikipedia_tables(index="sp500", html=None, opener=None):
    """{"current": [tickers], "changes": [{date, added, removed, reason}], ...}

    `html` short-circuits the network (a saved page); otherwise ONE request is made."""
    if index not in PAGES:
        raise ValueError(f"unknown index {index!r}; one of {sorted(PAGES)}")
    url = PAGES[index]
    if html is None:
        html = fetch_html(url, opener)
    doc = parse_page(html, index)
    doc.update({"index": index, "source_url": url})
    return doc


# ---------------------------------------------------------------- csv (offline mirrors)
def load_changes_csv(path):
    """A `date,add,remove` CSV (one row per date, tickers comma-separated inside the cell),
    the shape of the fja05680/sp500 mirror of the same Wikipedia table."""
    out, bad = [], 0
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        for r in rd:
            d = parse_date(r.get("date") or "")
            added = parse_tickers(r.get("add") or r.get("added") or "")
            removed = parse_tickers(r.get("remove") or r.get("removed") or "")
            if d is None or (not added and not removed):
                bad += 1
                continue
            out.append({"date": d, "added": added, "removed": removed,
                        "reason": r.get("reason") or "", "raw_date": r.get("date")})
    return out, bad


def load_current_csv(path):
    with open(path, encoding="utf-8", newline="") as f:
        rd = csv.DictReader(f)
        col = next((c for c in (rd.fieldnames or []) if c.lower() in ("symbol", "ticker")), None)
        if not col:
            raise ValueError(f"{path}: no Symbol/Ticker column")
        out = []
        for r in rd:
            for tk in parse_tickers(r.get(col) or "")[:1]:
                if tk not in out:
                    out.append(tk)
    return out


# ---------------------------------------------------------------- membership
def build_membership(current, changes, start_date=None, notes=None):
    """{ticker: [[from_date, to_date], ...]} walked backwards from today's list.

    A ticker removed on d was a member up to d (exclusive); a ticker added on d has been a
    member from d. Every ticker in `current` is open-ended. Two inconsistencies the
    "selected" table produces are recorded in `notes` rather than silently resolved:
      * added on d, never removed, not in the current list -> a rename or delisting the table
        does not carry. Kept as a member from d onward, which is the FLATTERING direction,
        so it is counted and reported.
      * removed on d while apparently a member later with no add row in between -> the
        re-add is missing; the later interval is closed at d going backwards and the earlier
        one opened, which is the only reading that loses no dated fact.
    `start_date` clamps every interval to the window and drops the ones that ended before
    it; `from_date = None` means "since before the history starts"."""
    notes = notes if notes is not None else []
    intervals = {}
    open_end = {t: None for t in current}      # ticker -> to_date of the interval being walked
    ordered = sorted((c for c in changes if c.get("date")),
                     key=lambda c: c["date"], reverse=True)
    for c in ordered:
        d = c["date"]
        for t in c.get("added") or []:
            if t in open_end:
                intervals.setdefault(t, []).append([d, open_end.pop(t)])
            else:
                notes.append(f"{t}: added {d} but neither removed later nor in the current "
                             "list (rename or delisting the table does not carry); kept as "
                             "a member from that date on")
                intervals.setdefault(t, []).append([d, None])
        for t in c.get("removed") or []:
            if t in open_end:
                notes.append(f"{t}: removed {d} while a member later with no add row in "
                             "between; the later interval is closed at that date")
                intervals.setdefault(t, []).append([d, open_end.pop(t)])
            open_end[t] = d
    for t, end in open_end.items():
        intervals.setdefault(t, []).append([None, end])

    out = {}
    for t, ivs in intervals.items():
        keep = []
        for frm, to in ivs:
            if start_date:
                if to is not None and to <= start_date:
                    continue
                if frm is None or frm < start_date:
                    frm = start_date
            keep.append([frm, to])
        if keep:
            keep.sort(key=lambda iv: iv[0] or "")
            out[t] = keep
    return out


def members_on(membership, when):
    """The set of tickers that were members on `when` (ISO date). Intervals are
    [from, to) — `to` is the first day OUT."""
    if hasattr(when, "isoformat"):
        when = when.isoformat()
    out = set()
    for t, ivs in (membership or {}).items():
        for frm, to in ivs:
            if (frm is None or frm <= when) and (to is None or when < to):
                out.add(t)
                break
    return out


def ever_members(membership):
    return set(membership or {})


def current_members(membership):
    return {t for t, ivs in (membership or {}).items() if any(to is None for _, to in ivs)}


def bias_statement(membership, start, end, unparseable=0, index="sp500", notes=None):
    """One paragraph every write-up that used this file must print. It names the count a
    current-constituent backtest would have silently dropped, and what still cannot be
    fixed from here."""
    on_start, on_end = members_on(membership, start), members_on(membership, end)
    ever = {t for t, ivs in (membership or {}).items()
            if any((frm is None or frm <= end) and (to is None or start < to) for frm, to in ivs)}
    cur = current_members(membership)
    gone = sorted(ever - cur)
    n_incons = len([n for n in (notes or []) if ": added" in n or ": removed" in n])
    return (
        f"UNIVERSE {index}-history {start}..{end}: {len(on_start)} members on {start}, "
        f"{len(on_end)} on {end}, {len(ever)} distinct names over the window. "
        f"{len(gone)} of those are NOT in the current constituent list"
        + (f" ({', '.join(gone[:12])}{', ...' if len(gone) > 12 else ''})" if gone else "")
        + " — a backtest on today's list would have dropped them, and removals skew toward "
        "the losers (acquired at a discount, fallen below the size floor, or delisted). "
        f"{unparseable} change rows carried no parseable ticker or date and were skipped"
        + (f"; {n_incons} rows were inconsistent with the current list (see notes)" if n_incons else "")
        + ". RESIDUAL BIAS: the source table is 'selected' changes, not all of them; price "
        "bars for removed names are only present if the bars source still serves delisted "
        "symbols — a name with membership but no bars is silently absent, exactly the old "
        "bias in miniature; and names renamed without a row keep their old ticker. State "
        "this in every write-up; do not delete it because the report reads better without it."
    )


# ---------------------------------------------------------------- files
def save(path, obj):
    """Write JSON atomically: a partial file is never left behind."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".universe_history-", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
        umask = os.umask(0)                 # mkstemp is 0600; a repo file should not be
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("membership"), dict):
        raise SystemExit(f"REFUSED: {path} is not a universe-history file (no `membership`). "
                         "Produce one with universe_history.py --index sp500 --out ...")
    return doc


def build_document(index, current, changes, unparseable=0, since=None, source=None,
                   notes=None):
    notes = list(notes or [])
    membership = build_membership(current, changes, since, notes)
    today = date.today().isoformat()
    dates = sorted(c["date"] for c in changes if c.get("date"))
    start = since or (dates[0] if dates else today)
    return {
        "_what": ("Point-in-time index membership for the backtest, walked backwards from "
                  "the current constituent list through the dated changes table. Intervals "
                  "are [from, to) with `to` exclusive; null = open / before history. "
                  "See engine/universe_history.py and docs/DATA.md."),
        "name": f"{index}-history",
        "index": index,
        "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                      .replace("+00:00", "Z"),
        "source": source or {"kind": "wikipedia", "url": PAGES.get(index)},
        "since": since,
        "changes_span": {"oldest": dates[0] if dates else None,
                         "newest": dates[-1] if dates else None, "rows": len(changes)},
        "n_current": len(current),
        "n_ever": len(membership),
        "unparseable_rows": unparseable,
        "notes": notes,
        "bias": bias_statement(membership, start, today, unparseable, index, notes),
        "current": list(current),
        "changes": changes,
        "membership": membership,
    }


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--index", choices=sorted(PAGES), help="which Wikipedia page to fetch")
    ap.add_argument("--html", help="parse this saved page instead of fetching (offline)")
    ap.add_argument("--changes-csv", help="offline: a date,add,remove CSV instead of the page")
    ap.add_argument("--current-csv", help="offline: a CSV with a Symbol column (with --changes-csv)")
    ap.add_argument("--in", dest="inp", help="an existing universe-history JSON (for --members-on)")
    ap.add_argument("--out", help="where to write the JSON")
    ap.add_argument("--since", help="clamp membership to this ISO date onward")
    ap.add_argument("--members-on", dest="members_on", metavar="DATE",
                    help="print the member set on this date")
    ap.add_argument("--source-note", dest="source_note",
                    help="free text recorded under source.note (why this route, what it lacks)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.inp:
        doc = load(a.inp)
    elif a.changes_csv:
        if not a.current_csv:
            ap.error("--changes-csv needs --current-csv (the current constituent list)")
        changes, bad = load_changes_csv(a.changes_csv)
        current = load_current_csv(a.current_csv)
        index = a.index or "sp500"
        doc = build_document(index, current, changes, bad, a.since,
                             source={"kind": "csv", "changes": os.path.basename(a.changes_csv),
                                     "current": os.path.basename(a.current_csv)})
    elif a.index:
        html = None
        if a.html:
            with open(a.html, encoding="utf-8") as f:
                html = f.read()
        try:
            t = fetch_wikipedia_tables(a.index, html=html)
        except FetchError as e:
            print(f"FETCH FAILED: {e}\n  Nothing was written. Wikipedia must be reachable "
                  "from this host (egress allowlist: en.wikipedia.org), or pass --html with "
                  "a saved copy of the page.", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"PARSE FAILED: {e}\n  Nothing was written.", file=sys.stderr)
            return 2
        src = {"kind": "wikipedia", "url": t["source_url"]}
        if a.html:
            src = {"kind": "wikipedia-saved-html", "url": t["source_url"],
                   "file": os.path.basename(a.html)}
        doc = build_document(a.index, t["current"], t["changes"], t["unparseable_rows"],
                             a.since, source=src, notes=t.get("notes"))
    else:
        ap.error("pass --index (fetch), --html, --changes-csv/--current-csv, or --in FILE")

    if a.source_note and not a.inp:
        doc["source"]["note"] = a.source_note
    if a.out:
        save(a.out, doc)
    if a.members_on:
        mem = sorted(members_on(doc["membership"], a.members_on))
        print(f"MEMBERS {doc['index']} on {a.members_on}: {len(mem)}")
        print(" ".join(mem))
    if not a.quiet:
        span = doc["changes_span"]
        print(f"UNIVERSE-HISTORY {doc['index']}  |  {doc['n_current']} current, "
              f"{doc['n_ever']} ever, {span['rows']} change rows "
              f"{span['oldest']} -> {span['newest']}, {doc['unparseable_rows']} unparseable"
              + (f"  ->  {a.out}" if a.out else ""))
        print(f"  {doc['bias']}")
        for n in doc["notes"][:10]:
            print(f"  note: {n}")
        if len(doc["notes"]) > 10:
            print(f"  ... {len(doc['notes']) - 10} more notes in the file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
