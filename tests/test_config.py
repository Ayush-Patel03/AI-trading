import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import config


def test_missing_config_returns_empty_not_crash(tmp_path):
    # A run staged without engine-config.json must degrade, not explode.
    assert config.load(str(tmp_path)) == {}
    assert config.board_url("trade_desk", base=str(tmp_path)) is None


def test_board_url_reads_config(tmp_path):
    (tmp_path / "engine-config.json").write_text(json.dumps(
        {"boards": {"trade_desk": "https://example.invalid/board"}}))
    assert config.board_url("trade_desk", base=str(tmp_path)) == "https://example.invalid/board"


def test_account_reads_config(tmp_path):
    (tmp_path / "engine-config.json").write_text(json.dumps(
        {"account": {"id": "000000000", "nickname": "Test"}}))
    assert config.account(str(tmp_path))["nickname"] == "Test"


def test_malformed_config_is_treated_as_absent(tmp_path):
    (tmp_path / "engine-config.json").write_text("{not json at all")
    assert config.load(str(tmp_path)) == {}


def _repo_text_files():
    root = pathlib.Path(__file__).resolve().parents[1]
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts:
            continue
        if p.name == config.FILENAME:
            continue          # gitignored: staged for a run, never part of the repo
        if p.suffix in (".py", ".md", ".json", ".yml", ".yaml", ".txt", ".cfg", ".toml"):
            yield root, p


# Shapes, never literals. A scanner that spells out the secret it hunts publishes the
# secret — which is exactly what the first version of this test did.
BANNED_SHAPES = [
    (re.compile(r"claude\.ai/code/artifact"), "a claude.ai artifact URL"),
    (re.compile(r"ghp_[A-Za-z0-9]{16,}"), "a GitHub personal access token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "a fine-grained GitHub token"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "an API key"),
    (re.compile(r"\u2022{4}\s*\d{4}"), "a masked account number"),
    (re.compile(r"\baccount[ _-]?(?:id|number)\b\s*[:=]\s*[\"']?\d{6,}"),
     "a hardcoded account id"),
]


def test_no_identifier_shape_appears_anywhere_in_the_repo():
    hits = []
    for root, p in _repo_text_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        for rx, what in BANNED_SHAPES:
            if rx.search(text):
                hits.append(f"{p.relative_to(root)}: {what}")
    assert hits == [], "this repo is public: " + "; ".join(hits)


def test_no_value_from_the_private_config_leaks_into_the_repo():
    """The strongest check, and the one that needs no literal in this file.

    When engine-config.json is staged (a real run, or a developer who copied it in), no
    string it contains may appear anywhere in the tree. Skipped in CI, where the private
    config is deliberately absent — the shape test above is what guards that case.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    cfg_path = root / "engine-config.json"
    if not cfg_path.exists():
        pytest.skip("engine-config.json not staged — shape test covers CI")
    cfg = json.loads(cfg_path.read_text())
    # Only the values that actually identify the account or the boards. The nickname is
    # a word that appears in the doctrine legitimately and is not an identifier.
    acct = cfg.get("account") or {}
    secrets = {v for v in (acct.get("id"), acct.get("display")) if isinstance(v, str)}
    secrets |= {v for v in (cfg.get("boards") or {}).values() if isinstance(v, str)}
    hits = []
    for _, p in _repo_text_files():
        if p == cfg_path:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for sec in secrets:
            if sec in text:
                hits.append(f"{p.relative_to(root)}: a value from engine-config.json")
    assert hits == [], "private config values must never reach the repo: " + "; ".join(hits)
