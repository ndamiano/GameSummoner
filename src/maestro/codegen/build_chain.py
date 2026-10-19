"""The build driver — what a finished build LLM turn does next.
A linear chain of `llm` jobs: advance() applies a turn, runs local work, suspends to infer.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Dict, Optional

from db import errors, games, jobs
from llm_clients.connector import get_connector
from maestro import shares
from maestro.codegen import (
    artifact_screen,
    build_state,
    build_steps,
    error_gate,
    play_gate,
    snapshots,
    turn_log,
)
from maestro.codegen.build_state import DEFAULT_MAX_STEPS, BuildCursor
from maestro.codegen.staging import entry_path, game_dir, stage_for_play
from maestro.codegen.tools import build_tools
from maestro.state import RunState
from tools.build_events import _emit
from tools.safety import log_violation

logger = logging.getLogger(__name__)

# One advance at a time per run — the completion handler and the reaper both call advance, and only
# the control-plane process ever does, so an in-process lock is sufficient mutual exclusion.
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(run_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(run_id)
        if lk is None:
            lk = _locks[run_id] = threading.Lock()
        return lk


def kickoff(run_id: str, *, kind: str = "build", note: str = "", fresh: bool = False,
            max_steps: int = DEFAULT_MAX_STEPS) -> str:
    build_id = games.create_build(run_id, kind=kind)
    games.build_started(build_id)
    start_build(run_id, build_id, kind=kind, note=note, fresh=fresh, max_steps=max_steps)
    return build_id


def pause(run_id: str) -> bool:
    lock = _lock_for(run_id)
    lock.acquire()   # an advance mid-flight must finish enqueueing before its job can be cancelled
    try:
        rs = RunState(run_id)
        cursor = build_state.load(rs.run_dir)
        if cursor is None or cursor.phase == "done":
            return False
        cursor.paused = True
        if cursor.build_id and jobs.cancel_pending_build_turn(cursor.build_id, "paused by hand"):
            cursor.step = max(0, cursor.step - 1)
        build_state.save(rs.run_dir, cursor)
        _emit("build_paused", run_id, build_id=cursor.build_id, step=cursor.step)
        return True
    finally:
        lock.release()


def stop(run_id: str) -> bool:
    """End everything the run has in flight and keep what it wrote; False when nothing was."""
    lock = _lock_for(run_id)
    lock.acquire()
    try:
        rs = RunState(run_id)
        cursor = build_state.load(rs.run_dir)
        cancelled = jobs.abandon_game_jobs(run_id, "the run was stopped by hand")
        live = cursor is not None and cursor.phase != "done"
        if live:
            _finalize(run_id, rs, cursor, ok=_playable(rs.run_dir), attempt="stopped")
        ended = games.finish_open_builds(run_id, "stopped")
        if any(b["kind"] == "design" for b in ended):
            _end_design(run_id, rs, next(b["id"] for b in ended if b["kind"] == "design"))
        return live or bool(cancelled) or bool(ended)
    finally:
        lock.release()


def _end_design(run_id: str, rs: RunState, build_id: str) -> None:
    from maestro.codegen.run import set_prompt
    spec = rs.read_spec() or {}
    if not spec.get("request") and spec.get("ask"):
        set_prompt(run_id, spec["ask"], event="prompt_proposed")
    games.set_status(run_id, "stopped")
    _emit("build_done", run_id, build_id=build_id, ok=False, steps=0)


def resume(run_id: str) -> None:
    """Re-drive a mid-flight build — one paused, or one whose driver process died."""
    rs = RunState(run_id)
    cursor = build_state.load(rs.run_dir)
    if cursor is not None and cursor.paused:
        cursor.paused = False
        build_state.save(rs.run_dir, cursor)
        _emit("build_resumed", run_id, build_id=cursor.build_id, step=cursor.step)
    advance(run_id)


def is_active(run_id: str) -> bool:
    cursor = build_state.load(RunState(run_id).run_dir)
    return cursor is not None and cursor.phase != "done"


def status_of(run_id: str) -> Optional[Dict]:
    cursor = build_state.load(RunState(run_id).run_dir)
    if cursor is None or cursor.phase == "done":
        return None
    return {"kind": cursor.kind, "paused": cursor.paused}


def start_build(run_id: str, build_id: str, *, kind: str = "build", note: str = "",
                fresh: bool = False, max_steps: int = DEFAULT_MAX_STEPS) -> None:
    rs = RunState(run_id)
    if rs.read_spec() is None:
        raise ValueError(f"no prompt for run {run_id!r}")
    if fresh:
        _clear_game(rs)
    _seed(rs)

    cursor = BuildCursor(build_id=build_id, kind=kind, max_steps=max_steps, t0=time.time())
    if kind == "fix":
        snapshots.take(rs.run_dir, "before-fix")
        cursor.request = ("The game is already written and playable. A person played it and "
                          f"reported this:\n{note}\n\nRead the files and fix exactly that. Call "
                          "done when it is fixed.")
    elif kind == "change":
        snapshots.take(rs.run_dir, "before-change")
        cursor.request = ("The game is already written and playable. A person played it and asked "
                          f"for this change:\n{note}\n\nRead the files and make exactly that "
                          "change, keeping everything else playing as it does. Call done when it "
                          "is made.")
    build_state.save(rs.run_dir, cursor)
    games.set_status(run_id, "building")
    _emit("build_started", run_id, build_id=build_id, max_steps=max_steps, started_at=cursor.t0)
    # wait=True: this first advance must never be dropped by a lock another finalize still holds.
    advance(run_id, wait=True)


def _clear_game(rs: RunState) -> None:
    """Empty the game folder before a fresh build, stopping the art the last one awaits."""
    d = game_dir(rs.run_dir)
    if not d.exists():
        return
    snapshots.take(rs.run_dir, "before-rebuild")
    # A render still in flight would land in the new build's folder and write itself into a manifest
    # that no longer asked for it.
    jobs.abandon_pending_batch_jobs(rs.run_id, "superseded: the game was rebuilt")
    shutil.rmtree(d)


def _seed(rs: RunState) -> None:
    from maestro.codegen.staging import seed_vendor
    seed_vendor(rs.run_dir)


def on_completion(run_id: str, build_id: str, result: Optional[Dict], error: Optional[str],
                  job_id: str, exec_seconds: float) -> None:
    advance(run_id, result if (error is None and result) else {},
            landed={"job_id": job_id, "exec_seconds": exec_seconds, "error": error})


def advance(run_id: str, result: Optional[Dict] = None, *, wait: bool = False,
            landed: Optional[Dict] = None) -> None:
    """Run the state machine forward until it must infer or the build finishes."""
    lock = _lock_for(run_id)
    if wait:
        lock.acquire()
    elif not lock.acquire(blocking=False):
        return   # another advance (completion or reaper) is already driving this run
    try:
        _advance_locked(run_id, result, landed)
    finally:
        lock.release()


def _advance_locked(run_id: str, result: Optional[Dict], landed: Optional[Dict] = None) -> None:
    rs = RunState(run_id)
    spec = rs.read_spec()
    cursor = build_state.load(rs.run_dir)
    if cursor is None or cursor.phase == "done":
        return
    if landed:
        _archive_turn(rs, cursor, landed, result)
    if cursor.paused and result is None:
        return   # nothing to apply, and a re-drive (reaper, resume race) must not restart a parked build
    tools = build_tools(rs, cursor.build_id)
    outcome = build_steps.step(spec, rs.run_dir, tools, cursor, result,
                               error=(landed or {}).get("error"))

    if isinstance(outcome, build_steps.Infer):
        if cursor.paused:
            # A turn claimed before the pause. Its work is kept; what pause withholds is the NEXT one.
            build_state.save(rs.run_dir, cursor)
            _emit_step(run_id, cursor, outcome.report)
            return
        cursor.step += 1
        if cursor.step > cursor.max_steps:
            _finalize(run_id, rs, cursor, ok=_playable(rs.run_dir))
            return
        if not cursor.meta_logged:
            turn_log.append_meta(
                rs.run_dir, run_id=run_id, build_id=cursor.build_id, system=cursor.system,
                tools=outcome.schemas, model=get_connector().model_name,
                max_tokens=outcome.max_tokens, reasoning=get_connector().reasoning,
                # The control plane enqueues the canonical chat shape and nothing else
                # (llm_clients/wire.py); the record says so rather than a reader inferring it from
                # whatever `llm.api` happens to be the day the log is read.
                wire="chat")
            cursor.meta_logged = True
        build_state.save(rs.run_dir, cursor)
        _enqueue_turn(run_id, cursor, outcome)
        # A refused compute budget ends the run inside _enqueue_turn; a step line after that
        # `build_done` would read as progress the build never made.
        if cursor.phase != "done":
            _emit_step(run_id, cursor, outcome.report)
        return

    _emit_step(run_id, cursor, outcome.report)
    _finalize(run_id, rs, cursor, ok=_playable(rs.run_dir)
              and (cursor.finished or cursor.turn >= cursor.max_steps))


def _archive_turn(rs: RunState, cursor: BuildCursor, landed: Dict, result: Optional[Dict]) -> None:
    message = ((result or {}).get("choices") or [{}])[0].get("message")
    turn_log.append_turn(rs.run_dir, turn=cursor.turn, job_id=landed["job_id"],
                         added=cursor.history[cursor.logged:], response=message,
                         usage=(result or {}).get("usage") or {},
                         exec_seconds=landed["exec_seconds"], error=landed["error"])
    cursor.logged = len(cursor.history)


def _playable(run_dir) -> bool:
    """An index.html is the whole contract: without one there is nothing for a browser to open."""
    return entry_path(run_dir).exists()


def _finalize(run_id: str, rs: RunState, cursor: BuildCursor, ok: bool,
              attempt: Optional[str] = None, error: Optional[str] = None) -> None:
    playable = _playable(rs.run_dir)
    held = artifact_screen.screen_artifact(rs.run_dir) if playable else None
    # A gated build is not FINISHED yet, and saying it is shows a person a built game that cuts
    # back to mending seconds later. It stages — the gate opens the staged game — but the status,
    # the event and the phase wait for `_settle`.
    gated = ok and held is None and attempt is None
    cursor.phase = "checking" if gated else "done"
    cursor.ok = ok and held is None
    build_state.save(rs.run_dir, cursor)
    if held is not None:
        # A held game exists only in its run dir: not staged, not snapshotted, not archived. The
        # owner sees a neutral status; the violation row is what the admin panel reads.
        path, violation = held
        log_violation(violation, run_id=run_id, source=f"artifact:{path}")
        games.set_status(run_id, "held")
        shares.unshare(run_id)
        if cursor.build_id:
            games.build_finished(cursor.build_id, "held", steps=cursor.step)
        logger.warning("build %s held: artifact screen hit in %s", run_id, path)
        _emit("build_done", run_id, build_id=cursor.build_id, ok=False, steps=cursor.step,
              held=True)
        return
    if playable:
        stage_for_play(rs.run_dir, run_id)
        snapshots.take(rs.run_dir, "built")
    if cursor.build_id:
        games.build_finished(cursor.build_id, attempt or ("succeeded" if ok else "failed"),
                                steps=cursor.step)
    logger.info("build %s finalized: ok=%s steps=%d", run_id, ok, cursor.step)
    if gated:
        # Post-finalize runs AFTER the lock this finalize holds is released — a kickoff blocks on
        # the same lock, so running it inline here would deadlock. A build stopped by hand gets
        # no gate: the human ended it, and an auto-build would restart what they stopped.
        threading.Thread(target=_post_finalize, args=(run_id, cursor.build_id), daemon=True).start()
        return
    _announce(run_id, rs, cursor, ok, error, stopped=attempt == "stopped")


def _announce(run_id: str, rs: RunState, cursor: BuildCursor, ok: bool,
              error: Optional[str] = None, stopped: bool = False) -> None:
    """The end of the whole chain, said once: the run's status and `build_done`."""
    games.set_status(run_id, "built" if ok else "stopped" if stopped else "failed")
    extra = {"error": error} if error else {}
    _emit("build_done", run_id, build_id=cursor.build_id, ok=ok, steps=cursor.step, **extra)


