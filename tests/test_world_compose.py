"""compose_world: what the tool answers, what reaches the game folder, and what runs behind it.

The pipeline itself is stubbed — a world costs an hour of GPU — but everything downstream of it
is real: the same `write_job` a build would call, the real publish, the real thread.

The tool answers AT ONCE. It used to block through `construct`, about fifteen minutes, inside a
program whose whole wall-clock budget is 120 seconds — so the call could only ever be killed:
the world landed, the program that asked for it died, and nothing was written to load it
(2026-09-10, run 8988a28a746e — 34 MB of world staged into a game that referenced it nowhere).
"""

import json
import re
import threading
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from db import games
from maestro.codegen.tools import build_tools
from maestro.state import RunState
from maestro.worldgen import compose as world_compose

PLAN = {
    "world": {"size_m": 40.0, "sea_level_m": -1.0, "boundary_blend_m": 2.0,
              "heightmap_resolution": 256},
    "layout": [{"region_id": "meadow", "category": "grassland", "center": [0.5, 0.5],
                "radius": 0.5, "falloff": 0.5, "coverage": 1.0}],
    "terrain": [{"region_id": "meadow", "base_elevation_m": 1.0}],
    "materials": [{"region_id": "meadow", "surface": "grass", "appearance": "green",
                   "variant": "bare brown earth",
                   "scale_m": 2.0}],
    "concept": "A small green meadow under a clear sky.",
}


def _write_world_build(out_dir, *, mesh=None):
    """The files the pipeline's stages leave behind, shaped like the real ones."""
    out_dir.mkdir(parents=True, exist_ok=True)
    height = np.tile(np.linspace(0.0, 4.0, 16, dtype=np.float32), (16, 1))
    np.save(out_dir / "heightmap.npy", height)
    np.save(out_dir / "layout_masks.npy", np.ones((1, 16, 16), np.float32))
    (out_dir / "terrain_plan.json").write_text(json.dumps(PLAN))

    materials = out_dir / "materials"
    materials.mkdir(exist_ok=True)
    for channel, colour in (("albedo", (90, 140, 60)), ("normal", (128, 128, 255))):
        Image.new("RGB", (8, 8), colour).save(materials / f"meadow_{channel}.png")
    (materials / "materials.json").write_text(json.dumps([{
        "region_id": "meadow",
        "albedo": "/gone/meadow_albedo.png",
        "normal": "/gone/meadow_normal.png",
    }]))
    (out_dir / "prototypes.json").write_text(json.dumps([{"category": "rock", "mesh": mesh}]))
    if mesh:
        meshes = out_dir / "subjects" / "meshes"
        meshes.mkdir(parents=True, exist_ok=True)
        (meshes / mesh).write_bytes(b"glTF\x02\x00\x00\x00")
    (out_dir / "scatter.json").write_text(json.dumps({"instances": [{
        "category": "rock", "position": [20.0, 2.0, 20.0],
        "yaw_deg": 0.0, "scale": 1.0, "height_m": 1.0,
    }]}))


@pytest.fixture
def run(tmp_runs):
    rs = RunState("r1")
    (rs.run_dir / "game").mkdir(exist_ok=True)
    return rs


@pytest.fixture
def pipeline(monkeypatch):
    """A stub `build_world` recording every leg; `done` fires when the build thread exits."""
    from tools.execution_context import get_run_id

    calls = []
    started = threading.Event()
    done = threading.Event()
    real_build = world_compose._build

    def fake_build_world(prompt, out_dir, *, start_at="scene", stop_after="final-render", **kw):
        calls.append({"prompt": prompt, "start_at": start_at, "stop_after": stop_after,
                      "run_id": get_run_id()})
        _write_world_build(out_dir, mesh=None if start_at == "scene" else "rock.glb")
        return {"name": "stub"}

    def build_then_signal(*args):
        started.set()
        try:
            real_build(*args)
        finally:
            done.set()

    monkeypatch.setattr("maestro.worldgen.build.build_world", fake_build_world)
    monkeypatch.setattr(world_compose, "_build", build_then_signal)
    fake_build_world.calls = calls
    fake_build_world.done = done
    yield fake_build_world
    if started.is_set():
        assert done.wait(10)


def _compose(run, pipeline, **kw):
    tools = build_tools(run, "b1")
    return tools["compose_world"](description="a meadow by the sea", **kw)


def test_the_tool_answers_with_the_path_and_nothing_it_cannot_know(run, pipeline):
    """The size and the regions are chosen by a plan that has not run yet, and the game reads
    them off the loaded world at runtime anyway. All the tool can promise is where to load."""
    res = _compose(run, pipeline)
    assert res["ok"] and res["world"] == "world/world.json"
    assert "size_m" not in res and "regions" not in res


def test_something_loadable_is_there_the_moment_the_tool_answers(run, pipeline):
    """Everything world.json names is beside it, by a path relative to it — the game fetches
    nothing outside its own folder, and never a file that has not landed."""
    def assert_loadable(world):
        job = json.loads((world / "world.json").read_text())
        paths = [job["heightmap"], *job["weight_textures"],
                 *(p for r in job["regions"] for p in (r["albedo"], r["normal"]))]
        assert job["regions"], "the terrain shader sizes its arrays by the region count"
        for relative in paths:
            assert not relative.startswith((".", "/"))
            assert (world / relative).is_file()

    _compose(run, pipeline)
    world = run.run_dir / "game" / "world"
    assert_loadable(world)
    assert pipeline.done.wait(10)
    assert_loadable(world)


