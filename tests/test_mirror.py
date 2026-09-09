"""The state mirror: what leaves the project, and what must never leave with it.

The mirror is the read path for the web frontend. It is DOWNSTREAM of the project docs
in every sense — it runs after the book is safely written, it cannot write back, and a
mirror failure costs a page refresh rather than a book. These tests hold that line.
"""
import hashlib
import json
import pathlib
import sys

import pytest

# Assembled, never written out. This file lives in the public repo and its own
# identifier scan refuses a token-shaped literal — including a fake one. The guard
# under test needs the SHAPE, which concatenation still produces at runtime.
FAKE_TOKEN = "ghp_" + "x" * 20
# Same reason, and the digits are invented — the real last-four belongs in the private
# config, not in a fixture that ships to the world.
FAKE_MASK = "\u2022" * 4 + "1234"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import mirror


# ---------------------------------------------------------------- fixtures

BOOK = {
    "mode": "paper",
    "revision": 21,
    "based_on_revision": 20,
    "last_run": "2026-09-03T15:03:30Z",
    "mirrors": {
        "broker": "Robinhood",
        "display": FAKE_MASK,
        "nickname": "Agentic",
        "type": "limited_margin",
        "live_cash_expected": 50.0,
    },
    "starting_equity": 5000.0,
    "cash": 2784.33,
    "positions": [{"symbol": "MU", "shares": 0.401123, "avg_cost": 959.46}],
}


