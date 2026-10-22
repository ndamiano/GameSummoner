"""Admin only APIs. These are gated by `require_admin`."""

import asyncio
import calendar
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException

from auth import store
from auth.deps import require_admin
from auth.store import User
from billing import ledger
from billing.estimates import QUEUE_SECONDS_ESTIMATES
from config.settings_manager import settings_manager
from db import events, games, jobs, workers
from maestro import shares
from maestro.codegen import build_chain
from scaler.runpod_client import RunPodClient

logger = logging.getLogger("admin")

router = APIRouter()

_DAY_SECONDS = 86400
_NEXT_LIMIT = 10


def _max_workers(queue: str) -> int:
    """The maximum workers to spin up for a queue"""
    settings = settings_manager.get_settings()
    rp = settings.get("runpod") or {}
    if not (rp.get("enabled") or (settings.get("aws") or {}).get("enabled")):
        return 0
    return int((rp.get("queues") or {}).get(queue, {}).get("max_workers", 0))


def _freshness() -> float:
    """How many seconds after a worker heartbeat before it's considered stale."""
    rp = settings_manager.get_settings().get("runpod") or {}
    return float(rp.get("stale_worker_seconds", 180))


def _stockouts(queue: str, now: float) -> Dict[str, Any]:
    """Returns stock refusal counts per window, and whether there's an active stock outage."""
    stats = workers.pod_stockout_stats(queue, now)
    return {
        "last_1h": stats["last_1h"],
        "last_24h": stats["last_24h"],
        "last_7d": stats["last_7d"],
        "last_at": stats["last_at"],
    }


def _workers(queue: str, now: float, freshness: float) -> List[Dict[str, Any]]:
    """Returns a list of live workers."""
    held = {j["worker_id"]: j for j in jobs.claimed_jobs(queue)}
    rows = []
    for w in workers.booting_workers(queue) + workers.live_workers(queue, freshness):
        job = held.get(w["id"])
        rows.append({
            "id": w["id"],
            "state": "booting" if w["registered_at"] is None else "busy" if job else "idle",
            "gpu_type": w["gpu_type"],
            "usd_per_hour": w["usd_per_hour"],
            "source": w["source"],
            "pod_id": w["pod_id"],
            "uptime_seconds": now - w["started_at"],
            "last_seen_seconds": now - (w["last_seen_at"] or w["started_at"]),
            "busy_seconds": w["busy_seconds"],
            "job": job and {"id": job["id"], "game_id": job["game_id"], "build_id": job["build_id"],
                            "running_seconds": now - (job["started_at"] or now),
                            "est_seconds": QUEUE_SECONDS_ESTIMATES[queue]},
        })
    return rows


@router.get("/queues")
async def get_queues(_: User = Depends(require_admin)) -> Dict[str, Any]:
    """Returns a queue snapshot: depth and backlog, current scaled fleet."""
    now = time.time()
    freshness = _freshness()

    queues: List[Dict[str, Any]] = []
    totals = {"pending": 0, "claimed": 0, "workers_live": 0, "backlog_seconds": 0.0}

    for q in QUEUE_SECONDS_ESTIMATES:
        stats = jobs.queue_stats(q)
        fleet = _workers(q, now, freshness)
        row = {
            "queue": q,
            "pending": stats["pending"],
            "claimed": stats["claimed"],
            "oldest_pending_age_seconds": stats["oldest_pending_age_seconds"],
            "workers_live": sum(w["state"] != "booting" for w in fleet),
            "workers_max": _max_workers(q),
            "est_seconds": QUEUE_SECONDS_ESTIMATES[q],
            "backlog_seconds": jobs.backlog_seconds(q),
            "next": [{"id": j["id"], "game_id": j["game_id"], "build_id": j["build_id"],
                      "waiting_seconds": now - j["created_at"], "est_seconds": QUEUE_SECONDS_ESTIMATES[q]}
                     for j in jobs.pending_jobs_head(q, _NEXT_LIMIT)],
            "workers": fleet,
            "stockouts": _stockouts(q, now),
        }
        queues.append(row)
        for k in totals:
            totals[k] += row[k]

    return {"queues": queues, "totals": totals}


_USAGE_COLUMNS = {
    "landing": "landing_view", "demo_plays": "demo_played", "share_views": "share_viewed",
    "share_plays": "share_played", "own_plays": "game_played", "builds": "build_started",
}
_HOT_LIST = 20


