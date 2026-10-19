"""`generate_media` — the tool the game asks for its own art with.

What is pinned here is the ANSWER, not the render: the model gets the path the file will live at
while the render is still queued, and writes code against it. A bad id, a blocked prompt and an
exhausted budget all come back as text instead, because a build that cannot have art has to be told
to draw one rather than left waiting for a file that is never coming.
"""

import json

import pytest

import maestro.state
from db import connection, games, jobs
from maestro.codegen import assets
from maestro.codegen.assets import read_manifest, request_media
from maestro.codegen.tools import build_tools
from maestro.state import RunState

RUN = "r1"
STYLE = "painted cartoon style, warm palette, soft dark outlines"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(maestro.state, "resolve_base_path", lambda input_path=None: tmp_path)
    games.create_game(RUN, "u1")
    games.charge_game(RUN, 1, 1_000_000)


@pytest.fixture
def run_dir(tmp_path):
    d = RunState(RUN).run_dir
    (d / "game").mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def media(run_dir):
    """The tool itself: what a call must send is checked against its signature, not in here."""
    return build_tools(RunState(RUN), "b1")["generate_media"]


def _image_jobs():
    with connection.platform_db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE queue = 'image' ORDER BY id").fetchall()
    return [jobs._job_dict(r) for r in rows]


def test_the_path_comes_back_before_the_render(run_dir):
    out = request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    assert (out["ok"], out["path"], out["status"]) == (True, "assets/goblin.webp", "rendering")
    assert "stand-in" in out["note"]
    assert len(_image_jobs()) == 1


def test_a_mesh_answers_with_a_glb_path_and_chains_trellis(run_dir):
    out = request_media(RUN, run_dir, "b1", "hut", "a thatched hut", STYLE, kind="mesh")
    assert out["path"] == "assets/hut.glb"
    # The one fact placement code cannot discover: the mesh arrives normalized to 1 unit.
    assert "1 unit" in out["note"]
    md = _image_jobs()[0]["metadata"]
    assert md["kind"] == "mesh"
    assert md["then"]["enqueue"] == "mesh_from_image"


@pytest.mark.parametrize("kind,op", [("sprite", "save_sprite"), ("tile", "save_flat"),
                                     ("scene", "save_flat")])
def test_only_a_sprite_is_matted_and_cropped(run_dir, kind, op):
    """A tile and a scene ARE the background — matting one leaves the ragged fragments of a floor
    that used to be a floor, and cropping to the "subject" finishes the job."""
    request_media(RUN, run_dir, "b1", "art", "some art", STYLE, kind=kind)
    md = _image_jobs()[0]["metadata"]
    assert md["kind"] == kind
    assert md["then"]["operations"] == [op]


def test_the_request_is_recorded_in_the_manifest(run_dir):
    """The model never writes assets.json — this file is the platform's record of what was asked
    for, and it is what the gallery, the top-up and the regenerate all read."""
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    request_media(RUN, run_dir, "b1", "hut", "a thatched hut", STYLE, kind="mesh")
    assert read_manifest(run_dir) == [
        {"id": "goblin", "file": "assets/goblin.webp", "kind": "sprite",
         "subject": "a snarling goblin", "style": STYLE,
         "prompt": f"{STYLE}. a snarling goblin.", "placeholder": True},
        {"id": "hut", "file": "assets/hut.glb", "kind": "mesh", "subject": "a thatched hut",
         "style": STYLE, "prompt": f"{STYLE}. a thatched hut.", "placeholder": True},
    ]


def test_a_placeholder_is_at_the_path_from_the_moment_the_tool_answers(run_dir):
    """The model writes `drawImage(img)` against the path it was given; a file that is not there
    yet is a broken image, and drawing one throws every frame — a black game until the render
    lands. A sprite stand-in is matted, a scene's is opaque, because the game draws one ON its
    background and the other AS it."""
    from PIL import Image
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    request_media(RUN, run_dir, "b1", "floor", "a stone floor", STYLE, kind="scene")
    goblin = Image.open(assets.asset_path(RUN, "goblin", "webp"))
    floor = Image.open(assets.asset_path(RUN, "floor", "webp"))
    assert goblin.format == "WEBP" and goblin.convert("RGBA").getpixel((0, 0))[3] == 0
    assert floor.format == "WEBP" and floor.convert("RGBA").getpixel((0, 0))[3] == 255
    assert not assets.asset_path(RUN, "hut", "glb").exists()


