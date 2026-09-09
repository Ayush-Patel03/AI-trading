"""Runtime identifiers, read from a PRIVATE project doc — never from this repo.

This repo is public. The account id, the account mask and the artifact board URLs live
in `claude/engine-config.json`, which the run stages into $SCAN_DIR alongside the books.
The engine carries field names only.

The account LOCK is unchanged in substance: the agentic account is still resolved from
`get_accounts` at run time and never by nickname. What is here is a cross-check, not the
authority — see docs/PM.md section 1.
"""
import json, os, re

FILENAME = "engine-config.json"
_CACHE = {}
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _base(base=None):
    return (base or os.environ.get("SCAN_DIR")
            or os.path.dirname(os.path.abspath(__file__)))


def load(base=None):
    """The config dict, or {} when the file was not staged. Never raises."""
    base = _base(base)
    if base in _CACHE:
        return _CACHE[base]
    try:
        with open(os.path.join(base, FILENAME), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    _CACHE[base] = data
    return data


def board_url(name, base=None):
    """A claude.ai artifact URL by key ('trade_desk' / 'scan_desk'), or None.

    None is a real answer and callers must handle it: a run staged without the config
    republishes nothing rather than forking a second rolling board.
    """
    return (load(base).get("boards") or {}).get(name)


def account(base=None):
    return load(base).get("account") or {}


def engine_sha(base=None):
    """The commit this run's engine came from, or None when not run from a clone.

    Written by the clone step as $SCAN_DIR/engine_sha. The engine does not shell out to
    git — it is not always running inside a work tree, and a run must never depend on
    git being present. A file that is not a plausible sha is treated as absent rather
    than stamped onto a decision.
    """
    try:
        with open(os.path.join(_base(base), "engine_sha"), encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        return None
    return v if _SHA_RE.match(v) else None
