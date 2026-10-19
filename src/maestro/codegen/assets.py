"""The asset stage: `generate_media` enqueues the art the game asked for and records it in
`game/assets.json`."""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from PIL import Image

from db import errors, games, jobs
from maestro.codegen.staging import game_dir
from maestro.state import RunState
from tools.comfyui_tools import MATTED_KINDS, build_image_payload
from tools.execution_context import run_scope

logger = logging.getLogger(__name__)

MANIFEST = "assets.json"

KINDS = ("sprite", "actor", "tile", "scene", "mesh")
DEFAULT_KIND = "sprite"


def render_kind(kind: str) -> str:
    return "sprite" if kind == "actor" else kind


def compose_prompt(subject: str, style: str, details: Optional[Dict]) -> str:
    parts = [style.strip().rstrip("."), subject.strip().rstrip(".")]
    view = ((details or {}).get("view") or "").strip().rstrip(".")
    if view:
        parts[1] += f", {view} view"
    return ". ".join(parts) + "."


_active: set = set()
_active_lock = threading.Lock()


class AlreadyRendering(Exception):
    """A batch for this run is already in flight."""


def manifest_path(run_dir) -> Path:
    return game_dir(run_dir) / MANIFEST


def read_manifest(run_dir) -> List[Dict]:
    """The manifest's image entries, or [] when there is no manifest or it is malformed. A game
    writing junk here loses its art, never its build."""
    p = manifest_path(run_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("assets: %s is not valid JSON", p)
        return []
    entries = data.get("images") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    return [e for e in entries
            if isinstance(e, dict) and e.get("id") and isinstance(e.get("prompt"), str)]


def asset_path(run_id: str, asset_id: str, ext: str) -> Path:
    d = game_dir(RunState(run_id).run_dir) / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{asset_id}.{ext}"


def check_render(path: Path, kind: str) -> Optional[str]:
    """A defect in a finished render, or None. BROKEN only — nothing here has an opinion about
    whether the picture is any good, because code cannot have one.

    What it can read is the alpha channel against what the kind asked for. A matted sprite that
    came back fully opaque is a scene the matte never cut, and the game will composite a square of
    someone else's background onto its own; one that came back empty is a subject the matte ate.
    A tile or a scene is the reverse — it fills its frame, so transparency in one is a hole."""
    im = Image.open(path).convert("RGBA")
    total = im.width * im.height
    if not total:
        return "the render is empty"
    hist = im.split()[-1].histogram()
    opaque = sum(hist[200:]) / total
    if kind in MATTED_KINDS:
        if opaque > 0.98:
            return ("the background was not removed — this render is a full opaque square, so it "
                    "will show its own background wherever the game draws it")
        if opaque < 0.02:
            return "almost nothing is left after the background was removed"
    elif opaque < 0.9:
        return f"a {kind} must fill its frame, and {(1 - opaque) * 100:.0f}% of this one is transparent"
    return None


WEBP_QUALITY = 90


def save_image(im: Image.Image, path: Path) -> None:
    """One writer for rendered art: the format rides the path's suffix, and webp always carries
    the one quality the pipeline uses — PIL's default (80) is a silent downgrade."""
    if path.suffix.lower() == ".webp":
        im.save(path, "WEBP", quality=WEBP_QUALITY)
    else:
        im.save(path)


def autocrop_image(im: Image.Image, pad_frac: float = 0.06) -> Image.Image:
    """Tighten a matted sprite to its opaque subject. ComfyUI renders on a 1024 frame with wide
    transparent margins, so a sprite drawn at a small on-screen size shows the subject at a fraction
    of its box — cropping to the alpha bbox (plus a small margin) makes it fill the box."""
    bbox = im.split()[-1].getbbox()
    if not bbox:
        return im
    pad = int(max(im.width, im.height) * pad_frac)
    box = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
           min(im.width, bbox[2] + pad), min(im.height, bbox[3] + pad))
    return im.crop(box)


def ext_for(kind: str) -> str:
    return "glb" if kind == "mesh" else "webp"