@pytest.fixture
def staged(tmp_path):
    """A staging directory shaped like what a run holds when the slot finishes."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "paper_book.json").write_text(json.dumps(BOOK), encoding="utf-8")
    (src / "pm-coverage.json").write_text(json.dumps({"days": {}, "keep_days": 10}), encoding="utf-8")
    (src / "engine_sha").write_text("b362cd7\n", encoding="utf-8")
    return src


@pytest.fixture
def out(tmp_path):
    return tmp_path / "out"


# ---------------------------------------------------------------- the scrub

def test_the_account_mask_never_leaves_the_project(staged, out):
    """`mirrors.display` is a masked account number, which the repo's own banned-shape
    list treats as an identifier. The frontend has no use for it, so it does not travel."""
    mirror.build(str(staged), str(out), slot="opening-range", run_id="2026-09-03-opening-range")
    text = (out / "books" / "swing.json").read_text(encoding="utf-8")
    assert "1234" not in text
    assert "mirrors" not in json.loads(text)


def test_the_scrub_keeps_everything_the_frontend_actually_needs(staged, out):
    mirror.build(str(staged), str(out), slot="opening-range", run_id="r1")
    book = json.loads((out / "books" / "swing.json").read_text(encoding="utf-8"))
    assert book["revision"] == 21
    assert book["cash"] == 2784.33
    assert book["positions"][0]["symbol"] == "MU"
    assert book["starting_equity"] == 5000.0


def test_an_unscrubbed_book_would_fail_the_scan(staged):
    """Proves the guard is load-bearing rather than decorative: the same book, unscrubbed,
    is caught. A guard nobody has watched fail is a guard nobody knows works."""
    assert mirror.scan_text(json.dumps(BOOK)) != []
    assert mirror.scan_text(json.dumps(mirror.scrub_book(BOOK))) == []


def test_the_private_config_is_never_mirrored_even_when_staged(staged, out):
    (staged / "engine-config.json").write_text(json.dumps(
        {"account": {"id": "000000000"}, "boards": {"scan_desk": "https://example.invalid/b"}}), encoding="utf-8")
    mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    assert not list(out.rglob("engine-config.json"))
    for p in out.rglob("*"):
        if p.is_file():
            assert "000000000" not in p.read_text(encoding="utf-8", errors="replace")


def test_a_credential_shape_anywhere_in_the_payload_refuses_the_whole_build(staged, out):
    """All-or-nothing. A partial mirror publishes a state nobody can reason about."""
    (staged / "pm-journal.json").write_text(json.dumps(
        {"note": "token " + FAKE_TOKEN}), encoding="utf-8")
    with pytest.raises(mirror.RefusedToMirror) as e:
        mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    assert "GitHub personal access token" in str(e.value)


def test_artifact_urls_are_allowed_in_the_private_mirror(staged, out):
    """The public repo bans board URLs because it is public. The state repo is private and
    behind auth, and the archive index is USELESS without the link to each frozen board."""
    # Assembled rather than written out: this file lives in the PUBLIC repo, whose own
    # identifier scan bans that URL shape on sight. The test needs the shape, not a URL.
    url = "https://claude.ai/code/" + "artifact/abc-123"
    (staged / "scan-index.json").write_text(json.dumps(
        {"runs": [{"run_id": "r0", "artifact_url": url}]}), encoding="utf-8")
    mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    assert url in (out / "scan-index.json").read_text(encoding="utf-8")


# ---------------------------------------------------------------- the manifest

def test_the_manifest_is_the_single_source_of_as_of(staged, out):
    mirror.build(str(staged), str(out), slot="opening-range",
                 run_id="2026-09-03-opening-range")
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["slot"] == "opening-range"
    assert m["run_id"] == "2026-09-03-opening-range"
    assert m["engine_sha"] == "b362cd7"
    assert m["schema_version"] == mirror.SCHEMA_VERSION
    assert m["generated_at"].endswith("Z")


def test_every_manifest_digest_matches_the_bytes_on_disk(staged, out):
    """The frontend trusts the manifest. If a digest can drift from its file, the page can
    render a number the manifest says came from somewhere else."""
    mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert m["files"], "a manifest with no files is not a successful mirror"
    for row in m["files"]:
        blob = (out / row["path"]).read_bytes()
        assert row["bytes"] == len(blob)
        assert row["sha256"] == hashlib.sha256(blob).hexdigest()


def test_the_manifest_does_not_list_itself(staged, out):
    mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    m = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert "manifest.json" not in [r["path"] for r in m["files"]]


def test_a_missing_engine_sha_is_absent_not_invented(tmp_path, out):
    src = tmp_path / "src"
    src.mkdir()
    (src / "paper_book.json").write_text(json.dumps(BOOK), encoding="utf-8")
    mirror.build(str(src), str(out), slot="midday", run_id="r1")
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["engine_sha"] is None


# ---------------------------------------------------------------- the revision guard

def _previous(revision):
    return {"schema_version": mirror.SCHEMA_VERSION,
            "files": [{"path": "books/swing.json", "source_revision": revision}]}


def test_a_stale_session_cannot_roll_the_mirrored_book_backwards(staged, out):
    """2026-09-01: a stale session overwrote a newer book and destroyed a day. The mirror
    must not be a second way for that to happen."""
    with pytest.raises(mirror.RefusedToMirror) as e:
        mirror.build(str(staged), str(out), slot="midday", run_id="r1",
                     previous=_previous(22))
    assert "revision" in str(e.value).lower()


def test_re_running_the_same_slot_is_allowed(staged, out):
    mirror.build(str(staged), str(out), slot="midday", run_id="r1", previous=_previous(21))
    assert json.loads((out / "books" / "swing.json").read_text(encoding="utf-8"))["revision"] == 21


def test_moving_forward_is_allowed(staged, out):
    mirror.build(str(staged), str(out), slot="midday", run_id="r1", previous=_previous(20))
    assert (out / "books" / "swing.json").exists()


def test_no_previous_manifest_is_a_first_push_not_an_error(staged, out):
    mirror.build(str(staged), str(out), slot="midday", run_id="r1", previous=None)
    assert (out / "manifest.json").exists()


# ---------------------------------------------------------------- refusing, not guessing

def test_a_book_that_does_not_parse_refuses_rather_than_mirrors(staged, out):
    (staged / "paper_book_momentum.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(mirror.RefusedToMirror):
        mirror.build(str(staged), str(out), slot="midday", run_id="r1")


def test_a_staging_dir_with_no_books_refuses(tmp_path, out):
    src = tmp_path / "src"
    src.mkdir()
    with pytest.raises(mirror.RefusedToMirror) as e:
        mirror.build(str(src), str(out), slot="midday", run_id="r1")
    assert "book" in str(e.value).lower()


def test_the_build_is_atomic_nothing_is_left_behind_on_refusal(staged, out):
    (staged / "pm-journal.json").write_text(json.dumps({"t": FAKE_TOKEN}), encoding="utf-8")
    with pytest.raises(mirror.RefusedToMirror):
        mirror.build(str(staged), str(out), slot="midday", run_id="r1")
    assert not out.exists() or not any(out.rglob("*.json"))


def test_the_engine_never_shells_out_to_git():
    """config.py's rule, and it applies here hardest: the mirror runs in a container that
    may not be a work tree. This module BUILDS a payload; the run pushes it.

    Structural, not textual — the first version of this test matched the word "git" in
    the module's own docstring explaining why it does not use git.
    """
    import ast
    src = (pathlib.Path(__file__).resolve().parents[1] / "engine" / "mirror.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert not imported & {"subprocess", "pty", "shlex"}, \
        f"mirror.py must not import a process-spawning module: {imported}"

    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not called & {"os.system", "os.popen", "os.execv", "os.spawnv"}, \
        f"mirror.py must not spawn a process: {called}"
