"""The asset chain: a job carries what happens after it.

The asset stage no longer waits. Every image job is enqueued at once (so queue depth is real work
the scaler can act on), each one carries the mesh job its result feeds, and the completion that
empties the batch owns the finalize. These tests pin the two things that make that safe: the
continuation lands INSIDE the completion transaction and before the batch is counted, and the
finalize is claimed exactly once no matter who gets there first.
"""

import pytest

from billing.estimates import QUEUE_MICRO_ESTIMATES, calculate_job_cost
from db import events, games, jobs, workers

RATE = 0.99   # the rate every test worker is created at


def _game(micros: int = 1_000_000, game_id: str = "g1") -> str:
    games.create_game(game_id, "u1")
    games.charge_game(game_id, 1, micros)
    for worker in ("w1", "w2"):
        workers.worker_created(worker, None, "llm", None, RATE)
    return game_id


def _image_job(batch: str, asset_id: str = "goblin", game_id: str = "g1") -> str:
    return jobs.enqueue_job(
        "comfy", {"kind": "comfy_image"}, game_id=game_id, build_id="b1", batch_id=batch,
        metadata={"run_id": game_id, "asset_id": asset_id, "kind": "mesh",
                  "then": {"enqueue": "mesh_from_image", "finalize": "assets"}})


_MESH = {"queue": "comfy", "payload": {"kind": "comfy_mesh"},
         "metadata": {"then": {"operations": ["decimate"], "finalize": "assets"}}}


def _complete(job_id: str, worker: str = "w1", continuation=None, error=None):
    jobs.claim_job(jobs.get_job(job_id)["queue"], worker, lease_seconds=60)
    return jobs.complete_job(job_id, worker, {"ok": True}, error, 5.0,
                              continuation=continuation)


def test_a_batch_with_a_continuation_pending_is_not_complete():
    """The load-bearing order: the follow-up is inserted BEFORE the remaining count. Inverted,
    the last image completion sees an empty batch and finalizes work that hasn't been done."""
    _game()
    job = _image_job("b1")
    out = _complete(job, continuation=_MESH)
    assert out["continuation_id"] is not None
    assert out["batch_complete"] is False


def test_the_last_job_of_a_batch_reports_complete():
    _game()
    out = _complete(_image_job("b1"), continuation=None)
    assert out["batch_complete"] is True


def test_only_one_completion_of_a_batch_sees_it_complete():
    _game()
    a, b = _image_job("b1", "one"), _image_job("b1", "two")
    first = _complete(a, worker="w1")
    second = _complete(b, worker="w2")
    assert [first["batch_complete"], second["batch_complete"]] == [False, True]


def test_a_job_outside_any_batch_never_reports_complete():
    _game()
    job = jobs.enqueue_job("llm", {"n": 1}, game_id="g1", build_id="b1")
    out = _complete(job)
    assert out["batch_id"] is None
    assert out["batch_complete"] is False


def test_a_continuation_inherits_its_parent_game_batch_and_build():
    """Nothing enqueues the follow-up inside a run_scope, so the parent row is the only place its
    game can come from — the attribution hole that let the platform's most expensive work run off
    the books."""
    _game()
    parent = jobs.enqueue_job("comfy", {"k": 1}, game_id="g1", build_id="bld1", batch_id="b1",
                               metadata={"then": {"finalize": "assets"}})
    out = _complete(parent, continuation=_MESH)
    child = jobs.get_job(out["continuation_id"])
    assert (child["game_id"], child["build_id"], child["batch_id"]) == ("g1", "bld1", "b1")
    assert child["queue"] == "comfy"


def test_a_continuations_cost_debits_the_parents_game():
    _game()
    out = _complete(_image_job("b1"), continuation=_MESH)
    before = games.game("g1")["spent_micros"]
    jobs.claim_job("comfy", "w2", lease_seconds=60)
    jobs.complete_job(out["continuation_id"], "w2", {"glb_file": "/x.glb"}, None, 30.0)
    assert games.game("g1")["spent_micros"] == before + calculate_job_cost(30.0, RATE)


def test_a_failed_job_enqueues_no_continuation():
    _game()
    out = _complete(_image_job("b1"), continuation=_MESH, error="comfy died")
    assert out["continuation_id"] is None
    assert out["batch_complete"] is True


