"""User-action analytics intake — the SPA's `track()` module posts batches here.
Rows land in the events table keyed by user_id; a bad row is DROPPED, never a 400.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from fastapi import APIRouter, Body, Depends

from auth.deps import get_current_user
from auth.store import User
from db import events

router = APIRouter()


KINDS = frozenset({
    "page_view", "create_opened", "build_started", "game_played", "change_sent",
    "credits_opened", "purchase_started",
})
ANON_USER = "anon"
MAX_REFERRER_HOST = 253
MAX_BATCH = 100
MAX_PAYLOAD_CHARS = 2048
# Client timestamps are advisory (a flush can lag its events); anything outside this window is
# clock skew and takes the server's time instead.
MAX_TS_AGE = 7 * 24 * 3600
MAX_TS_AHEAD = 300
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@router.post("", response_model=Dict)
async def ingest(batch: List[Any] = Body(...), user: User = Depends(get_current_user)):
    now = time.time()
    rows: List[Dict] = []
    for raw in batch[:MAX_BATCH]:
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            continue
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > MAX_PAYLOAD_CHARS:
            continue
        run_id = raw.get("run_id")
        if not (isinstance(run_id, str) and _RUN_ID.match(run_id)):
            run_id = None
        ts = raw.get("ts")
        created = ts / 1000.0 if isinstance(ts, (int, float)) else now
        if not (now - MAX_TS_AGE <= created <= now + MAX_TS_AHEAD):
            created = now
        rows.append({"kind": raw["kind"], "payload": payload,
                     "game_id": run_id, "created_at": created})
    if rows:
        await asyncio.to_thread(events.record_user_events, user.id, rows)
    return {"accepted": len(rows)}


def referrer_host(body: Any) -> str:
    referrer = body.get("referrer") if isinstance(body, dict) else None
    host = urlsplit(referrer).hostname if isinstance(referrer, str) else None
    return (host or "")[:MAX_REFERRER_HOST]


async def record_anon(kind: str, payload: Dict, game_id: Optional[str] = None) -> None:
    await asyncio.to_thread(events.record_user_events, ANON_USER, [
        {"kind": kind, "payload": payload, "game_id": game_id, "created_at": time.time()}])


@router.post("/landing", response_model=Dict)
async def landing(body: Any = Body(default=None)):
    await record_anon("landing_view", {"referrer": referrer_host(body)})
    return {"accepted": 1}
