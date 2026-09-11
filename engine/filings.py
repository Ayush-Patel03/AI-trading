"""10-K / 10-Q text-change signal — "Lazy Prices" (P-02, experiment E16).

Cohen, Malloy & Nguyen (2020, Journal of Finance, "Lazy Prices"): firms whose periodic
filings change little against the prior year's filing of the same form ("non-changers")
outperform the firms whose filings changed a lot ("changers") — up to 188 bp/month of
five-factor alpha on the Risk Factors section alone, 30–60 bp on the broader measures,
drifting over roughly three months with a monthly rebalance. The informative changes are
the ones in Risk Factors, MD&A, litigation and the CEO/CFO language; boilerplate that moved
because the fiscal year did is noise. The market is slow to read the document, which is the
whole point: the signal is a SLOW NEGATIVE SCREEN, not an entry trigger.

What this module does — stdlib only (html.parser, re, math, difflib), no network:

  extract_sections(text_or_html, form)   the four sections, by locating the Item headings
                                          (10-K: 1A / 7 / 3 / 1; 10-Q: Part II 1A / Part I 2 /
                                          Part II 1; no Business section in a 10-Q), tolerant
                                          of HTML, tables, page headers and case. A section
                                          that cannot be located is None — never "".
  similarity(a, b)                        cosine on TF word vectors (lower-cased, stop-words
                                          and bare numbers stripped), Jaccard on the word
                                          sets, and minimum_edit_ratio = difflib
                                          SequenceMatcher.ratio() over the SENTENCE lists.
                                          The last one is O(n²)-ish in the number of
                                          sentences in the worst case; it is applied to
                                          sections, never to a whole filing.
  change_score(current, prior)            per-section similarities, risk_factors_change =
                                          1 − cosine(Risk Factors), overall_change = mean of
                                          (1 − cosine) over the sections present on BOTH
                                          sides, `changer` = overall_change >= threshold,
                                          n_sections_compared. A section missing on either
                                          side is null in the output, not 0 — "we could not
                                          compare" and "nothing changed" must never look alike.
  load_staged(run_dir)                    filings_signal.json, staged by the scheduled task:
                                          {SYMBOL: {filed, form, prior_filed, accession,
                                          prior_accession, change_score: {...}}}. None when
                                          the file is not there.
  features_for(row, today)                the three scan-row features scanner.py attaches:
                                          filing_change_score, filing_changer,
                                          filing_days_since — null when the row is missing,
                                          the score is null, or the filing post-dates the scan
                                          (point-in-time guard: a filing is knowable from its
                                          filing date, not before).

THRESHOLD. `changer` is overall_change >= CHANGER_THRESHOLD (0.15). The paper sorts on
quintiles of the similarity measure, which needs a cross-section; 0.15 is a PROVISIONAL
top-quintile proxy chosen from the paper's reported distribution of cosine similarity (the
bottom quintile of similarity sits roughly at 0.85 and below). It is a placeholder until the
staged archive carries enough filings for a real distribution, at which point the batch CLI
should set it from the observed quintile and this constant becomes the fallback. The
threshold travels in every output so a reader can tell which one produced the flag.

NOT SCORED. scanner.py attaches the three features to the row's `features` block when the
staged file is present; no pillar reads them. `ic.py --by-feature` is the only consumer until
E16 (docs/BACKTEST.md §6e) has a result, after which the intended use is a slow negative
screen: a changer is not a candidate for the swing desk for the next ~60 sessions.

EDGAR. `--edgar-plan` prints, per symbol, the URLs a fetch step ON THE BOX should pull
(URL construction only; nothing here opens a socket). SEC fair-access rules, which the
fetch step must obey or be blocked: a descriptive `User-Agent: <company> <email>` header
on every request, at most 10 requests per second, `Accept-Encoding: gzip, deflate`, and
`Host: www.sec.gov` / `Host: data.sec.gov` as appropriate. Bulk crawling of the Archives
without the header gets the IP blocked for ten minutes at a time.

    submissions   https://data.sec.gov/submissions/CIK##########.json     (10-digit CIK)
    tickers→CIK   https://www.sec.gov/files/company_tickers.json          (when no cik map)
    document      https://www.sec.gov/Archives/edgar/data/<cik>/<accession-no-dashes>/<primaryDocument>
    full text     https://efts.sec.gov/LATEST/search-index?q=<phrase>&forms=10-K&dateRange=custom&startdt=…&enddt=…

`pick_filing_pair(submissions, form)` turns a submissions payload into (current, prior)
where prior is the same form filed about a year earlier (270–460 days, the paper's
year-over-year comparison; a 10-Q is compared with the same fiscal quarter's 10-Q), falling
back to the previous filing of that form when no year-earlier one exists.

CLI
---
    python3 filings.py --current cur.htm --prior prior.htm --form 10-K --out change.json
    python3 filings.py --batch DIR --out filings_signal.json
        DIR/<SYMBOL>/current.htm + prior.htm (+ optional meta.json with filed, form,
        prior_filed, accession, prior_accession) -> the staged-file shape.
    python3 filings.py --edgar-plan --symbols AAPL,MSFT --cik-map cik.json [--form 10-K]

Paths resolve from SCAN_DIR, else this file's directory.
"""
import argparse
import difflib
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from html.parser import HTMLParser