def _hot_shares(rows: List[Dict]) -> List[Dict]:
    by_game: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        g = by_game.setdefault(r["game_id"], {"views": 0, "plays": 0, "last_at": 0.0, "referrers": {}})
        g["last_at"] = max(g["last_at"], r["last_at"])
        if r["kind"] == "share_played":
            g["plays"] += r["n"]
            continue
        g["views"] += r["n"]
        host = r["host"] or "direct"
        g["referrers"][host] = g["referrers"].get(host, 0) + r["n"]
    handles = {u.id: u.handle for u in store.list_users()}
    hot = sorted(by_game.items(), key=lambda kv: (kv[1]["views"], kv[1]["plays"]), reverse=True)
    out = []
    for run_id, g in hot[:_HOT_LIST]:
        row = games.game(run_id) or {}
        out.append({"run_id": run_id, "share_id": shares.share_id_of(run_id),
                    "title": row.get("title") or run_id, "owner": handles.get(row.get("user_id")),
                    **g, "referrers": sorted(g["referrers"].items(), key=lambda kv: -kv[1])})
    return out


@router.get("/analytics")
async def get_analytics(days: int = 14, _: User = Depends(require_admin)) -> Dict[str, Any]:
    days = max(1, min(days, 90))
    now = time.time()
    since = now - days * _DAY_SECONDS
    rollup = events.user_event_rollup(since)
    every_signup = store.signup_times()
    signups = [t for t in every_signup if t >= since]
    day_names = [time.strftime("%Y-%m-%d", time.gmtime(now - i * _DAY_SECONDS)) for i in range(days)]
    by_day: Dict[str, Dict[str, int]] = {d: {"signups": 0, "active": 0, **dict.fromkeys(_USAGE_COLUMNS, 0)}
                                         for d in day_names}
    kind_to_col = {k: col for col, k in _USAGE_COLUMNS.items()}
    for r in rollup["kinds"]:
        if r["day"] in by_day and r["kind"] in kind_to_col:
            by_day[r["day"]][kind_to_col[r["kind"]]] += r["n"]
    for r in rollup["users"]:
        if r["day"] in by_day:
            by_day[r["day"]]["active"] = r["n"]
    for t in signups:
        day = time.strftime("%Y-%m-%d", time.gmtime(t))
        if day in by_day:
            by_day[day]["signups"] += 1
    totals = {col: sum(d[col] for d in by_day.values()) for col in _USAGE_COLUMNS}
    totals.update(users=len(every_signup), signups=len(signups), active=rollup["active"],
                  **ledger.purchase_totals(since))
    return {
        "totals": totals,
        "days": [{"day": d, **by_day[d]} for d in day_names],
        "shares": await asyncio.to_thread(_hot_shares, rollup["shares"]),
    }


@router.get("/violations")
async def list_violations(_: User = Depends(require_admin)) -> Dict[str, Any]:
    """Safety refusals, with the offending account's handle."""
    handles = {u.id: u.handle for u in store.list_users()}
    rows = events.list_violations()
    for r in rows:
        r["handle"] = handles.get(r["user_id"])
    return {"violations": rows}


_COST_CACHE_TTL = 300.0
_MAX_COST_DAYS = 30
_cost_cache: Dict[int, Dict[str, Any]] = {}

Spend = Dict[str, float]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _ledger(since: float, now: float) -> Optional[List[Dict[str, Any]]]:
    """Returns RunPod's hourly billing records since `since`, or None if unreachable."""
    key = (settings_manager.get_settings().get("runpod") or {}).get("api_key")
    if not key:
        return None
    try:
        return [{"pod_id": r["podId"],
                 "end": calendar.timegm(time.strptime(r["endTime"], "%Y-%m-%dT%H:%M:%SZ")),
                 "total": float(r.get("totalAmount") or 0),
                 "gpu": float(r.get("gpuAmount") or 0),
                 "disk": float(r.get("diskAmount") or 0)}
                for r in RunPodClient(key).billing_pods(_iso(since), _iso(now), bucket="hour")]
    except Exception:
        return None


