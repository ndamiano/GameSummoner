"""The worker-pull job queue: admission, claim, completion, and what a finished row keeps."""

import json
import logging
import sqlite3
import time
import uuid
from typing import Dict, List, Optional

from billing.estimates import (
    QUEUE_MICRO_ESTIMATES,
    QUEUE_SECONDS_ESTIMATES,
    calculate_job_cost,
)
from db.connection import platform_db
from db.errors import BuildEnded, GamePaused, InsufficientCompute
from db.games import remaining_locked

logger = logging.getLogger(__name__)


def _insert_job_locked(conn, queue: str, payload: Dict, game_id: Optional[str],
                       build_id: Optional[str], model: Optional[str],
                       batch_id: Optional[str], metadata: Optional[Dict]) -> str:
    reserved = QUEUE_MICRO_ESTIMATES[queue]
    job_id = uuid.uuid4().hex[:16]
    if game_id is not None:
        remaining = remaining_locked(conn, game_id)
        if remaining < reserved:
            raise InsufficientCompute(game_id, remaining, reserved)
        if build_id is None:
            raise ValueError(f"job on game {game_id!r} has no build_id")
        game = conn.execute("SELECT status FROM games WHERE id = ?", (game_id,)).fetchone()
        if game is not None and game["status"] in ("paused", "held"):
            raise GamePaused(f"game {game_id} is {game['status']}")
        build = conn.execute("SELECT status FROM builds WHERE id = ?", (build_id,)).fetchone()
        if build is not None and build["status"] == "stopped":
            raise BuildEnded(f"build {build_id} of game {game_id} was stopped")
    conn.execute(
        "INSERT INTO jobs (id, queue, game_id, build_id, status, payload, model, "
        "reserved_micros, batch_id, metadata, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
        (job_id, queue, game_id, build_id, json.dumps(payload, ensure_ascii=False), model, reserved,
         batch_id, json.dumps(metadata, ensure_ascii=False) if metadata else None, time.time()),
    )
    return job_id


def enqueue_job(queue: str, payload: Dict, game_id: Optional[str] = None,
                build_id: Optional[str] = None, model: Optional[str] = None,
                batch_id: Optional[str] = None, metadata: Optional[Dict] = None) -> str:
    """Land a job as pending, admitting it against its game's compute budget first."""
    with platform_db(immediate=game_id is not None) as conn:
        return _insert_job_locked(conn, queue, payload, game_id, build_id, model,
                                  batch_id, metadata)