BASE = os.environ.get("SCAN_DIR") or os.path.dirname(os.path.abspath(__file__))

STAGED_FILE = "filings_signal.json"
CHANGER_THRESHOLD = 0.15          # provisional top-quintile proxy — see the docstring
SECTIONS = ("risk_factors", "mdna", "legal_proceedings", "business")
FEATURE_KEYS = ("filing_change_score", "filing_changer", "filing_days_since")

# Year-over-year window for the prior filing (days before the current filing).
PRIOR_MIN_DAYS, PRIOR_MAX_DAYS = 270, 460

# Fair-access rules a fetch step must obey (documented here so the plan prints them).
SEC_FAIR_ACCESS = {
    "user_agent": "REQUIRED on every request: 'User-Agent: <company or project name> <contact email>'",
    "rate": "at most 10 requests per second; bursts above that get the IP blocked ~10 minutes",
    "headers": "Accept-Encoding: gzip, deflate; Host: data.sec.gov for submissions, www.sec.gov for Archives",
    "cache": "keep every fetched document under the run archive — the same accession never needs a second GET",
}
SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
SEC_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
SEC_FULLTEXT = ("https://efts.sec.gov/LATEST/search-index?q={q}&forms={form}"
                "&dateRange=custom&startdt={start}&enddt={end}")

STOPWORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from
further had has have having he her here hers him his how i if in into is it its itself
just me more most my no nor not of off on once only or other our ours out over own same
she should so some such than that the their theirs them then there these they this those
through to too under until up very was we were what when where which while who whom why
will with would you your yours company companies may might shall inc corp corporation
fiscal year years quarter quarters ended
""".split())

WORD = re.compile(r"[a-z][a-z'\-]*[a-z]|[a-z]")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


# ---------------------------------------------------------------- HTML -> text
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table",
               "ul", "ol", "section", "article", "header", "footer", "hr", "title"}
_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"script", "style", "head", "noscript"}


class _TextExtractor(HTMLParser):
    """HTML -> plain text. Block elements become line breaks, table cells become spaces,
    script/style bodies are dropped. Entities are decoded by the parser itself."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip += 1
        elif t in _BLOCK_TAGS:
            self.parts.append("\n")
        elif t in _CELL_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif t in _BLOCK_TAGS:
            self.parts.append("\n")
        elif t in _CELL_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        # Inside markup a newline is whitespace; only block tags break lines, so a line in
        # the extracted text is a block, never a source-wrapping artefact.
        if not self._skip:
            self.parts.append(data.replace("\r", " ").replace("\n", " "))

    def text(self):
        return "".join(self.parts)


