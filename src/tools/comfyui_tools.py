"""Image and mesh job payloads: a resolved workflow and a screened prompt for the worker to run."""

import copy
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from tools.safety import log_violation, screen_image_prompt

logger = logging.getLogger(__name__)

_WORKFLOWS_DIR = Path(__file__).parent.parent / "config" / "workflows"
_TXT2IMG_TILE_WORKFLOW_PATH = _WORKFLOWS_DIR / "txt2img_tile.json"
_IMG2IMG_TILE_WORKFLOW_PATH = _WORKFLOWS_DIR / "img2img_tile.json"
_TXT2IMG_SUBJECT_WORKFLOW_PATH = _WORKFLOWS_DIR / "txt2img_subject.json"
_IMG2IMG_SUBJECT_WORKFLOW_PATH = _WORKFLOWS_DIR / "img2img_subject.json"
_IMG2MESH_WORKFLOW_PATH = _WORKFLOWS_DIR / "img2mesh.json"
_MESH_SAMPLERS = ("ks1", "ks2", "ks4")


def _build_tile_workflow(base_workflow: dict, positive: str, negative: str) -> dict:
    wf = copy.deepcopy(base_workflow)
    wf["6"]["inputs"]["text"] = positive
    wf["7"]["inputs"]["text"] = negative
    wf["3"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))
    return wf


# Both samplers run at a real cfg, so the negative reaches the model. Tiles measured 2026-08-06
# drift photoreal and grow cracks and objects without their extra terms; a subject drawn by Qwen
# is cropped by the frame without its own (the art lab's sprite re-bake-off, 2026-09-04).
_NEGATIVE_BASE = "worst quality, low quality, blurry, watermark, signature, text, photo, " \
                 "photorealistic"
_NEGATIVE_TILE = _NEGATIVE_BASE + ", people, person, animal, border, frame, vignette, cracks, " \
                 "cracked ground"
_NEGATIVE_SUBJECT = _NEGATIVE_BASE + ", cropped, cut off, out of frame, partial, close-up, " \
                    "clipped edges"

# A mesh's picture is never seen by the player, so its style serves TRELLIS, not the game: a
# hand-painted game-asset render lifts into geometry where a photo smears and the anime item
# recipe drifts off-subject (`docs/experiments.md`, 2026-09-03).
# Both clauses reconstruct alike; which look a game should have is a taste call not yet made.
_MESH_FRAMING = (" Single object, complete and unobstructed, centred and filling the frame. "
                 "Three-quarter view from slightly above, showing its depth and thickness. Plain "
                 "flat mid-grey background. ")
MESH_STYLES = {
    "hand-painted": _MESH_FRAMING + "Hand-painted stylized game asset, painterly textures, chunky "
                    "exaggerated proportions, rich saturated colours, soft even lighting, 3D render.",
    "low-poly": _MESH_FRAMING + "Low-poly stylized game asset, flat colours, clean faceted shapes, "
                "simple bold silhouette, soft even lighting, 3D render.",
}
MESH_STYLE = "hand-painted"
_NEGATIVE_MESH = ("blurry, low detail, cropped, cut off, partial object, multiple objects, "
                  "scenery, landscape, ground, floor, horizon, cast shadow, text, watermark")

# A sprite is composited onto the game's own background, so it is matted to its subject, and a
# mesh's subject is matted because TRELLIS lifts a cut-out. A tile or a backdrop IS the
# background: matting one leaves the ragged fragments of a floor that used to be a floor, so those
# keep the full opaque frame the sampler drew.
MATTED_KINDS = ("sprite", "mesh")

_MATTE_NODE = "47"
_DECODE_NODE = "8"
_OUTPUT_NODE = "9"


def _drop_matte(wf: dict) -> dict:
    """Take BiRefNet out of the graph: the output reads the VAE decode directly."""
    wf[_OUTPUT_NODE]["inputs"]["images"] = [_DECODE_NODE, 0]
    wf.pop(_MATTE_NODE, None)
    return wf


def _subject_prompt(description: str, kind: str) -> tuple:
    """(positive, negative) for the Qwen subject graph. A mesh's prose goes first and the style
    last, so the object stays the subject of the sentence; everything else is the prompt verbatim —
    Qwen takes prose, and the quality tags the anime checkpoint wanted are off-distribution."""
    if kind == "mesh":
        return description.rstrip(". ") + "." + MESH_STYLES[MESH_STYLE], _NEGATIVE_MESH
    return description, _NEGATIVE_SUBJECT