def _fail_locked(conn, where: str, params: tuple, error: str) -> int:
    rows = conn.execute(f"SELECT id, queue, payload FROM jobs WHERE {where}", params).fetchall()
    now = time.time()
    for row in rows:
        payload = elide_payload(row["queue"], json.loads(row["payload"] or "{}"))
        conn.execute(
            "UPDATE jobs SET status = 'failed', payload = ?, error = ?, finished_at = ? "
            "WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), error, now, row["id"]))
    return len(rows)


def abandon_job(job_id: str, error: str) -> bool:
    with platform_db() as conn:
        n = _fail_locked(conn, "id = ? AND status IN ('pending', 'claimed')", (job_id,), error)
    return n == 1


def abandon_build_jobs(build_id: str, error: str) -> int:
    """Fail a build's unfinished turns; a claimed one is left to its worker."""
    with platform_db() as conn:
        return _fail_locked(conn, "build_id = ? AND status = 'pending'", (build_id,), error)


def cancel_pending_game_jobs(game_id: str, error: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute("SELECT id, build_id, queue, metadata FROM jobs "
                            "WHERE game_id = ? AND status = 'pending'", (game_id,)).fetchall()
        _fail_locked(conn, "game_id = ? AND status = 'pending'", (game_id,), error)
    return [{**dict(r), "metadata": json.loads(r["metadata"] or "{}")} for r in rows]


def abandon_pending_batch_jobs(game_id: str, error: str) -> int:
    with platform_db() as conn:
        return _fail_locked(
            conn, "game_id = ? AND batch_id IS NOT NULL AND status = 'pending'", (game_id,),
            error)


def queue_has_work(queue: str) -> bool:
    """Lock-free peek for the claim long-poll: a pending row, or one whose lease lapsed."""
    now = time.time()
    with platform_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE queue = ? AND (status = 'pending' "
            "OR (status = 'claimed' AND lease_expires_at < ?)) LIMIT 1",
            (queue, now)).fetchone()
    return row is not None


def claim_job(queue: str, worker_id: str, lease_seconds: float) -> Optional[Dict]:
    """Atomically claim the oldest pending job on `queue`, requeueing expired leases first."""
    now = time.time()
    with platform_db() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'pending', worker_id = NULL, lease_expires_at = NULL "
            "WHERE status = 'claimed' AND lease_expires_at < ?", (now,))
        row = conn.execute(
            "UPDATE jobs SET status = 'claimed', worker_id = ?, started_at = ?, "
            "lease_expires_at = ? WHERE id = ("
            "  SELECT id FROM jobs WHERE queue = ? AND status = 'pending' "
            "  ORDER BY created_at LIMIT 1) "
            "RETURNING *",
            (worker_id, now, now + lease_seconds, queue),
        ).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["payload"] = json.loads(job["payload"]) if job["payload"] else {}
    return job


def heartbeat_job(job_id: str, worker_id: str, lease_seconds: float) -> bool:
    """Extend a claimed job's lease; False when the job is no longer this worker's claim."""
    with platform_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET lease_expires_at = ? "
            "WHERE id = ? AND worker_id = ? AND status = 'claimed'",
            (time.time() + lease_seconds, job_id, worker_id),
        )
    return cur.rowcount == 1


def served_model(result: Optional[Dict]) -> Optional[str]:
    name = (result or {}).get("model")
    return name.rsplit("/", 1)[-1] if isinstance(name, str) and name else None


def _system_of(body: Dict) -> str:
    if body.get("instructions") is not None:
        return body["instructions"]
    first = (body.get("messages") or [{}])[0]
    return first.get("content") or "" if first.get("role") == "system" else ""


def _text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False)) if content else 0


def elide_payload(queue: str, payload: Dict) -> Dict:
    if queue == "llm":
        body = payload.get("body") or {}
        turns = body.get("messages") or body.get("input") or []
        system = _system_of(body)
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            reasoning = reasoning.get("effort")
        return {"model": body.get("model"),
                "n_messages": len(turns) - (1 if body.get("messages") and system else 0),
                "prompt_chars": len(system) + sum(
                    _text_len(m.get("content") if "content" in m else m.get("output")
                              or m.get("arguments"))
                    for m in turns if m.get("role") != "system"),
                "reasoning": reasoning,
                "max_tokens": body.get("max_output_tokens") or body.get("max_tokens")}
    if queue == "comfy" and payload.get("kind") == "comfy_image":
        wf = payload.get("workflow") or {}
        node = next((wf[k] for k in ("p", "6") if k in wf), {})
        return {"prompt": (node.get("inputs") or {}).get("text") or "",
                "mode": "img2img" if payload.get("uploads") else "txt2img"}
    return {"kind": payload.get("kind")}


def elide_result(queue: str, result: Optional[Dict]) -> Optional[Dict]:
    if result is None or "tool_names" in result:
        return result
    if queue != "llm":
        return {k: ([{kk: vv for kk, vv in i.items() if not kk.endswith("_b64")} for i in v]
                    if k == "images" else v)
                for k, v in result.items() if not k.endswith("_b64")}
    if "choices" in result:
        choice = (result.get("choices") or [{}])[0]
        calls = (choice.get("message") or {}).get("tool_calls") or []
        names = [(c.get("function") or {}).get("name") for c in calls]
        finish = choice.get("finish_reason")
    else:
        items = result.get("output") or []
        names = [i.get("name") for i in items if i.get("type") == "function_call"]
        finish = result.get("status")
    return {"usage": result.get("usage"), "finish_reason": finish,
            "tool_names": [n for n in names if n]}