def test_a_placeholder_is_not_a_landed_render(run_dir):
    """Everything that used to read 'file exists' as 'art arrived' — the repeat-ask answer, the
    top-up, the batch finalize — reads the manifest flag instead, or a stand-in would count as
    art and the real render would never be paid for."""
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    again = request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    assert again["status"] == "rendering"
    assert assets.landed(RUN, read_manifest(run_dir)[0]) is False

    assets.start_from_manifest(RUN, run_dir, "b1")
    assert len(_image_jobs()) == 2, "the top-up re-renders past a placeholder"


def test_a_landed_render_replaces_the_placeholder_and_the_flag(run_dir, monkeypatch, tmp_path):
    from PIL import Image

    from maestro.codegen.asset_chain import OPERATIONS
    request_media(RUN, run_dir, "b1", "floor", "a stone floor", STYLE, kind="scene")
    src = tmp_path / "render.png"
    Image.new("RGBA", (64, 64), (0, 255, 0, 255)).save(src)
    monkeypatch.setattr("maestro.codegen.asset_chain._record_defect", lambda md, d: None)
    OPERATIONS["save_flat"]({"run_id": RUN, "asset_id": "floor", "kind": "scene"},
                            {"images": [{"file": str(src),
                                         "safety": {"scores": {"NSFW": 0.0, "SFW": 1.0}}}]})
    entry = read_manifest(run_dir)[0]
    assert "placeholder" not in entry and assets.landed(RUN, entry)
    assert Image.open(assets.asset_path(RUN, "floor", "webp")).size == (64, 64)
    assert request_media(RUN, run_dir, "b1", "floor", "a stone floor", STYLE)["status"] == "ready"


def test_a_confirmed_replace_keeps_the_landed_art_until_the_new_render(run_dir, monkeypatch):
    request_media(RUN, run_dir, "b1", "goblin", "a goblin", STYLE)
    assets.set_landed(run_dir, "goblin")
    assets.asset_path(RUN, "goblin", "webp").write_bytes(b"REAL")
    request_media(RUN, run_dir, "b1", "goblin", "a bigger goblin", STYLE)
    request_media(RUN, run_dir, "b1", "goblin", "a bigger goblin", STYLE)
    assert assets.asset_path(RUN, "goblin", "webp").read_bytes() == b"REAL"
    assert "placeholder" not in read_manifest(run_dir)[0]


def test_the_batch_carries_the_build_and_a_finalize_that_leaves_it_open(run_dir):
    """The render is the build's cost, and only the build machine may end the build's row: the
    batch lands under the "assets" finalize, never "art_build"."""
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    job = _image_jobs()[0]
    assert job["build_id"] == "b1"
    assert job["metadata"]["then"]["finalize"] == "assets"


def test_each_request_is_its_own_batch(run_dir):
    """One asset per batch, so each finalize re-stages as it lands instead of waiting on the
    slowest render in a set."""
    request_media(RUN, run_dir, "b1", "goblin", "a goblin", STYLE)
    request_media(RUN, run_dir, "b1", "hut", "a hut", STYLE)
    batches = {j["batch_id"] for j in _image_jobs()}
    assert len(batches) == 2 and None not in batches


def test_the_same_id_asked_twice_is_answered_not_obeyed(run_dir):
    """The first repeat costs nothing — an id asked for twice is usually the model losing track."""
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    again = request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin, but bigger", STYLE)

    assert "not requeued" in again["note"]
    assert len(_image_jobs()) == 1
    assert len(read_manifest(run_dir)) == 1
    assert read_manifest(run_dir)[0]["subject"] == "a snarling goblin"