def test_the_placeholder_carries_every_field_the_loader_reads(run, pipeline):
    loader = (Path(__file__).resolve().parents[1] / "runtime" / "vendor" / "world.js").read_text()
    world_compose._placeholder(run.run_dir / "game")
    job = json.loads((run.run_dir / "game" / "world" / "world.json").read_text())

    for key in sorted(set(re.findall(r"job\.([a-z_]+)", loader))):
        assert key in job, f"the loader reads job.{key}; the placeholder has no such field"
    region = job["regions"][0]
    for key in sorted(set(re.findall(r"\br\.([a-z_]+)", loader))):
        if key in ("x", "z", "radius") or key.startswith("variant_"):
            continue
        assert key in region, f"the loader reads r.{key}; the placeholder region has not got it"
    heights = (run.run_dir / "game" / "world" / job["heightmap"]).read_bytes()
    assert len(heights) == job["resolution"] ** 2 * 4 and set(heights) == {0}, "flat ground"


def test_the_intermediates_stay_out_of_the_game(run, pipeline):
    _compose(run, pipeline)
    assert pipeline.done.wait(10)
    assert (run.run_dir / "world_build" / "heightmap.npy").is_file()
    game = run.run_dir / "game"
    assert not (game / "world" / "heightmap.npy").exists()
    assert not (game / "world_build").exists()


def test_no_leg_is_synchronous_and_the_ground_is_the_first(run, pipeline):
    """The build writes the game while the whole world renders — the ground included, since a
    program cannot outlive the fifteen minutes that one takes."""
    _compose(run, pipeline)
    assert pipeline.done.wait(10)
    assert [(c["start_at"], c["stop_after"]) for c in pipeline.calls] == list(world_compose.LEGS)
    assert pipeline.calls[0]["stop_after"] == "construct", "the ground lands first, and publishes"


def test_the_run_scope_reaches_the_thread(run, pipeline):
    """Every job the later legs enqueue is metered to this game, and a thread starts with none of
    the context that started it."""
    _compose(run, pipeline)
    assert pipeline.done.wait(10)
    assert {c["run_id"] for c in pipeline.calls} == {run.run_id}


def test_a_later_leg_republishes_and_restages(run, pipeline, monkeypatch):
    """A mesh that lands after the build finished has to reach the copy people play."""
    staged = []
    monkeypatch.setattr(games, "game", lambda run_id: {"status": "succeeded", "stage": "checking"})
    monkeypatch.setattr("maestro.codegen.staging.is_staged", lambda slug: True)
    monkeypatch.setattr("maestro.codegen.staging.stage_for_play",
                        lambda run_dir, slug: staged.append(slug))
    _compose(run, pipeline)
    assert pipeline.done.wait(10)
    world = run.run_dir / "game" / "world"
    job = json.loads((world / "world.json").read_text())
    assert job["instances"]["rock"]["mesh"] == "subjects/meshes/rock.glb"
    assert (world / "subjects" / "meshes" / "rock.glb").is_file()
    assert staged == [run.run_id] * len(world_compose.LEGS)


def test_a_failed_leg_leaves_the_published_world_standing(run, pipeline, monkeypatch, caplog):
    def explode(prompt, out_dir, *, start_at="scene", stop_after="final-render", **kw):
        if start_at == "scene":
            return pipeline(prompt, out_dir, start_at=start_at, stop_after=stop_after)
        raise RuntimeError("the mesh server went away")

    monkeypatch.setattr("maestro.worldgen.build.build_world", explode)
    res = _compose(run, pipeline)
    assert res["ok"]
    assert pipeline.done.wait(10)
    job = json.loads((run.run_dir / "game" / "world" / "world.json").read_text())
    assert job["size_m"] == PLAN["world"]["size_m"], "the leg that succeeded still stands"


def test_world_json_is_replaced_by_rename(run, pipeline, monkeypatch):
    """A game reading the folder mid-publish must never see a world.json naming a file that is not
    there yet, or half of one."""
    renamed = []
    real_replace = world_compose.os.replace
    monkeypatch.setattr(world_compose.os, "replace",
                        lambda src, dst: (renamed.append(str(dst)), real_replace(src, dst))[1])
    _compose(run, pipeline)
    assert pipeline.done.wait(10)
    world = run.run_dir / "game" / "world"
    assert renamed[-1] == str(world / "world.json"), "world.json is renamed last, after its files"
    assert not list(world.rglob("*.tmp"))


def test_a_second_world_is_refused(run, pipeline):
    _compose(run, pipeline)
    again = _compose(run, pipeline)
    assert again["ok"] is False and "world/world.json" in again["error"]
    assert len(pipeline.calls) <= len(world_compose.LEGS), "no second pipeline was started"


def test_a_description_is_required(run, pipeline):
    tools = build_tools(run, "b1")
    assert "needs the argument 'description'" in tools["compose_world"]()["error"]
