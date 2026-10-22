
import asyncio
import logging
import re
import threading
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from auth import playgrants
from auth.deps import get_current_user
from auth.store import User
from billing import ledger
from billing.packages import MICROS_PER_CREDIT, cost
from config.settings_manager import settings_manager
from db import events, games, jobs
from maestro.codegen import archive, build_chain
from maestro.codegen.assets import (
    AlreadyRendering,
    add_assets,
    entry_kind,
    ext_for,
    read_manifest,
    regenerate_asset,
)
from maestro import shares
from maestro.codegen.run import create_run, propose_prompt, set_prompt
from maestro.codegen.staging import (
    game_dir,
    has_authored_files,
    is_staged,
    staged_title,
)
from maestro.state import RunState
from tools.build_events import _emit
from tools.safety import log_violation, screen_text

logger = logging.getLogger(__name__)
router = APIRouter()


class NewGameBody(BaseModel):
    prompt: str


class BuildBody(BaseModel):
    prompt: Optional[str] = None
    # Start over on an EMPTY game folder instead of carrying the last attempt's files forward.
    fresh: bool = False


class ChangeBody(BaseModel):
    note: str = ""


class RegenerateBody(BaseModel):
    prompt: str
    mode: Literal["full", "img2img"] = "full"


# Asset skins in flight (one per run) — they run on the image/mesh queues, not the build GPU, so
# they don't ride the build queue. A daemon thread runs each; the set gates re-entry.
_active_lock = threading.Lock()
_active: set = set()


def _built(run_id: str) -> bool:
    return is_staged(run_id)


def _require_state(run_id: str, user: User):
    """The run's state, scoped to its owner: 404 if there's no spec, 403 if it isn't this user's."""
    owner = games.owner_of(run_id)
    state = RunState(run_id)
    if owner is None or state.read_spec() is None:
        raise HTTPException(status_code=404, detail=f"no game {run_id!r}")
    if owner != user.id:
        raise HTTPException(status_code=403, detail="not your game")
    return state


def _shown(row: Dict) -> str:
    status = row.get("status")
    if status == "running" and row.get("stage") != "designing":
        kind = (games.current_build(row["id"]) or {}).get("kind")
        return {"fix": "fixing", "change": "changing"}.get(kind, "building")
    return {"running": "building", "succeeded": "built"}.get(status, status)


def _require_not_paused(run_id: str) -> None:
    if (games.game(run_id) or {}).get("status") == "paused":
        raise HTTPException(status_code=409, detail="this game is paused — resume it first")


def _require_compute(run_id: str) -> None:
    """Refuse work a game can't pay for, BEFORE it occupies the build queue. Enqueue enforces the
    same budget per job, so this is the fast, legible failure rather than the safety net: without
    it a broke run wins the GPU slot and then thrashes on refused jobs until its step cap."""
    if not games.can_afford(run_id, "llm"):
        raise HTTPException(status_code=402, detail={
            "reason": "compute_exhausted", "run_id": run_id})


def _require_not_held(run_id: str) -> None:
    """A held game is frozen — no play, no build, no change — until a human has looked at it. The
    message is deliberately neutral: what the screen matched is for the admin panel, not the
    person probing it."""
    if (games.game(run_id) or {}).get("status") == "held":
        raise HTTPException(status_code=423,
                            detail="something went wrong with this build — we're looking into it")


@router.get("", response_model=List[Dict])
async def list_games(user: User = Depends(get_current_user)):
    """Lightweight summary of the caller's games. A built game is named by its own <title> — the
    name the model gave it — falling back to the prompt-derived row title."""
    out: List[Dict] = []
    for row in games.list_games(user.id):
        if not row["title"]:
            continue   # created but has no prompt yet — nothing to show
        built = _built(row["id"]) and row["status"] != "held"
        out.append({
            "run_id": row["id"],
            "title": staged_title(row["id"]) or row["title"],
            "status": _shown(row),
            "built": built,
            "building": row["status"] == "running",
            "paused": row["status"] == "paused",
            "mtime": row["updated_at"],
        })
    out.sort(key=lambda g: g["mtime"], reverse=True)
    return out


