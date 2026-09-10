"""runner/seed_state.py and runner/validate_state.py — laying out the private state repo.

The fixture set under tests/fixtures/state_seed/ is built from real copies of the project
docs (paper-book*.json, pm-journal*.json, pm-coverage.json, latest-scan.json) with the
`mirrors` block stripped, the artifact URLs nulled (this repo is public) and the long
lists trimmed; scan-index, scan-history, the watch journal, one compact scan record and
one book-history snapshot are small companions in the project's shapes. The hygiene test
below is what keeps the fixtures clean — the masked account number, in either spelling,
must never be in this repo.
"""
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).parent / "fixtures" / "state_seed"
sys.path.insert(0, str(ROOT / "runner"))
import run as runner        # noqa: E402
import seed_state           # noqa: E402
import validate_state       # noqa: E402
import selftest             # noqa: E402

DESKS = ("swing", "pullback", "momentum")
# Assembled at runtime, never written out (tests/test_mirror.py does the same).
FAKE_MASK = "•" * 4 + "1234"
FAKE_MASK_ESCAPED = "\\u2022" * 4 + "1234"
ARTIFACT_URL = "claude.ai/code/" + "artifact"        # public repo: the literal is banned here too


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8").stdout


def _fixture_revisions():
    out = {}
    for desk, name in (("swing", "paper-book.json"), ("pullback", "paper-book-pullback.json"),
                       ("momentum", "paper-book-momentum.json")):
        out[desk] = json.loads((FIX / name).read_text(encoding="utf-8"))["revision"]
    return out


def _seed(src, dst, *extra):
    return seed_state.main(["--from", str(src), "--to", str(dst), "--engine", str(ROOT), *extra])


# ---------------------------------------------------------------- fixture hygiene
def test_fixtures_carry_neither_the_broker_block_nor_the_mask():
    files = sorted(p for p in FIX.rglob("*") if p.is_file())
    assert len(files) >= 10
    for p in files:
        text = p.read_text(encoding="utf-8")
        assert '"mirrors"' not in text, p.name
        assert seed_state.scan_text(text) == [], p.name
        assert ARTIFACT_URL not in text, p.name
        json.loads(text)


def test_the_shape_list_is_mirror_pys():
    sys.path.insert(0, str(ROOT / "engine"))
    import mirror
    assert seed_state.banned_shapes() == mirror.SHAPES
    assert seed_state.scan_text(json.dumps({"display": FAKE_MASK})) == ["a masked account number"]
    assert seed_state.scan_text(FAKE_MASK) == ["a masked account number"]


# ---------------------------------------------------------------- the mapping
def test_mapping_accepts_both_spellings_and_skips_inactive_desks():
    m = seed_state.build_mapping(ROOT / "engine")
    assert m["paper-book.json"] == ("book", "swing", "books/swing.json")
    assert m["paper_book.json"] == ("book", "swing", "books/swing.json")
    assert m["pm-journal-pullback.json"] == ("journal", "pullback", "journals/pullback.json")
    assert m["pm_journal_current_momentum.json"] == ("journal", "momentum", "journals/momentum.json")
    assert m["latest-scan.json"] == ("scan", None, "scans/latest.json")
    assert m["pm-watch-journal.json"] == ("watch", None, "journals/watch.json")
    assert "paper-book-rotation.json" not in m and "paper_book_orb.json" not in m
    c = seed_state.classify
    assert c("claude/paper-book.json", m) == ("book", "swing", "books/swing.json")
    assert c("scans/2026-09-10-midday.json", m) == ("scan-record", None, "scans/2026-09-10-midday.json")
    assert c("claude/scans/latest-scan.json", m) == ("scan", None, "scans/latest.json")
    assert c("book-history/2026-09-10-midday.json", m) == \
        ("book-history", None, "archive/book-history/2026-09-10-midday.json")
    assert c("health/2026-09-10-x.md", m) is None
    assert c("plans/whatever.json", m) is None


