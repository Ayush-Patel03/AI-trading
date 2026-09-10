"""validate_state.py — check that a state repo is laid out the way run.py expects.

    python validate_state.py --state <dir> [--engine <clone root>]     exit 0 ok, 1 not

Read-only. It walks the tree (skipping .git/ and .runs/) and reports, as errors (exit 1)
or warnings (printed, exit 0):

  layout      books/<desk>.json for every active desk in engine/desks.json (error),
              journals/, coverage/pm-coverage.json, scans/latest.json (warning when absent)
  json        every *.json parses; experiments/ledger.jsonl parses line by line (error)
  hygiene     no `mirrors` key anywhere in any JSON document and no banned shape —
              mirror.py's list: the masked account number in either spelling, tokens,
              account ids — in any text file (error)
  revisions   every book's `revision` is an integer, >= what manifests/seed.json recorded
              for it and >= the newest book_revision the coverage rows saw for that desk;
              a book-history snapshot for the desk never exceeds the living book (error)
  coverage    coverage/pm-coverage.json is {days: {YYYY-MM-DD: {runs: [row]}}} where every
              row has an ISO `ts`, a `slot` and a `desks` map, and an aborted row a
              `reason` (error)
  gitignore   .gitignore carries .runs/ and .runner.lock (warning)

The runner's health slot appends the result as its `state_layout` row (run.py). Stdlib
only; importable (`validate(state, engine_dir)`) as well as runnable.
"""
import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run as runner        # noqa: E402
import seed_state           # noqa: E402  (scan_text, the shared banned-shape list)

SKIP_DIRS = {".git", ".runs", "__pycache__"}
TEXT_SUFFIXES = (".json", ".jsonl", ".md", ".txt", ".html")
DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _walk(state):
    for p in sorted(Path(state).rglob("*")):
        if not p.is_file():
            continue
        if SKIP_DIRS & set(p.relative_to(state).parts[:-1]):
            continue
        yield p


