import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "engine"))
import config


def test_engine_sha_absent_is_none(tmp_path):
    assert config.engine_sha(str(tmp_path)) is None


def test_engine_sha_is_read_and_stripped(tmp_path):
    (tmp_path / "engine_sha").write_text("a1b2c3d\n", encoding="utf-8")
    assert config.engine_sha(str(tmp_path)) == "a1b2c3d"


def test_full_length_sha_is_accepted(tmp_path):
    full = "0123456789abcdef0123456789abcdef01234567"
    (tmp_path / "engine_sha").write_text(full, encoding="utf-8")
    assert config.engine_sha(str(tmp_path)) == full


def test_engine_sha_ignores_a_junk_file(tmp_path):
    # A truncated clone or a stray file must not be stamped onto a decision.
    (tmp_path / "engine_sha").write_text("not a sha, this is prose\n" * 50, encoding="utf-8")
    assert config.engine_sha(str(tmp_path)) is None
