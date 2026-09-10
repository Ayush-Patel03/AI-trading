"""seed_state.py — lay out the private `ai-trading-state` repo from copies of the project docs.

The runner (run.py) reads and writes one layout — books/<desk>.json, journals/<desk>.json,
journals/watch.json, coverage/pm-coverage.json, scans/latest.json, scans/<run_id>.json,
scan-index.json, scan-history.json, archive/book-history/<run_id>.json, manifests/,
health/, experiments/. The project docs carry the same state under other names
(claude/paper-book.json, claude/pm-journal-pullback.json, claude/latest-scan.json, …).
This program maps a directory of project-doc copies onto that layout, once, so the first
scheduled run on the box finds a book instead of PM.md section 9's "nothing else is safe".

    python seed_state.py --from <dir of project-doc copies> --to <state repo dir>
                         [--init-git] [--dry-run] [--ledger <experiments dir or ledger.jsonl>]
                         [--engine <clone root>]

What it guarantees, in the order it matters:

1. The `mirrors` block is dropped from every book (run.py's scrub_book, mirror.py's rule):
   the masked brokerage account number must never enter git. Every output blob is then
   scanned with mirror.py's banned shapes — both spellings of the mask, tokens, account
   ids — and a single hit refuses the whole seed. Nothing partial is written: the payload
   is built in memory, every guard runs, and only then does anything touch disk.
2. A book never moves backwards. A re-seed whose book carries a lower `revision` than the
   one already in the target (or than manifests/seed.json recorded) is refused. Equal is
   fine — re-seeding the same copy is a no-op the guard allows.
3. It never pushes. --init-git makes the repo and ONE commit on `main`; the remote and the
   push are a person's step (runner/README.md, "Seeding the state repo").

Exit codes: 0 seeded (or dry run), 1 refused (bad input, a banned shape, a revision going
backwards), 2 git failed. Stdlib only, pathlib throughout — it runs on the box.
"""
import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as runner  # noqa: E402

SEED_VERSION = "0.1.0"
SEED_MANIFEST = "manifests/seed.json"
LEDGER = "experiments/ledger.jsonl"
STATE_IGNORE = runner.STATE_IGNORE + ("engine-config.json",)

# Banned shapes: the same list mirror.py scans the mirror payload with. Imported rather
# than copied so the two cannot drift; the fallback is only for a checkout with no engine/.
_shapes = None


def banned_shapes():
    global _shapes
    if _shapes is None:
        try:
            sys.path.insert(0, str(HERE.parent / "engine"))
            import mirror
            _shapes = list(mirror.SHAPES)
        except Exception:      # pragma: no cover — engine/ missing; mirror.py's list, verbatim
            _shapes = [
                (re.compile(r"ghp_[A-Za-z0-9]{16,}"), "a GitHub personal access token"),
                (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "a fine-grained GitHub token"),
                (re.compile(r"sk-[A-Za-z0-9]{20,}"), "an API key"),
                (re.compile(r"(?:•|\\u2022){4}\s*\d{4}"), "a masked account number"),
                (re.compile(r"\baccount[ _-]?(?:id|number)\b\s*[:=]\s*[\"']?\d{6,}"),
                 "a hardcoded account id"),
            ]
    return _shapes


def scan_text(text):
    """Every banned shape found in `text`, described. Empty list means clean."""
    return [what for rx, what in banned_shapes() if rx.search(text)]


class Refused(Exception):
    """A guard fired. Nothing was written."""


class GitFailed(Exception):
    pass


# ------------------------------------------------------------------ the mapping
def _basename(p):
    return str(p).replace("\\", "/").rsplit("/", 1)[-1]


def build_mapping(engine_dir):
    """{input basename: (kind, desk, state path)} for the flat names, from desks.json.

    Both spellings of every desk file are accepted: the project-doc name
    (`paper-book-pullback.json`, `pm-journal-pullback.json`) and the staged name pm.py
    uses (`paper_book_pullback.json`, `pm_journal_current_pullback.json`). Inactive desk
    templates map nowhere — a book for a desk the runner refuses to run is not state.
    """
    m = {}
    for desk, d in sorted(runner.desks_config(engine_dir).items()):
        if d.get("inactive"):
            continue
        for name in (_basename(d.get("doc_book") or ""), d.get("book") or ""):
            if name:
                m[name] = ("book", desk, f"books/{desk}.json")
        for name in (_basename(d.get("doc_journal") or ""), d.get("journal") or ""):
            if name:
                m[name] = ("journal", desk, f"journals/{desk}.json")
    m.update({
        "pm-coverage.json": ("coverage", None, "coverage/pm-coverage.json"),
        "latest-scan.json": ("scan", None, "scans/latest.json"),
        "scan_results.json": ("scan", None, "scans/latest.json"),
        "latest.json": ("scan", None, "scans/latest.json"),
        "scan-index.json": ("index", None, "scan-index.json"),
        "scan-history.json": ("history", None, "scan-history.json"),
        "pm-watch-journal.json": ("watch", None, "journals/watch.json"),
        "watch_journal_current.json": ("watch", None, "journals/watch.json"),
    })
    return m


