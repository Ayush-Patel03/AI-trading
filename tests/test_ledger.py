"""The experiment ledger — the multiple-testing counter must count, and must never rewrite."""
import json

import pytest

from conftest import ENGINE


@pytest.fixture
def lg(run_dir):
    import importlib
    import ledger as l
    importlib.reload(l)
    return l


def _row(i, **kw):
    base = {"id": f"E{i}", "hypothesis": f"trial {i}", "config_diff": {"w": i},
            "harness_cmd": ["backtest.py", "--every", "5"],
            "window": {"start": "2024-08-21", "end": "2026-08-28"},
            "universe": {"name": "bars.json", "n_symbols": 69}, "n": 100 * i,
            "horizons": [5, 10, 20], "in_sample": {"20": {"ic_mean": 0.01 * i}},
            "out_of_sample": None}
    base.update(kw)
    return base


def test_append_increments_the_trial_counter(lg, tmp_path):
    p = tmp_path / "exp" / "ledger.jsonl"
    a = lg.append(str(p), _row(1))
    b = lg.append(str(p), _row(2))
    c = lg.append(str(p), _row(3))
    assert [a["n_trials_to_date"], b["n_trials_to_date"], c["n_trials_to_date"]] == [1, 2, 3]
    assert lg.count(str(p)) == 3


def test_missing_file_is_an_empty_ledger(lg, tmp_path):
    p = str(tmp_path / "nothing.jsonl")
    assert lg.read(p) == []
    assert lg.count(p) == 0
    assert "empty" in lg.table(p)


def test_read_returns_rows_in_order_with_every_schema_key(lg, tmp_path):
    p = str(tmp_path / "l.jsonl")
    lg.append(p, _row(1))
    lg.append(p, {"hypothesis": "bare"})          # minimal row: everything else filled
    rows = lg.read(p)
    assert [r["id"] for r in rows][0] == "E1"
    for r in rows:
        assert set(lg.TRIAL_KEYS) <= set(r)
    auto = rows[1]
    assert auto["id"].startswith("X-") and auto["id"].endswith("-2")
    assert auto["decision"] is None and auto["out_of_sample"] is None
    assert auto["date"] and len(auto["date"]) == 10


def test_engine_sha_is_stamped_when_the_clone_wrote_one(lg, run_dir, tmp_path):
    (run_dir / "engine_sha").write_text("abc1234\n", encoding="utf-8")
    import config
    config._CACHE.clear()
    row = lg.append(str(tmp_path / "l.jsonl"), _row(1))
    assert row["engine_sha"] == "abc1234"


def test_decisions_are_appended_never_rewritten(lg, tmp_path):
    p = tmp_path / "l.jsonl"
    lg.append(str(p), _row(1))
    lg.append(str(p), _row(2))
    before = p.read_text(encoding="utf-8")
    lg.set_decision(str(p), "E1", "not promoted")
    after = p.read_text(encoding="utf-8")
    assert after.startswith(before), "an existing line changed — the ledger is append-only"
    lines = [json.loads(l) for l in after.splitlines() if l.strip()]
    assert lines[-1] == {"kind": "decision", "ref": "E1", "decision": "not promoted",
                         "date": lines[-1]["date"]}
    # decision rows are not trials, and the counter does not move
    assert lg.count(str(p)) == 2
    assert lg.append(str(p), _row(3))["n_trials_to_date"] == 3
    # but a reader sees the decision on the trial, and the latest one wins
    lg.set_decision(str(p), "E1", "promoted to hold-out")
    by_id = {t["id"]: t for t in lg.trials(str(p))}
    assert by_id["E1"]["decision"] == "promoted to hold-out"
    assert by_id["E2"]["decision"] is None


def test_a_decision_for_an_unknown_trial_is_refused(lg, tmp_path):
    p = str(tmp_path / "l.jsonl")
    lg.append(p, _row(1))
    with pytest.raises(SystemExit) as e:
        lg.set_decision(p, "E99", "whatever")
    assert "REFUSED" in str(e.value)
    assert lg.count(p) == 1


def test_a_corrupt_line_is_refused_not_skipped(lg, tmp_path):
    p = tmp_path / "l.jsonl"
    lg.append(str(p), _row(1))
    with open(p, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    with pytest.raises(SystemExit):
        lg.read(str(p))


def test_cli_list_prints_a_table(lg, tmp_path, capsys):
    p = str(tmp_path / "l.jsonl")
    lg.append(p, _row(1))
    lg.append(p, _row(2))
    assert lg.main(["--path", p, "--set-decision", "E2", "slice of E1"]) == 0
    assert lg.main(["--path", p, "--list"]) == 0
    out = capsys.readouterr().out
    assert "E1" in out and "E2" in out
    assert "slice of E1" in out
    assert "(none yet)" in out
    assert "2 trial(s) to date" in out
    assert "20d IC +0.010" in out


def test_cli_refuses_to_do_nothing(lg, tmp_path):
    with pytest.raises(SystemExit):
        lg.main(["--path", str(tmp_path / "l.jsonl")])


def test_the_backfilled_ledger_in_the_repo_reads_and_counts_four(lg):
    p = ENGINE.parent / "experiments" / "ledger.jsonl"
    assert p.exists()
    rows = lg.trials(str(p))
    assert [r["n_trials_to_date"] for r in rows] == [1, 2, 3, 4]
    assert rows[0]["id"] == "BT-2026-09-10" and rows[0]["n"] == 6834
    assert all(r["decision"] for r in rows), "backfilled trials all carry a decision"
    assert all(r["out_of_sample"] is None for r in rows)