# ---------------------------------------------------------------- seeding
def test_seed_lays_out_the_runner_layout_without_the_broker_block(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    for f in ("books/swing.json", "books/pullback.json", "books/momentum.json",
              "journals/swing.json", "journals/pullback.json", "journals/momentum.json",
              "journals/watch.json", "coverage/pm-coverage.json", "scans/latest.json",
              "scans/2026-09-10-opening-range.json", "scan-index.json", "scan-history.json",
              "archive/book-history/2026-09-10-midday.json", "manifests/seed.json",
              "experiments/ledger.jsonl", "health/.gitkeep", "README.md", ".gitignore"):
        assert (state / f).exists(), f
    revs = _fixture_revisions()
    for desk in DESKS:
        raw = (state / "books" / f"{desk}.json").read_bytes()
        book = json.loads(raw)
        assert "mirrors" not in book and b'"mirrors"' not in raw
        assert book["revision"] == revs[desk]
        assert seed_state.scan_text(raw.decode("utf-8")) == []
        assert runner.json_bytes(book) == raw          # exactly how run.py writes a book
    man = runner.load_json(state / "manifests" / "seed.json")
    assert man["revisions"] == revs
    assert man["engine_sha_hint"] == "423ed33" and "journals/swing.json" in man["engine_sha_hint_from"]
    assert set(man["source_files"]) >= {"paper-book.json", "pm-journal.json", "latest-scan.json",
                                        "scans/2026-09-10-opening-range.json",
                                        "book-history/2026-09-10-midday.json"}
    for rel, digest in man["source_files"].items():
        assert digest == runner.sha256_file(FIX / rel)
    assert man["seeded_at"].endswith("Z") and man["previous_seed"] is None
    ignore = (state / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".runs/" in ignore and ".runner.lock" in ignore and "engine-config.json" in ignore
    readme = (state / "README.md").read_text(encoding="utf-8")
    assert "Private" in readme and "books/swing.json" in readme
    assert (state / "experiments" / "ledger.jsonl").read_bytes() == b""


def test_seed_stages_like_the_runner_and_the_runner_can_read_it(tmp_path):
    """The books the seed writes are the books stage_run copies into a run directory."""
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    run_dir = tmp_path / "run"
    (tmp_path / "no-inputs").mkdir()
    staged = runner.stage_run(ROOT / "engine", state, tmp_path / "no-inputs", run_dir, list(DESKS), "abc")
    assert staged["books"] == list(DESKS) and staged["missing_books"] == []
    assert set(staged["state"]) == {"scans/latest.json", "scan-index.json", "scan-history.json",
                                    "journals/watch.json"}
    assert (run_dir / "paper_book_pullback.json").exists()


def test_seed_refuses_a_mask_that_survives_and_writes_nothing(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(FIX, src)
    book = json.loads((src / "paper-book.json").read_text(encoding="utf-8"))
    book["positions"][0]["note"] = f"account {FAKE_MASK}"          # outside the mirrors block
    (src / "paper-book.json").write_text(json.dumps(book), encoding="utf-8")
    state = tmp_path / "state"
    assert _seed(src, state) == 1
    assert not state.exists()
    # the escaped spelling — what json.dumps produces — is caught too
    book["positions"][0]["note"] = "x"
    (src / "paper-book.json").write_text(json.dumps(book), encoding="utf-8")
    (src / "pm-journal.json").write_text('{"entries": [{"n": "' + FAKE_MASK_ESCAPED + '"}]}', encoding="utf-8")
    assert _seed(src, state) == 1
    assert not state.exists()


def test_seed_refuses_without_a_book(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(FIX / "pm-coverage.json", src / "pm-coverage.json")
    assert _seed(src, tmp_path / "state") == 1
    assert not (tmp_path / "state").exists()


def test_seed_refuses_two_inputs_for_one_book(tmp_path):
    src = tmp_path / "src"
    shutil.copytree(FIX, src)
    shutil.copy(FIX / "paper-book.json", src / "paper_book.json")
    assert _seed(src, tmp_path / "state") == 1


def test_reseed_is_idempotent_and_refuses_to_move_a_book_backwards(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    before = {d: (state / "books" / f"{d}.json").read_bytes() for d in DESKS}
    first = runner.load_json(state / "manifests" / "seed.json")
    # same copy again: allowed, books unchanged, the manifest remembers the first seed
    assert _seed(FIX, state, "--now", "2026-09-11T12:00:00Z") == 0
    assert {d: (state / "books" / f"{d}.json").read_bytes() for d in DESKS} == before
    second = runner.load_json(state / "manifests" / "seed.json")
    assert second["previous_seed"] == first["seeded_at"]
    # an older copy of one book: refused, nothing written (the other books included)
    src = tmp_path / "older"
    shutil.copytree(FIX, src)
    book = json.loads((src / "paper-book-pullback.json").read_text(encoding="utf-8"))
    book["revision"] -= 1
    (src / "paper-book-pullback.json").write_text(json.dumps(book), encoding="utf-8")
    momentum = json.loads((src / "paper-book-momentum.json").read_text(encoding="utf-8"))
    momentum["revision"] += 5
    (src / "paper-book-momentum.json").write_text(json.dumps(momentum), encoding="utf-8")
    assert _seed(src, state) == 1
    assert {d: (state / "books" / f"{d}.json").read_bytes() for d in DESKS} == before
    assert runner.load_json(state / "manifests" / "seed.json") == second
    # a newer copy moves forward
    (src / "paper-book-pullback.json").write_text(
        json.dumps(dict(book, revision=book["revision"] + 1)), encoding="utf-8")
    assert _seed(src, state) == 0
    assert runner.load_json(state / "books" / "momentum.json")["revision"] == momentum["revision"]


def test_revision_guard_holds_even_when_the_book_file_was_deleted(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    (state / "books" / "swing.json").unlink()
    src = tmp_path / "older"
    shutil.copytree(FIX, src)
    book = json.loads((src / "paper-book.json").read_text(encoding="utf-8"))
    book["revision"] -= 3
    (src / "paper-book.json").write_text(json.dumps(book), encoding="utf-8")
    assert _seed(src, state) == 1          # manifests/seed.json still remembers r36
    assert not (state / "books" / "swing.json").exists()


def test_dry_run_writes_nothing(tmp_path, capsys):
    state = tmp_path / "state"
    assert _seed(FIX, state, "--dry-run", "--init-git") == 0
    assert not state.exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "books/swing.json" in out and "revision" in out


def test_init_git_makes_one_commit_on_main_and_never_pushes(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state, "--init-git", "--now", "2026-09-10T20:00:00Z") == 0
    assert (state / ".git").exists()
    assert _git(state, "branch", "--show-current").strip() == "main"
    log = _git(state, "log", "--format=%s").strip().splitlines()
    assert log == ["seed state from project docs 2026-09-10"]
    assert _git(state, "status", "--porcelain").strip() == ""
    assert _git(state, "remote").strip() == ""
    tracked = _git(state, "ls-files").split()
    assert "books/swing.json" in tracked and "manifests/seed.json" in tracked
    assert ".runner.lock" not in tracked
    # a re-seed of the same copy: the books are untouched, only manifests/seed.json moves
    books = {d: _git(state, "rev-parse", f"HEAD:books/{d}.json") for d in DESKS}
    assert _seed(FIX, state, "--init-git", "--now", "2026-09-11T20:05:00Z") == 0
    assert _git(state, "status", "--porcelain").strip() == ""
    assert len(_git(state, "log", "--format=%s").strip().splitlines()) == 2
    assert _git(state, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["manifests/seed.json"]
    assert {d: _git(state, "rev-parse", f"HEAD:books/{d}.json") for d in DESKS} == books


def test_ledger_is_copied_from_the_engine_repo_when_asked(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state, "--ledger", str(ROOT / "experiments")) == 0
    got = (state / "experiments" / "ledger.jsonl").read_bytes()
    assert got == (ROOT / "experiments" / "ledger.jsonl").read_bytes()
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"ok": 1}\nnot json\n', encoding="utf-8")
    assert _seed(FIX, tmp_path / "state2", "--ledger", str(bad)) == 1
    assert not (tmp_path / "state2").exists()


def test_seed_accepts_the_staging_spellings_and_a_claude_prefix(tmp_path):
    src = tmp_path / "src" / "claude"
    src.mkdir(parents=True)
    for name, alias in (("paper-book.json", "paper_book.json"),
                        ("paper-book-pullback.json", "paper_book_pullback.json"),
                        ("paper-book-momentum.json", "paper_book_momentum.json"),
                        ("pm-journal.json", "pm-journal.json"),
                        ("latest-scan.json", "latest-scan.json")):
        shutil.copy(FIX / name, src / alias)
    (src / "plans").mkdir()
    (src / "plans" / "x.md").write_text("not state\n", encoding="utf-8")
    state = tmp_path / "state"
    assert _seed(tmp_path / "src", state) == 0
    for desk in DESKS:
        assert (state / "books" / f"{desk}.json").exists()
    man = runner.load_json(state / "manifests" / "seed.json")
    assert man["skipped_inputs"] == ["claude/plans/x.md"]
    assert (state / "archive" / ".gitkeep").exists()          # nothing else under archive/


# ---------------------------------------------------------------- validating
def test_validate_passes_on_a_seeded_repo(tmp_path, capsys):
    state = tmp_path / "state"
    assert _seed(FIX, state, "--init-git") == 0
    res = validate_state.validate(state, ROOT / "engine")
    assert res["ok"] and res["errors"] == [], res
    assert res["summary"]["books"] == _fixture_revisions()
    assert res["summary"]["coverage_rows"] > 0
    assert validate_state.main(["--state", str(state), "--engine", str(ROOT)]) == 0
    assert "VALIDATE OK" in capsys.readouterr().out


def test_validate_fails_on_a_planted_mask_or_broker_block(tmp_path, capsys):
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    book = runner.load_json(state / "books" / "momentum.json")
    book["mirrors"] = {"display": FAKE_MASK}
    runner.write_json(state / "books" / "momentum.json", book)
    res = validate_state.validate(state, ROOT / "engine")
    assert not res["ok"]
    assert any("masked account number" in e for e in res["errors"])
    assert any("`mirrors` block" in e for e in res["errors"])
    assert validate_state.main(["--state", str(state), "--engine", str(ROOT)]) == 1
    assert "VALIDATE FAIL" in capsys.readouterr().out
    # the mask alone, in a markdown file under health/, is enough
    assert _seed(FIX, tmp_path / "s2") == 0
    (tmp_path / "s2" / "health" / "2026-09-10.md").write_text(f"# ok\n{FAKE_MASK}\n", encoding="utf-8")
    assert validate_state.main(["--state", str(tmp_path / "s2"), "--engine", str(ROOT)]) == 1


def test_validate_catches_layout_json_revision_and_coverage_faults(tmp_path):
    state = tmp_path / "state"
    assert _seed(FIX, state) == 0
    (state / "books" / "pullback.json").unlink()
    (state / "scans" / "broken.json").write_text("{not json", encoding="utf-8")
    swing = runner.load_json(state / "books" / "swing.json")
    swing["revision"] = 1
    runner.write_json(state / "books" / "swing.json", swing)
    cov = runner.load_json(state / "coverage" / "pm-coverage.json")
    day = sorted(cov["days"])[0]
    cov["days"][day]["runs"].append({"ts": "yesterday", "desks": {}, "aborted": True})
    cov["days"]["bad-day"] = {"runs": "nope"}
    runner.write_json(state / "coverage" / "pm-coverage.json", cov)
    (state / "experiments" / "ledger.jsonl").write_text("{}\n{oops\n", encoding="utf-8")
    res = validate_state.validate(state, ROOT / "engine")
    joined = "\n".join(res["errors"])
    assert "books/pullback.json missing" in joined
    assert "scans/broken.json: not valid JSON" in joined
    assert "books/swing.json is at revision 1 but manifests/seed.json saw" in joined
    assert "the coverage rows saw" in joined and "archive/book-history saw" in joined
    assert "not an ISO timestamp" in joined and "no slot" in joined
    assert "aborted without a reason" in joined
    assert "not YYYY-MM-DD" in joined and "no `runs` list" in joined
    assert "ledger.jsonl line 2" in joined
    assert not res["ok"]


def test_validate_warns_but_passes_on_a_sparse_repo(tmp_path):
    state = tmp_path / "state"
    (state / "books").mkdir(parents=True)
    for d in DESKS:
        runner.write_json(state / "books" / f"{d}.json", {"mode": "paper", "revision": 1, "positions": []})
    res = validate_state.validate(state, ROOT / "engine")
    assert res["ok"]
    assert any("coverage/pm-coverage.json absent" in w for w in res["warnings"])
    assert any(".gitignore lacks" in w for w in res["warnings"])


# ---------------------------------------------------------------- the runner over a seeded repo
def test_health_slot_reports_the_state_layout_row(tmp_path):
    state = selftest.make_state_repo(tmp_path / "state")
    row = runner.state_layout_row(state, ROOT)
    assert row["name"] == "state_layout"
    assert row["status"] == "fail"           # the fixture books still carry the broker block
    assert "mirrors" in row["detail"]
    seeded = tmp_path / "seeded"
    assert _seed(FIX, seeded, "--init-git") == 0
    row = runner.state_layout_row(seeded, ROOT)
    assert row["status"] == "pass" and "swing r" in row["detail"]
    assert runner.state_layout_row(tmp_path / "nowhere", ROOT)["status"] == "fail"


def test_a_pm_run_over_the_seeded_repo_keeps_it_valid(tmp_path):
    """The seeded books go through pm.py, come back scrubbed, and the repo still validates."""
    state = tmp_path / "state"
    assert _seed(FIX, state, "--init-git") == 0
    _git(state, "config", "user.email", "t@local")
    _git(state, "config", "user.name", "t")
    now = selftest.synthetic_now("13:15")
    inputs = selftest.write_inputs(tmp_path / "inputs",
                                   {"pm_quotes.json": selftest.fresh_quotes(now),
                                    "scan_results.json": selftest.fresh_scan(now)}, as_of=now)
    code = runner.main(["--slot", "midday", "--desk", "all", "--inputs", str(inputs), "--state", str(state),
                        "--engine", str(ROOT), "--no-push", "--now", runner.iso(now)])
    outcome = runner.load_json(inputs / "outcome.json")
    assert code == 0 and outcome["outcome"] == "committed", outcome
    res = validate_state.validate(state, ROOT / "engine")
    assert res["ok"], res["errors"]
    revs = _fixture_revisions()
    for d in DESKS:
        assert runner.load_json(state / "books" / f"{d}.json")["revision"] >= revs[d]