_LOOKS_HTML = re.compile(r"<\s*(?:html|body|div|p|table|span|br|font|td|tr|h\d)\b", re.I)


def strip_html(text_or_html):
    """Plain text from an HTML filing (or the text unchanged when it is not HTML).
    Whitespace is normalised: runs of blanks collapse, blank lines collapse to one."""
    s = text_or_html or ""
    if _LOOKS_HTML.search(s[:200000] if len(s) > 200000 else s):
        p = _TextExtractor()
        try:
            p.feed(s)
            p.close()
        except Exception:                          # noqa: BLE001 — malformed markup
            pass
        s = p.text()
    s = s.replace("\xa0", " ").replace("\u200b", "")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in s.split("\n")]
    out, blank = [], False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


_PAGE_NOISE = re.compile(
    r"^(?:table of contents|\d{1,4}|[ivxlc]{1,6}|page \d+(?: of \d+)?|-\s*\d+\s*-|"
    r"form 10-[kq]|[a-z0-9 .,&'\-]{0,60}\| 20\d\d form 10-[kq](?: \| \d+)?)$", re.I)


def clean_page_noise(text):
    """Drop the lines a paginated filing repeats: bare page numbers, 'Table of Contents'
    back-links, running 'Form 10-K' headers."""
    keep = []
    for ln in text.split("\n"):
        if ln and _PAGE_NOISE.match(ln.strip()):
            continue
        keep.append(ln)
    return "\n".join(keep)


# ---------------------------------------------------------------- section extraction
# (form) -> section -> (start item, [end items], title regex). Title is optional in the
# match but decides between candidates, and it is what separates a 10-Q's Part I Item 1
# (financial statements) from its Part II Item 1 (legal proceedings).
_ITEM = r"(?:^|\n)[ \t]*(?:part\s+(?:i{1,3}|iv)\b[\s.,:\-—–]*)?item[\s]*@ITEM@\b[\s.:\-—–)]*"
_SPEC = {
    "10-K": {
        "business":          ("1",  ["1A", "1B", "1C", "2"], r"business"),
        "risk_factors":      ("1A", ["1B", "1C", "2"],       r"risk\s+factors"),
        "legal_proceedings": ("3",  ["4", "5"],              r"legal\s+proceedings"),
        "mdna":              ("7",  ["7A", "8"],
                              r"management'?s?\s+discussion"),
    },
    "10-Q": {
        "mdna":              ("2",  ["3", "4"],             r"management'?s?\s+discussion"),
        "legal_proceedings": ("1",  ["1A", "2", "3"],       r"legal\s+proceedings"),
        "risk_factors":      ("1A", ["2", "3", "4", "5"],   r"risk\s+factors"),
    },
}
_OTHER_TITLES = {
    "10-K": {"1": r"business", "1A": r"risk", "1B": r"unresolved", "1C": r"cybersecurity",
             "2": r"properties", "3": r"legal", "4": r"mine", "5": r"market", "7": r"management",
             "7A": r"quantitative", "8": r"financial"},
    "10-Q": {"1": r"(?:financial|legal)", "1A": r"risk", "2": r"(?:management|unregistered)",
             "3": r"(?:quantitative|defaults)", "4": r"(?:controls|mine)", "5": r"other",
             "6": r"exhibits"},
}
MIN_SECTION_CHARS = 200     # shorter than this is a table-of-contents entry, not a section


def normalise_form(form):
    f = (form or "10-K").upper().replace("_", "-").strip()
    if f.endswith("/A"):
        f = f[:-2]
    if f in ("10-K405", "10-KT", "10-K"):
        return "10-K"
    if f in ("10-QT", "10-Q"):
        return "10-Q"
    return f