@router.post("", response_model=Dict)
async def create_game(body: NewGameBody, user: User = Depends(get_current_user)):
    """Make a new game: the words the user wrote become the run's ask, its DESIGN starts, and the
    build starts the moment the design lands (`prompt_proposed`, then `build_started`). Nothing
    exists server-side until this call, so an abandoned box leaves nothing behind.

    The credit is charged here: the design is the game's first inference, and it meters against
    the grant it buys."""
    text = body.prompt.strip()
    if not text:
        raise HTTPException(status_code=400, detail="the prompt is empty")
    violation = screen_text(text)
    if violation is not None:
        log_violation(violation, user_id=user.id, source="new_game")
        raise HTTPException(status_code=400, detail="this prompt can't be built")

    price = cost({"request": text})
    if ledger.balance(user.id) < price:
        raise HTTPException(status_code=402, detail={
            "reason": "insufficient_credits", "balance": ledger.balance(user.id), "cost": price})

    run_id = await asyncio.to_thread(create_run, user.id)
    if not ledger.deduct(user.id, price, "build", run_id):
        raise HTTPException(status_code=402, detail={
            "reason": "insufficient_credits", "balance": ledger.balance(user.id), "cost": price})
    games.charge_game(run_id, price, price * MICROS_PER_CREDIT)
    await asyncio.to_thread(propose_prompt, text, run_id)
    return {"run_id": run_id, "status": "designing"}


@router.get("/{run_id}", response_model=Dict)
async def get_game(run_id: str, user: User = Depends(get_current_user)):
    """Full detail for one game: the build prompt, built/building state, and live status."""
    state = _require_state(run_id, user)
    spec_data = state.read_spec()
    row = games.game(run_id) or {}
    built = _built(run_id) and row.get("status") != "held"
    return {
        "run_id": run_id,
        "ask": spec_data.get("ask", ""),
        # None while the design is still being written — the page's only signal for that state.
        "prompt": spec_data.get("request"),
        "title": staged_title(run_id) or spec_data.get("title", ""),
        "built": built,
        "building": row.get("status") == "running",
        "status": _shown(row),
        "assets_exist": (game_dir(state.run_dir) / "assets.json").exists(),
        # Whether a previous attempt left files behind — what the from-scratch build would discard.
        "has_game": has_authored_files(state.run_dir),
        "credits_spent": row.get("credits_spent", 0),
        "share_id": shares.share_id_of(run_id),
        # Compute budget as a fraction remaining (0..1), never raw spend — spent_micros is
        # deliberately not surfaced (it would expose actual GPU spend). None ⇒ uncharged, no bar.
        "budget_pct_remaining": _budget_pct(row, run_id),
    }


@router.post("/{run_id}/play-session", response_model=Dict)
async def play_session(run_id: str, user: User = Depends(get_current_user)):
    """Mint a play session for a built game: a single-use handoff URL the SPA points its iframe
    (or a new tab) at. The bearer token proves ownership HERE, on the app origin; what reaches
    the game origin is only the short-lived token — see auth/playgrants.py. `origin` is what the
    parent page must verify reporter postMessages against ('' ⇒ games share the app origin)."""
    _require_state(run_id, user)
    _require_not_held(run_id)
    if not _built(run_id):
        raise HTTPException(status_code=409, detail="not built yet")
    # An evicted game is a download away from playable — pull it back before minting a session.
    await asyncio.to_thread(archive.ensure_local, run_id)
    origin = (settings_manager.get_settings().get("play") or {}).get("origin", "").rstrip("/")
    token = playgrants.issue_handoff(playgrants.target("games", run_id))
    if token is None:
        raise HTTPException(status_code=429, detail="too many open sessions — try again shortly")
    return {"url": f"{origin}/handoff?t={token}", "origin": origin}


@router.post("/{run_id}/share", response_model=Dict)
async def share_game(run_id: str, user: User = Depends(get_current_user)):
    _require_state(run_id, user)
    _require_not_held(run_id)
    if not _built(run_id):
        raise HTTPException(status_code=409, detail="not built yet")
    await asyncio.to_thread(archive.ensure_local, run_id)
    return {"share_id": await asyncio.to_thread(shares.share, run_id)}


@router.delete("/{run_id}/share", response_model=Dict)
async def unshare_game(run_id: str, user: User = Depends(get_current_user)):
    _require_state(run_id, user)
    await asyncio.to_thread(shares.unshare, run_id)
    return {"share_id": None}


def _budget_pct(row: Dict, run_id: str) -> Optional[float]:
    granted = row.get("granted_micros", 0)
    if granted <= 0:
        return None
    return max(0.0, min(1.0, games.compute_remaining(run_id) / granted))


_ASSET_ID = re.compile(r"^[a-zA-Z0-9_-]+$")
_MEDIA = {"webp": "image/webp", "png": "image/png", "glb": "model/gltf-binary"}


def _ext(entry: Dict) -> str:
    return ext_for(entry_kind(entry))