def elide_job_result(job_id: str) -> None:
    with platform_db() as conn:
        row = conn.execute("SELECT queue, result FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None or not row["result"]:
            return
        elided = elide_result(row["queue"], json.loads(row["result"]))
        conn.execute("UPDATE jobs SET result = ? WHERE id = ?",
                     (json.dumps(elided, ensure_ascii=False), job_id))


def complete_job(job_id: str, worker_id: str, result: Optional[Dict], error: Optional[str],
                 exec_seconds: float, gpu_type: Optional[str] = None,
                 continuation: Optional[Dict] = None) -> Optional[Dict]:
    """Land a job's outcome, debit its game, and advance its chain — one transaction."""
    now = time.time()
    status = "failed" if error else "done"
    with platform_db() as conn:
        job_row = conn.execute(
            "SELECT queue, payload FROM jobs WHERE id = ? AND worker_id = ? AND status = 'claimed'",
            (job_id, worker_id)).fetchone()
        if job_row is None:
            return None
        payload = elide_payload(job_row["queue"], json.loads(job_row["payload"]))
        rate = conn.execute("SELECT usd_per_hour FROM workers WHERE id = ?", (worker_id,)).fetchone()
        billed = calculate_job_cost(exec_seconds, rate["usd_per_hour"])
        cur = conn.execute(
            "UPDATE jobs SET status = ?, payload = ?, result = ?, error = ?, exec_seconds = ?, "
            "billed_micros = ?, gpu_type = ?, model = COALESCE(?, model), "
            "finished_at = ? WHERE id = ?",
            (status, json.dumps(payload, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False) if result is not None else None,
             error, exec_seconds, billed, gpu_type, served_model(result), now, job_id),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT game_id, build_id, batch_id, metadata FROM jobs WHERE id = ?",
            (job_id,)).fetchone()
        if error is None and row["game_id"]:
            conn.execute(
                "UPDATE games SET spent_micros = spent_micros + ?, updated_at = ? WHERE id = ?",
                (billed, now, row["game_id"]))
        if error is None and row["build_id"]:
            conn.execute(
                "UPDATE builds SET spent_micros = spent_micros + ? WHERE id = ?",
                (billed, row["build_id"]))
        conn.execute(
            "UPDATE workers SET busy_seconds = busy_seconds + ?, last_seen_at = ? WHERE id = ?",
            (exec_seconds, now, worker_id))

        continuation_id = None
        if continuation is not None and error is None:
            try:
                continuation_id = _insert_job_locked(
                    conn, continuation["queue"], continuation["payload"], row["game_id"],
                    row["build_id"], continuation.get("model"), row["batch_id"],
                    continuation.get("metadata"))
            except InsufficientCompute as e:
                logger.error("continuation for job %s refused: %s", job_id, e)

        batch_complete = False
        if row["batch_id"]:
            left = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE batch_id = ? "
                "AND status IN ('pending', 'claimed')", (row["batch_id"],)).fetchone()["n"]
            batch_complete = left == 0
    return {"batch_id": row["batch_id"], "batch_complete": batch_complete,
            "metadata": json.loads(row["metadata"]) if row["metadata"] else {},
            "game_id": row["game_id"], "build_id": row["build_id"],
            "continuation_id": continuation_id}


def get_job(job_id: str) -> Optional[Dict]:
    with platform_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    return _job_dict(row)


def _job_dict(row: sqlite3.Row) -> Dict:
    job = dict(row)
    job["payload"] = json.loads(job["payload"]) if job["payload"] else {}
    job["result"] = json.loads(job["result"]) if job["result"] else None
    job["metadata"] = json.loads(job["metadata"]) if job["metadata"] else {}
    return job


