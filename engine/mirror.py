"""Build the state-mirror payload: the project's state, scrubbed, for the web frontend.

The frontend cannot read a Claude project, so each run publishes the state it just wrote
to a private repo the site builds from. This module produces that payload. It does NOT
push it — config.py's rule applies hardest here: the engine never shells out to git,
because a run is not always inside a work tree and must never depend on git being
present. This BUILDS; the run performs the side effect, exactly as render.py writes HTML
and the run publishes the artifact.

Three properties, in order of how much they matter:

1. DOWNSTREAM. The project docs are the source of truth. This runs after they are safely
   written, and nothing here can write back. A failure costs a page refresh, not a book.
2. ALL OR NOTHING. Every guard runs against the whole payload in memory before a single
   byte reaches disk. A half-written mirror publishes a state nobody can reason about.
3. REFUSES RATHER THAN GUESSES. An unparseable book, a missing book, a credential shape,
   a book moving backwards — each stops the build with a reason, the way render.py exits
   rather than publishing a board that misstates what the run did.
"""
import datetime as dt
import hashlib
import json
import os
import re

import config

SCHEMA_VERSION = 1


class RefusedToMirror(Exception):
    """A guard fired. The payload was not written and nothing partial was left behind."""


# Shapes, never literals — a scanner that spells out the secret it hunts publishes it.
#
# This list is deliberately NOT the public repo's list in tests/test_config.py. That one
# also bans claude.ai artifact URLs, because the engine repo is public and a board URL is
# an unguessable capability. The state repo is private and behind auth, and scan-index is
# useless without the link to each frozen board. What stays banned everywhere: anything
# that is a credential or identifies the brokerage account.
SHAPES = [
    (re.compile(r"ghp_[A-Za-z0-9]{16,}"), "a GitHub personal access token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "a fine-grained GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "an API key"),
    # Both spellings. json.dumps escapes the bullet to • unless ensure_ascii is off,
    # and a scanner that only knows the literal character silently passes the escaped
    # form — which is exactly the payload this module produces. Caught by its own test.
    (re.compile(r"(?:•|\\u2022){4}\s*\d{4}"), "a masked account number"),
    (re.compile(r"\baccount[ _-]?(?:id|number)\b\s*[:=]\s*[\"']?\d{6,}"),
     "a hardcoded account id"),
]

# staged filename -> path inside the mirror. Anything not named here does not travel;
# engine-config.json is excluded by construction rather than by a rule that can be edited.
BOOKS = {
    "paper_book.json": "books/swing.json",
    "paper_book_pullback.json": "books/pullback.json",
    "paper_book_momentum.json": "books/momentum.json",
}
JOURNALS = {
    "pm-journal.json": "journals/swing.json",
    "pm-journal-pullback.json": "journals/pullback.json",
    "pm-journal-momentum.json": "journals/momentum.json",
}
FLAT = {
    "latest-scan.json": "scans/latest.json",
    "scan-index.json": "scan-index.json",
    "scan-history.json": "scan-history.json",
    "pm-coverage.json": "pm-coverage.json",
}
TREES = {"scans": "scans", "health": "health"}


def scan_text(text):
    """Every banned shape found in `text`, described. Empty list means clean."""
    return [what for rx, what in SHAPES if rx.search(text)]


def scrub_book(book):
    """A book without its broker block.

    `mirrors.display` is a masked account number, which this repo's own banned-shape list
    treats as an identifier — correctly. The frontend shows a paper book; which brokerage
    account it shadows is not its business, so the whole block stays home rather than
    being partially redacted into something that invites a later mistake.
    """
    return {k: v for k, v in book.items() if k != "mirrors"}