def _settle(run_id: str) -> None:
    """No gate kicked a fix, so this build is the end of the chain."""
    rs = RunState(run_id)
    cursor = build_state.load(rs.run_dir)
    if cursor is None or cursor.phase != "checking":
        return
    cursor.phase = "done"
    build_state.save(rs.run_dir, cursor)
    _announce(run_id, rs, cursor, bool(cursor.ok))


def _post_finalize(run_id: str, build_id: str) -> None:
    """The error gate, then the play gate, then the archive."""
    from maestro.codegen import archive
    try:
        kicked = (error_gate.after_build(run_id, build_id)
                  or play_gate.after_build(run_id, build_id))
    except Exception:
        # A gate that raises must not leave the run `checking` forever — the page would show a
        # build that never ends and the CLI would never return.
        logger.exception("build %s: a gate raised; settling the run where it stands", run_id)
        _settle(run_id)
        raise
    if kicked:
        return          # the fix build announces the end of the chain when IT settles
    _settle(run_id)
    archive.archive(run_id)


def _enqueue_turn(run_id: str, cursor: BuildCursor, inf: "build_steps.Infer") -> None:
    conn = get_connector()
    payload, model = conn.build_llm_job(inf.messages, inf.schemas, inf.max_tokens)

    def _enqueue():
        jobs.enqueue_job("llm", payload, game_id=run_id, build_id=cursor.build_id, model=model,
                             metadata={"stage": "build", "run_id": run_id, "build_id": cursor.build_id})

    try:
        return _enqueue()
    except errors.InsufficientCompute as e:
        err = e
        released = jobs.abandon_pending_batch_jobs(
            run_id, "preempted: the build needs the remaining compute")
        if released:
            logger.warning("build %s: preempted %d queued asset job(s) to admit the next turn",
                           run_id, released)
            try:
                return _enqueue()
            except errors.InsufficientCompute as e2:
                err = e2
        logger.error("build %s turn refused: %s", run_id, err)
        _finalize(run_id, RunState(run_id), cursor, ok=False, error=f"compute exhausted: {err}")


def _emit_step(run_id: str, cursor: BuildCursor, summary: str) -> None:
    _emit("build_step", run_id, build_id=cursor.build_id, step=cursor.step,
          max_steps=cursor.max_steps, summary=summary, elapsed=time.time() - cursor.t0)
    logger.info("build %s step %d: %s", run_id, cursor.step, summary)