@router.get("/{run_id}/assets", response_model=List[Dict])
async def game_assets(run_id: str, user: User = Depends(get_current_user)):
    """The game's own asset manifest with per-asset render status. Empty until the game declares
    one. `status`: ready (the file is on disk), rendering (a batch is in flight), or pending. The
    bytes come from the sibling blob route, so the frontend never touches the public /play mount."""
    state = _require_state(run_id, user)
    assets_dir = game_dir(state.run_dir) / "assets"
    rendering = jobs.has_active_batch(run_id)
    out: List[Dict] = []
    for entry in read_manifest(state.run_dir):
        aid = entry["id"]
        ready = (assets_dir / f"{aid}.{_ext(entry)}").exists()
        if ready:
            status = "ready"
        elif entry.get("refused"):
            # The message is neutral on purpose — the policy's reasoning is the admin panel's.
            status = "blocked"
        else:
            status = "rendering" if rendering else "pending"
        out.append({"id": aid, "kind": entry_kind(entry), "status": status,
                    "prompt": entry["prompt"],
                    "defect": "this render was blocked" if status == "blocked"
                              else entry.get("defect")})
    return out


@router.get("/{run_id}/assets/{asset_id}")
async def game_asset_blob(run_id: str, asset_id: str, user: User = Depends(get_current_user)):
    """Stream one rendered asset (png or glb), authed + ownership-checked — the management UI
    never reads the public /play static path."""
    state = _require_state(run_id, user)
    if not _ASSET_ID.match(asset_id):
        raise HTTPException(status_code=400, detail="bad asset id")
    assets_dir = game_dir(state.run_dir) / "assets"
    for ext, media in _MEDIA.items():
        path = assets_dir / f"{asset_id}.{ext}"
        if path.exists():
            return FileResponse(path, media_type=media)
    raise HTTPException(status_code=404, detail="no such asset")


@router.post("/{run_id}/assets/{asset_id}/regenerate", response_model=Dict)
async def regenerate_game_asset(run_id: str, asset_id: str, body: RegenerateBody,
                                user: User = Depends(get_current_user)):
    """Re-render ONE asset with a change note, without re-rendering the whole game. Enqueues a
    single image job through the same finalize a full render uses, so assets_done fires and the
    gallery refetches."""
    _require_state(run_id, user)
    if not _ASSET_ID.match(asset_id):
        raise HTTPException(status_code=400, detail="bad asset id")
    note = body.prompt.strip()
    if not note:
        raise HTTPException(status_code=400, detail="a prompt is required")
    _require_not_held(run_id)
    _require_not_paused(run_id)
    violation = screen_text(note)
    if violation is not None:
        log_violation(violation, user_id=user.id, source="regenerate_note", run_id=run_id)
        raise HTTPException(status_code=400, detail="this note can't be applied")
    _require_compute(run_id)
    out = await asyncio.to_thread(regenerate_asset, run_id, asset_id, note, body.mode)
    if not out["ok"]:
        raise HTTPException(status_code=400, detail=out["error"])
    return {"status": "regenerating", "run_id": run_id, "asset_id": asset_id}


@router.get("/{run_id}/events", response_model=List[Dict])
async def game_events(run_id: str, after: int = 0, user: User = Depends(get_current_user)):
    """The game's durable event log (spec/build lifecycle), for catch-up after a reconnect —
    the websocket only delivers what happens while a socket is open."""
    _require_state(run_id, user)
    return events.events_for(run_id, after_id=after)


@router.post("/{run_id}/build", response_model=Dict)
async def build_game(run_id: str, body: BuildBody = BuildBody(),
                     user: User = Depends(get_current_user)):
    """Start a build by hand — a rebuild of a built game, a retry after the design's own kickoff
    was refused, or the from-scratch button. Progress streams over the websocket.

    `prompt` carries the user's edit of the design: the build is the only thing that writes it.
    The driver reads it from disk, not from this request — a build outlives the process that
    started it.

    `fresh` is the from-scratch button: it empties the game folder first, so the model opens on
    nothing rather than on a dead build's half-written files. Without it a re-trigger carries them
    forward, which is what a resumed build wants and what a second attempt does not.

    A run is charged ONCE, gated on a durable `charged` flag — create charges it before the design,
    so this path only charges a run that was made without one. Charged stays charged — there is
    no automatic refund."""
    state = _require_state(run_id, user)
    _require_not_held(run_id)
    if build_chain.is_running(run_id):
        raise HTTPException(status_code=409, detail="build already in progress")
    # A build or change on an evicted run must open on its real files, not an empty seed.
    await asyncio.to_thread(archive.ensure_local, run_id)
    if body.prompt is not None:
        violation = screen_text(body.prompt)
        if violation is not None:
            log_violation(violation, user_id=user.id, source="prompt_edit")
            raise HTTPException(status_code=400, detail="this prompt can't be built")
        try:
            await asyncio.to_thread(set_prompt, run_id, body.prompt)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    spec_data = state.read_spec()
    if "request" not in spec_data:
        raise HTTPException(status_code=409, detail="the design is still being written")
    if not games.is_charged(run_id):
        price = cost(spec_data)
        if not ledger.deduct(user.id, price, "build", run_id):
            raise HTTPException(status_code=402, detail={
                "reason": "insufficient_credits", "balance": ledger.balance(user.id), "cost": price})
        games.charge_game(run_id, price, price * MICROS_PER_CREDIT)
    _require_compute(run_id)

    # kickoff seeds the game folder before enqueueing the first llm turn, so it touches disk.
    if await asyncio.to_thread(build_chain.kickoff, run_id, kind="build", fresh=body.fresh) is None:
        raise HTTPException(status_code=409, detail="build already in progress")
    return {"status": "building", "run_id": run_id}


