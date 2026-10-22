"""Games and their builds: ownership, lifecycle status, and the compute grant each one spends."""

import time
import uuid
from typing import Dict, List, Optional, Sequence

from billing.estimates import QUEUE_MICRO_ESTIMATES
from db.connection import platform_db, row_dict
from tools.version import maestro_rev


def create_game(game_id: str, user_id: str) -> None:
    now = time.time()
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO games (id, user_id, stage, status, created_at, updated_at) "
            "VALUES (?, ?, 'designing', 'running', ?, ?)",
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
    with platform_db() as conn:
        conn.execute("UPDATE games SET title = ?, updated_at = ? WHERE id = ?",
                     (title, time.time(), game_id))


def move(game_id: str, status: str, stage: Optional[str] = None, *,
         when_status: Sequence[str] = (), when_stage: Sequence[str] = (),
         when_build: Optional[str] = None) -> bool:
    where, params = ["id = ?"], [game_id]
    if when_status:
        where.append(f"status IN ({','.join('?' * len(when_status))})")
        params += list(when_status)
    if when_stage:
        where.append(f"stage IN ({','.join('?' * len(when_stage))})")
        params += list(when_stage)
    if when_build is not None:
        where.append(f"(SELECT id FROM builds WHERE game_id = games.id AND kind IN {CURSOR_KINDS} "
                     "ORDER BY queued_at DESC, rowid DESC LIMIT 1) = ?")
        params.append(when_build)
    sets, values = ["status = ?", "updated_at = ?"], [status, time.time()]
    if stage is not None:
        sets.append("stage = ?")
        values.append(stage)
    with platform_db(immediate=True) as conn:
        cur = conn.execute(f"UPDATE games SET {', '.join(sets)} WHERE {' AND '.join(where)}",
                           values + params)
    return cur.rowcount == 1


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


CURSOR_KINDS = "('build', 'fix', 'change')"


def current_build(game_id: str) -> Optional[Dict]:
    with platform_db() as conn:
        return row_dict(conn.execute(
            f"SELECT * FROM builds WHERE game_id = ? AND kind IN {CURSOR_KINDS} "
            "ORDER BY queued_at DESC, rowid DESC LIMIT 1", (game_id,)).fetchone())


def save_cursor(build_id: str, cursor: str) -> None:
    with platform_db() as conn:
        conn.execute("UPDATE builds SET cursor = ? WHERE id = ?", (cursor, build_id))


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


def finish_open_builds(game_id: str, status: str, keep: str = "",
                       kinds: str = CURSOR_KINDS) -> None:
    with platform_db() as conn:
        conn.execute(
            f"UPDATE builds SET status = ?, finished_at = ? WHERE game_id = ? AND id != ? "
            f"AND kind IN {kinds} AND status IN ('queued', 'running')",
            (status, time.time(), game_id, keep))


def builds_for(game_id: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM builds WHERE game_id = ? ORDER BY queued_at", (game_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def stuck_builds(grace_seconds: float) -> List[str]:
    cutoff = time.time() - grace_seconds
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT g.id FROM games g WHERE g.status = 'running' AND g.stage = 'building' "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.game_id = g.id "
            "  AND j.status IN ('pending', 'claimed')) "
            "AND COALESCE((SELECT MAX(j2.finished_at) FROM jobs j2 WHERE j2.game_id = g.id), "
            "  g.updated_at) < ?",
            (cutoff,)).fetchall()
    return [r["id"] for r in rows]


def checking_games() -> List[str]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT id FROM games WHERE status = 'running' AND stage = 'checking'").fetchall()
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