def classify(rel, mapping):
    """(kind, desk, state path) for one input path relative to --from, or None."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if parts and parts[0] == "claude":
        parts = parts[1:]
    if not parts:
        return None
    name = parts[-1]
    if len(parts) == 1:
        return mapping.get(name)
    folder = "/".join(parts[:-1])
    if folder == "scans" and name.endswith(".json"):
        return mapping.get(name) or ("scan-record", None, f"scans/{name}")
    if folder == "book-history" and name.endswith(".json"):
        return ("book-history", None, f"archive/book-history/{name}")
    return None


# ------------------------------------------------------------------ the payload
def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as exc:
        raise Refused(f"{path.name} is not valid JSON: {exc}")
    except OSError as exc:
        raise Refused(f"cannot read {path}: {exc}")


def _engine_sha_hint(books, journals):
    """(sha, where it came from) — the book's own stamp, else the newest journal entry's."""
    for desk, book in sorted(books.items()):
        sha = book.get("engine_sha")
        if isinstance(sha, str) and sha:
            return sha, f"books/{desk}.json:engine_sha"
    best = None
    for desk, jrn in sorted(journals.items()):
        for e in (jrn.get("entries") or []) if isinstance(jrn, dict) else []:
            if isinstance(e, dict) and isinstance(e.get("engine_sha"), str) and e["engine_sha"]:
                if best is None or str(e.get("ts") or "") > best[0]:
                    best = (str(e.get("ts") or ""), e["engine_sha"], f"journals/{desk}.json:entries[].engine_sha")
    return (best[1], best[2]) if best else (None, None)


def collect(src, mapping):
    """Read every recognised input into memory. Returns the plan; raises Refused.

    plan = {files: {state path: bytes}, sources: {rel input path: sha256}, revisions:
    {desk: revision}, skipped: [rel paths], books, journals}
    """
    src = Path(src)
    if not src.is_dir():
        raise Refused(f"--from {src} is not a directory")
    files, sources, revisions, skipped, books, journals = {}, {}, {}, [], {}, {}
    seen_dest = {}
    for p in sorted(src.rglob("*")):
        if not p.is_file() or ".git" in p.relative_to(src).parts:
            continue
        rel = p.relative_to(src).as_posix()
        spec = classify(rel, mapping)
        if spec is None:
            skipped.append(rel)
            continue
        kind, desk, dest = spec
        if dest in seen_dest:
            raise Refused(f"{rel} and {seen_dest[dest]} both map to {dest} — keep one copy")
        seen_dest[dest] = rel
        obj = _load(p)
        if kind == "book":
            if not isinstance(obj, dict) or not isinstance(obj.get("revision"), int):
                raise Refused(f"{rel} is not a book: no integer `revision`")
            obj = runner.scrub_book(obj)
            revisions[desk] = obj["revision"]
            books[desk] = obj
        elif kind == "book-history":
            if not isinstance(obj, dict):
                raise Refused(f"{rel} is not a book snapshot")
            obj = runner.scrub_book(obj)
        elif kind == "journal":
            if not isinstance(obj, dict) or not isinstance(obj.get("entries"), list):
                raise Refused(f"{rel} is not a journal: no `entries` list")
            journals[desk] = obj
        elif kind == "coverage":
            if not isinstance(obj, dict) or not isinstance(obj.get("days"), dict):
                raise Refused(f"{rel} is not the coverage file: no `days` map")
        elif kind == "watch":
            if not isinstance(obj, dict) or not isinstance(obj.get("entries"), list):
                raise Refused(f"{rel} is not the watch journal: no `entries` list")
        files[dest] = runner.json_bytes(obj)
        sources[rel] = runner.sha256_file(p)
    if not books:
        raise Refused(f"no book found under {src} — a state repo with no book is not a state "
                      "of the system (looked for " + ", ".join(sorted(
                          n for n, (k, _, _) in mapping.items() if k == "book")) + ")")
    return {"files": files, "sources": sources, "revisions": revisions, "skipped": skipped,
            "books": books, "journals": journals}


def check_shapes(files):
    hits = []
    for dest, blob in sorted(files.items()):
        for what in scan_text(blob.decode("utf-8", errors="replace")):
            hits.append(f"{dest}: {what}")
        if dest.startswith("books/") and b'"mirrors"' in blob:
            hits.append(f"{dest}: the broker block survived the scrub")
    if hits:
        raise Refused("the seed would carry " + "; ".join(hits) +
                      " — nothing was written. Fix the source, never the scrubber.")


def check_revisions(state, revisions):
    """Refuse a book that would move backwards relative to the target or its last seed."""
    state = Path(state)
    previous = runner.load_json(state / SEED_MANIFEST) or {}
    known = dict((previous.get("revisions") or {}))
    for desk in revisions:
        b = runner.load_json(state / "books" / f"{desk}.json")
        if isinstance(b, dict) and isinstance(b.get("revision"), int):
            known[desk] = max(b["revision"], known.get(desk) or 0)
    for desk, now in sorted(revisions.items()):
        was = known.get(desk)
        if isinstance(was, int) and now < was:
            raise Refused(f"books/{desk}.json is at revision {now} but the target already holds "
                          f"{was} — this seed is working from a stale copy and must not overwrite "
                          "a newer book. Fetch the current project doc and re-run.")
    return previous


def read_ledger(path):
    """The engine repo's ledger, validated line by line. Returns bytes."""
    p = Path(path)
    if p.is_dir():
        p = p / "ledger.jsonl"
    if not p.is_file():
        raise Refused(f"--ledger {path}: no ledger.jsonl there")
    text = p.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                json.loads(line)
            except ValueError as exc:
                raise Refused(f"--ledger {p} line {i} is not JSON: {exc}")
    return text.encode("utf-8")


