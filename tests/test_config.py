import json, pathlib, sys

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


def test_no_identifier_is_hardcoded_anywhere_in_the_repo():
    root = pathlib.Path(__file__).resolve().parents[1]
    banned = ["621506005", "••••6005", "claude.ai/code/artifact",
              "ghp_", "github_pat_"]
    hits = []
    for p in list(root.rglob("*.py")) + list(root.rglob("*.md")) + list(root.rglob("*.json")):
        if ".git" in p.parts or "fixtures" in p.parts or p.name == "test_config.py":
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for b in banned:
            if b in text:
                hits.append(f"{p.relative_to(root)}: {b}")
    assert hits == [], "identifiers must never reach the public repo: " + "; ".join(hits)
