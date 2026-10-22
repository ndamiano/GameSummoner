"""A run's game folder has a git history: one commit per playable finalize, one before each fix.

The regression this exists for: a fix round re-edits code that already worked, and nothing else on
disk holds the version that did.
"""

import pytest

from db import games
from maestro.codegen import build_chain, build_state, build_steps, snapshots
from maestro.codegen.build_state import BuildCursor
from maestro.codegen.staging import game_dir


class _Run:
    """RunState, as much of it as the driver touches."""
    def __init__(self, run_dir):
        self.run_dir = run_dir

    def read_spec(self):
        return {"request": "make a game"}


def _write(run_dir, name, body):
    p = game_dir(run_dir) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture
def run(tmp_path):
    _write(tmp_path, "index.html", "<h1>v1</h1>")
    _write(tmp_path, "game.js", "const v = 1")
    return tmp_path


def test_a_snapshot_is_a_commit_labelled_by_what_took_it(run):
    commit = snapshots.take(run, "built")

    (row,) = snapshots.list_snapshots(run)
    assert (row["id"], row["label"]) == (commit, "built")


def test_the_repo_stays_out_of_the_folder_the_model_sees(run):
    """The game folder is listed, staged and served as written — a `.git` in it would show up in
    the model's first list_files and in every staged copy."""
    snapshots.take(run, "built")

    assert (run / "game.git" / "HEAD").exists()
    assert not (game_dir(run) / ".git").exists()
    assert sorted(p.name for p in game_dir(run).iterdir()) == ["game.js", "index.html"]


def test_a_run_with_no_game_has_nothing_to_keep(tmp_path):
    assert snapshots.take(tmp_path, "built") is None
    assert snapshots.list_snapshots(tmp_path) == []


def test_an_unchanged_folder_does_not_make_a_second_commit(run):
    snapshots.take(run, "built")

    assert snapshots.take(run, "before-fix") is None
    assert len(snapshots.list_snapshots(run)) == 1


def test_restore_puts_the_game_back(run):
    built = snapshots.take(run, "built")
    _write(run, "game.js", "const v = 2 // broken by a fix")

    snapshots.restore(run, built)

    assert (game_dir(run) / "game.js").read_text() == "const v = 1"


def test_restore_removes_a_file_the_snapshot_never_had(run):
    """A fix that broke the game by ADDING a file is only undone by taking it away."""
    built = snapshots.take(run, "built")
    _write(run, "boot.js", "throw new Error('boom')")

    snapshots.restore(run, built)

    assert not (game_dir(run) / "boot.js").exists()


def test_the_art_rides_along(run):
    """Restoring code to one version and leaving the art at another is not a restore."""
    _write(run, "assets/goblin.png", "PNG-v1")
    built = snapshots.take(run, "built")
    _write(run, "assets/goblin.png", "PNG-v2")

    snapshots.restore(run, built)

    assert (game_dir(run) / "assets" / "goblin.png").read_text() == "PNG-v1"


def test_restoring_an_unknown_snapshot_says_so(run):
    snapshots.take(run, "built")
    with pytest.raises(ValueError, match="deadbee"):
        snapshots.restore(run, "deadbee")


def test_a_run_with_no_history_cannot_be_restored(run):
    with pytest.raises(ValueError, match="no snapshots"):
        snapshots.restore(run, "HEAD")


def test_snapshots_read_oldest_first(run):
    first = snapshots.take(run, "built")
    _write(run, "game.js", "const v = 2")
    second = snapshots.take(run, "before-fix")

    assert [s["id"] for s in snapshots.list_snapshots(run)] == [first, second]


def test_a_missing_git_does_not_end_a_build(run, monkeypatch):
    """Losing history is not a reason to lose a game."""
    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(snapshots.subprocess, "run", no_git)
    assert snapshots.take(run, "built") is None


def test_a_playable_finalize_keeps_the_game(run, monkeypatch):
    monkeypatch.setattr(build_chain, "stage_for_play", lambda *a: None)
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "_post_finalize", lambda *a: None)
    monkeypatch.setattr(build_chain.build_steps, "step", lambda *a, **k: build_steps.Done("done"))
    monkeypatch.setattr(build_chain, "RunState", lambda rid: _Run(run))
    games.create_game("g1", "u1")
    games.move("g1", "running", "building")
    build_state.save(BuildCursor(build_id=games.create_build("g1"), step=1, finished=True))

    build_chain.advance("g1", {"choices": []})

    assert [s["label"] for s in snapshots.list_snapshots(run)] == ["built"]


def test_a_fix_keeps_the_game_before_it_edits(run, monkeypatch):
    """The version a fix is about to change is the one worth keeping."""
    monkeypatch.setattr(build_chain, "advance", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "_seed", lambda rs: None)
    monkeypatch.setattr(build_chain, "RunState", lambda rid: _Run(run))

    build_chain.start_build(str(run), "b1", kind="fix", note="the door does not open")

    assert [s["label"] for s in snapshots.list_snapshots(run)] == ["before-fix"]
