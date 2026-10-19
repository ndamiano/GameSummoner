"""Text-to-image and image-editing, through the worker-pull queue.
Qwen Image 2512 generates, Qwen Image Edit 2511 edits; prompts are screened before the queue.
"""

from __future__ import annotations

import base64
import uuid
from pathlib import Path
from typing import Any, Sequence

from maestro.codegen.assets import render_verdict
from maestro.worldgen import WORLD_JOB_TIMEOUT
from tools.execution_context import get_run_id
from tools.safety import SafetyViolation, log_violation, screen_image_prompt
from workqueue.client import run_job

# The two checkpoints share a text encoder and a VAE and differ only in the
# diffusion model, which is why one class covers both.
TXT2IMG_UNET = "qwen_image_2512_fp8_e4m3fn.safetensors"
EDIT_UNET = "qwen_image_edit_2511_fp8mixed.safetensors"
CLIP = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
VAE = "qwen_image_vae.safetensors"

# Sampler settings these checkpoints are tuned for. Distilled: cfg stays low and
# 20 steps is the knee, not a compromise.
STEPS = 20
CFG = 2.5
SAMPLER = "euler"
SCHEDULER = "simple"
SHIFT = 3.1  # ModelSamplingAuraFlow; Qwen Image is a flow model, not epsilon

# TextEncodeQwenImageEditPlus takes image1..image3. Later stages condition on
# more than one reference at once — a composition on both a terrain render and
# the concept image — so the ceiling is worth stating rather than discovering.
MAX_EDIT_IMAGES = 3

LATENT_MULTIPLE = 16  # the VAE downsamples by 8 and the patchifier pairs pixels


class ImageModelError(RuntimeError):
    """A request was refused before it reached the queue, or came back with no image."""