def test_a_confirmed_repeat_replaces_the_art_and_the_prompt(run_dir):
    """Asking again after the answer is the model meaning it — which is how a playtest note that
    says redraw these reaches the renderer at all."""
    request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    request_media(RUN, run_dir, "b1", "goblin", "a goblin, full body, for a 3d model", STYLE)
    out = request_media(RUN, run_dir, "b1", "goblin", "a goblin, full body, for a 3d model", STYLE)

    assert (out["path"], out["status"]) == ("assets/goblin.webp", "rendering")
    assert len(_image_jobs()) == 2
    entries = read_manifest(run_dir)
    assert len(entries) == 1, "a replacement rewrites its record rather than adding one"
    assert entries[0]["subject"] == "a goblin, full body, for a 3d model"
    assert "replace_asked" not in entries[0]


def test_a_replacement_is_answered_again_before_the_next_one(run_dir):
    """The flag clears with the render it authorized, so the turn after does not requeue blind."""
    request_media(RUN, run_dir, "b1", "goblin", "a goblin", STYLE)
    request_media(RUN, run_dir, "b1", "goblin", "a bigger goblin", STYLE)
    request_media(RUN, run_dir, "b1", "goblin", "a bigger goblin", STYLE)
    again = request_media(RUN, run_dir, "b1", "goblin", "a bigger goblin still", STYLE)

    assert "not requeued" in again["note"]
    assert len(_image_jobs()) == 2


def test_an_already_rendered_asset_is_not_paid_for_again(run_dir):
    """A fix or a resumed build re-runs the same code path over a folder that already has its art."""
    (run_dir / "game" / "assets").mkdir(parents=True, exist_ok=True)
    (run_dir / "game" / "assets" / "goblin.webp").write_bytes(b"RIFFwebp")
    out = request_media(RUN, run_dir, "b1", "goblin", "a snarling goblin", STYLE)
    assert out == {"ok": True, "path": "assets/goblin.webp", "status": "ready"}
    assert _image_jobs() == []


@pytest.mark.parametrize("bad", ["", "../escape", "a b", "x" * 65, "a/b"])
def test_an_id_that_cannot_name_a_file_is_refused(media, bad):
    out = media(id=bad, subject="a goblin", style=STYLE)
    assert out["ok"] is False and "id" in out["error"]
    assert _image_jobs() == []


def test_a_missing_prompt_is_refused(media):
    out = media(id="goblin", subject="   ", style=STYLE)
    assert out["ok"] is False and "subject" in out["error"]


def test_an_omitted_argument_is_named_back(media):
    """An absent argument has to read as one, not as a malformed value of something else."""
    out = media(id="goblin", style=STYLE)
    assert out["ok"] is False
    assert "needs the argument 'subject'" in out["error"] and "id, style" in out["error"]
    assert _image_jobs() == []


def test_an_unknown_kind_is_refused(media):
    out = media(id="goblin", subject="a goblin", style=STYLE, kind="anim")
    assert out["ok"] is False and "anim" in out["error"]


def test_an_explicitly_empty_kind_is_the_default(media):
    """A program that computes its kind and gets nothing has still asked for a sprite; the
    signature default only ever fires when the argument is absent."""
    assert media(id="goblin", subject="a goblin", style=STYLE, kind=None)["ok"] is True
    assert _image_jobs()[0]["metadata"]["kind"] == "sprite"


def test_a_blocked_prompt_tells_the_model_to_draw_it_instead(run_dir, monkeypatch):
    monkeypatch.setattr(assets, "build_image_payload", lambda *a, **kw: None)
    out = request_media(RUN, run_dir, "b1", "goblin", "something refused", STYLE)
    assert out["ok"] is False and "draw this one with code" in out["error"]
    assert read_manifest(run_dir) == []


def test_an_exhausted_budget_tells_the_model_to_draw_it_instead(run_dir):
    """The compute budget is the only cap on how much art a build may ask for — there is no call
    limit, because a refused enqueue already says so in words the model can act on."""
    with connection.platform_db() as conn:
        conn.execute("UPDATE games SET spent_micros = 1000000 WHERE id = ?", (RUN,))
    out = request_media(RUN, run_dir, "b1", "goblin", "a goblin", STYLE)
    assert out["ok"] is False and "draw this one with code" in out["error"]
    assert _image_jobs() == []
    assert read_manifest(run_dir) == []


