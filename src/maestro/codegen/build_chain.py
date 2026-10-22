"""The build driver — what a finished build LLM turn does next.
A linear chain of `llm` jobs: advance() applies a turn, runs local work, suspends to infer.
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
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

class _RunLock:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.local = threading.RLock()
        self.depth = 0
        self.fd = None

    @contextmanager
    def hold(self, blocking: bool = True):
        if not self.local.acquire(blocking=blocking):
            yield False
            return
        try:
            if self.depth == 0:
                fd = os.open(RunState(self.run_id).run_dir / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    os.close(fd)
                    yield False
                    return
                self.fd = fd
            self.depth += 1
            try:
                yield True
            finally:
                self.depth -= 1
                if self.depth == 0:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                    os.close(self.fd)
                    self.fd = None
        finally:
            self.local.release()


_locks: Dict[str, _RunLock] = {}
_locks_guard = threading.Lock()


def _run_lock(run_id: str, blocking: bool = True):
    with _locks_guard:
        lk = _locks.get(run_id)
        if lk is None:
            lk = _locks[run_id] = _RunLock(run_id)
    return lk.hold(blocking)


def kickoff(run_id: str, *, kind: str = "build", note: str = "", fresh: bool = False,
            max_steps: int = DEFAULT_MAX_STEPS, after: Optional[str] = None,
            designed: bool = False) -> Optional[str]:
    if after is not None:
        guard = dict(when_status=("running",), when_stage=("checking",), when_build=after)
    elif designed:
        guard = dict(when_status=("running",), when_stage=("designing",))
    else:
        guard = dict(when_status=("paused", "failed", "succeeded"))
    with _run_lock(run_id):
        if not games.move(run_id, "running", "building", **guard):
            return None
        return _begin(run_id, kind=kind, note=note, fresh=fresh, max_steps=max_steps)


def _begin(run_id: str, *, kind: str = "build", note: str = "", fresh: bool = False,
           max_steps: int = DEFAULT_MAX_STEPS) -> str:
    replaced = games.current_build(run_id)
    if replaced is not None:
        jobs.abandon_build_jobs(replaced["id"], "replaced by a new build")
    build_id = games.create_build(run_id, kind=kind)
    games.finish_open_builds(run_id, "stopped", keep=build_id)
    games.build_started(build_id)
    start_build(run_id, build_id, kind=kind, note=note, fresh=fresh, max_steps=max_steps)
    return build_id


def pause(run_id: str) -> bool:
    with _run_lock(run_id):
        stage = (games.game(run_id) or {}).get("stage")
        if not games.move(run_id, "paused", when_status=("running",)):
            return False
        cancelled = jobs.cancel_pending_game_jobs(run_id, "paused by hand")
        if stage == "designing":
            games.finish_open_builds(run_id, "stopped", kinds="('design')")
        cursor = build_state.load(run_id)
        if stage == "building" and cursor is not None:
            if any(j["build_id"] == cursor.build_id and j["metadata"].get("stage") == "build"
                   for j in cancelled):
                cursor.step = max(0, cursor.step - 1)
                build_state.save(cursor)
            if _stage(run_id, RunState(run_id), cursor, "paused"):
                return True
        _emit("build_paused", run_id, build_id=cursor.build_id if cursor else None,
              step=cursor.step if cursor else 0)
        return True


def resume(run_id: str) -> bool:
    from maestro.codegen import design
    with _run_lock(run_id):
        stage = (games.game(run_id) or {}).get("stage")
        if not games.move(run_id, "running", when_status=("paused",)):
            return False
        cursor = build_state.load(run_id)
        _emit("build_resumed", run_id, build_id=cursor.build_id if cursor else None,
              step=cursor.step if cursor else 0)
        if stage == "designing":
            spec = RunState(run_id).read_spec() or {}
            if not spec.get("request"):
                design.enqueue(run_id, spec.get("ask", ""))
                return True
            games.move(run_id, "running", "building")
            _begin(run_id)
        elif stage == "checking" and cursor is not None:
            threading.Thread(target=_post_finalize, args=(run_id, cursor.build_id),
                             daemon=True).start()
        elif cursor is None or games.current_build(run_id)["status"] not in ("queued", "running"):
            _begin(run_id)
        else:
            advance(run_id, wait=True)
        return True


def recover() -> None:
    for run_id in games.checking_games():
        cursor = build_state.load(run_id)
        if cursor is not None:
            logger.warning("build %s: re-running gates the last process never finished", run_id)
            threading.Thread(target=_post_finalize, args=(run_id, cursor.build_id),
                             daemon=True).start()


def is_running(run_id: str) -> bool:
    return (games.game(run_id) or {}).get("status") == "running"


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
    build_state.save(cursor)
    _emit("build_started", run_id, build_id=build_id, max_steps=max_steps, started_at=cursor.t0)
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
    advance(run_id, result if (error is None and result) else {}, wait=True, build_id=build_id,
            landed={"job_id": job_id, "exec_seconds": exec_seconds, "error": error})


def advance(run_id: str, result: Optional[Dict] = None, *, wait: bool = False,
            landed: Optional[Dict] = None, build_id: Optional[str] = None) -> None:
    with _run_lock(run_id, blocking=wait) as held:
        if held:
            _advance_locked(run_id, result, landed, build_id)


def _advance_locked(run_id: str, result: Optional[Dict], landed: Optional[Dict],
                    build_id: Optional[str]) -> None:
    game = games.game(run_id)
    build = games.current_build(run_id)
    if game is None or build is None:
        return
    if build_id is not None and build_id != build["id"]:
        logger.warning("build %s: dropped a turn from replaced build %s", run_id, build_id)
        return
    if game["stage"] != "building" or game["status"] not in ("running", "paused"):
        return
    cursor = build_state.load(run_id)
    if cursor is None or build["status"] not in ("queued", "running"):
        if games.move(run_id, "failed", when_status=("running",), when_stage=("building",)):
            logger.error("build %s: running with no live build to drive", run_id)
            _emit("build_done", run_id, build_id=build["id"], ok=False, steps=0,
                  error="the build lost its place")
        return
    rs = RunState(run_id)
    if landed:
        _archive_turn(rs, cursor, landed, result)
    paused = game["status"] == "paused"
    if paused and result is None:
        return
    tools = build_tools(rs, cursor.build_id)
    outcome = build_steps.step(rs.read_spec(), rs.run_dir, tools, cursor, result,
                               error=(landed or {}).get("error"))

    if isinstance(outcome, build_steps.Infer):
        if paused:
            # A turn claimed before the pause. Its work is kept; what pause withholds is the NEXT one.
            build_state.save(cursor)
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
        build_state.save(cursor)
        if _enqueue_turn(run_id, cursor, outcome):
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


def _stage(run_id: str, rs: RunState, cursor: BuildCursor, label: str) -> bool:
    """Stage a playable game, or hold it when the artifact screen hits; True when held."""
    if not _playable(rs.run_dir):
        return False
    held = artifact_screen.screen_artifact(rs.run_dir)
    if held is None:
        stage_for_play(rs.run_dir, run_id)
        snapshots.take(rs.run_dir, label)
        return False
    path, violation = held
    log_violation(violation, run_id=run_id, source=f"artifact:{path}")
    games.move(run_id, "held")
    shares.unshare(run_id)
    games.build_finished(cursor.build_id, "held", steps=cursor.step)
    logger.warning("build %s held: artifact screen hit in %s", run_id, path)
    _emit("build_done", run_id, build_id=cursor.build_id, ok=False, steps=cursor.step, held=True)
    return True


def _finalize(run_id: str, rs: RunState, cursor: BuildCursor, ok: bool,
              error: Optional[str] = None) -> None:
    if _stage(run_id, rs, cursor, "built"):
        return
    cursor.ok = ok
    build_state.save(cursor)
    games.build_finished(cursor.build_id, "succeeded" if ok else "failed", steps=cursor.step)
    logger.info("build %s finalized: ok=%s steps=%d", run_id, ok, cursor.step)
    status = (games.game(run_id) or {}).get("status")
    if not ok:
        if games.move(run_id, "failed", when_status=("running", "paused"),
                      when_stage=("building",), when_build=cursor.build_id):
            extra = {"error": error} if error else {}
            _emit("build_done", run_id, build_id=cursor.build_id, ok=False, steps=cursor.step,
                  **extra)
        return
    if not games.move(run_id, status, "checking", when_status=(status,), when_stage=("building",),
                      when_build=cursor.build_id):
        return
    if status == "running":
        threading.Thread(target=_post_finalize, args=(run_id, cursor.build_id), daemon=True).start()


def _settle(run_id: str, build_id: str) -> bool:
    """No gate kicked a fix, so this build is the end of the chain."""
    cursor = build_state.load(run_id)
    if cursor is None or cursor.build_id != build_id:
        return False
    if not games.move(run_id, "succeeded" if cursor.ok else "failed", when_status=("running",),
                      when_stage=("checking",), when_build=build_id):
        return False
    _emit("build_done", run_id, build_id=build_id, ok=bool(cursor.ok), steps=cursor.step)
    return True


def _post_finalize(run_id: str, build_id: str) -> None:
    from maestro.codegen import archive
    try:
        kicked = (error_gate.after_build(run_id, build_id)
                  or (is_running(run_id) and play_gate.after_build(run_id, build_id)))
    except Exception:
        if not is_running(run_id):
            logger.info("build %s: gates stopped by a pause", run_id)
            return
        logger.exception("build %s: a gate raised; settling the run where it stands", run_id)
        _settle(run_id, build_id)
        raise
    if kicked or not is_running(run_id):
        return
    if _settle(run_id, build_id):
        archive.archive(run_id)


def _enqueue_turn(run_id: str, cursor: BuildCursor, inf: "build_steps.Infer") -> bool:
    conn = get_connector()
    payload, model = conn.build_llm_job(inf.messages, inf.schemas, inf.max_tokens)

    def _enqueue():
        jobs.enqueue_job("llm", payload, game_id=run_id, build_id=cursor.build_id, model=model,
                             metadata={"stage": "build", "run_id": run_id, "build_id": cursor.build_id})

    try:
        _enqueue()
        return True
    except errors.GamePaused:
        cursor.step = max(0, cursor.step - 1)
        build_state.save(cursor)
        return False
    except errors.InsufficientCompute as e:
        err = e
        released = jobs.abandon_pending_batch_jobs(
            run_id, "preempted: the build needs the remaining compute")
        if released:
            logger.warning("build %s: preempted %d queued asset job(s) to admit the next turn",
                           run_id, released)
            try:
                _enqueue()
                return True
            except errors.InsufficientCompute as e2:
                err = e2
        logger.error("build %s turn refused: %s", run_id, err)
        _finalize(run_id, RunState(run_id), cursor, ok=False, error=f"compute exhausted: {err}")
        return False


def _emit_step(run_id: str, cursor: BuildCursor, summary: str) -> None:
    _emit("build_step", run_id, build_id=cursor.build_id, step=cursor.step,
          max_steps=cursor.max_steps, summary=summary, elapsed=time.time() - cursor.t0)
    logger.info("build %s step %d: %s", run_id, cursor.step, summary)