def write_placeholder(path: Path, kind: str, asset_id: str) -> None:
    """A stand-in at the promised path: a `drawImage` of a missing file throws every frame."""
    from PIL import ImageDraw
    hue = sum(asset_id.encode()) % 360
    rgb = Image.new("HSV", (1, 1), (int(hue * 255 / 360), 140, 200)).convert("RGB").getpixel((0, 0))
    if kind in MATTED_KINDS:
        im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((24, 24, 232, 232), fill=rgb + (255,))
    else:
        im = Image.new("RGBA", (512, 512), rgb + (255,))
        d = ImageDraw.Draw(im)
        for y in range(0, 512, 64):
            for x in range(0, 512, 64):
                if (x + y) // 64 % 2:
                    d.rectangle((x, y, x + 63, y + 63), fill=tuple(c * 7 // 8 for c in rgb) + (255,))
    save_image(im, path)


def landed(run_id: str, entry: Dict) -> bool:
    """Whether an asked-for asset's REAL file is on disk — the placeholder at the same path does
    not count."""
    return (not entry.get("placeholder")
            and asset_path(run_id, entry["id"], ext_for(entry_kind(entry))).exists())


def _pending(run_id: str, run_dir, entries: List[Dict]) -> List[Dict]:
    """Entries whose render hasn't landed — so a re-run tops up rather than re-paying for art
    that already rendered."""
    out = []
    for e in entries:
        if e.get("refused"):
            continue
        if not landed(run_id, e):
            out.append(e)
    return out


# An id names a file and is handed back to the asset routes, so it carries only what both accept.
MEDIA_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _record(run_dir, entry: Dict) -> None:
    """Append one requested asset to the manifest, AFTER its enqueue lands — the file records what
    was actually asked of the queue."""
    p = manifest_path(run_dir)
    data = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    images = data.get("images") if isinstance(data, dict) else None
    if not isinstance(images, list):
        images = []
    data = {"images": images + [entry]}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _update(run_dir, asset_id: str, **fields) -> None:
    """Rewrite one manifest entry in place. `None` drops the key."""
    p = manifest_path(run_dir)
    entries = read_manifest(run_dir)
    for e in entries:
        if e["id"] == asset_id:
            for k, v in fields.items():
                e.pop(k, None) if v is None else e.update({k: v})
    p.write_text(json.dumps({"images": entries}, indent=2, ensure_ascii=False), encoding="utf-8")


def set_defect(run_dir, asset_id: str, defect: Optional[str]) -> None:
    """Record (or clear) what a landed render came back broken as."""
    _update(run_dir, asset_id, defect=defect)


def set_landed(run_dir, asset_id: str) -> None:
    """The real file has replaced the placeholder at its path."""
    _update(run_dir, asset_id, placeholder=None)


# Above the measured innocent ceiling (0.954): the classifier overcalls stylized game art, and a
# lower bar refuses real builds' cows and cabbages — see docs/experiments.md 2026-08-03. The
# pre-gen prompt screen owns steered content; this refuses only the unmistakable render.
NSFW_REFUSE_THRESHOLD = 0.98


def render_verdict(img_entry: Optional[Dict]) -> Optional[str]:
    safety = (img_entry or {}).get("safety") or {}
    scores = safety.get("scores")
    if not scores:
        return "no safety verdict — " + (safety.get("error") or "the render carried no scores")
    if scores.get("NSFW", 1.0) >= NSFW_REFUSE_THRESHOLD:
        return f"explicit content (score {scores['NSFW']:.2f})"
    return None


def set_refused(run_dir, asset_id: str, reason: str) -> None:
    """Mark a manifest entry whose render was refused by policy — the top-up skips it (the same
    prompt would render and refuse again), and a regenerate with a new note clears it."""
    _update(run_dir, asset_id, refused=reason)


def entry_kind(entry: Dict) -> str:
    """The kind the pipeline renders a manifest entry as."""
    return render_kind(entry.get("kind") or DEFAULT_KIND)


def _then_for(kind: str, finalize: str = "assets") -> Dict:
    if kind == "mesh":
        return {"enqueue": "mesh_from_image", "finalize": finalize}
    op = "save_sprite" if kind in MATTED_KINDS else "save_flat"
    return {"operations": [op], "finalize": finalize}


def request_media(run_id: str, run_dir, build_id: str, asset_id: str, subject: str, style: str,
                  kind: str = DEFAULT_KIND, details: Optional[Dict] = None) -> Dict:
    """Enqueues one render and returns the file path that will eventually be filled."""
    if details is not None and not isinstance(details, dict):
        return {"ok": False, "error": "details must be an object: {body, view}"}
    details = details or None
    prompt = compose_prompt(subject, style, details)
    rkind = render_kind(kind)

    mesh = kind == "mesh"
    ext = ext_for(rkind)
    rel = f"assets/{asset_id}.{ext}"
    existing = next((e for e in read_manifest(run_dir) if e["id"] == asset_id), None)
    if not existing and asset_path(run_id, asset_id, ext).exists():
        return {"ok": True, "path": rel, "status": "ready"}
    if existing and not existing.get("replace_asked"):
        # An id asked for twice is usually the model losing track, so the first repeat is ANSWERED
        # rather than obeyed — the done-nudge shape. What it must not do is agree and keep the old
        # picture, which is what someone asking for the art to be redrawn used to get.
        _update(run_dir, asset_id, replace_asked=True)
        return {"ok": True, "path": rel,
                "status": "ready" if landed(run_id, existing) else "rendering",
                "note": "not requeued — this id already has art. Call again with the prompt you "
                        "want to replace it with, or use a different id to draw something new."}

    payload = build_image_payload(prompt, rkind)
    if payload is None:
        return {"ok": False, "error": "that prompt was refused by the safety filter — "
                                      "draw this one with code instead"}
    then = _then_for(rkind)
    try:
        jobs.enqueue_job("image", payload, game_id=run_id, build_id=build_id,
                             batch_id=uuid.uuid4().hex[:16],
                             metadata={"run_id": run_id, "asset_id": asset_id, "kind": rkind,
                                       "then": then})
    except errors.InsufficientCompute:
        return {"ok": False, "error": "no compute left for art — draw this one with code instead"}
    # A confirmed replace keeps the art it has until the new render lands.
    stand_in = not mesh and not asset_path(run_id, asset_id, ext).exists()
    if stand_in:
        write_placeholder(asset_path(run_id, asset_id, ext), rkind, asset_id)
    fields = {"kind": kind, "subject": subject, "style": style, "prompt": prompt,
              **({"details": details} if details else {})}
    if existing:
        # The manifest holds the request a regenerate re-prompts against, so a confirmed replace
        # rewrites it rather than leaving the record describing art that no longer exists.
        _update(run_dir, asset_id, replace_asked=None, defect=None, refused=None,
                placeholder=True if stand_in else None, **fields)
    else:
        _record(run_dir, {"id": asset_id, "file": rel, **fields, "placeholder": True})
    logger.info("assets %s: %s requested (%s)", run_id, asset_id, rkind)
    out = {"ok": True, "path": rel, "status": "rendering"}
    if mesh:
        out["note"] = ("the model will span exactly 1 unit at its longest side — scale it in the "
                       "scene to its real-world size (person ≈ 1.7 units, building ≈ 6+).")
    elif stand_in:
        out["note"] = "a stand-in image is at that path now; the render replaces it when it lands."
    return out


def start_from_manifest(run_id: str, run_dir, build_id: str) -> Optional[str]:
    """Enqueue every missing asset at once and return the batch id — the TOP-UP path, for a run
    whose renders failed or were never paid for.

    `None` means there is no manifest to top up; `""` means settled with nothing to do.
    NOTHING WAITS: each job carries what follows it (for a mesh, the TRELLIS job its image feeds)
    and the batch's finalize, so the stage lives in the queue where its depth is real work the
    scaler can act on."""
    entries = read_manifest(run_dir)
    if not entries:
        return None
    pending = _pending(run_id, run_dir, entries)
    if not pending:
        return ""
    with _active_lock:
        if run_id in _active:
            return ""
        _active.add(run_id)
    try:
        return _enqueue_batch(run_id, pending, build_id)
    finally:
        with _active_lock:
            _active.discard(run_id)


def _enqueue_batch(run_id: str, entries: List[Dict], build_id: str) -> str:
    batch_id = uuid.uuid4().hex[:16]
    enqueued = 0
    for e in entries:
        kind = entry_kind(e)
        mesh = kind == "mesh"
        queue = "image"
        payload = build_image_payload(e["prompt"], kind)
        then = _then_for(kind, "art_build")
        src = asset_path(run_id, e["id"], "src.png")
        if mesh and src.exists():
            # A mesh needs ComfyUI and then TRELLIS, and one GPU can only hold one of them, so a
            # top-up that always restarted at the image leg could never reach the second half.
            queue = "mesh"
            then = {"operations": ["decimate"], "finalize": "art_build"}
            payload = {"kind": "trellis_mesh",
                       "image_b64": base64.b64encode(src.read_bytes()).decode("ascii")}
        if payload is None:
            logger.warning("assets %s: %s blocked by the safety filter — not sent", run_id, e["id"])
            continue
        try:
            jobs.enqueue_job(queue, payload, game_id=run_id, build_id=build_id,
                                 batch_id=batch_id,
                                 metadata={"run_id": run_id, "asset_id": e["id"],
                                           "kind": kind, "then": then})
        except errors.InsufficientCompute as err:
            logger.error("assets %s: budget refused after %d job(s): %s", run_id, enqueued, err)
            break
        enqueued += 1
    logger.info("assets %s: enqueued %d/%d job(s) as batch %s",
                run_id, enqueued, len(entries), batch_id)
    return batch_id if enqueued else ""


def add_assets(run_id: str, build_id: str) -> Dict:
    """The manual re-render (the API's `assets` action): top up whatever the manifest declares and
    the run doesn't have yet."""
    state = RunState(run_id)
    entries = read_manifest(state.run_dir)
    if not entries:
        return {"ok": False, "error": "the game declares no assets.json", "batch_id": ""}
    if jobs.has_active_batch(run_id):
        raise AlreadyRendering(run_id)
    batch = start_from_manifest(run_id, state.run_dir, build_id) or ""
    return {"ok": bool(batch), "batch_id": batch, "planned": len(entries)}


def regenerate_asset(run_id: str, asset_id: str, note: str, mode: str = "full") -> Dict:
    """Re-render ONE asset as a one-job batch, reusing the same finalize. The user's text is a CHANGE
    NOTE, not the finished prompt: it is merged with the entry's ORIGINAL prompt so "give him a red
    cape" keeps the goblin. `img2img` seeds the render from the existing image, keeping composition."""
    state = RunState(run_id)
    entry = next((e for e in read_manifest(state.run_dir) if e["id"] == asset_id), None)
    if entry is None:
        return {"ok": False, "error": f"no asset {asset_id!r} in the manifest"}
    build_id = games.create_build(run_id, kind="regen")
    games.build_started(build_id)
    with run_scope(run_id, build_id):
        prompt = _merge_prompt(entry["prompt"], note)
    kind = entry_kind(entry)
    init_b64 = None
    if mode == "img2img":
        src = asset_path(run_id, asset_id, "src.png" if kind == "mesh" else "webp")
        if src.exists():
            init_b64 = base64.b64encode(src.read_bytes()).decode("ascii")
    payload = build_image_payload(prompt, kind, init_image_b64=init_b64)
    if payload is None:
        return {"ok": False, "error": "the prompt was blocked by the safety filter"}
    batch_id = uuid.uuid4().hex[:16]
    jobs.enqueue_job("image", payload, game_id=run_id, build_id=build_id, batch_id=batch_id,
                         metadata={"run_id": run_id, "asset_id": asset_id,
                                   "kind": kind, "then": _then_for(kind, "art_build")})
    _update(state.run_dir, asset_id, defect=None, refused=None)
    return {"ok": True, "batch_id": batch_id, "prompt": prompt}


def _merge_prompt(original: str, note: str) -> str:
    """One small LLM call to fold a change note into the original prompt. Falls back to appending —
    a merge that fails must not cost the user their regenerate."""
    from llm_clients.connector import get_connector
    from llm_clients.message_builder import MessageBuilder
    system = ("You rewrite image prompts. Given the ORIGINAL prompt and a CHANGE the user asked "
              "for, output the full revised prompt and nothing else. Keep everything the change "
              "does not touch.")
    user = f"ORIGINAL: {original}\n\nCHANGE: {note}"
    try:
        reply = get_connector().generate_with_tools(
            MessageBuilder(system).add_user(user).build(), [], max_tokens=300)
        text = ((reply.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        merged = text.strip().strip('"')
        if merged:
            return merged
    except Exception:
        logger.exception("prompt merge failed for %r", note)
    return f"{original}, {note}"
