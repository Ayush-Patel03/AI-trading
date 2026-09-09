"""Every input a run reads is staged into $SCAN_DIR by the caller (M5).

`os.path.join(BASE, path)` silently honours an absolute path and lets `..` walk out, so a
caller mistake became a read of a file the run never staged — or, more usually, a crash
deep inside the run instead of the graceful "treated as absent" path every caller already
handles. A trading engine reading a file nobody staged is not a small bug.
"""
import json


def test_a_staged_file_still_loads(run_dir, pm):
    (run_dir / "probe.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    assert pm._load("probe.json") == {"ok": True}


def test_a_nested_staged_file_still_loads(run_dir, pm):
    (run_dir / "sub").mkdir()
    (run_dir / "sub" / "probe.json").write_text(json.dumps([1, 2]), encoding="utf-8")
    assert pm._load("sub/probe.json") == [1, 2]


def test_an_absolute_path_outside_the_run_dir_is_treated_as_absent(run_dir, pm, tmp_path_factory):
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.json"
    outside.write_text(json.dumps({"do not": "read me"}), encoding="utf-8")
    assert pm._load(str(outside), default="ABSENT") == "ABSENT"


def test_a_dotdot_escape_is_treated_as_absent(run_dir, pm):
    (run_dir.parent / "escaped.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert pm._load("../escaped.json", default="ABSENT") == "ABSENT"


def test_a_missing_file_still_returns_the_default(run_dir, pm):
    assert pm._load("never_staged.json", default={"d": 1}) == {"d": 1}


def test_a_non_string_path_is_absent_rather_than_a_crash(run_dir, pm):
    for junk in (None, 7, [], ""):
        assert pm._load(junk, default="ABSENT") == "ABSENT"


def test_an_absolute_path_INSIDE_the_run_dir_is_allowed(run_dir, pm):
    """The rule is containment, not a ban on absolute paths — a caller that passes the
    fully-qualified staged path is doing nothing wrong."""
    (run_dir / "probe.json").write_text(json.dumps({"ok": 1}), encoding="utf-8")
    assert pm._load(str(run_dir / "probe.json")) == {"ok": 1}