def state_readme(revisions, seeded_at):
    desks = ", ".join(f"{d} @ r{r}" for d, r in sorted(revisions.items()))
    return f"""# ai-trading-state

**Private.** This repository is the living state of the paper-trading engine: the paper
books, the decision journals, the coverage record and the scan hand-offs that
`runner/run.py` (in the engine repo `AI-trading`) reads before every slot and writes back
after it. One commit per invocation; `git log` is the run history. It holds paper state
only — no credentials, no brokerage identifiers (every book's `mirrors` block is stripped
before it lands here, and the seed and the health slot refuse a masked account number).

Seeded {seeded_at} from copies of the project docs by `runner/seed_state.py` ({desks}).

## Layout

```
books/swing.json  pullback.json  momentum.json      the living books (mirrors block stripped)
journals/swing.json  pullback.json  momentum.json   the decision journals; watch.json for the watch
coverage/pm-coverage.json                          COVER-01 rows, including aborted: true rows
scans/latest.json  <scan run_id>.json               the hand-off and the compact records
scan-index.json  scan-history.json                  the archive index and the daily history
archive/book-history/<pm run_id>.json               every revision that changed something
archive/runs/<runner run_id>/                       boards, pm_state, engine stdout/stderr
manifests/seed.json                                 what this repo was seeded from
manifests/<date>/<slot>-<desk>.json                 one per invocation; the idempotency record
health/<date>.json  <date>.md  heartbeat.json       the health sheet and the runner's heartbeat
experiments/ledger.jsonl                            the experiment ledger (append-only)
.runs/  .runner.lock                                ignored; scratch and the lock
```

## Rules

- Nothing here is edited by hand. The runner writes it; `runner/validate_state.py --state .`
  checks it (layout, JSON, no broker block, no banned shape, revisions monotonic).
- `engine-config.json` never lives here (it is ignored; it belongs next to the venv).
- A book may not move backwards: the runner's `--check` and the seed's revision guard both
  refuse it.
"""


def gitignore_text():
    return "\n".join(STATE_IGNORE) + "\n"


# ------------------------------------------------------------------ writing
def write_tree(state, files):
    state = Path(state)
    written = []
    for dest, blob in sorted(files.items()):
        p = state / dest
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(blob)
        written.append(dest)
    return written