def _item_re(item, title=None):
    pat = _ITEM.replace("@ITEM@", re.escape(item))
    if title:
        pat += r"(?:[^\n]{0,20}\n[ \t]*)?" + title      # title on the same line or the next
    return re.compile(pat, re.I)


def _candidates(text, item, title):
    """Line-start occurrences of `Item <item>` as (start, end_of_heading) pairs; those
    followed by the section's title are preferred and, when any exist, the only ones used."""
    titled = [(m.start(), m.end()) for m in _item_re(item, title).finditer(text)]
    if titled:
        return titled
    return [(m.start(), m.end()) for m in _item_re(item).finditer(text)]


def _section_end(text, start, end_items):
    """First line-start `Item <end>` after `start`. A line that starts with the item and
    runs straight into prose ("Item 2 of Part I ...") is a cross-reference, not a heading,
    and does not end the section."""
    best = None
    for it in end_items:
        for m in _item_re(it).finditer(text, start + 1):
            if _PROSE_TAIL.match(text[m.end(): m.end() + 40]):
                continue
            best = m.start() if best is None else min(best, m.start())
            break
    return best


_PROSE_TAIL = re.compile(r"(?:of|in|to|and|under|above|below|herein|,|\()\b", re.I)
MAX_TITLE_LINE = 160     # a first line longer than this is body text, not a heading residue


def _body(text, heading_end, section_end):
    """The section text after its heading: the remainder of the heading line (the title)
    is dropped when it is short enough to be a title rather than the first sentence."""
    body = text[heading_end:section_end].lstrip()
    first, _, rest = body.partition("\n")
    if len(first) <= MAX_TITLE_LINE and not first.rstrip().endswith("."):
        body = rest
    return body.strip()


def extract_sections(text_or_html, form="10-K"):
    """{'risk_factors', 'mdna', 'legal_proceedings', 'business'}: the text of each section,
    or None when the heading could not be located. A 10-Q has no Business item; it is None.

    Strategy: every line-start `Item N` occurrence is a candidate start (those with the
    section's title right after it are preferred); each candidate's end is the first
    line-start occurrence of the next item(s); the candidate with the LONGEST body wins. The
    table-of-contents entry always loses that contest — its body is one line — and so does
    a cross-reference inside another section, which is never at a line start."""
    form = normalise_form(form)
    spec = _SPEC.get(form) or _SPEC["10-K"]
    text = clean_page_noise(strip_html(text_or_html))
    out = {k: None for k in SECTIONS}
    for name, (item, ends, title) in spec.items():
        best_body = None
        for s, h_end in _candidates(text, item, title):
            e = _section_end(text, s, ends)
            body = _body(text, h_end, e if e is not None else len(text))
            if len(body) < MIN_SECTION_CHARS:
                continue
            if best_body is None or len(body) > len(best_body):
                best_body = body
        out[name] = best_body
    return out


# ---------------------------------------------------------------- similarity
def tokens(text):
    """Lower-cased words, stop-words and bare numbers removed. Numbers move every filing
    (dates, dollar figures); the paper's signal is in the prose."""
    if not text:
        return []
    return [w for w in WORD.findall(text.lower()) if w not in STOPWORDS and len(w) > 1]


def sentences(text):
    """Sentence list for the edit ratio: normalised whitespace, split on terminal punctuation
    followed by a capital, blanks dropped."""
    if not text:
        return []
    flat = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in SENT_SPLIT.split(flat) if s.strip()]


