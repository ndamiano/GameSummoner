"""When a build is allowed to say it is finished.

The gates run AFTER a build finalizes, and a gate that finds an uncaught exception kicks a fix
build. Announcing `built` before they have run shows a person a finished game that cuts back to
mending seconds later — on the web page that is the whole of what they see, and they may be
watching the build happen. So a gated build stages (the gate opens the staged game) and says
nothing until the chain settles.
"""

import pytest

from maestro.codegen import build_chain, build_state
from maestro.codegen.build_state import BuildCursor
from maestro.state import RunState


@pytest.fixture
def seen(monkeypatch):
    out = {"status": [], "events": [], "payloads": [], "finished": [], "archived": [],
           "threads": [], "staged": []}

    def _emit(ev, run_id, **kw):
        out["events"].append(ev)
        out["payloads"].append(kw)
    monkeypatch.setattr(build_chain, "_emit", _emit)
    monkeypatch.setattr(build_chain.games, "set_status",
                        lambda run_id, s: out["status"].append(s))
    monkeypatch.setattr(build_chain.games, "build_finished",
                        lambda bid, s, steps=None: out["finished"].append(s))
    monkeypatch.setattr(build_chain, "stage_for_play", lambda run_dir, slug: out["staged"].append(slug))
    monkeypatch.setattr(build_chain.snapshots, "take", lambda *a, **k: None)
    monkeypatch.setattr(build_chain.artifact_screen, "screen_artifact", lambda *a, **k: None)

    class _Thread:                       # the gates are driven by hand, so the test is not a race
        def __init__(self, target=None, args=(), daemon=None):
            out["threads"].append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(build_chain.threading, "Thread", _Thread)
    return out


def _gates(monkeypatch, *, error=False, play=False, raises=False):
    def _err(run_id, build_id):
        if raises:
            raise RuntimeError("chromium would not start")
        return error
    monkeypatch.setattr(build_chain.error_gate, "after_build", _err)
    monkeypatch.setattr(build_chain.play_gate, "after_build", lambda r, b: play)


def _run(tmp_path):
    """The run id IS the run dir here, because `_settle` reopens the run by id."""
    (tmp_path / "game").mkdir(exist_ok=True)
    (tmp_path / "game" / "index.html").write_text("<html></html>", encoding="utf-8")
    return str(tmp_path), RunState(tmp_path), BuildCursor(build_id="b1", step=29)


def test_a_gated_build_says_nothing_until_the_gates_have_run(tmp_path, seen):
    rid, rs, cursor = _run(tmp_path)
    build_chain._finalize(rid, rs, cursor, ok=True)
    assert build_state.load(tmp_path).phase == "checking"
    assert seen["status"] == [] and seen["events"] == []
    assert seen["finished"] == ["succeeded"]      # the BUILD is over; the chain is not


def test_the_chain_settles_when_no_gate_kicks_a_fix(tmp_path, seen, monkeypatch):
    rid, rs, cursor = _run(tmp_path)
    _gates(monkeypatch)
    monkeypatch.setattr("maestro.codegen.archive.archive", lambda run_id: None)
    build_chain._finalize(rid, rs, cursor, ok=True)
    build_chain._post_finalize(rid, "b1")
    assert build_state.load(tmp_path).phase == "done"
    assert seen["status"] == ["built"] and seen["events"] == ["build_done"]


def test_a_gate_that_kicks_a_fix_leaves_the_end_to_the_fix(tmp_path, seen, monkeypatch):
    """The fix build finalizes through this same path, so the chain is announced once, by whichever
    build actually ends it."""
    rid, rs, cursor = _run(tmp_path)
    _gates(monkeypatch, error=True)
    build_chain._finalize(rid, rs, cursor, ok=True)
    build_chain._post_finalize(rid, "b1")
    assert seen["status"] == [] and seen["events"] == []
    assert build_state.load(tmp_path).phase == "checking"


def test_a_gate_that_raises_still_ends_the_run(tmp_path, seen, monkeypatch):
    """Otherwise the page shows a build that never finishes and the CLI never returns."""
    rid, rs, cursor = _run(tmp_path)
    _gates(monkeypatch, raises=True)
    build_chain._finalize(rid, rs, cursor, ok=True)
    with pytest.raises(RuntimeError):
        build_chain._post_finalize(rid, "b1")
    assert build_state.load(tmp_path).phase == "done"
    assert seen["status"] == ["built"] and seen["events"] == ["build_done"]


def test_a_build_stopped_by_hand_is_announced_at_once(tmp_path, seen):
    """No gate runs on a build a person ended, so there is nothing to wait for."""
    rid, rs, cursor = _run(tmp_path)
    build_chain._finalize(rid, rs, cursor, ok=True, attempt="stopped")
    assert build_state.load(tmp_path).phase == "done"
    assert seen["status"] == ["built"] and seen["events"] == ["build_done"]
    assert seen["threads"] == []


def test_a_failed_build_is_announced_at_once_and_still_staged_when_playable(tmp_path, seen):
    rid, rs, cursor = _run(tmp_path)
    build_chain._finalize(rid, rs, cursor, ok=False)
    assert build_state.load(tmp_path).phase == "done"
    assert seen["status"] == ["failed"] and seen["events"] == ["build_done"]
    assert seen["staged"] == [rid] and seen["finished"] == ["failed"]
    assert seen["payloads"][0]["ok"] is False


def test_a_failed_build_with_no_entry_point_stages_nothing(tmp_path, seen):
    rid, rs, cursor = _run(tmp_path)
    (tmp_path / "game" / "index.html").unlink()
    build_chain._finalize(rid, rs, cursor, ok=False)
    assert seen["staged"] == [] and seen["status"] == ["failed"]


def test_running_out_of_compute_mid_build_stages_what_was_made(tmp_path, seen, monkeypatch):
    rid, rs, cursor = _run(tmp_path)

    def _refuse(*a, **k):
        raise build_chain.errors.InsufficientCompute("g1", 0, 1)
    monkeypatch.setattr(build_chain.jobs, "enqueue_job", _refuse)
    monkeypatch.setattr(build_chain.jobs, "abandon_pending_batch_jobs", lambda *a: 0)
    monkeypatch.setattr(build_chain, "get_connector", lambda: type(
        "C", (), {"build_llm_job": lambda self, *a: ({}, "m")})())
    build_chain._enqueue_turn(rid, cursor, build_chain.build_steps.Infer(
        messages=[], schemas=[], max_tokens=1, report=""))
    assert build_state.load(tmp_path).phase == "done"
    assert seen["staged"] == [rid] and seen["status"] == ["failed"]
    assert seen["payloads"][0]["error"].startswith("compute exhausted")


@pytest.mark.parametrize("turn, finished, want", [
    (200, False, "succeeded"),
    (37, False, "failed"),
    (37, True, "succeeded"),
])
def test_only_a_build_that_gave_up_early_fails(tmp_path, seen, monkeypatch, turn, finished, want):
    rid, rs, cursor = _run(tmp_path)
    cursor.turn, cursor.max_steps, cursor.finished = turn, 200, finished
    build_state.save(tmp_path, cursor)
    monkeypatch.setattr(build_chain, "build_tools", lambda rs, build_id: None)
    monkeypatch.setattr(build_chain.build_steps, "step",
                        lambda *a, **k: build_chain.build_steps.Done("ended"))
    build_chain._advance_locked(rid, None)
    assert seen["finished"] == [want] and seen["staged"] == [rid]

