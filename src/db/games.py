"""Games and their builds: ownership, lifecycle status, and the compute grant each one spends."""

import time
import uuid
from typing import Dict, List, Optional

from billing.estimates import QUEUE_MICRO_ESTIMATES
from db.connection import platform_db, row_dict
from tools.version import maestro_rev


def create_game(game_id: str, user_id: str) -> None:
    now = time.time()
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO games (id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (game_id, user_id, now, now),
        )


def game(game_id: str) -> Optional[Dict]:
    with platform_db() as conn:
        return row_dict(conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone())


def owner_of(game_id: str) -> Optional[str]:
    with platform_db() as conn:
        row = conn.execute("SELECT user_id FROM games WHERE id = ?", (game_id,)).fetchone()
    return row["user_id"] if row else None


def list_games(user_id: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM games WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_prompt_meta(game_id: str, title: str) -> None:
    """Mirror the prompt's title onto the game row and mark the run buildable."""
    with platform_db() as conn:
        conn.execute(
            "UPDATE games SET title = ?, status = ?, updated_at = ? WHERE id = ?",
            (title, "ready", time.time(), game_id),
        )


def set_status(game_id: str, status: str) -> None:
    with platform_db() as conn:
        conn.execute("UPDATE games SET status = ?, updated_at = ? WHERE id = ?",
                     (status, time.time(), game_id))


def charge_game(game_id: str, credits: int, micros: int) -> None:
    """Grant `micros` (millionths of a dollar) of compute for `credits`."""
    with platform_db() as conn:
        conn.execute(
            "UPDATE games SET credits_spent = credits_spent + ?, "
            "granted_micros = granted_micros + ?, updated_at = ? WHERE id = ?",
            (credits, micros, time.time(), game_id),
        )


def is_charged(game_id: str) -> bool:
    with platform_db() as conn:
        row = conn.execute("SELECT credits_spent FROM games WHERE id = ?", (game_id,)).fetchone()
    return bool(row and row["credits_spent"] > 0)


def remaining_locked(conn, game_id: str) -> int:
    """`compute_remaining` on a connection that already holds the write lock."""
    row = conn.execute(
        "SELECT granted_micros, spent_micros FROM games WHERE id = ?", (game_id,)).fetchone()
    if row is None:
        return 0
    reserved = conn.execute(
        "SELECT COALESCE(SUM(reserved_micros), 0) AS s FROM jobs "
        "WHERE game_id = ? AND status IN ('pending', 'claimed')", (game_id,)).fetchone()["s"]
    return int(row["granted_micros"] - row["spent_micros"] - reserved)


def compute_remaining(game_id: str) -> int:
    """The grant minus measured spend minus the estimates of everything already in flight."""
    with platform_db() as conn:
        return remaining_locked(conn, game_id)


def can_afford(game_id: str, queue: str) -> bool:
    return compute_remaining(game_id) >= QUEUE_MICRO_ESTIMATES[queue]


def create_build(game_id: str, kind: str = "build") -> str:
    build_id = uuid.uuid4().hex[:12]
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO builds (id, game_id, kind, status, queued_at, maestro_rev) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (build_id, game_id, kind, "queued", time.time(), maestro_rev()),
        )
    return build_id


def build_started(build_id: str) -> None:
    with platform_db() as conn:
        conn.execute("UPDATE builds SET status = 'running', started_at = ? WHERE id = ?",
                     (time.time(), build_id))


def build_finished(build_id: str, status: str, steps: Optional[int] = None) -> None:
    with platform_db() as conn:
        conn.execute(
            "UPDATE builds SET status = ?, steps = ?, finished_at = ? WHERE id = ?",
            (status, steps, time.time(), build_id),
        )


def finish_open_builds(game_id: str, status: str) -> List[Dict]:
    now = time.time()
    with platform_db() as conn:
        rows = conn.execute(
            "UPDATE builds SET status = ?, finished_at = ? "
            "WHERE game_id = ? AND status IN ('queued', 'running') RETURNING id, kind",
            (status, now, game_id)).fetchall()
    return [dict(r) for r in rows]


def builds_for(game_id: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM builds WHERE game_id = ? ORDER BY queued_at", (game_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def stuck_builds(grace_seconds: float) -> List[str]:
    """Games marked 'building' with no job of any kind in flight, for the reaper to re-advance."""
    cutoff = time.time() - grace_seconds
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT g.id FROM games g WHERE g.status = 'building' "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.game_id = g.id "
            "  AND j.status IN ('pending', 'claimed')) "
            "AND (SELECT MAX(j2.finished_at) FROM jobs j2 WHERE j2.game_id = g.id) < ?",
            (cutoff,)).fetchall()
    return [r["id"] for r in rows]


_WORK_BY_GPU = ("SELECT COALESCE(gpu_type, 'unknown') AS gpu, "
                "COALESCE(SUM(exec_seconds), 0) AS seconds, "
                "COALESCE(SUM(billed_micros), 0) AS micros FROM jobs ")


def games_exec_seconds_by_gpu(game_ids: List[str]) -> Dict[str, Dict[str, float]]:
    if not game_ids:
        return {}
    with platform_db() as conn:
        rows = conn.execute(
            _WORK_BY_GPU + "WHERE finished_at IS NOT NULL "
            f"AND game_id IN ({','.join('?' * len(game_ids))}) GROUP BY gpu",
            game_ids).fetchall()
    return {r["gpu"]: {"seconds": r["seconds"], "micros": r["micros"]} for r in rows}


def games_built_since(since: float) -> List[str]:
    """Games whose full build finished in the window, whatever its outcome."""
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT game_id FROM builds WHERE kind = 'build' AND finished_at >= ?",
            (since,)).fetchall()
    return [r["game_id"] for r in rows]


def games_change_count(game_ids: List[str]) -> int:
    if not game_ids:
        return 0
    with platform_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM builds WHERE kind = 'change' "
            f"AND game_id IN ({','.join('?' * len(game_ids))})", game_ids).fetchone()
    return row["n"]