def test_a_refused_continuation_still_finishes_the_batch():
    """A budget refusal mid-chain must not strand the batch: the game renders shapes, which is the
    stage's soft-degrade, and the finalize still has to run."""
    _game(QUEUE_MICRO_ESTIMATES["comfy"] + 1)
    out = _complete(_image_job("b1"), continuation=_MESH)
    assert out["continuation_id"] is None
    assert out["batch_complete"] is True


def test_a_batch_finalize_is_claimed_exactly_once():
    _game()
    _complete(_image_job("b1"))
    assert jobs.claim_batch_finalize("b1") is True
    assert jobs.claim_batch_finalize("b1") is False


def test_batch_jobs_returns_parsed_metadata():
    _game()
    _image_job("b1", "goblin")
    rows = jobs.batch_jobs("b1")
    assert [j["metadata"]["asset_id"] for j in rows] == ["goblin"]


def test_a_game_with_queued_batch_work_reads_as_active():
    _game()
    _image_job("b1")
    assert jobs.has_active_batch("g1") is True
    _complete(jobs.batch_jobs("b1")[0]["id"])
    assert jobs.has_active_batch("g1") is False


def test_unbatched_work_does_not_count_as_an_active_batch():
    _game()
    jobs.enqueue_job("llm", {"n": 1}, game_id="g1", build_id="b1")
    assert jobs.has_active_batch("g1") is False


def test_a_lapsed_lease_is_requeued_without_any_claim_traffic():
    """claim_job requeues these too, but only when a claim arrives. A queue that goes quiet would
    otherwise hold a dead job and its reservation forever."""
    _game()
    job = jobs.enqueue_job("comfy", {"k": 1}, game_id="g1", build_id="b1")
    jobs.claim_job("comfy", "w1", lease_seconds=-1)
    assert jobs.requeue_lapsed_leases() == 1
    assert jobs.get_job(job)["status"] == "pending"


def test_stale_pending_jobs_fail_and_release_their_reservation():
    _game()
    jobs.enqueue_job("comfy", {"k": 1}, game_id="g1", build_id="b1")
    reserved = games.compute_remaining("g1")
    failed = jobs.fail_stale_pending(-1.0)
    assert len(failed) == 1
    assert games.compute_remaining("g1") > reserved


def test_a_stranded_batch_is_offered_for_finalize_after_the_grace_window():
    _game()
    _complete(_image_job("b1"))
    assert jobs.batches_awaiting_finalize(3600.0) == []
    assert jobs.batches_awaiting_finalize(-1.0) == ["b1"]


def test_a_finalized_batch_is_never_offered_again():
    _game()
    _complete(_image_job("b1"))
    jobs.claim_batch_finalize("b1")
    assert jobs.batches_awaiting_finalize(-1.0) == []


def test_a_batch_with_work_left_is_never_offered():
    _game()
    _image_job("b1", "one")
    _image_job("b1", "two")
    _complete(jobs.batch_jobs("b1")[0]["id"])
    assert jobs.batches_awaiting_finalize(-1.0) == []


@pytest.fixture
def _asset_env(monkeypatch, tmp_path, tmp_runs):
    from maestro.codegen import asset_chain

    staged, events = [], []
    monkeypatch.setattr(asset_chain, "stage_for_play", lambda rd, rid: staged.append(rid))
    monkeypatch.setattr(asset_chain, "_emit",
                        lambda et, rid, **f: events.append((et, f)))
    monkeypatch.setattr(asset_chain, "RunState",
                        lambda rid: type("S", (), {"run_dir": tmp_path})())
    monkeypatch.setattr(asset_chain, "is_staged", lambda rid: True)
    return asset_chain, staged, events


def _md():
    return {"run_id": "g1", "then": {"finalize": "assets"}}


@pytest.mark.parametrize("stage, status, published, restaged, ok", [
    ("building", "running", True, False, True),
    ("checking", "running", True, True, True),
    ("checking", "succeeded", True, True, True),
    ("building", "failed", True, True, True),
    ("building", "paused", True, True, True),
    ("checking", "succeeded", False, False, True),
    ("building", "held", True, False, False),
])
def test_landed_art_reaches_the_published_game_unless_a_build_is_writing_it_or_it_is_held(
        _asset_env, monkeypatch, stage, status, published, restaged, ok):
    asset_chain, staged, events = _asset_env
    monkeypatch.setattr(asset_chain, "is_staged", lambda rid: published)
    _game()
    games.move("g1", status, stage)
    asset_chain._finalize_assets(_md(), [{"metadata": {}, "build_id": None}])
    assert staged == (["g1"] if restaged else [])
    assert events == [("assets_done", {"build_id": None, "ok": ok, "rendered": []})]


