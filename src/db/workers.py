"""The worker fleet and what the provider did when we asked for a pod."""

import json
import time
from typing import Dict, List, Optional

from db.connection import platform_db

_DAY = 24 * 3600
_POD_REFUSAL_KEEP_SECONDS = 30 * _DAY


def worker_created(worker_id: str, pod_id: str, queue: str, gpu_type: Optional[str],
                   usd_per_hour: Optional[float], source: str = "runpod",
                   model: Optional[str] = None) -> None:
    now = time.time()
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO workers (id, queue, model, gpu_type, source, pod_id, usd_per_hour, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_id, queue, model, gpu_type, source, pod_id, usd_per_hour, now))


def worker_seen(worker_id: str, queue: str, gpu_type: Optional[str] = None,
                source: Optional[str] = None) -> bool:
    now = time.time()
    with platform_db() as conn:
        cur = conn.execute(
            "UPDATE workers SET queue = ?, last_seen_at = ?, registered_at = COALESCE(registered_at, ?), "
            "gpu_type = COALESCE(?, gpu_type), source = COALESCE(?, source) "
            "WHERE id = ?",
            (queue, now, now, gpu_type, source, worker_id))
    return cur.rowcount > 0


def touch_worker(worker_id: str) -> None:
    with platform_db() as conn:
        conn.execute("UPDATE workers SET last_seen_at = ? WHERE id = ?",
                     (time.time(), worker_id))


def set_worker_terminated(worker_id: str) -> None:
    with platform_db() as conn:
        conn.execute("UPDATE workers SET terminated_at = ? WHERE id = ?",
                     (time.time(), worker_id))


def set_pod_terminated(pod_id: str) -> None:
    """Terminate every worker row the pod carried, booting ones included."""
    with platform_db() as conn:
        conn.execute("UPDATE workers SET terminated_at = ? WHERE pod_id = ? AND terminated_at IS NULL",
                     (time.time(), pod_id))


def live_workers(queue: str, model: Optional[str], freshness_seconds: float) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM workers WHERE queue = ? AND model IS ? AND terminated_at IS NULL "
            "AND registered_at IS NOT NULL AND last_seen_at >= ?",
            (queue, model, time.time() - freshness_seconds)).fetchall()
    return [dict(r) for r in rows]


def booting_workers(queue: str, model: Optional[str]) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM workers WHERE queue = ? AND model IS ? AND terminated_at IS NULL "
            "AND registered_at IS NULL ORDER BY started_at", (queue, model)).fetchall()
    return [dict(r) for r in rows]


def stale_workers(queue: str, model: Optional[str], staleness_seconds: float,
                  boot_deadlines: Dict[str, float]) -> List[Dict]:
    now = time.time()
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM workers WHERE queue = ? AND model IS ? AND terminated_at IS NULL "
            "AND pod_id IS NOT NULL AND ((registered_at IS NOT NULL AND last_seen_at < ?) "
            "  OR registered_at IS NULL)",
            (queue, model, now - staleness_seconds)).fetchall()
    return [dict(r) for r in rows if r["registered_at"] is not None
            or r["started_at"] < now - boot_deadlines.get(r["source"], 0.0)]


def terminated_workers_with_pods(queue: str, model: Optional[str]) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM workers w WHERE queue = ? AND model IS ? AND terminated_at IS NOT NULL "
            "AND pod_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM workers later_life "
            "WHERE later_life.pod_id = w.pod_id AND later_life.started_at > w.started_at)",
            (queue, model)).fetchall()
    return [dict(r) for r in rows]


def pod_ledger(since: float) -> List[Dict]:
    """Returns each pod life (worker row) created since `since`, with its jobs and customer spend."""
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT w.id AS worker_id, w.pod_id, w.source, w.queue, w.gpu_type, w.usd_per_hour, "
            "w.started_at, w.terminated_at, "
            "COUNT(j.id) AS jobs, "
            "COALESCE(SUM(j.status = 'failed'), 0) AS failed, "
            "COALESCE(SUM(j.exec_seconds), 0) AS exec_seconds, "
            "COALESCE(SUM(CASE WHEN j.status = 'done' AND j.game_id IS NOT NULL "
            "THEN j.billed_micros END), 0) AS customer_micros "
            "FROM workers w LEFT JOIN jobs j ON j.worker_id = w.id AND j.finished_at IS NOT NULL "
            "WHERE w.pod_id IS NOT NULL AND w.started_at >= ? "
            "GROUP BY w.id ORDER BY w.started_at DESC", (since,)).fetchall()
    return [dict(r) for r in rows]


def pod_lives() -> Dict[str, List[tuple]]:
    """Returns each pod id's lives as (started_at, worker_id), oldest first."""
    with platform_db() as conn:
        rows = conn.execute("SELECT pod_id, started_at, id FROM workers WHERE pod_id IS NOT NULL "
                            "ORDER BY started_at").fetchall()
    out: Dict[str, List[tuple]] = {}
    for r in rows:
        out.setdefault(r["pod_id"], []).append((r["started_at"], r["id"]))
    return out


def _bump_pod_day(conn, queue: str, model: Optional[str], now: float,
                  refusal_kind: Optional[str]) -> None:
    """The never-pruned daily rollup behind the provider-facing attempt/refusal ratio."""
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    key = (queue, model or "", day)
    conn.execute("INSERT OR IGNORE INTO pod_request_days (queue, model, day) VALUES (?, ?, ?)", key)
    col = {None: "", "stock": ", stock_refusals = stock_refusals + 1",
           "other": ", other_refusals = other_refusals + 1"}[refusal_kind]
    conn.execute(f"UPDATE pod_request_days SET attempts = attempts + 1{col} "
                 "WHERE queue = ? AND model = ? AND day = ?", key)


def record_pod_created(queue: str, model: Optional[str]) -> None:
    with platform_db() as conn:
        _bump_pod_day(conn, queue, model, time.time(), None)


def record_pod_refusal(queue: str, model: Optional[str], kind: str, attempts: List[Dict],
                       error: str) -> None:
    now = time.time()
    with platform_db() as conn:
        _bump_pod_day(conn, queue, model, now, kind)
        conn.execute("DELETE FROM pod_refusals WHERE created_at < ?",
                     (now - _POD_REFUSAL_KEEP_SECONDS,))
        conn.execute(
            "INSERT INTO pod_refusals (queue, model, kind, attempts, error, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (queue, model, kind, json.dumps(attempts, ensure_ascii=False), error, now))


def pod_stockout_stats(queue: str, model: Optional[str], now: float) -> Dict:
    with platform_db() as conn:
        counts = conn.execute(
            "SELECT SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS last_1h, "
            "SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS last_24h, "
            "COUNT(*) AS last_7d FROM pod_refusals "
            "WHERE queue = ? AND model IS ? AND kind = 'stock' AND created_at >= ?",
            (now - 3600, now - _DAY, queue, model, now - 7 * _DAY)).fetchone()
        last = conn.execute(
            "SELECT created_at FROM pod_refusals WHERE queue = ? AND model IS ? AND kind = 'stock' "
            "ORDER BY id DESC LIMIT 1", (queue, model)).fetchone()
    return {
        "last_1h": counts["last_1h"] or 0,
        "last_24h": counts["last_24h"] or 0,
        "last_7d": counts["last_7d"],
        "last_at": last["created_at"] if last else None,
    }
