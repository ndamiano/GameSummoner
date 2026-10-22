import subprocess
import sys

import pytest

from db import games
from maestro.codegen import build_chain, build_state, turn_log
from maestro.codegen.build_state import BuildCursor
from maestro.state import RunState


@pytest.fixture
def seen(monkeypatch):
    out = {"events": [], "payloads": [], "archived": [], "threads": [], "staged": []}

    def _emit(ev, run_id, **kw):
        out["events"].append(ev)
        out["payloads"].append(kw)
    monkeypatch.setattr(build_chain, "_emit", _emit)
    monkeypatch.setattr(build_chain, "stage_for_play",
                        lambda run_dir, slug: out["staged"].append(slug))
    monkeypatch.setattr(build_chain.snapshots, "take", lambda *a, **k: None)
    monkeypatch.setattr("maestro.codegen.archive.archive",
                        lambda run_id: out["archived"].append(run_id))

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            self.call = (target, args)

        def start(self):
            out["threads"].append(self.call)

    monkeypatch.setattr(build_chain.threading, "Thread", _Thread)
    return out


def _gates(monkeypatch, *, error=False, play=False, raises=False):
    def _err(run_id, build_id):
        if raises:
            raise RuntimeError("chromium would not start")
        return error
    monkeypatch.setattr(build_chain.error_gate, "after_build", _err)
    monkeypatch.setattr(build_chain.play_gate, "after_build", lambda r, b: play)


def _run(run_id="g1", *, stage="building", status="running", playable=True):
    games.create_game(run_id, "u1")
    games.charge_game(run_id, 10, 10_000_000)
    games.move(run_id, status, stage)
    RunState(run_id).write_spec({"ask": "frogs", "title": "frogs", "request": "a frog game"})
    bid = games.create_build(run_id, kind="build")
    games.build_started(bid)
    cursor = BuildCursor(build_id=bid, step=29)
    build_state.save(cursor)
    if playable:
        d = RunState(run_id).run_dir / "game"
        d.mkdir(exist_ok=True)
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
    return run_id, RunState(run_id), cursor


def _where(run_id):
    g = games.game(run_id)
    return g["stage"], g["status"]


def _build_rows(run_id):
    return [b["status"] for b in games.builds_for(run_id)]


def test_a_good_build_goes_to_its_gates_once_and_says_nothing_yet(tmp_runs, seen):
    rid, rs, cursor = _run()
    build_chain._finalize(rid, rs, cursor, ok=True)
    build_chain._finalize(rid, rs, build_state.load(rid), ok=True)
    assert _where(rid) == ("checking", "running")
    assert seen["events"] == [] and _build_rows(rid) == ["succeeded"]
    assert seen["threads"] == [(build_chain._post_finalize, (rid, cursor.build_id))]


def test_a_re_drive_while_the_gates_run_takes_no_step(tmp_runs, seen, monkeypatch):
    rid, rs, cursor = _run(stage="checking")

    def boom(*a, **k):
        raise AssertionError("stepped while checking")
    monkeypatch.setattr(build_chain.build_steps, "step", boom)
    build_chain.advance(rid)
    build_chain.on_completion(rid, cursor.build_id, {"choices": []}, None, "j1", 1.0)
    assert seen["threads"] == [] and _where(rid) == ("checking", "running")


@pytest.mark.parametrize("gates, want, events, archived", [
    (dict(), ("checking", "succeeded"), ["build_done"], ["g1"]),
    (dict(error=True), ("checking", "running"), [], []),
    (dict(play=True), ("checking", "running"), [], []),
])
def test_the_chain_settles_only_when_no_gate_kicks_a_fix(tmp_runs, seen, monkeypatch, gates,
                                                         want, events, archived):
    rid, rs, cursor = _run()
    _gates(monkeypatch, **gates)
    build_chain._finalize(rid, rs, cursor, ok=True)
    build_chain._post_finalize(rid, cursor.build_id)
    assert _where(rid) == want
    assert seen["events"] == events and seen["archived"] == archived


def test_a_gate_that_raises_still_ends_the_run(tmp_runs, seen, monkeypatch):
    rid, rs, cursor = _run()
    _gates(monkeypatch, raises=True)
    build_chain._finalize(rid, rs, cursor, ok=True)
    with pytest.raises(RuntimeError):
        build_chain._post_finalize(rid, cursor.build_id)
    assert _where(rid) == ("checking", "succeeded") and seen["events"] == ["build_done"]


def test_a_gate_that_fails_because_of_a_pause_leaves_the_game_checking(tmp_runs, seen, monkeypatch):
    rid, rs, cursor = _run(stage="checking")

    def _paused_mid_gate(run_id, build_id):
        games.move(run_id, "paused")
        raise RuntimeError("the gate's job was cancelled")
    monkeypatch.setattr(build_chain.error_gate, "after_build", _paused_mid_gate)
    build_chain._post_finalize(rid, cursor.build_id)
    assert _where(rid) == ("checking", "paused") and seen["events"] == []


def test_a_done_that_lands_while_paused_waits_at_the_gates_without_running_them(tmp_runs, seen,
                                                                                monkeypatch):
    rid, rs, cursor = _run(status="paused")
    monkeypatch.setattr(build_chain, "build_tools", lambda rs, build_id: None)
    monkeypatch.setattr(build_chain.build_steps, "step",
                        lambda *a, **k: build_chain.build_steps.Done("done"))
    build_state.save(BuildCursor(build_id=cursor.build_id, step=29, finished=True))
    build_chain.advance(rid, {"choices": []})
    assert _where(rid) == ("checking", "paused") and seen["threads"] == []