def cosine(a_tokens, b_tokens):
    ca, cb = Counter(a_tokens), Counter(b_tokens)
    if not ca and not cb:
        return None
    if not ca or not cb:
        return 0.0
    dot = sum(v * cb[k] for k, v in ca.items() if k in cb)
    na = math.sqrt(sum(v * v for v in ca.values()))
    nb = math.sqrt(sum(v * v for v in cb.values()))
    if na == 0 or nb == 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def jaccard(a_tokens, b_tokens):
    sa, sb = set(a_tokens), set(b_tokens)
    if not sa and not sb:
        return None
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def minimum_edit_ratio(a_text, b_text, max_sentences=4000):
    """difflib.SequenceMatcher.ratio() over the two sentence lists: 2·M / (|A|+|B|), M the
    number of matched sentences. 1.0 when identical, 0.0 when no sentence is shared.

    Cost: SequenceMatcher is quadratic in the sequence length in the worst case (its
    longest-matching-block search runs once per unmatched region). On sections — a few
    hundred to a few thousand sentences — it is fine; on a whole 10-K it is not, which is
    why the module never calls it on one. Lists longer than `max_sentences` are truncated
    and the result is still reported (the tail of a section is exhibits and signatures)."""
    sa, sb = sentences(a_text), sentences(b_text)
    if not sa and not sb:
        return None
    if not sa or not sb:
        return 0.0
    sa, sb = sa[:max_sentences], sb[:max_sentences]
    return difflib.SequenceMatcher(None, sa, sb, autojunk=False).ratio()


def similarity(a, b):
    """{cosine, jaccard, minimum_edit_ratio, n_tokens_a, n_tokens_b}. Every measure is 1.0
    for identical texts and 0.0 for texts with nothing in common; None when both are empty."""
    ta, tb = tokens(a), tokens(b)
    return {"cosine": _r(cosine(ta, tb)), "jaccard": _r(jaccard(ta, tb)),
            "minimum_edit_ratio": _r(minimum_edit_ratio(a, b)),
            "n_tokens_a": len(ta), "n_tokens_b": len(tb)}


def _r(v, nd=4):
    return None if v is None else round(v, nd)


# ---------------------------------------------------------------- change score
SECTION_WEIGHTS = {"risk_factors": 0.40, "mdna": 0.30, "legal_proceedings": 0.15,
                   "business": 0.15}


def change_score(current_sections, prior_sections, threshold=CHANGER_THRESHOLD,
                 weights=None):
    """Compare the sections present on BOTH sides.

    Returns {sections: {name: {cosine, jaccard, minimum_edit_ratio} | None},
             risk_factors_change, mdna_change, overall_change, changer,
             n_sections_compared, threshold, weights, basis}
    overall_change is the WEIGHTED mean of (1 − cosine) over the sections present on both
    sides, weights renormalised over those sections (SECTION_WEIGHTS: Risk Factors 0.40,
    MD&A 0.30, Legal 0.15, Business 0.15 — the paper's ordering of where the alpha sits; the
    Risk Factors section alone carries the largest). A section that is None (or empty) on
    either side is None in `sections` and contributes nothing. With no comparable section at
    all, overall_change and changer are None — a null, not a non-changer."""
    weights = weights or SECTION_WEIGHTS
    cur = current_sections or {}
    pri = prior_sections or {}
    per, num, den = {}, 0.0, 0.0
    for name in SECTIONS:
        a, b = cur.get(name), pri.get(name)
        if not a or not b:
            per[name] = None
            continue
        sim = similarity(a, b)
        per[name] = {k: sim[k] for k in ("cosine", "jaccard", "minimum_edit_ratio")}
        if sim["cosine"] is not None:
            w = float(weights.get(name, 0.0)) or 1e-9
            num += w * (1.0 - sim["cosine"])
            den += w
    n_cmp = sum(1 for v in per.values() if v is not None and v["cosine"] is not None)
    rf = per.get("risk_factors")
    md = per.get("mdna")
    overall = _r(num / den) if den > 0 else None
    return {
        "sections": per,
        "risk_factors_change": _r(1.0 - rf["cosine"]) if rf and rf["cosine"] is not None else None,
        "mdna_change": _r(1.0 - md["cosine"]) if md and md["cosine"] is not None else None,
        "overall_change": overall,
        "changer": (overall >= threshold) if overall is not None else None,
        "n_sections_compared": n_cmp,
        "threshold": threshold,
        "weights": {k: weights.get(k) for k in SECTIONS},
        "basis": "weighted mean of 1 - cosine(TF, stop-words and numbers stripped) over the "
                 "sections present in both filings, weights renormalised; changer = "
                 "overall_change >= threshold (provisional top-quintile proxy until the "
                 "archive gives a distribution)",
    }