class ImageModel:
    """The image queue, addressed as if it were an image model."""
    def __init__(self, *, timeout: float = WORLD_JOB_TIMEOUT) -> None:
        self.timeout = timeout

    def generate(
        self,
        prompt: str,
        out: Path | str,
        *,
        negative: str = "",
        width: int = 1024,
        height: int = 1024,
        seed: int = 0,
        steps: int = STEPS,
        cfg: float = CFG,
        cutout: bool = False,
    ) -> Path:
        self._screen(prompt)
        width, height = _snap(width), _snap(height)
        graph = {
            "unet": _node("UNETLoader", unet_name=TXT2IMG_UNET, weight_dtype="default"),
            "clip": _node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default"),
            "vae": _node("VAELoader", vae_name=VAE),


            "sampling": _node("ModelSamplingAuraFlow", shift=SHIFT, model=["unet", 0]),
            "pos": _node("CLIPTextEncode", text=prompt, clip=["clip", 0]),
            "neg": _node("CLIPTextEncode", text=negative, clip=["clip", 0]),
            "latent": _node("EmptySD3LatentImage", width=width, height=height, batch_size=1),
            "sample": _node(
                "KSampler",
                seed=seed, steps=steps, cfg=cfg,
                sampler_name=SAMPLER, scheduler=SCHEDULER, denoise=1.0,
                model=["sampling", 0], positive=["pos", 0], negative=["neg", 0],
                latent_image=["latent", 0],
            ),
            "decode": _node("VAEDecode", samples=["sample", 0], vae=["vae", 0]),
        }
        final = "decode"
        if cutout:
            graph["cutout"] = _node(
                "BiRefNetRMBG",
                model="BiRefNet-general", mask_blur=0, mask_offset=-1,
                invert_output=False, refine_foreground=False,
                background="Alpha", background_color="#ffffff",
                image=["decode", 0],
            )
            final = "cutout"
        graph["save"] = _node("SaveImage", images=[final, 0], filename_prefix="wc_gen")
        return self._run_to(graph, out)

    def edit(
        self,
        prompt: str,
        images: Sequence[Path | str],
        out: Path | str,
        *,
        negative: str = "",
        seed: int = 0,
        steps: int = STEPS,
        cfg: float = CFG,
        denoise: float = 1.0,
    ) -> Path:
        if not images:
            raise ValueError("edit() needs at least one image to edit")
        if len(images) > MAX_EDIT_IMAGES:
            raise ValueError(
                f"edit() takes at most {MAX_EDIT_IMAGES} images, got {len(images)}"
            )
        self._screen(prompt)
        uploads = [self._upload(p) for p in images]
        names = [u["name"] for u in uploads]
        graph: dict[str, Any] = {
            "unet": _node("UNETLoader", unet_name=EDIT_UNET, weight_dtype="default"),
            "clip": _node("CLIPLoader", clip_name=CLIP, type="qwen_image", device="default"),
            "vae": _node("VAELoader", vae_name=VAE),
            "sampling": _node("ModelSamplingAuraFlow", shift=SHIFT, model=["unet", 0]),
        }
        refs = {}
        for i, name in enumerate(names, start=1):
            graph[f"img{i}"] = _node("LoadImage", image=name)
            refs[f"image{i}"] = [f"img{i}", 0]
        graph["pos"] = _node(
            "TextEncodeQwenImageEditPlus",
            prompt=prompt, clip=["clip", 0], vae=["vae", 0], **refs,
        )
        graph["neg"] = _node(
            "TextEncodeQwenImageEditPlus",
            prompt=negative, clip=["clip", 0], vae=["vae", 0], **refs,
        )
        graph["encode"] = _node("VAEEncode", pixels=["img1", 0], vae=["vae", 0])
        graph["sample"] = _node(
            "KSampler",
            seed=seed, steps=steps, cfg=cfg,
            sampler_name=SAMPLER, scheduler=SCHEDULER, denoise=denoise,
            model=["sampling", 0], positive=["pos", 0], negative=["neg", 0],
            latent_image=["encode", 0],
        )
        graph["decode"] = _node("VAEDecode", samples=["sample", 0], vae=["vae", 0])
        graph["save"] = _node("SaveImage", images=["decode", 0], filename_prefix="wc_edit")
        return self._run_to(graph, out, uploads=uploads)

    # -- the queue --------------------------------------------------------------

    def _screen(self, prompt: str) -> None:
        violation = screen_image_prompt(prompt)
        if violation is not None:
            log_violation(violation, run_id=get_run_id(), source="image_prompt")
            raise ImageModelError(f"prompt blocked: {violation.matched}")

    def _run_to(self, graph: dict[str, Any], out: Path | str,
               *, uploads: list[dict] | None = None) -> Path:
        produced = self.run(graph, uploads=uploads)
        if not produced:
            raise ImageModelError("the job completed but returned no admissible image")
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(produced[0])
        return out

    def run(self, graph: dict[str, Any], *, uploads: list[dict] | None = None) -> list[bytes]:
        """Queue `graph`, wait for it, and return the images it saved, admitted only."""
        payload = {"kind": "comfy_image", "workflow": graph, "uploads": uploads or []}
        job = run_job("comfy", payload, timeout_seconds=self.timeout)
        if job.get("status") != "done":
            raise ImageModelError(job.get("error") or "image job failed")
        result = job.get("result") or {}
        admitted = []
        for entry in result.get("images") or []:
            reason = render_verdict(entry)
            if reason is not None:
                log_violation(SafetyViolation("nsfw_render", reason),
                              run_id=get_run_id(), source="image_render")
                continue
            path = Path(entry["file"])
            admitted.append(path.read_bytes())
            path.unlink(missing_ok=True)
        return admitted

    def _upload(self, path: Path | str) -> dict[str, str]:
        path = Path(path)
        name = f"wc_{uuid.uuid4().hex}{path.suffix or '.png'}"
        return {"name": name, "b64": base64.b64encode(path.read_bytes()).decode("ascii")}


def _node(class_type: str, **inputs: Any) -> dict[str, Any]:
    """One node of a ComfyUI graph."""
    return {"class_type": class_type, "inputs": inputs}


def _snap(px: int, multiple: int = LATENT_MULTIPLE) -> int:
    if px < multiple:
        raise ValueError(f"{px}px is below the {multiple}px minimum")
    return -(-int(px) // multiple) * multiple


__all__ = ["ImageModel", "ImageModelError"]