def _build_subject_workflow(base_workflow: dict, description: str, kind: str) -> dict:
    wf = copy.deepcopy(base_workflow)
    positive, negative = _subject_prompt(description, kind)
    wf["p"]["inputs"]["text"] = positive
    wf["n"]["inputs"]["text"] = negative
    wf["k"]["inputs"]["seed"] = int(uuid.uuid4().int % (2**32))
    if kind not in MATTED_KINDS:
        wf["s"]["inputs"]["images"] = ["d", 0]
        wf.pop("m")
    return wf


def build_image_job(description: str, kind: str = "sprite") -> dict:
    """Return a {prompt, workflow_override} job dict for ONE rendered image.

    `description` is the manifest's SAVED prompt and the whole of the positive's substance: the
    prose is never embedded mid-phrase ("a single {X}, one object only, ...") — that garbled the
    grammar and drove subject drift. `kind` picks the graph, the negative and whether the matte
    runs."""
    if kind != "tile":
        wf = _build_subject_workflow(_load_workflow(_TXT2IMG_SUBJECT_WORKFLOW_PATH), description, kind)
        return {"prompt": description, "workflow_override": wf}
    wf = _build_tile_workflow(_load_workflow(_TXT2IMG_TILE_WORKFLOW_PATH), description, _NEGATIVE_TILE)
    wf["5"]["inputs"]["width"] = 1024
    wf["5"]["inputs"]["height"] = 1024
    _drop_matte(wf)
    return {"prompt": description, "workflow_override": wf}


def build_img2img_job(description: str, init_name: str, kind: str = "sprite",
                      denoise: float = 0.6) -> dict:
    """The same per-kind workflow seeded from an EXISTING render instead of an empty latent: the
    init image (uploaded to ComfyUI under `init_name` by the worker) is VAE-encoded and partially
    denoised, so the output keeps the original's composition while the prompt steers the change."""
    if kind != "tile":
        wf = _build_subject_workflow(_load_workflow(_IMG2IMG_SUBJECT_WORKFLOW_PATH), description, kind)
        wf["li"]["inputs"]["image"] = init_name
        wf["k"]["inputs"]["denoise"] = denoise
        return {"prompt": description, "workflow_override": wf}
    wf = _build_tile_workflow(_load_workflow(_IMG2IMG_TILE_WORKFLOW_PATH), description, _NEGATIVE_TILE)
    wf["50"]["inputs"]["image"] = init_name
    wf["3"]["inputs"]["denoise"] = denoise
    _drop_matte(wf)
    return {"prompt": description, "workflow_override": wf}


def build_image_payload(description: str, kind: str = "sprite",
                        init_image_b64: Optional[str] = None,
                        denoise: float = 0.6) -> Optional[Dict[str, Any]]:
    """The queue payload for ONE image, safety-screened. None = blocked, never sent.

    With `init_image_b64` the job is img2img: the payload carries the init image as an upload the
    worker lands on ComfyUI before submitting, and the workflow denoises from it instead of noise.

    The local image model has no built-in guardrails, so every finalized prompt is screened before
    it can reach a worker. A blocked prompt degrades like any other missing asset — the game renders
    that entity as its shape."""
    violation = screen_image_prompt(description)
    if violation is not None:
        log_violation(violation, source="image_prompt")
        return None
    if init_image_b64 is None:
        return {"kind": "comfy_image",
                "workflow": build_image_job(description, kind)["workflow_override"]}
    init_name = f"init_{uuid.uuid4().hex}.png"
    wf = build_img2img_job(description, init_name, kind, denoise)["workflow_override"]
    return {"kind": "comfy_image", "workflow": wf,
            "uploads": [{"name": init_name, "b64": init_image_b64}]}


def build_mesh_payload(image_b64: str, seed: int = 42) -> Dict[str, Any]:
    name = f"subject_{uuid.uuid4().hex}.png"
    wf = _load_workflow(_IMG2MESH_WORKFLOW_PATH)
    wf["li"]["inputs"]["image"] = name
    for node in _MESH_SAMPLERS:
        wf[node]["inputs"]["seed"] = seed
    return {"kind": "comfy_mesh", "workflow": wf, "uploads": [{"name": name, "b64": image_b64}]}


def _load_workflow(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


