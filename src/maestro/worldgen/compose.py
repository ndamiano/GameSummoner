"""`compose_world`'s build face: a world built into the game folder while the build carries on.

The tool ANSWERS AT ONCE with the path the world will appear at, and every stage runs behind it
on one thread for that run — the same bargain `generate_media` strikes, for the same reason. A
program has a wall-clock budget measured in seconds and the ground alone takes about fifteen
minutes, so a tool that waited for it could only ever be killed mid-call: the world landed, the
program that asked for it died, and the code to load it was never written (measured 2026-09-10,
run 8988a28a746e — a 34 MB world staged into a game that referenced it nowhere, paid for in
full).

Nothing is lost by answering early. `world.sizeM` and `world.regions` are read from `world.json`
by the loader in the browser, so a game places its content BY REGION from the loaded world at
runtime, which is where those numbers were always going to come from. What the tool could have
returned was a copy of them, one turn sooner, at the cost of the turn.

The stages PUBLISH in four legs, each ending where what a game draws changes: construct lays the
height field and scatters — from that moment the ground loads and COLLIDES, because the loader
reads collision from the stated heights and never from a GLB — then refinement moves the ground,
objects add meshes and placements, and scene refinement rebuilds them. A publish copies only the
files `world.json` names into `<game>/world/`, then replaces `world.json` itself by rename, so a
browser reading the folder mid-publish never sees a world pointing at a file that is not there
yet. A build that calls `done` while the world is still rendering finalizes on a game whose
ground already loads, and a leg that lands after that re-stages the game people play.

The intermediates stay in `<run_dir>/world_build/` — a quarter of a gigabyte of concept images,
per-region renders and refinement views that no game reads. That folder is also the record that
a world is already coming: the tool refuses a second call rather than replacing the ground under
a game already written against the first one.

A leg that fails is logged and left: the pipeline resumes by stage, so what is published stands
and the world simply stops improving.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

WORLD_DIR = "world"
BUILD_DIR = "world_build"

# Each leg ends at a stage that changes what a game draws, so each is followed by a publish.
LEGS = (("scene", "construct"),
        ("terrain-refine", "terrain-refine"),
        ("regional-plan", "objects"),
        ("scene-refine", "final-render"))


def build_dir(run_dir) -> Path:
    return Path(run_dir) / BUILD_DIR


def world_dir(game_root) -> Path:
    return Path(game_root) / WORLD_DIR


# A world nobody has built yet, in the loader's own terms: flat ground of one region, the size
# a world tends to be. It is not a look — it is what makes `loadWorld` succeed the moment the
# model writes it, instead of throwing on a file that has not landed. Every field here is one
# `runtime/vendor/world.js` reads; `tests/test_world_compose.py` holds them to that.
PLACEHOLDER_RESOLUTION = 128
PLACEHOLDER_SIZE_M = 600.0


def _placeholder(game_root: Path) -> None:
    """Write a flat world a game can load while the real one is still being made."""
    import struct

    from PIL import Image

    target = world_dir(game_root)
    target.mkdir(parents=True, exist_ok=True)
    n = PLACEHOLDER_RESOLUTION
    (target / "heightmap.f32").write_bytes(struct.pack(f"<{n * n}f", *([0.0] * (n * n))))
    # The weight map is read as data: full red is "all of region 0", which is the only one here.
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(target / "placeholder_weights.png")
    Image.new("RGB", (8, 8), (108, 118, 96)).save(target / "placeholder_albedo.png")
    Image.new("RGB", (8, 8), (128, 128, 255)).save(target / "placeholder_normal.png")
    job = {
        "heightmap": "heightmap.f32",
        "resolution": n,
        "mesh_resolution": n,
        "size_m": PLACEHOLDER_SIZE_M,
        "sea_level_m": 0.0,
        "lowest_m": 0.0,
        "sun_elevation_deg": 45.0,
        "sun_azimuth_deg": 135.0,
        "weight_textures": ["placeholder_weights.png"],
        "regions": [{"region_id": "ground", "category": "ground",
                     "centre_m": [PLACEHOLDER_SIZE_M / 2, PLACEHOLDER_SIZE_M / 2],
                     "radius_m": PLACEHOLDER_SIZE_M, "scale_m": 4.0,
                     "albedo": "placeholder_albedo.png", "normal": "placeholder_normal.png"}],
        "instances": {},
        "cameras": [],
    }
    # By rename, like every publish: a browser reading the folder never sees a half-written world.
    tmp = target / "world.json.tmp"
    tmp.write_text(json.dumps(job, indent=1), encoding="utf-8")
    os.replace(tmp, target / "world.json")


def coming(run_dir) -> bool:
    """Is a world already on its way for this run? The build folder is made by the first stage
    and outlives the run, so it answers for a world still rendering and for one long finished."""
    return build_dir(run_dir).exists()


def compose(game_root: Path, run_dir, run_id: str, build_id: str, description: str,
            seed: Optional[int]) -> Dict:
    """Start the world and answer at once with the path it will appear at."""
    out_dir = build_dir(run_dir)
    prompt = description if seed is None else f"{description} (variation {seed})"
    out_dir.mkdir(parents=True, exist_ok=True)
    _placeholder(game_root)
    threading.Thread(target=_build, args=(game_root, out_dir, run_id, build_id, prompt),
                     daemon=True).start()

    return {"ok": True,
            "world": f"{WORLD_DIR}/world.json",
            "note": ("the world is being built and appears at that path — write the code that "
                     "loads it now, exactly as the loader is documented. Its ground lands "
                     "first and its scenery fills in as it renders, all through the same "
                     "world.json. Do not build a second world, and do not wait for this one.")}


def _build(game_root: Path, out_dir: Path, run_id: str, build_id: str, prompt: str) -> None:
    """Every leg, on this run's own thread.

    `run_scope` is re-entered here rather than inherited: a thread starts with none of the
    context that started it, and every job these stages enqueue is metered to this game."""
    from maestro.worldgen.build import build_world
    from tools.execution_context import run_scope

    with run_scope(run_id, build_id):
        for start_at, stop_after in LEGS:
            try:
                build_world(prompt, out_dir, start_at=start_at, stop_after=stop_after)
            except Exception:
                logger.exception("world %s: %s..%s failed — the published world stands",
                                 run_id, start_at, stop_after)
                return
            try:
                publish(out_dir, game_root)
                _restage(run_id)
            except Exception:
                logger.exception("world %s: publishing after %s failed", run_id, stop_after)


def _restage(run_id: str) -> None:
    """A world that lands after the build finished must reach the staged copy people play. A
    build still running stages its own game at finalize, and re-staging under it would publish a
    half-written game."""
    from db import games
    from maestro.codegen.staging import is_staged, stage_for_play
    from maestro.state import RunState
    game = games.game(run_id) or {}
    building = game.get("status") == "running" and game.get("stage") == "building"
    if not building and game.get("status") != "held" and is_staged(run_id):
        stage_for_play(RunState(run_id).run_dir, run_id)


def published_files(job: Dict) -> List[str]:
    """The files a game reads, in `world.json`'s own words. Everything else in the build folder —
    concept images, per-region compositions, refinement views — is working material."""
    files = [job["heightmap"], *job["weight_textures"]]
    for region in job["regions"]:
        files += [region["albedo"], region["normal"]]
        files += [region[key] for key in ("variant_albedo", "variant_normal") if region.get(key)]
    files += [group["mesh"] for group in job["instances"].values() if group.get("mesh")]
    return list(dict.fromkeys(files))


def publish(out_dir: Path, game_root: Path) -> Dict:
    """Copy the world a game reads into `<game>/world/`, `world.json` last and by rename.

    `world.json` is rewritten from the world as it stands rather than copied as some stage left
    it: a stage moves the ground, adds a mesh or replaces one, and only a fresh job names what is
    actually on disk. Paths inside it are relative to the folder it sits in and the folder's shape
    is kept, so the copy needs no rewriting to resolve against the game."""
    from maestro.worldgen.terrain.models import TerrainPlan
    from maestro.worldgen.terrain.render import write_job

    out_dir, game_root = Path(out_dir), Path(game_root)
    plan = TerrainPlan.model_validate_json(
        (out_dir / "terrain_plan.json").read_text(encoding="utf-8"))
    job = json.loads(write_job(plan, out_dir).read_text(encoding="utf-8"))
    target = world_dir(game_root)
    target.mkdir(parents=True, exist_ok=True)
    for name in published_files(job):
        src = out_dir / name
        if src.is_file():
            _replace(src, target / name)
    _replace_bytes(target / "world.json", (out_dir / "world.json").read_bytes())
    return job


def _replace(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _replace_bytes(dst: Path, data: bytes) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, dst)
