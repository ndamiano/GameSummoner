"""Image-to-3D: each image is its own TRELLIS.2 job on the ComfyUI box, fanned out at once."""

from __future__ import annotations

import base64
import contextvars
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Sequence

from maestro.worldgen import WORLD_JOB_TIMEOUT
from maestro.worldgen.seed import seed_for
from tools.comfyui_tools import build_mesh_payload
from workqueue.client import run_job

DECIMATION_TARGET = 50_000
_RUNTIME_DIR = Path(__file__).resolve().parents[4] / "runtime"


class MeshModelError(RuntimeError):
    """A reconstruction job did not come back with a mesh."""


class MeshModel:
    """TRELLIS2, addressed as if it were a local function from image to mesh."""
    def __init__(
        self,
        *,
        decimation_target: int = DECIMATION_TARGET,
        timeout: float = WORLD_JOB_TIMEOUT,
        max_workers: int = 16,
    ) -> None:
        self.decimation_target = decimation_target
        self.timeout = timeout
        self.max_workers = max_workers

    def reconstruct(
        self,
        images: Sequence[Path | str],
        out_dir: Path | str,
        *,


        targets: dict[str, int] | None = None,
        seed: int | None = None,

        overwrite: bool = False,
        verbose: bool = True,
    ) -> dict[str, Path | None]:
        """Reconstruct each image as a GLB under `out_dir`, or None where that one failed."""
        images = [Path(p) for p in images]
        missing = [p for p in images if not p.exists()]
        if missing:
            raise FileNotFoundError(f"no such image: {missing[0]}")

        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        results: dict[str, Path | None] = {}
        pending: list[Path] = []
        for image in images:
            glb = out_dir / f"{image.stem}.glb"
            if glb.exists() and not overwrite:
                # reconstruction is the expensive step in the whole pipeline; a
                # rerun of a stage must not pay for it twice
                results[str(image)] = glb
                continue
            results[str(image)] = None
            pending.append(image)

        if not pending:
            return results

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(pending))) as pool:
            futures = {
                pool.submit(contextvars.copy_context().run, self._one, image, out_dir,
                           (targets or {}).get(image.stem, self.decimation_target),
                           seed if seed is not None else seed_for(image.stem) % 100_000): image
                for image in pending
            }
            for future, image in futures.items():
                try:
                    glb = future.result()
                except Exception as e:  # noqa: BLE001 — one image's failure, not the batch's
                    if verbose:
                        print(f"[mesh] failed {image.name}: {e}")
                    continue
                results[str(image)] = glb
                if verbose and glb is not None:
                    print(f"[mesh] reconstructed {image.name}")
        return results

    def _one(self, image: Path, out_dir: Path, target: int, seed: int) -> Path | None:
        payload = build_mesh_payload(base64.b64encode(image.read_bytes()).decode("ascii"), seed)
        job = run_job("image", payload, timeout_seconds=self.timeout)
        if job.get("status") != "done":
            raise MeshModelError(job.get("error") or "mesh job failed")
        result = job.get("result") or {}
        glb_file = result.get("glb_file")
        if not glb_file:
            raise MeshModelError("mesh job returned no glb")
        glb = out_dir / f"{image.stem}.glb"
        src = Path(glb_file)
        glb.write_bytes(src.read_bytes())
        src.unlink(missing_ok=True)
        _decimate(glb, target)
        return glb


def _decimate(glb_path: Path, target: int) -> bool:
    """Shrink a raw TRELLIS GLB to `target` triangles; a failure keeps the fat original."""
    tmp = glb_path.with_suffix(glb_path.suffix + ".dec.glb")
    try:
        p = subprocess.run(
            ["node", str(_RUNTIME_DIR / "decimate.mjs"), str(glb_path), str(tmp), str(target)],
            capture_output=True, text=True, timeout=300, cwd=_RUNTIME_DIR,
        )
        if p.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, glb_path)
            return True
        # Soft, but never silent: a decimator that never runs ships every mesh at TRELLIS's
        # raw density, which reads as a slow world rather than as a failure.
        print(f"[mesh] decimation of {glb_path.name} failed: {p.stderr.strip()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"[mesh] decimation of {glb_path.name} failed: {e}")
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return False


__all__ = ["MeshModel", "MeshModelError"]
