"""The public demo surface: the landing page's playable games, the run ids listed in `demo_games`."""

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.routers.events import record_anon
from auth import playgrants
from config.settings_manager import settings_manager
from maestro import demos

router = APIRouter()


def _demo_entries() -> List[Dict]:
    """The listed entries: {"id": run_id, "thumb": a path inside the game or None}."""
    return [{"id": str(e["id"]), "thumb": e.get("thumb")}
            for e in settings_manager.get_settings().get("demo_games") or []
            if isinstance(e, dict) and e.get("id")]


def _thumb_of(run_id: str) -> Optional[str]:
    return next((e["thumb"] for e in _demo_entries() if e["id"] == run_id), None)


@router.get("", response_model=List[Dict])
async def list_demos():
    """Each listed demo that has a snapshot, with the prompt that made it."""
    return [{"run_id": e["id"],
             "title": demos.title(e["id"]) or e["id"],
             "prompt": demos.meta(e["id"]).get("prompt", ""),
             "thumb_url": f"/api/demos/{e['id']}/thumb" if e["thumb"] else None}
            for e in _demo_entries() if demos.exists(e["id"])]


@router.get("/{run_id}/thumb")
async def demo_thumb(run_id: str):
    """The one image the owner picked from the snapshot's own files. Public like the list — it
    is the card's face — but only for listed games, and never a path outside the game."""
    thumb = _thumb_of(run_id)
    if not thumb:
        raise HTTPException(status_code=404, detail="no thumb")
    base = demos.demo_dir(run_id).resolve()
    path = (base / thumb).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise HTTPException(status_code=404, detail="no thumb")
    # The deploy image's mimetypes table lacks webp — the format every render saves as.
    media = {".webp": "image/webp"}.get(path.suffix.lower())
    return FileResponse(path, media_type=media)


@router.post("/{run_id}/play-session", response_model=Dict)
async def demo_play_session(run_id: str):
    """A play session anyone may mint, for listed games only. The grant opens this one
    snapshot's static files and nothing else."""
    if run_id not in {e["id"] for e in _demo_entries()} or not demos.exists(run_id):
        raise HTTPException(status_code=404, detail="not a demo")
    token = playgrants.issue_handoff(playgrants.target("demos", run_id))
    if token is None:
        raise HTTPException(status_code=429, detail="too many open sessions — try again shortly")
    await record_anon("demo_played", {}, run_id)
    origin = (settings_manager.get_settings().get("play") or {}).get("origin", "").rstrip("/")
    return {"url": f"{origin}/handoff?t={token}", "origin": origin}