# ---------------------------------------------------------------- staged file
def load_staged(run_dir=None):
    """filings_signal.json from the run directory, parsed by `load_file`. None when the file
    is absent or unreadable — the scanner must be able to tell 'not staged' from 'staged and
    empty'."""
    return load_file(os.path.join(run_dir or BASE, STAGED_FILE))


def load_file(path):
    """A filings file at any path: {SYMBOL: {...}} — or {SYMBOL: [{...}, ...]}, a per-symbol
    history for a replay — with symbols upper-cased and other rows dropped; a top-level
    `_meta` is kept under '_meta'; the {"_meta", "symbols": {...}} envelope is accepted too.
    None when absent or unreadable."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("symbols"), dict):
        meta, raw = raw.get("_meta"), raw["symbols"]
    else:
        meta = raw.get("_meta") if isinstance(raw, dict) else None
    if not isinstance(raw, dict):
        return None
    out = {}
    for k, v in raw.items():
        if str(k).startswith("_") or not isinstance(v, (dict, list)):
            continue
        out[str(k).upper()] = v
    if isinstance(meta, dict):
        out["_meta"] = meta
    return out


def _parse_date(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def features_for(row, today):
    """The three features for one scan row. All null unless the row carries a dated filing
    on or before `today` with a numeric overall_change.

    `row` is one filing row, or a LIST of them (a per-symbol history, for a replay): the
    latest row filed on or before `today` is the one that counts, so a record on a
    historical date sees only what had been filed by then.

    filing_change_score   change_score.overall_change (0 = identical text, 1 = disjoint)
    filing_changer        change_score.changer (bool; ic.py reads it as 1.0 / 0.0)
    filing_days_since     sessions-agnostic calendar days from `filed` to `today`"""
    null = {k: None for k in FEATURE_KEYS}
    if isinstance(today, str):
        today = _parse_date(today)
    if isinstance(row, list):
        dated = [(d, r) for r in row if isinstance(r, dict)
                 for d in [_parse_date(r.get("filed"))] if d is not None]
        dated = [(d, r) for d, r in dated if today is not None and d <= today]
        if not dated:
            return null
        row = max(dated, key=lambda dr: dr[0])[1]
    if not isinstance(row, dict):
        return null
    filed = _parse_date(row.get("filed"))
    cs = row.get("change_score") if isinstance(row.get("change_score"), dict) else {}
    score = cs.get("overall_change")
    if score is None:
        score = row.get("overall_change")
    if filed is None or today is None or filed > today:
        return null                                   # not knowable on this date
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        return null
    changer = cs.get("changer", row.get("changer"))
    if changer is None:
        changer = score >= float(cs.get("threshold", row.get("threshold", CHANGER_THRESHOLD)))
    return {"filing_change_score": float(score), "filing_changer": bool(changer),
            "filing_days_since": (today - filed).days}


# ---------------------------------------------------------------- EDGAR plan (URLs only)
def cik10(cik):
    return str(int(str(cik).strip().lstrip("0") or "0")).zfill(10)


def primary_document_url(cik, accession, primary_document):
    acc = str(accession).replace("-", "")
    return SEC_ARCHIVE.format(cik=int(cik10(cik)), acc=acc, doc=primary_document)


def pick_filing_pair(submissions, form="10-K"):
    """From a data.sec.gov submissions payload, (current, prior) for `form`: each
    {form, filed, accession, primary_document, report_date}. Prior is the same form filed
    270–460 days before the current one (year-over-year, as the paper compares); when there
    is none, the previous filing of that form; (current, None) when only one exists;
    (None, None) when there is none. Amendments (10-K/A) are ignored. Pure — no network."""
    form = normalise_form(form)
    rec = ((submissions or {}).get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    rows = []
    for i, f in enumerate(forms):
        if normalise_form(f) != form or str(f).upper().endswith("/A"):
            continue
        filed = _parse_date((rec.get("filingDate") or [None] * len(forms))[i])
        if filed is None:
            continue
        rows.append({"form": form, "filed": filed.isoformat(),
                     "accession": (rec.get("accessionNumber") or [None] * len(forms))[i],
                     "primary_document": (rec.get("primaryDocument") or [None] * len(forms))[i],
                     "report_date": (rec.get("reportDate") or [None] * len(forms))[i]})
    rows.sort(key=lambda r: r["filed"], reverse=True)
    if not rows:
        return None, None
    cur = rows[0]
    cur_d = _parse_date(cur["filed"])
    prior = None
    for r in rows[1:]:
        gap = (cur_d - _parse_date(r["filed"])).days
        if PRIOR_MIN_DAYS <= gap <= PRIOR_MAX_DAYS:
            prior = r
            break
    if prior is None and len(rows) > 1:
        prior = rows[1]
    return cur, prior


def edgar_plan(symbols, cik_map=None, form="10-K"):
    """Per symbol, the URLs to pull and the order to pull them in. cik_map is
    {SYMBOL: cik}; a symbol without one gets the company_tickers.json lookup as step 0."""
    cik_map = {str(k).upper(): v for k, v in (cik_map or {}).items()}
    form = normalise_form(form)
    plan = {"_fair_access": dict(SEC_FAIR_ACCESS), "_form": form, "symbols": {}}
    for sym in symbols:
        s = str(sym).upper().strip()
        if not s:
            continue
        cik = cik_map.get(s)
        entry = {"cik": cik10(cik) if cik is not None else None, "steps": []}
        if cik is None:
            entry["steps"].append({"step": 0, "get": SEC_TICKERS,
                                   "then": f"cik = the row whose ticker == {s!r}; save it to the cik map"})
            sub = SEC_SUBMISSIONS.format(cik10="<cik10>")
        else:
            sub = SEC_SUBMISSIONS.format(cik10=cik10(cik))
        entry["steps"].append({"step": 1, "get": sub,
                               "then": f"filings.pick_filing_pair(payload, {form!r}) -> (current, prior)"})
        entry["steps"].append({"step": 2,
                               "get": SEC_ARCHIVE.format(cik="<cik>", acc="<accession-no-dashes>",
                                                        doc="<primaryDocument>"),
                               "then": "once for current, once for prior; save as "
                                       f"<batch>/{s}/current.htm and prior.htm, write meta.json "
                                       "{filed, form, prior_filed, accession, prior_accession}"})
        entry["steps"].append({"step": 3, "run": f"python3 filings.py --batch <batch> --out {STAGED_FILE}"})
        entry["full_text_search_example"] = SEC_FULLTEXT.format(
            q="%22risk%20factors%22", form=form, start="<YYYY-MM-DD>", end="<YYYY-MM-DD>")
        plan["symbols"][s] = entry
    return plan


# ---------------------------------------------------------------- CLI
def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _load_json(path):
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(BASE, path)
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def compare_files(current_path, prior_path, form="10-K", threshold=CHANGER_THRESHOLD):
    cur = extract_sections(_read(current_path), form)
    pri = extract_sections(_read(prior_path), form)
    cs = change_score(cur, pri, threshold)
    cs["sections_found"] = {"current": [k for k in SECTIONS if cur.get(k)],
                            "prior": [k for k in SECTIONS if pri.get(k)]}
    cs["form"] = normalise_form(form)
    return cs


def run_batch(batch_dir, threshold=CHANGER_THRESHOLD, form_default="10-K"):
    """DIR/<SYMBOL>/{current.htm|.html|.txt, prior.*, meta.json?} -> staged-file dict."""
    out, meta = {}, {"generated_at": datetime.now().isoformat(timespec="seconds"),
                     "threshold": threshold, "n_symbols": 0, "n_changers": 0,
                     "skipped": [], "basis": "filings.py --batch"}
    if not os.path.isdir(batch_dir):
        meta["skipped"].append(f"{batch_dir}: not a directory")
        return {"_meta": meta}
    for sym in sorted(os.listdir(batch_dir)):
        d = os.path.join(batch_dir, sym)
        if not os.path.isdir(d):
            continue
        cur = _find(d, "current")
        pri = _find(d, "prior")
        if not cur or not pri:
            meta["skipped"].append(f"{sym}: current/prior file missing")
            continue
        m = _load_json(os.path.join(d, "meta.json")) or {}
        form = m.get("form") or form_default
        try:
            cs = compare_files(cur, pri, form, threshold)
        except Exception as exc:                       # noqa: BLE001 — one bad filing, not the batch
            meta["skipped"].append(f"{sym}: {type(exc).__name__}: {exc}")
            continue
        row = {"filed": m.get("filed"), "form": normalise_form(form),
               "prior_filed": m.get("prior_filed"), "accession": m.get("accession"),
               "prior_accession": m.get("prior_accession"), "change_score": cs}
        out[sym.upper()] = row
        meta["n_symbols"] += 1
        if cs.get("changer"):
            meta["n_changers"] += 1
    out["_meta"] = meta
    return out


def _find(d, stem):
    for ext in (".htm", ".html", ".txt", ""):
        p = os.path.join(d, stem + ext)
        if os.path.isfile(p):
            return p
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="10-K/10-Q text-change signal (Lazy Prices).")
    ap.add_argument("--current", help="current filing (HTML or text)")
    ap.add_argument("--prior", help="prior filing of the same form")
    ap.add_argument("--form", default="10-K", help="10-K or 10-Q (default 10-K)")
    ap.add_argument("--threshold", type=float, default=CHANGER_THRESHOLD)
    ap.add_argument("--batch", help="directory of <SYMBOL>/current.htm + prior.htm (+ meta.json)")
    ap.add_argument("--edgar-plan", action="store_true",
                    help="print the EDGAR URLs a fetch step should pull (no network)")
    ap.add_argument("--symbols", help="A,B,C for --edgar-plan")
    ap.add_argument("--cik-map", dest="cik_map", help='{"AAPL": 320193, ...}')
    ap.add_argument("--out", help="write the JSON result here (default: stdout)")
    a = ap.parse_args(argv)

    if a.edgar_plan:
        syms = [s for s in (a.symbols or "").split(",") if s.strip()]
        if not syms:
            ap.error("--edgar-plan needs --symbols")
        res = edgar_plan(syms, _load_json(a.cik_map), a.form)
    elif a.batch:
        res = run_batch(a.batch, a.threshold, a.form)
    elif a.current and a.prior:
        res = compare_files(a.current, a.prior, a.form, a.threshold)
    else:
        ap.error("give --current and --prior, or --batch DIR, or --edgar-plan --symbols ...")
        return 2

    text = json.dumps(res, indent=2)
    if a.out:
        p = a.out if os.path.isabs(a.out) else os.path.join(BASE, a.out)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        if a.batch:
            m = res.get("_meta", {})
            print(f"{p}: {m.get('n_symbols', 0)} symbol(s), {m.get('n_changers', 0)} changer(s) "
                  f"at threshold {m.get('threshold')}"
                  + (f"; skipped {len(m['skipped'])}" if m.get("skipped") else ""))
        else:
            print(p)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
