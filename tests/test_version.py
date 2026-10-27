import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools import version


def test_a_shipped_revision_file_wins_and_git_answers_without_one(tmp_path, monkeypatch):
    monkeypatch.setattr(version, "_REVISION", tmp_path / "REVISION")
    version.maestro_rev.cache_clear()
    assert re.fullmatch(r"[0-9a-f]{7,}(-dirty)?", version.maestro_rev())

    (tmp_path / "REVISION").write_text("abc1234\n")
    version.maestro_rev.cache_clear()
    assert version.maestro_rev() == "abc1234"
    version.maestro_rev.cache_clear()
