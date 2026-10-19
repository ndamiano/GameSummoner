"""worldgen's image and mesh backends, against a fake `run_job` — no real queue, no real GPU.

Covers the queue payload shapes, safety admission/refusal, prompt screening, and the mesh
fan-out (concurrent submission, run_id propagation into worker threads, partial failure).
"""
import base64
import threading
import uuid

import pytest

from maestro.worldgen.backends import images as images_mod
from maestro.worldgen.backends import meshes as meshes_mod
from maestro.worldgen.seed import seed_for
from tools.execution_context import run_scope


def _png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


def _ok_image_entry(tmp_path, nsfw: float = 0.01) -> dict:
    """The shape the control plane actually hands back: a blob file on disk, not b64 —
    see `workqueue._offload_blobs`, which rewrites every image entry's `b64` to `file`."""
    path = tmp_path / f"blob-{uuid.uuid4().hex}.png"
    path.write_bytes(_png_bytes())
    return {"filename": "out.png", "file": str(path), "safety": {"scores": {"NSFW": nsfw}}}


class _FakeJob:
    def __init__(self, result):
        self._result = result

    def __call__(self, queue, payload, model=None, timeout_seconds=None):
        self.queue = queue
        self.payload = payload
        return {"status": "done", "result": self._result}


def test_generate_builds_comfy_image_payload(monkeypatch, tmp_path):
    captured = {}

    def fake_run_job(queue, payload, model=None, timeout_seconds=None):
        captured["queue"] = queue
        captured["payload"] = payload
        return {"status": "done", "result": {"images": [_ok_image_entry(tmp_path)]}}

    monkeypatch.setattr(images_mod, "run_job", fake_run_job)
    monkeypatch.setattr(images_mod, "screen_image_prompt", lambda p: None)

    out = tmp_path / "out.png"
    images_mod.ImageModel().generate("a friendly castle", out, width=64, height=64)

    assert captured["queue"] == "image"
    assert captured["payload"]["kind"] == "comfy_image"
    assert "workflow" in captured["payload"]
    assert captured["payload"]["uploads"] == []
    assert out.read_bytes() == _png_bytes()


def test_edit_carries_uploads(monkeypatch, tmp_path):
    captured = {}

    def fake_run_job(queue, payload, model=None, timeout_seconds=None):
        captured["payload"] = payload
        return {"status": "done", "result": {"images": [_ok_image_entry(tmp_path)]}}

    monkeypatch.setattr(images_mod, "run_job", fake_run_job)
    monkeypatch.setattr(images_mod, "screen_image_prompt", lambda p: None)

    src = tmp_path / "src.png"
    src.write_bytes(_png_bytes())
    out = tmp_path / "edited.png"
    images_mod.ImageModel().edit("repaint it", [src], out)

    uploads = captured["payload"]["uploads"]
    assert len(uploads) == 1
    assert uploads[0]["b64"] == base64.b64encode(_png_bytes()).decode()
    assert uploads[0]["name"].endswith(".png")