def test_the_tool_reaches_the_queue_and_defaults_to_a_sprite(run_dir):
    tools = build_tools(RunState(RUN), "b1")
    out = tools["generate_media"](id="goblin", subject="a snarling goblin", style=STYLE)
    assert (out["ok"], out["path"], out["status"]) == (True, "assets/goblin.webp", "rendering")
    assert _image_jobs()[0]["metadata"]["kind"] == "sprite"


def test_the_tool_reports_a_missing_argument_rather_than_guessing(run_dir):
    tools = build_tools(RunState(RUN), "b1")
    assert tools["generate_media"](subject="a goblin", style=STYLE)["ok"] is False
    assert tools["generate_media"](id="goblin", style=STYLE)["ok"] is False
    assert tools["generate_media"](id="goblin", subject="a goblin")["ok"] is False
    assert _image_jobs() == []


def _jobs(queue):
    with connection.platform_db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE queue = ? ORDER BY id", (queue,)).fetchall()
    return [jobs._job_dict(r) for r in rows]


def test_a_top_up_resumes_a_mesh_from_the_source_render_it_already_has(run_dir):
    """A mesh is ComfyUI and then TRELLIS, and one GPU holds one of them, so a top-up that always
    restarted at the image leg could never reach the second half on a single-card box."""
    request_media(RUN, run_dir, "b1", "hut", "a thatched hut", STYLE, kind="mesh")
    src = assets.asset_path(RUN, "hut", "src.png")
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\x89PNG-the-render-we-already-paid-for")

    assets.start_from_manifest(RUN, run_dir, "b1")

    mesh_jobs = _jobs("mesh")
    assert len(mesh_jobs) == 1, "the top-up went to TRELLIS, not back to ComfyUI"
    assert mesh_jobs[0]["payload"]["kind"] == "trellis_mesh"
    assert mesh_jobs[0]["metadata"]["then"]["operations"] == ["decimate"]
    assert len(_jobs("image")) == 1, "no second image render was paid for"


def test_a_mesh_with_no_source_render_still_starts_at_the_image(run_dir):
    request_media(RUN, run_dir, "b1", "hut", "a thatched hut", STYLE, kind="mesh")

    assets.start_from_manifest(RUN, run_dir, "b1")

    assert len(_jobs("image")) == 2 and _jobs("mesh") == []


# ── The structured ask: subject + style + details, prose composed here ─────────────────────


def test_the_prompt_is_composed_style_first_then_subject_then_view(run_dir):
    """The samplers see one prose string, and its shape is ours: where the style sits and how the
    view is phrased are edits here, not whatever the builder typed."""
    request_media(RUN, run_dir, "b1", "ben", "a man in a green t-shirt", STYLE, kind="actor",
                  details={"body": "biped", "view": "3/4 top-down"})
    (entry,) = read_manifest(run_dir)
    assert entry["prompt"] == f"{STYLE}. a man in a green t-shirt, 3/4 top-down view."
    assert entry["details"] == {"body": "biped", "view": "3/4 top-down"}
    assert _image_jobs()[0]["payload"]["workflow"]["p"]["inputs"]["text"] == entry["prompt"]


def test_an_actor_renders_as_a_sprite(run_dir):
    out = request_media(RUN, run_dir, "b1", "ben", "a man", STYLE, kind="actor",
                        details={"body": "biped"})
    assert out["path"] == "assets/ben.webp"
    md = _image_jobs()[0]["metadata"]
    assert md["kind"] == "sprite"
    assert read_manifest(run_dir)[0]["kind"] == "actor"


def test_details_must_be_an_object(media):
    out = media(id="ben", subject="a man", style=STYLE, kind="actor", details="biped")
    assert out["ok"] is False and "details" in out["error"]
    assert _image_jobs() == []


def test_a_missing_style_is_refused(media):
    out = media(id="ben", subject="a man", style="  ")
    assert out["ok"] is False and "style" in out["error"]
    assert _image_jobs() == []