@router.post("/{run_id}/pause", response_model=Dict)
async def pause_game(run_id: str, user: User = Depends(get_current_user)):
    _require_state(run_id, user)
    if not await asyncio.to_thread(build_chain.pause, run_id):
        raise HTTPException(status_code=409, detail="nothing is running for this run")
    return {"run_id": run_id, "status": "paused"}


@router.post("/{run_id}/resume", response_model=Dict)
async def resume_game(run_id: str, user: User = Depends(get_current_user)):
    _require_state(run_id, user)
    _require_not_held(run_id)
    _require_compute(run_id)
    if await asyncio.to_thread(build_chain.resume, run_id):
        return {"run_id": run_id, "status": "building"}
    if not games.is_charged(run_id):
        raise HTTPException(status_code=409, detail="no build to resume for this run")
    if await asyncio.to_thread(build_chain.kickoff, run_id, kind="build") is None:
        raise HTTPException(status_code=409, detail="build already in progress")
    return {"status": "building", "run_id": run_id}


@router.post("/{run_id}/change", response_model=Dict)
async def change_game(run_id: str, body: ChangeBody, user: User = Depends(get_current_user)):
    """Change a built game from a free-text note ('make the enemies faster'). It re-enters the
    same turn machine a build runs, so the two can't run at once. Progress + completion stream
    over the websocket as a build's own events (build_started, build_step, build_done)."""
    _require_state(run_id, user)
    _require_not_held(run_id)
    violation = screen_text(body.note)
    if violation is not None:
        log_violation(violation, user_id=user.id, source="change_note", run_id=run_id)
        raise HTTPException(status_code=400, detail="this note can't be applied")
    _require_compute(run_id)
    busy = HTTPException(status_code=409,
                         detail="a build or change is already running for this run")
    if build_chain.is_running(run_id):
        raise busy
    await asyncio.to_thread(archive.ensure_local, run_id)
    if await asyncio.to_thread(build_chain.kickoff, run_id, kind="change", note=body.note) is None:
        raise busy
    return {"status": "changing", "run_id": run_id}


@router.post("/{run_id}/assets", response_model=Dict)
async def skin_assets(run_id: str, user: User = Depends(get_current_user)):
    """Render the art the game declared in assets.json, on a background thread. Emits
    assets_started / assets_done over the websocket."""
    _require_state(run_id, user)
    _require_not_held(run_id)
    _require_not_paused(run_id)
    _require_compute(run_id)
    key = f"assets:{run_id}"
    # Two guards, because the stage outlives its thread: the key covers the plan-and-enqueue
    # half, the batch query covers the queued half. Without the second, a double-click enqueues a
    # second full set of image/mesh jobs and pays for them.
    if jobs.has_active_batch(run_id):
        raise HTTPException(status_code=409, detail="assets are already rendering for this run")
    with _active_lock:
        if key in _active:
            raise HTTPException(status_code=409, detail="assets are already rendering for this run")
        _active.add(key)

    def _run(build_id: str):
        games.build_started(build_id)
        _emit("assets_started", run_id, build_id=build_id)
        try:
            # Returns once the asset jobs are ENQUEUED. assets_done and build_finished are the
            # batch finalize's job, since the render outlives this thread by minutes.
            add_assets(run_id, build_id=build_id)
        except AlreadyRendering:
            # Lost the race to the build's own asset lane — that batch owns the endgame.
            games.build_finished(build_id, "failed")
        except Exception:
            logger.exception("asset render failed for %s", run_id)
            _emit("assets_done", run_id, build_id=build_id, ok=False, rendered=[])
            games.build_finished(build_id, "failed")
        finally:
            with _active_lock:
                _active.discard(key)

    # Anything that throws before the thread owns the key has to hand it back, or every later
    # skin of this run 409s until the process restarts.
    try:
        build_id = games.create_build(run_id, kind="assets")
        threading.Thread(target=_run, args=(build_id,), daemon=True, name=f"assets-{run_id}").start()
    except Exception:
        with _active_lock:
            _active.discard(key)
        raise
    return {"status": "skinning", "run_id": run_id}