def test_refused_prompt_raises_before_enqueue(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(images_mod, "run_job", lambda *a, **k: calls.append(1))

    violation = images_mod.SafetyViolation("csam_explicit", "matched")
    monkeypatch.setattr(images_mod, "screen_image_prompt", lambda p: violation)
    logged = []
    monkeypatch.setattr(images_mod, "log_violation", lambda v, **kw: logged.append((v, kw)))

    with pytest.raises(images_mod.ImageModelError):
        images_mod.ImageModel().generate("blocked", tmp_path / "out.png")

    assert not calls  # never reached the queue
    assert logged and logged[0][1]["source"] == "image_prompt"


def test_nsfw_render_dropped_and_logged(monkeypatch, tmp_path):
    def fake_run_job(queue, payload, model=None, timeout_seconds=None):
        return {"status": "done", "result": {"images": [_ok_image_entry(tmp_path, nsfw=0.999)]}}

    monkeypatch.setattr(images_mod, "run_job", fake_run_job)
    monkeypatch.setattr(images_mod, "screen_image_prompt", lambda p: None)
    logged = []
    monkeypatch.setattr(images_mod, "log_violation", lambda v, **kw: logged.append((v, kw)))

    with pytest.raises(images_mod.ImageModelError):
        images_mod.ImageModel().generate("a scene", tmp_path / "out.png")

    assert logged and logged[0][1]["source"] == "image_render"
    assert logged[0][0].category == "nsfw_render"


# -- meshes -------------------------------------------------------------------


def test_reconstruct_fans_out_and_writes_glbs(monkeypatch, tmp_path):
    images = []
    for name in ("a", "b", "c"):
        p = tmp_path / f"{name}.png"
        p.write_bytes(_png_bytes())
        images.append(p)

    barrier = threading.Barrier(len(images), timeout=5)
    submitted_run_ids = []

    def fake_run_job(queue, payload, model=None, timeout_seconds=None):
        from tools.execution_context import get_run_id
        submitted_run_ids.append(get_run_id())
        barrier.wait()  # every job must be enqueued before any of them "completes"
        blob = tmp_path / f"blob-{uuid.uuid4().hex}.glb"
        blob.write_bytes(b"glTF-fake-bytes")
        return {"status": "done", "result": {"glb_file": str(blob)}}

    monkeypatch.setattr(meshes_mod, "run_job", fake_run_job)
    monkeypatch.setattr(meshes_mod, "_decimate", lambda path, target: True)

    with run_scope("run-123", "b1"):
        made = meshes_mod.MeshModel().reconstruct(images, tmp_path / "out")

    assert set(made) == {str(p) for p in images}
    for p in images:
        glb = made[str(p)]
        assert glb is not None
        assert glb.exists()
        assert glb.read_bytes() == b"glTF-fake-bytes"
    assert submitted_run_ids == ["run-123"] * len(images)


def test_reconstruct_one_failure_leaves_one_none(monkeypatch, tmp_path):
    images = []
    for name in ("good", "bad"):
        p = tmp_path / f"{name}.png"
        p.write_bytes(_png_bytes())
        images.append(p)

    calls = {"n": 0}

    def flaky_run_job(queue, payload, model=None, timeout_seconds=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "failed", "error": "worker died"}
        blob = tmp_path / f"blob-{uuid.uuid4().hex}.glb"
        blob.write_bytes(b"ok")
        return {"status": "done", "result": {"glb_file": str(blob)}}

    monkeypatch.setattr(meshes_mod, "run_job", flaky_run_job)
    monkeypatch.setattr(meshes_mod, "_decimate", lambda path, target: True)

    made = meshes_mod.MeshModel().reconstruct(images, tmp_path / "out")

    assert sorted(made.values(), key=lambda v: v is None) != []
    assert None in made.values()
    assert sum(1 for v in made.values() if v is not None) == 1


def test_every_reconstruction_carries_a_seed(monkeypatch, tmp_path):
    """Without one the sampler is fixed, so asking again for a mesh the editor rejected
    hands back the same mesh."""
    seen = []

    def fake_run_job(queue, payload, model=None, timeout_seconds=None):
        assert queue == "image"
        seeds = {payload["workflow"][node]["inputs"]["seed"] for node in ("ks1", "ks2", "ks4")}
        assert len(seeds) == 1, "every TRELLIS.2 stage samples from the one seed"
        seen.append(seeds.pop())
        glb = tmp_path / f"landed_{len(seen)}.glb"
        glb.write_bytes(b"glb")
        return {"status": "done", "result": {"glb_file": str(glb)}}

    monkeypatch.setattr(meshes_mod, "run_job", fake_run_job)
    monkeypatch.setattr(meshes_mod, "_decimate", lambda path, target: True)
    for name in ("a", "b"):
        (tmp_path / f"{name}.png").write_bytes(b"png")

    meshes_mod.MeshModel().reconstruct(
        [tmp_path / "a.png", tmp_path / "b.png"], tmp_path / "out", verbose=False)
    assert sorted(seen) == sorted([seed_for("a") % 100_000, seed_for("b") % 100_000])

    seen.clear()
    meshes_mod.MeshModel().reconstruct(
        [tmp_path / "a.png"], tmp_path / "out2", seed=7919, verbose=False)
    assert seen == [7919]


def test_the_decimator_script_is_where_the_backend_looks():
    """A wrong path fails soft, so nothing would report it: every mesh would just ship
    at TRELLIS's raw density."""
    assert (meshes_mod._RUNTIME_DIR / "decimate.mjs").exists()