def _attribute(records: List[Dict[str, Any]],
               lives: Dict[str, List[Tuple[float, str]]]) -> Tuple[Dict[str, Spend], Dict[str, Spend]]:
    """Returns spend per worker id, and ghost spend per pod id."""
    by_life: Dict[str, Spend] = {}
    ghosts: Dict[str, Spend] = {}
    for r in records:
        lives_newest_first = reversed(lives.get(r["pod_id"], []))
        owner = next((wid for started, wid in lives_newest_first if started < r["end"]), None)
        slot = (by_life.setdefault(owner, {"total": 0.0, "gpu": 0.0, "disk": 0.0}) if owner
                else ghosts.setdefault(r["pod_id"], {"total": 0.0, "gpu": 0.0, "disk": 0.0}))
        for k in slot:
            slot[k] += r[k]
    return by_life, ghosts


def _launch_rate_over_lifetime(pod: Dict[str, Any], now: float) -> Spend:
    usd = pod["usd_per_hour"] * ((pod["terminated_at"] or now) - pod["started_at"]) / 3600.0
    return {"total": usd, "gpu": usd, "disk": 0.0}


def _pod_row(pod: Dict[str, Any], spend: Optional[Spend]) -> Dict[str, Any]:
    """Returns one pod life's costs row."""
    rate = pod.get("usd_per_hour")
    return {
        "worker_id": pod.get("worker_id"),
        "pod_id": pod["pod_id"],
        "source": pod.get("source"),
        "tracked": pod.get("worker_id") is not None,
        "queue": pod.get("queue"),
        "gpu_type": pod.get("gpu_type"),
        "usd_per_hour": rate,
        "started_at": pod.get("started_at"),
        "terminated_at": pod.get("terminated_at"),
        "provider_usd": round(spend["total"], 4) if spend else None,
        "disk_usd": round(spend["disk"], 4) if spend else None,
        "billed_seconds": spend["gpu"] / rate * 3600.0 if spend and rate else None,
        "jobs": pod.get("jobs", 0),
        "failed": pod.get("failed", 0),
        "exec_seconds": pod.get("exec_seconds", 0.0),
        "customer_usd": round(pod.get("customer_micros", 0) / 1e6, 4),
    }


def _games(since: float) -> Dict[str, Any]:
    """Returns the count and per-game averages of games built since `since`."""
    game_ids = games.games_built_since(since)
    per_game = games.games_exec_seconds_by_gpu(game_ids).values()
    n = len(game_ids)
    seconds = sum(g["seconds"] for g in per_game)
    usd = sum(g["micros"] for g in per_game) / 1e6
    return {"n": n,
            "avg_gpu_hours": round(seconds / 3600.0 / n, 4) if n else None,
            "avg_usd": round(usd / n, 4) if n else None,
            "avg_changes": round(games.games_change_count(game_ids) / n, 2) if n else None}


@router.get("/costs")
async def get_costs(days: int = 7, _: User = Depends(require_admin)) -> Dict[str, Any]:
    """Returns every pod life created in the last `days`, plus ghost spend."""
    days = max(1, min(days, _MAX_COST_DAYS))
    now = time.time()
    hit = _cost_cache.get(days)
    if hit is not None and now - hit["generated_at"] < _COST_CACHE_TTL:
        return hit

    since = now - days * _DAY_SECONDS
    records = await asyncio.to_thread(_ledger, since, now)
    by_life, ghosts = _attribute(records or [], workers.pod_lives())
    pods = [_pod_row(p, _launch_rate_over_lifetime(p, now) if p["source"] == "aws" else by_life.get(p["worker_id"]))
            for p in workers.pod_ledger(since)]
    pods += [_pod_row({"pod_id": pid}, spend)
             for pid, spend in sorted(ghosts.items(), key=lambda kv: -kv[1]["total"])]

    data = {"generated_at": now, "cache_seconds": _COST_CACHE_TTL, "days": days,
            "runpod_reachable": records is not None, "pods": pods, "games": _games(since)}
    _cost_cache[days] = data
    return data


@router.post("/games/{run_id}/pause")
async def pause_any_game(run_id: str, admin: User = Depends(require_admin)) -> Dict[str, Any]:
    owner = games.owner_of(run_id)
    if owner is None:
        raise HTTPException(status_code=404, detail=f"no game {run_id!r}")
    logger.warning("admin %s pausing run %s owned by %s", admin.id, run_id, owner)
    if not await asyncio.to_thread(build_chain.pause, run_id):
        raise HTTPException(status_code=409, detail="nothing is running for this run")
    return {"run_id": run_id, "status": "paused", "owner": owner}