def _find_key(obj, key, path="$"):
    """Every JSON path at which `key` appears as a dict key, anywhere in `obj`."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                hits.append(f"{path}.{k}")
            hits += _find_key(v, key, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _find_key(v, key, f"{path}[{i}]")
    return hits


def _desk_of_snapshot(name, desks):
    """archive/book-history/<date>-<slot>[-<desk>].json → desk (swing has no suffix)."""
    for d in desks:
        if d != "swing" and name.endswith(f"-{d}"):
            return d
    return "swing" if "swing" in desks else None


def validate(state, engine_dir=None):
    """{ok, errors, warnings, summary} for the repo at `state`."""
    state = Path(state)
    engine_dir = Path(engine_dir) if engine_dir else HERE.parent / "engine"
    errors, warnings = [], []
    summary = {"files": 0, "json": 0, "books": {}, "coverage_rows": 0, "desks": []}
    if not state.is_dir():
        return {"ok": False, "errors": [f"{state} is not a directory"], "warnings": [], "summary": summary}
    cfg = runner.desks_config(engine_dir)
    desks = [d for d, c in cfg.items() if not c.get("inactive")]
    summary["desks"] = desks
    if not desks:
        errors.append(f"no active desks in {engine_dir / 'desks.json'}")

    # json + hygiene, one pass over the tree
    docs = {}
    for p in _walk(state):
        rel = p.relative_to(state).as_posix()
        summary["files"] += 1
        if p.suffix not in TEXT_SUFFIXES:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for what in seed_state.scan_text(text):
            errors.append(f"{rel}: carries {what}")
        if p.suffix == ".json":
            try:
                docs[rel] = json.loads(text)
                summary["json"] += 1
            except ValueError as exc:
                errors.append(f"{rel}: not valid JSON ({exc})")
                continue
            for where in _find_key(docs[rel], "mirrors"):
                errors.append(f"{rel}: a `mirrors` block at {where} — the broker block must never enter git")
        elif p.suffix == ".jsonl":
            for i, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        json.loads(line)
                    except ValueError as exc:
                        errors.append(f"{rel} line {i}: not JSON ({exc})")

    # layout
    for d in desks:
        if f"books/{d}.json" not in docs:
            errors.append(f"books/{d}.json missing — the runner refuses every PM slot for {d}")
        if f"journals/{d}.json" not in docs:
            warnings.append(f"journals/{d}.json absent (fine for a fresh desk)")
    for want in ("coverage/pm-coverage.json", "scans/latest.json"):
        if want not in docs:
            warnings.append(f"{want} absent")
    gi = state / ".gitignore"
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    for line in (".runs/", ".runner.lock"):
        if line not in have:
            warnings.append(f".gitignore lacks {line} (run.py adds it on the first run)")
    if (state / "engine-config.json").exists() and "engine-config.json" not in have:
        errors.append("engine-config.json is in the tree and not ignored — identifiers must not be committed")

    # revisions
    seed_rev = ((docs.get("manifests/seed.json") or {}).get("revisions") or {})
    cov = docs.get("coverage/pm-coverage.json")
    cov_rev = {}
    if isinstance(cov, dict) and isinstance(cov.get("days"), dict):
        for day, rec in cov["days"].items():
            for row in (rec.get("runs") if isinstance(rec, dict) else None) or []:
                for d, cell in ((row.get("desks") or {}).items() if isinstance(row, dict) else []):
                    r = cell.get("book_revision") if isinstance(cell, dict) else None
                    if isinstance(r, int):
                        cov_rev[d] = max(r, cov_rev.get(d, 0))
    hist_rev = {}
    for rel, doc in docs.items():
        if rel.startswith("archive/book-history/") and isinstance(doc, dict):
            d = _desk_of_snapshot(Path(rel).stem, desks)
            if d and isinstance(doc.get("revision"), int):
                hist_rev[d] = max(doc["revision"], hist_rev.get(d, 0))
    for d in desks:
        b = docs.get(f"books/{d}.json")
        if b is None:
            continue
        if not isinstance(b, dict) or not isinstance(b.get("revision"), int):
            errors.append(f"books/{d}.json: no integer `revision`")
            continue
        rev = b["revision"]
        summary["books"][d] = rev
        if b.get("mode") != "paper":
            errors.append(f"books/{d}.json: mode is {b.get('mode')!r}, not paper")
        for label, table in (("manifests/seed.json", seed_rev), ("the coverage rows", cov_rev),
                             ("archive/book-history", hist_rev)):
            was = table.get(d)
            if isinstance(was, int) and rev < was:
                errors.append(f"books/{d}.json is at revision {rev} but {label} saw {was} — the book moved backwards")

    # coverage rows
    if cov is not None:
        if not isinstance(cov, dict) or not isinstance(cov.get("days"), dict):
            errors.append("coverage/pm-coverage.json: not {days: {...}}")
        else:
            for day, rec in cov["days"].items():
                if not DATE_RX.match(str(day)):
                    errors.append(f"coverage: day key {day!r} is not YYYY-MM-DD")
                runs = rec.get("runs") if isinstance(rec, dict) else None
                if not isinstance(runs, list):
                    errors.append(f"coverage: {day} has no `runs` list")
                    continue
                for i, row in enumerate(runs):
                    where = f"coverage {day} runs[{i}]"
                    if not isinstance(row, dict):
                        errors.append(f"{where}: not an object")
                        continue
                    summary["coverage_rows"] += 1
                    try:
                        runner.parse_iso(row.get("ts"))
                    except (ValueError, TypeError):
                        errors.append(f"{where}: ts {row.get('ts')!r} is not an ISO timestamp")
                    if not isinstance(row.get("slot"), str) or not row["slot"]:
                        errors.append(f"{where}: no slot")
                    if not isinstance(row.get("desks"), dict):
                        errors.append(f"{where}: `desks` is not a map")
                    if row.get("aborted") and not row.get("reason"):
                        errors.append(f"{where}: aborted without a reason")

    # journals
    for rel, doc in docs.items():
        if rel.startswith("journals/") and not (isinstance(doc, dict) and isinstance(doc.get("entries"), list)):
            errors.append(f"{rel}: not a journal ({{entries: [...]}})")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "summary": summary}


def format_report(res):
    s = res["summary"]
    lines = [f"state: {s['files']} file(s), {s['json']} JSON document(s), "
             f"{s['coverage_rows']} coverage row(s); books "
             + (", ".join(f"{d} r{r}" for d, r in sorted(s["books"].items())) or "none")]
    lines += [f"  WARN  {w}" for w in res["warnings"]]
    lines += [f"  FAIL  {e}" for e in res["errors"]]
    lines.append("VALIDATE " + ("OK" if res["ok"] else f"FAIL ({len(res['errors'])} error(s))"))
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", required=True)
    ap.add_argument("--engine", default=None, help="engine clone root (for engine/desks.json)")
    a = ap.parse_args(argv)
    engine_dir = None
    if a.engine:
        root = Path(a.engine).resolve()
        engine_dir = root / "engine" if (root / "engine" / "desks.json").exists() else root
    res = validate(a.state, engine_dir)
    print(format_report(res))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
