"""The public demo surface — the landing page's playable games.

Unauthenticated and read-only, limited to the run ids the OWNER listed in settings
(`demo_games`): curation is a deploy-time decision, never a flag a build can set. The list is
TIERED — `showcase` is what the machine can achieve (playtest notes, fix rounds), `oneshot`
is what a single sentence gets, kept as it first came out — and the tier is the owner's claim,
curated exactly like membership. Playing costs nothing meterable — the files are static and the
grant is an in-memory entry — and the games run on the play origin, which holds no API and no
credential (auth/playgrants.py)."""

from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from api.routers.events import record_anon
from auth import playgrants
from config.settings_manager import settings_manager
from maestro import demos

router = APIRouter()


TIERS = ("showcase", "oneshot")


def _demo_tiers() -> Dict[str, List[Dict]]:
    """Each tier's entries: {"id": run_id, "thumb": path-inside-the-game or None}. The thumb is
    curation like the listing itself — the owner names one of the game's own files."""
    cfg = settings_manager.get_settings().get("demo_games") or {}
    out: Dict[str, List[Dict]] = {}
    for tier in TIERS:
        out[tier] = [{"id": str(e["id"]), "thumb": e.get("thumb")}
                     for e in (cfg.get(tier) or []) if isinstance(e, dict) and e.get("id")]
    return out


def _demo_ids() -> List[str]:
    tiers = _demo_tiers()
    return [e["id"] for tier in TIERS for e in tiers[tier]]


def _thumb_of(run_id: str) -> Optional[str]:
    for tier in TIERS:
        for e in _demo_tiers()[tier]:
            if e["id"] == run_id:
                return e.get("thumb")
    return None


@router.get("", response_model=List[Dict])
async def list_demos():
    """Each demo with the prompt that made it and the tier it was listed under — the product
    story is the pairing, not the game alone. A listed id with no snapshot is skipped, not an
    error: the list lives in settings and the snapshots on disk, and a redeploy may see one
    before the other."""
    out = []
    tiers = _demo_tiers()
    for tier in TIERS:
        for entry in tiers[tier]:
            run_id = entry["id"]
            if not demos.exists(run_id):
                continue
            out.append({"run_id": run_id,
                        "title": demos.title(run_id) or run_id,
                        "prompt": demos.meta(run_id).get("prompt", ""),
                        "tier": tier,
                        "thumb_url": f"/api/demos/{run_id}/thumb" if entry.get("thumb") else None})
    return out


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
    if run_id not in _demo_ids() or not demos.exists(run_id):
        raise HTTPException(status_code=404, detail="not a demo")
    token = playgrants.issue_handoff(playgrants.target("demos", run_id))
    if token is None:
        raise HTTPException(status_code=429, detail="too many open sessions — try again shortly")
    await record_anon("demo_played", {}, run_id)
    origin = (settings_manager.get_settings().get("play") or {}).get("origin", "").rstrip("/")
    return {"url": f"{origin}/handoff?t={token}", "origin": origin}