@pytest.mark.parametrize("status, after_current, started", [
    ("running", True, True),
    ("paused", True, False),
    ("running", False, False),
])
def test_a_gates_fix_starts_only_for_the_build_it_checked_on_a_running_game(
        tmp_runs, seen, monkeypatch, status, after_current, started):
    rid, rs, cursor = _run(stage="checking", status=status)
    monkeypatch.setattr(build_chain, "advance", lambda *a, **k: None)
    fix = build_chain.kickoff(rid, kind="fix", note="the frog never jumps",
                              after=cursor.build_id if after_current else "an-older-build")
    assert (fix is not None) is started
    assert len(games.builds_for(rid)) == (2 if started else 1)
    assert _where(rid) == (("building", "running") if started else ("checking", status))


def test_a_turn_from_a_replaced_build_is_dropped_not_applied(tmp_runs, seen, monkeypatch):
    rid, rs, old = _run()
    new = games.create_build(rid, kind="fix")
    games.finish_open_builds(rid, "stopped", keep=new)
    games.build_started(new)
    build_state.save(BuildCursor(build_id=new, kind="fix", step=1))
    before = build_state.load(rid)

    def boom(*a, **k):
        raise AssertionError("applied a turn from the replaced build")
    monkeypatch.setattr(build_chain.build_steps, "step", boom)
    build_chain.on_completion(rid, old.build_id, {"choices": []}, None, "late-job", 3.0)

    assert build_state.load(rid) == before
    assert turn_log.read_turn(rs.run_dir, "late-job") is None
    assert _build_rows(rid) == ["stopped", "running"]


def test_recover_re_runs_the_gates_of_every_running_game_left_checking(tmp_runs, seen):
    _, _, a = _run("a", stage="checking")
    _run("b", stage="checking", status="paused")
    _run("c", stage="building")
    _run("d", stage="checking", status="succeeded")
    build_chain.recover()
    assert seen["threads"] == [(build_chain._post_finalize, ("a", a.build_id))]


@pytest.mark.parametrize("playable, staged", [(True, ["g1"]), (False, [])])
def test_a_failed_build_is_announced_at_once_and_staged_when_playable(tmp_runs, seen, playable,
                                                                      staged):
    rid, rs, cursor = _run(playable=playable)
    build_chain._finalize(rid, rs, cursor, ok=False)
    assert _where(rid) == ("building", "failed") and _build_rows(rid) == ["failed"]
    assert seen["events"] == ["build_done"] and seen["payloads"][0]["ok"] is False
    assert seen["staged"] == staged and seen["threads"] == []


def test_running_out_of_compute_mid_build_stages_what_was_made(tmp_runs, seen, monkeypatch):
    rid, rs, cursor = _run()

    def _refuse(*a, **k):
        raise build_chain.errors.InsufficientCompute(rid, 0, 1)
    monkeypatch.setattr(build_chain.jobs, "enqueue_job", _refuse)
    monkeypatch.setattr(build_chain, "get_connector", lambda: type(
        "C", (), {"build_llm_job": lambda self, *a: ({}, "m")})())
    assert build_chain._enqueue_turn(rid, cursor, build_chain.build_steps.Infer(
        messages=[], schemas=[], max_tokens=1, report="")) is False
    assert _where(rid) == ("building", "failed") and seen["staged"] == [rid]
    assert seen["payloads"][0]["error"].startswith("compute exhausted")


@pytest.mark.parametrize("turn, finished, want, stage", [
    (200, False, "succeeded", "checking"),
    (37, False, "failed", "building"),
    (37, True, "succeeded", "checking"),
])
def test_only_a_build_that_gave_up_early_fails(tmp_runs, seen, monkeypatch, turn, finished, want,
                                               stage):
    rid, rs, cursor = _run()
    cursor.turn, cursor.max_steps, cursor.finished = turn, 200, finished
    build_state.save(cursor)
    monkeypatch.setattr(build_chain, "build_tools", lambda rs, build_id: None)
    monkeypatch.setattr(build_chain.build_steps, "step",
                        lambda *a, **k: build_chain.build_steps.Done("ended"))
    build_chain.advance(rid)
    assert _build_rows(rid) == [want] and seen["staged"] == [rid]
    assert games.game(rid)["stage"] == stage


def test_a_running_game_whose_build_lost_its_cursor_fails_instead_of_looping(tmp_runs, seen):
    games.create_game("g1", "u1")
    games.move("g1", "running", "building")
    bid = games.create_build("g1", kind="build")
    games.build_started(bid)
    build_chain.advance("g1")
    assert _where("g1") == ("building", "failed") and seen["events"] == ["build_done"]
    assert games.stuck_builds(0) == []


def test_the_run_lock_keeps_out_a_second_process(tmp_runs, seen, monkeypatch):
    monkeypatch.setattr(build_chain, "_locks", {})
    rid, rs, cursor = _run()
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR); "
         "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)",
         str(rs.run_dir / ".lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"

        def boom(*a, **k):
            raise AssertionError("stepped while another process held the run")
        monkeypatch.setattr(build_chain.build_steps, "step", boom)
        build_chain.advance(rid)
        with build_chain._run_lock(rid, blocking=False) as held:
            assert held is False
    finally:
        holder.kill()
        holder.wait()
    with build_chain._run_lock(rid, blocking=False) as held:
        assert held is True
