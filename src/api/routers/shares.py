"""The public side of a shared game: its title, its thumb, and a play session."""

from typing import Any, Dict

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse

from api.routers.events import record_anon, referrer_host
from auth import playgrants
from config.settings_manager import settings_manager
from maestro import shares

router = APIRouter()


def _require_share(share_id: str) -> None:
    if not shares.exists(share_id):
        raise HTTPException(status_code=404, detail="no such game")


@router.get("/{share_id}", response_model=Dict)
async def get_share(share_id: str):
    _require_share(share_id)
    return {"share_id": share_id,
            "title": shares.meta(share_id).get("title") or "A GameSummoner game",
            "thumb_url": f"/api/shares/{share_id}/thumb" if shares.thumb(share_id) else None}


@router.get("/{share_id}/thumb")
async def share_thumb(share_id: str):
    path = shares.thumb(share_id)
    if path is None:
        raise HTTPException(status_code=404, detail="no thumb")
    return FileResponse(path, media_type="image/png")


@router.post("/{share_id}/view", response_model=Dict)
async def share_view(share_id: str, body: Any = Body(default=None)):
    _require_share(share_id)
    await record_anon("share_viewed", {"share_id": share_id, "referrer": referrer_host(body)},
                      shares.meta(share_id).get("run_id"))
    return {"accepted": 1}


@router.post("/{share_id}/play-session", response_model=Dict)
async def share_play_session(share_id: str):
    _require_share(share_id)
    token = playgrants.issue_handoff(playgrants.target("shares", share_id))
    if token is None:
        raise HTTPException(status_code=429, detail="too many open sessions — try again shortly")
    await record_anon("share_played", {"share_id": share_id}, shares.meta(share_id).get("run_id"))
    origin = (settings_manager.get_settings().get("play") or {}).get("origin", "").rstrip("/")
    return {"url": f"{origin}/handoff?t={token}", "origin": origin}