def test_a_build_turns_art_lands_without_ending_the_build(_asset_env):
    """generate_media art carries the build's id for cost, and the "assets" finalize leaves the
    build's row to the build machine — the model is still writing the game."""
    asset_chain, staged, events = _asset_env
    _game()
    bid = games.create_build("g1", kind="build")
    games.build_started(bid)
    games.move("g1", "running", "building")
    asset_chain.FINALIZERS["assets"](_md(), [{"metadata": {}, "build_id": bid}])
    (b,) = games.builds_for("g1")
    assert b["status"] == "running" and b["finished_at"] is None
    assert events == [("assets_done", {"build_id": bid, "ok": True, "rendered": []})]


def test_a_standalone_art_batch_is_its_own_build_and_ends_it(_asset_env):
    asset_chain, staged, events = _asset_env
    _game()
    bid = games.create_build("g1", kind="assets")
    games.build_started(bid)
    games.move("g1", "succeeded", "checking")
    asset_chain.FINALIZERS["art_build"](_md(), [{"metadata": {}, "build_id": bid}])
    (b,) = games.builds_for("g1")
    assert b["status"] == "succeeded" and b["finished_at"] is not None
    assert staged == ["g1"]


# ── The safety verdict: worker scores, control-plane policy, fail closed ─────────────────────


@pytest.fixture
def _policy_env(monkeypatch, tmp_path):
    from maestro.codegen import asset_chain, assets

    fake_state = lambda rid: type("S", (), {"run_dir": tmp_path})()  # noqa: E731
    monkeypatch.setattr(asset_chain, "RunState", fake_state)
    monkeypatch.setattr(assets, "RunState", fake_state)
    from maestro.codegen.assets import _record
    _record(tmp_path, {"id": "goblin", "file": "assets/goblin.webp",
                       "kind": "sprite", "prompt": "a goblin"})
    return asset_chain, tmp_path


def _render(tmp_path, safety=None):
    from PIL import Image
    p = tmp_path / "render.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(p)
    entry = {"file": str(p)}
    if safety is not None:
        entry["safety"] = safety
    return p, {"images": [entry]}


def _sprite_md():
    return {"run_id": "g1", "asset_id": "goblin", "kind": "sprite"}


def test_a_clean_verdict_admits_the_render(_policy_env):
    from maestro.codegen.assets import read_manifest
    asset_chain, tmp_path = _policy_env
    _, result = _render(tmp_path, {"scores": {"NSFW": 0.01, "SFW": 0.99}})
    asset_chain._save_sprite(_sprite_md(), result)
    assert (tmp_path / "game" / "assets" / "goblin.webp").exists()
    assert not read_manifest(tmp_path)[0].get("refused")


def test_an_explicit_verdict_refuses_and_marks_the_manifest(_policy_env):
    from maestro.codegen.assets import read_manifest
    asset_chain, tmp_path = _policy_env
    src, result = _render(tmp_path, {"scores": {"NSFW": 0.99, "SFW": 0.01}})
    asset_chain._save_sprite(_sprite_md(), result)
    assert not (tmp_path / "game" / "assets" / "goblin.webp").exists()
    assert not src.exists()   # the blob is deleted, not left on disk
    assert "explicit" in read_manifest(tmp_path)[0]["refused"]
    rows = events.list_violations()
    assert rows and rows[0]["category"] == "nsfw_render"


def test_a_render_without_a_verdict_is_refused(_policy_env):
    """Fail closed: an old worker image, a classifier that failed to load — either way no scores
    means no save."""
    from maestro.codegen.assets import read_manifest
    asset_chain, tmp_path = _policy_env
    _, result = _render(tmp_path, safety=None)
    asset_chain._save_sprite(_sprite_md(), result)
    assert not (tmp_path / "game" / "assets" / "goblin.webp").exists()
    assert "no safety verdict" in read_manifest(tmp_path)[0]["refused"]


