"""The artifact text gate: the finished game's own text is screened at finalize, and a hit HOLDS
the build — no staging, no snapshot — with a violation row for the admin panel. Same narrow screen
as every other seam: synthetic CSAM-combination fixtures only, mature themes pass."""

import pytest

from db import events
from maestro.codegen import build_state
from maestro.codegen.artifact_screen import screen_artifact
from maestro.codegen.build_state import BuildCursor

FLAGGED = "dialogue: a nude schoolgirl appears"


def _game(tmp_path, files: dict):
    d = tmp_path / "game"
    d.mkdir(exist_ok=True)
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


def test_a_clean_game_passes(tmp_path):
    _game(tmp_path, {"index.html": "<html>a dark revenge story</html>",
                     "main.js": "const hero = 'grizzled'"})
    assert screen_artifact(tmp_path) is None


def test_a_hit_names_the_file_and_the_violation(tmp_path):
    _game(tmp_path, {"index.html": "<html></html>", "story.js": FLAGGED})
    path, violation = screen_artifact(tmp_path)
    assert path == "story.js"
    assert violation.category == "csam_combination"


def test_the_combination_is_per_file_not_across_files(tmp_path):
    """One half per file is the innocent case — a kids' game with an unrelated adult reference
    elsewhere must not combine across file boundaries."""
    _game(tmp_path, {"a.js": "a schoolgirl walks to class", "b.js": "a steamy nude painting"})
    assert screen_artifact(tmp_path) is None


def test_the_vendored_renderer_is_not_the_games_text(tmp_path):
    _game(tmp_path, {"three.module.js": FLAGGED})
    assert screen_artifact(tmp_path) is None


def test_binary_suffixes_are_not_read(tmp_path):
    d = _game(tmp_path, {"index.html": "<html></html>"})
    (d / "assets").mkdir()
    (d / "assets" / "sprite.webp").write_bytes(FLAGGED.encode())
    assert screen_artifact(tmp_path) is None


def test_a_missing_game_folder_passes(tmp_path):
    assert screen_artifact(tmp_path) is None




@pytest.fixture
def held_env(monkeypatch, tmp_runs):
    from db import games
    from maestro.codegen import build_chain
    from maestro.state import RunState

    seen = {"staged": False, "snapped": False, "events": []}
    monkeypatch.setattr(build_chain, "_emit",
                        lambda kind, run_id, **kw: seen["events"].append(kind))
    monkeypatch.setattr(build_chain, "stage_for_play",
                        lambda *a, **k: seen.__setitem__("staged", True))
    monkeypatch.setattr(build_chain.snapshots, "take",
                        lambda *a, **k: seen.__setitem__("snapped", True))
    monkeypatch.setattr(build_chain.threading, "Thread", lambda **k: type(
        "T", (), {"start": lambda self: None})())
    games.create_game("g1", "u1")
    games.move("g1", "running", "building")
    bid = games.create_build("g1", kind="build")
    games.build_started(bid)
    cursor = BuildCursor(build_id=bid, step=5)
    build_state.save(cursor)
    return build_chain, cursor, RunState("g1"), seen


def _end(build_chain, cursor, rs, how):
    if how == "finalize":
        build_chain._finalize("g1", rs, cursor, ok=True)
    else:
        build_chain.pause("g1")


@pytest.mark.parametrize("how", ["finalize", "pause"])
def test_flagged_text_holds_the_game_and_records_the_violation(held_env, how):
    from db import games
    build_chain, cursor, rs, seen = held_env
    _game(rs.run_dir, {"index.html": f"<html>{FLAGGED}</html>"})
    _end(build_chain, cursor, rs, how)
    assert games.game("g1")["status"] == "held"
    assert [b["status"] for b in games.builds_for("g1")] == ["held"]
    assert seen["staged"] is False and seen["snapped"] is False
    assert seen["events"] == ["build_done"] and not build_state.load("g1").ok
    rows = events.list_violations()
    assert rows and rows[0]["source"] == "artifact:index.html"
    assert rows[0]["category"] == "csam_combination"


@pytest.mark.parametrize("how, where", [("finalize", ("checking", "running")),
                                        ("pause", ("building", "paused"))])
def test_a_clean_game_is_staged(held_env, how, where):
    from db import games
    build_chain, cursor, rs, seen = held_env
    _game(rs.run_dir, {"index.html": "<html>a space pirate game</html>"})
    _end(build_chain, cursor, rs, how)
    assert seen["staged"] is True
    assert (games.game("g1")["stage"], games.game("g1")["status"]) == where