def claim_batch_finalize(batch_id: str) -> bool:
    """Take ownership of a batch's finalize; true for exactly one caller."""
    with platform_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET finalized_at = ? WHERE batch_id = ? AND finalized_at IS NULL",
            (time.time(), batch_id))
    return cur.rowcount > 0


def has_active_batch(game_id: str) -> bool:
    with platform_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE game_id = ? AND batch_id IS NOT NULL "
            "AND status IN ('pending', 'claimed') LIMIT 1", (game_id,)).fetchone()
    return row is not None


def batch_jobs(batch_id: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE batch_id = ? ORDER BY created_at", (batch_id,)).fetchall()
    return [_job_dict(r) for r in rows]


def requeue_lapsed_leases() -> int:
    with platform_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending', worker_id = NULL, lease_expires_at = NULL "
            "WHERE status = 'claimed' AND lease_expires_at < ?", (time.time(),))
    return cur.rowcount


def fail_stale_pending(max_age_seconds: float) -> List[Dict]:
    cutoff = time.time() - max_age_seconds
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT id, queue, batch_id, game_id FROM jobs "
            "WHERE status = 'pending' AND created_at < ?", (cutoff,)).fetchall()
        _fail_locked(conn, "status = 'pending' AND created_at < ?", (cutoff,),
                     f"pending longer than {max_age_seconds:.0f}s with no worker")
    return [dict(r) for r in rows]


def batches_awaiting_finalize(grace_seconds: float) -> List[str]:
    cutoff = time.time() - grace_seconds
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT batch_id FROM jobs WHERE batch_id IS NOT NULL AND finalized_at IS NULL "
            "GROUP BY batch_id "
            "HAVING SUM(CASE WHEN status IN ('pending', 'claimed') THEN 1 ELSE 0 END) = 0 "
            "   AND MAX(finished_at) < ?", (cutoff,)).fetchall()
    return [r["batch_id"] for r in rows]


def queue_stats(queue: str) -> Dict:
    now = time.time()
    with platform_db() as conn:
        row = conn.execute(
            "SELECT SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END) AS claimed, "
            "MIN(CASE WHEN status = 'pending' THEN created_at END) AS oldest "
            "FROM jobs WHERE queue = ? AND status IN ('pending', 'claimed')",
            (queue,)).fetchone()
    return {
        "pending": row["pending"] or 0,
        "claimed": row["claimed"] or 0,
        "oldest_pending_age_seconds": (now - row["oldest"]) if row["oldest"] else None,
    }


def recent_job_seconds(queue: str, since: float) -> Optional[float]:
    """Mean exec time of jobs done since `since`, slowest tenth dropped."""
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT exec_seconds FROM jobs WHERE queue = ? AND status = 'done' "
            "AND exec_seconds IS NOT NULL AND finished_at >= ? ORDER BY exec_seconds",
            (queue, since)).fetchall()
    if not rows:
        return None
    kept = [r["exec_seconds"] for r in rows][:max(1, len(rows) * 9 // 10)]
    return sum(kept) / len(kept)


def backlog_seconds(queue: str) -> float:
    with platform_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs "
            "WHERE queue = ? AND status IN ('pending', 'claimed')", (queue,)).fetchone()
    return row["n"] * QUEUE_SECONDS_ESTIMATES[queue]


def pending_jobs_head(queue: str, limit: int) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT id, game_id, build_id, created_at FROM jobs "
            "WHERE queue = ? AND status = 'pending' ORDER BY created_at LIMIT ?",
            (queue, limit)).fetchall()
    return [dict(r) for r in rows]


def claimed_jobs(queue: str) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT id, game_id, build_id, worker_id, started_at FROM jobs "
            "WHERE queue = ? AND status = 'claimed'", (queue,)).fetchall()
    return [dict(r) for r in rows]
