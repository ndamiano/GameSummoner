"""Starting a build over an EMPTY game folder.

A second attempt at the same prompt used to open on the dead build's half-written files. The model
reads them, believes them, and re-asks for art it already has under new ids — measured 2026-08-01:
three naming schemes for one cast, 40 renders, no finished game. `fresh` is the from-scratch button;
a plain re-trigger, a resume and a fix all still carry the folder forward.
"""

import pytest

from db import games
from maestro.codegen import build_chain, build_state, staging


@pytest.fixture
def run(tmp_path, monkeypatch):
    """A run whose last build left files behind, with everything past the folder stubbed out."""
    games.create_game(str(tmp_path), "u1")
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "advance", lambda *a, **k: None)
    monkeypatch.setattr(build_chain.RunState, "read_spec", lambda self: {"request": "a game"})
    monkeypatch.setattr(build_chain.snapshots, "take", lambda *a, **k: "abc123")
    monkeypatch.setattr(build_chain.jobs, "abandon_pending_batch_jobs", lambda *a, **k: 0)
    d = tmp_path / "game"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<html>old</html>", encoding="utf-8")
    (d / "story.js").write_text("const STORY = {}", encoding="utf-8")
    (d / "assets.json").write_text('{"images": [{"id": "elara-portrait"}]}', encoding="utf-8")
    (d / "assets" / "elara-portrait.png").write_bytes(b"\x89PNG")
    return str(tmp_path)


def test_a_fresh_build_opens_on_an_empty_folder(run, tmp_path):
    build_chain.start_build(run, "b2", fresh=True)
    left = {p.name for p in (tmp_path / "game").rglob("*") if p.is_file()}
    assert "index.html" not in left and "story.js" not in left
    assert "assets.json" not in left and "elara-portrait.png" not in left


def test_a_fresh_build_still_gets_the_renderer(run, tmp_path):
    """The folder is emptied, then seeded — a 3D game can only import three.js from beside it."""
    build_chain.start_build(run, "b2", fresh=True)
    assert (tmp_path / "game" / "three.module.js").exists()


def test_a_plain_rebuild_keeps_what_is_there(run, tmp_path):
    build_chain.start_build(run, "b2")
    assert (tmp_path / "game" / "index.html").read_text(encoding="utf-8") == "<html>old</html>"


def test_a_fix_never_empties_the_folder(run, tmp_path):
    """A fix edits code that already works — there is nothing it may start over from."""
    build_chain.start_build(run, "b2", kind="fix", note="the player falls through the floor")
    assert (tmp_path / "game" / "story.js").exists()


def test_what_a_fresh_build_deletes_is_snapshotted_first(run, tmp_path, monkeypatch):
    """git is the only copy left afterwards, so the commit has to happen before the delete."""
    seen = []
    monkeypatch.setattr(build_chain.snapshots, "take",
                        lambda run_dir, label: seen.append((label, (tmp_path / "game" / "index.html").exists())))
    build_chain.start_build(run, "b2", fresh=True)
    assert seen == [("before-rebuild", True)]


def test_a_fresh_build_stops_the_art_the_last_one_is_still_waiting_on(run, tmp_path, monkeypatch):
    """A render still in flight would land in the new build's folder and write itself into a
    manifest that no longer asked for it."""
    seen = []
    monkeypatch.setattr(build_chain.jobs, "abandon_pending_batch_jobs",
                        lambda run_id, err: seen.append(run_id) or 1)
    build_chain.start_build(run, "b2", fresh=True)
    assert seen == [run]


def test_a_first_build_has_nothing_to_clear(tmp_path, monkeypatch):
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "advance", lambda *a, **k: None)
    monkeypatch.setattr(build_chain.RunState, "read_spec", lambda self: {"request": "a game"})
    taken = []
    monkeypatch.setattr(build_chain.snapshots, "take", lambda *a, **k: taken.append(a))
    build_chain.start_build(str(tmp_path), "b1", fresh=True)
    assert taken == []
    assert (tmp_path / "game" / "three.module.js").exists()


def test_the_seeded_renderer_alone_is_not_a_game(tmp_path):
    """What the from-scratch button is offered for: files the MODEL wrote, not the seed."""
    staging.seed_vendor(tmp_path)
    assert staging.has_authored_files(tmp_path) is False
    (tmp_path / "game" / "index.html").write_text("<html></html>", encoding="utf-8")
    assert staging.has_authored_files(tmp_path) is True


def test_a_run_with_no_folder_has_no_game(tmp_path):
    assert staging.has_authored_files(tmp_path) is False


def test_a_change_keeps_the_folder_and_asks_for_the_change(run, tmp_path):
    """A change note is a person's 'let's change X' on a game that plays — it edits in place, the
    request names it as a change and not a defect, and the folder is snapshotted first."""
    build_id = games.create_build(run, kind="change")
    build_chain.start_build(run, build_id, kind="change", note="make the enemies twice as fast")
    assert (tmp_path / "game" / "story.js").exists()
    cursor = build_state.load(run)
    assert "asked for this change" in cursor.request
    assert "make the enemies twice as fast" in cursor.request
    assert "fix" not in cursor.request