def _json_bytes(obj):
    """UTF-8, unescaped, stable key order — the payload lands in git and gets diffed."""
    return json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as exc:
        raise RefusedToMirror(f"{os.path.basename(path)} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise RefusedToMirror(f"cannot read {os.path.basename(path)}: {exc}") from exc


def _collect(base):
    """The payload as {mirror_path: (bytes, source_revision)}, entirely in memory."""
    payload = {}

    for name, dest in BOOKS.items():
        src = os.path.join(base, name)
        if not os.path.exists(src):
            continue
        book = _load(src)
        if not isinstance(book, dict):
            raise RefusedToMirror(f"{name} is not a book object")
        payload[dest] = (_json_bytes(scrub_book(book)), book.get("revision"))

    if not any(dest in payload for dest in BOOKS.values()):
        raise RefusedToMirror(
            "no book was found in the staging directory — a mirror with no book is not a "
            "state of the system, and publishing one would blank the site")

    for group in (JOURNALS, FLAT):
        for name, dest in group.items():
            src = os.path.join(base, name)
            if os.path.exists(src):
                payload[dest] = (_json_bytes(_load(src)), None)

    for name, dest_dir in TREES.items():
        src_dir = os.path.join(base, name)
        if not os.path.isdir(src_dir):
            continue
        for entry in sorted(os.listdir(src_dir)):
            src = os.path.join(src_dir, entry)
            if not os.path.isfile(src):
                continue
            if entry.endswith(".json"):
                payload[f"{dest_dir}/{entry}"] = (_json_bytes(_load(src)), None)
            elif entry.endswith(".md"):
                with open(src, "rb") as f:
                    payload[f"{dest_dir}/{entry}"] = (f.read(), None)

    return payload


def _check_shapes(payload):
    hits = []
    for path, (blob, _) in sorted(payload.items()):
        for what in scan_text(blob.decode("utf-8", errors="replace")):
            hits.append(f"{path}: {what}")
    if hits:
        raise RefusedToMirror(
            "the payload contains " + "; ".join(hits) +
            " — nothing was written. Fix the source, never the scrubber.")


def _check_revisions(payload, previous):
    """A book may not move backwards relative to what is already mirrored.

    2026-09-01: a stale session overwrote a newer book and cost a day of record. The
    mirror must not become a second route to the same outcome.
    """
    if not previous:
        return
    known = {row.get("path"): row.get("source_revision")
             for row in (previous.get("files") or [])}
    for path, (_, revision) in sorted(payload.items()):
        was, now = known.get(path), revision
        if isinstance(was, int) and isinstance(now, int) and now < was:
            raise RefusedToMirror(
                f"{path} is at revision {now} but the mirror already holds {was} — "
                "this run is working from a stale book and must not publish it")


def manifest(payload, slot, run_id, engine_sha, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    files = []
    for path, (blob, revision) in sorted(payload.items()):
        row = {"path": path, "bytes": len(blob),
               "sha256": hashlib.sha256(blob).hexdigest()}
        if revision is not None:
            row["source_revision"] = revision
        files.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "slot": slot,
        "run_id": run_id,
        "engine_sha": engine_sha,
        "files": files,
    }


def build(base, out, slot, run_id, previous=None, now=None):
    """Build the mirror payload from staging dir `base` into `out`. Returns the manifest.

    Every guard runs before anything is written, so a refusal leaves no partial payload.
    """
    payload = _collect(base)
    _check_shapes(payload)
    _check_revisions(payload, previous)

    m = manifest(payload, slot, run_id, config.engine_sha(base=base), now=now)

    for path, (blob, _) in sorted(payload.items()):
        dest = os.path.join(out, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(blob)
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(m, indent=2) + "\n")
    return m


def _main(argv):
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", required=True, help="directory to build the payload into")
    p.add_argument("--slot", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--previous", help="the mirror's current manifest.json, if any")
    p.add_argument("--base", default=os.environ.get("SCAN_DIR") or ".")
    a = p.parse_args(argv)

    prev = None
    if a.previous and os.path.exists(a.previous):
        with open(a.previous, encoding="utf-8") as f:
            prev = json.load(f)

    m = build(a.base, a.out, a.slot, a.run_id, previous=prev)
    total = sum(r["bytes"] for r in m["files"])
    print(f"mirror payload: {len(m['files'])} files, {total:,} bytes -> {a.out}")
    print(f"  as of {m['generated_at']}  slot={m['slot']}  engine_sha={m['engine_sha']}")
    print()
    print("PUSH, from the state repo clone:")
    print(f"  1. copy {a.out}/* over the clone")
    print("  2. git add -A && git commit -m "
          f"'state: {m['slot']} {m['run_id']}' && git pull --rebase && git push")
    print("  3. on a rebase, RE-RUN this command against the rebased manifest —")
    print("     the digests must describe the tree that actually got pushed")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