def test_a_refused_image_never_reaches_trellis(_policy_env):
    asset_chain, tmp_path = _policy_env
    _, result = _render(tmp_path, {"scores": {"NSFW": 0.99, "SFW": 0.01}})
    md = {"run_id": "g1", "asset_id": "goblin", "kind": "mesh",
          "then": {"enqueue": "mesh_from_image", "finalize": "assets"}}
    assert asset_chain._mesh_from_image(md, result) is None
    assert not (tmp_path / "game" / "assets" / "goblin.src.png").exists()


# ── The tile post-op: quilt seamless, soft on failure ────────────────────────────────────────


def _flat_render(tmp_path, size=160):
    from PIL import Image
    p = tmp_path / "render.png"
    Image.new("RGB", (size, size), (120, 80, 40)).save(p)
    return p, {"images": [{"file": str(p),
                           "safety": {"scores": {"NSFW": 0.01, "SFW": 0.99}}}]}


def _flat_md(kind):
    return {"run_id": "g1", "asset_id": "ground", "kind": kind}


@pytest.fixture
def _flat_env(monkeypatch, tmp_path):
    from maestro.codegen import asset_chain, assets

    fake_state = lambda rid: type("S", (), {"run_dir": tmp_path})()  # noqa: E731
    monkeypatch.setattr(asset_chain, "RunState", fake_state)
    monkeypatch.setattr(assets, "RunState", fake_state)
    from maestro.codegen.assets import _record
    _record(tmp_path, {"id": "ground", "file": "assets/ground.webp",
                       "kind": "tile", "prompt": "dirt ground"})
    return asset_chain, tmp_path


def test_a_tile_save_quilts_to_a_384_webp(_flat_env):
    from PIL import Image

    from maestro.codegen.assets import read_manifest
    asset_chain, tmp_path = _flat_env
    src, result = _flat_render(tmp_path)
    asset_chain._save_flat(_flat_md("tile"), result)
    dst = tmp_path / "game" / "assets" / "ground.webp"
    assert Image.open(dst).size == (384, 384)
    assert not src.exists()
    assert not read_manifest(tmp_path)[0].get("defect")


def test_a_scene_save_keeps_the_frame_it_rendered(_flat_env):
    from PIL import Image
    asset_chain, tmp_path = _flat_env
    _, result = _flat_render(tmp_path)
    asset_chain._save_flat(_flat_md("scene"), result)
    assert Image.open(tmp_path / "game" / "assets" / "ground.webp").size == (160, 160)


def test_a_failed_quilt_still_saves_the_raw_render(_flat_env, monkeypatch):
    from PIL import Image

    def boom(im, **kw):
        raise RuntimeError("no quilt today")

    asset_chain, tmp_path = _flat_env
    monkeypatch.setattr(asset_chain, "quilt_tile", boom)
    _, result = _flat_render(tmp_path)
    asset_chain._save_flat(_flat_md("tile"), result)
    assert Image.open(tmp_path / "game" / "assets" / "ground.webp").size == (160, 160)


def test_the_top_up_skips_a_refused_entry(_policy_env):
    """Re-rendering the same prompt would refuse again — a regenerate with a new note is the
    escape hatch, and it clears the marker."""
    from maestro.codegen.assets import _pending, _update, read_manifest
    _, tmp_path = _policy_env
    _update(tmp_path, "goblin", refused="explicit content (score 0.97)")
    assert _pending("g1", tmp_path, read_manifest(tmp_path)) == []
    _update(tmp_path, "goblin", refused=None)
    assert [e["id"] for e in _pending("g1", tmp_path, read_manifest(tmp_path))] == ["goblin"]


def test_finalize_reports_an_image_asset_as_rendered(_asset_env):
    """The asset writers save webp; a finalize that looked for png reported every image as
    missing, so assets_done never named the art that was in the game."""
    from maestro.codegen.assets import asset_path, ext_for
    asset_chain, staged, events = _asset_env
    _game()
    games.move("g1", "running", "building")
    asset_path("g1", "goblin", ext_for("sprite")).write_bytes(b"webp")
    asset_chain._finalize_assets(
        _md(), [{"metadata": {"asset_id": "goblin", "kind": "sprite"}, "build_id": None}])
    assert events[0][1]["rendered"] == ["goblin"]