def _git(state, *args):
    r = runner.git(state, *args)
    if r.returncode != 0:
        raise GitFailed(f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout


def init_git(state, message):
    """git init on `main` (idempotent for an existing repo), one commit. Never pushes."""
    state = Path(state)
    if not (state / ".git").exists():
        _git(state, "init", "-q")
        _git(state, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(state, "add", "-A")
    if not _git(state, "status", "--porcelain").strip():
        return {"committed": False, "sha": _git(state, "rev-parse", "HEAD").strip() or None}
    _git(state, "-c", "user.name=ai-trading-runner", "-c", "user.email=runner@ai-trading.local",
         "commit", "-q", "-m", message)
    return {"committed": True, "sha": _git(state, "rev-parse", "HEAD").strip()}


def seed(src, state, engine_dir=None, ledger=None, init=False, dry_run=False, now=None,
         log=print):
    """The whole seed. Returns the manifest dict; raises Refused / GitFailed."""
    src, state = Path(src), Path(state)
    engine_dir = Path(engine_dir) if engine_dir else HERE.parent / "engine"
    if not (engine_dir / "desks.json").exists():
        raise Refused(f"no desks.json under {engine_dir} — pass --engine <clone root>")
    now = now or runner.utc_now()
    seeded_at = runner.iso(now)
    mapping = build_mapping(engine_dir)
    plan = collect(src, mapping)
    files = dict(plan["files"])

    previous = check_revisions(state, plan["revisions"])
    sha_hint, sha_from = _engine_sha_hint(plan["books"], plan["journals"])
    manifest = {
        "_what": "What runner/seed_state.py laid this repo out from. The runner's own manifests "
                 "live under manifests/<date>/. Revisions here are the guard a re-seed is checked "
                 "against: a book may not move backwards.",
        "seed_version": SEED_VERSION, "seeded_at": seeded_at,
        "source_dir": str(src.resolve()), "source_files": plan["sources"],
        "engine_sha_hint": sha_hint, "engine_sha_hint_from": sha_from,
        "revisions": plan["revisions"], "written": sorted(files),
        "skipped_inputs": plan["skipped"],
        "previous_seed": previous.get("seeded_at") if previous else None,
    }
    files[SEED_MANIFEST] = runner.json_bytes(manifest)
    if ledger:
        files[LEDGER] = read_ledger(ledger)
    elif not (state / LEDGER).exists():
        files[LEDGER] = b""
    if not (state / "README.md").exists():
        files["README.md"] = state_readme(plan["revisions"], seeded_at).encode("utf-8")
    if not (state / ".gitignore").exists():
        files[".gitignore"] = gitignore_text().encode("utf-8")
    for keep in ("health/.gitkeep", "archive/.gitkeep"):
        if not (state / keep).exists() and not any(d.startswith(keep.split("/")[0] + "/") for d in files):
            files[keep] = b""
    check_shapes(files)

    total = sum(len(b) for b in files.values())
    log(f"{'DRY RUN — would write' if dry_run else 'seed'}: {len(files)} file(s), {total:,} bytes "
        f"from {src} -> {state}")
    for dest in sorted(files):
        extra = f"  (revision {plan['revisions'][dest[6:-5]]})" if dest.startswith("books/") else ""
        log(f"  {dest:48s} {len(files[dest]):>9,} bytes{extra}")
    for rel in plan["skipped"]:
        log(f"  skipped (not state): {rel}")
    if sha_hint:
        log(f"  engine_sha hint {sha_hint} from {sha_from}")
    if dry_run:
        return manifest

    state.mkdir(parents=True, exist_ok=True)
    manifest["written"] = write_tree(state, files)
    if (state / ".gitignore").exists():
        runner.ensure_state_ignore(state)      # an older .gitignore still gets the runner's lines
    if init:
        date = now.date().isoformat()
        manifest["git"] = init_git(state, f"seed state from project docs {date}")
        log(f"git: {'committed ' + manifest['git']['sha'][:10] if manifest['git']['committed'] else 'nothing to commit'}"
            " on main — not pushed")
    return manifest


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="src", required=True, help="directory of project-doc copies")
    ap.add_argument("--to", dest="state", required=True, help="the state repo directory (created if missing)")
    ap.add_argument("--engine", default=None, help="engine clone root (for engine/desks.json); default: this clone")
    ap.add_argument("--ledger", default=None, help="experiments/ directory or ledger.jsonl to copy in")
    ap.add_argument("--init-git", action="store_true", help="git init on main and commit once; never pushes")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    ap.add_argument("--now", default=None, help="ISO clock override (tests)")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    now = runner.parse_iso(a.now) if a.now else None
    engine_dir = None
    if a.engine:
        root = Path(a.engine).resolve()
        engine_dir = root / "engine" if (root / "engine" / "desks.json").exists() else root
    try:
        seed(a.src, a.state, engine_dir=engine_dir, ledger=a.ledger, init=a.init_git,
             dry_run=a.dry_run, now=now)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except GitFailed as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
