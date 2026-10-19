"""The image job a kind resolves to.

A game asks for different KINDS of picture and they want opposite things: a sprite is cut out and
drawn on top of the game, a tile and a scene ARE the background. Rendering all of them through one
item-icon workflow is what matted a floor down to a handful of planks.

A kind picks the graph too: tiles render through DreamShaperXL Turbo with a negative that holds off
the photoreal drift and cracks tiles grow without it; everything else — sprites, scenes, anim
stills, mesh subjects — through the Qwen subject graph, prose verbatim, since the art lab's sprite
re-bake-off (2026-09-04) retired the anime checkpoint.
"""

import pytest

from tools.comfyui_tools import (MESH_STYLE, MESH_STYLES, build_image_job, build_image_payload,
                                 build_img2img_job)


def test_the_prompt_is_the_positive_verbatim():
    # The manifest's saved prompt survives verbatim in job["prompt"] and IS the positive: no
    # quality tags (Qwen takes prose), and never embedded mid-phrase ("a single {X}, one object
    # only, ...") — that garbled the grammar and drove subject drift.
    job = build_image_job("A rusty iron key with a worn bow. A single object icon, centered.")
    assert job["prompt"] == "A rusty iron key with a worn bow. A single object icon, centered."
    assert job["workflow_override"]["p"]["inputs"]["text"] == job["prompt"]
    assert job["workflow_override"]["l"]["inputs"]["width"] == 1024
    assert job["workflow_override"]["l"]["inputs"]["height"] == 1024


@pytest.mark.parametrize("kind", ["sprite", "scene"])
def test_subjects_render_through_qwen(kind):
    wf = build_image_job("A key.", kind)["workflow_override"]
    assert wf["u"]["inputs"]["unet_name"].startswith("qwen_image_2512")
    assert wf["k"]["inputs"]["model"] == ["ms", 0]
    assert wf["k"]["inputs"]["cfg"] == 2.5


def test_tiles_render_through_dreamshaper():
    wf = build_image_job("Worn wooden floorboards.", "tile")["workflow_override"]
    assert wf["4"]["inputs"]["ckpt_name"] == "DreamShaperXL_Turbo_v2_1.safetensors"
    assert wf["3"]["inputs"]["model"] == ["4", 0]
    assert wf["5"]["inputs"]["width"] == 1024
    assert wf["5"]["inputs"]["height"] == 1024
    assert wf["6"]["inputs"]["text"] == "Worn wooden floorboards."


def test_the_negative_varies_by_kind():
    sprite = build_image_job("A key.", "sprite")["workflow_override"]["n"]["inputs"]["text"]
    scene = build_image_job("A key.", "scene")["workflow_override"]["n"]["inputs"]["text"]
    tile = build_image_job("A key.", "tile")["workflow_override"]["7"]["inputs"]["text"]
    assert "photorealistic" in sprite
    # a subject Qwen draws is cropped by the frame without these
    assert "cropped" in sprite
    assert scene == sprite
    # tiles measured 2026-08-06 drift photoreal and grow cracks/objects without the extra terms
    for term in ("person", "border", "cracks"):
        assert term in tile
    assert "cracks" not in sprite


def test_the_seed_is_set_per_job():
    a = build_image_job("A key.")["workflow_override"]["k"]["inputs"]["seed"]
    b = build_image_job("A key.")["workflow_override"]["k"]["inputs"]["seed"]
    assert isinstance(a, int) and a != b


@pytest.mark.parametrize("kind", ["sprite", "mesh"])
def test_a_matted_kind_is_matted(kind):
    wf = build_image_job("A rusty iron key.", kind)["workflow_override"]
    assert wf["s"]["inputs"]["images"] == ["m", 0]      # the output reads BiRefNet


def test_a_scene_keeps_its_whole_frame():
    """Matting a backdrop leaves the ragged fragments of a backdrop."""
    wf = build_image_job("A cobblestone courtyard.", "scene")["workflow_override"]
    assert "m" not in wf
    assert wf["s"]["inputs"]["images"] == ["d", 0]


def test_a_tile_keeps_its_whole_frame():
    wf = build_image_job("Worn wooden floorboards.", "tile")["workflow_override"]
    assert "47" not in wf
    assert wf["9"]["inputs"]["images"] == ["8", 0]


def test_img2img_job_seeds_from_the_init_image():
    job = build_img2img_job("A rusty iron key, now golden.", "init_abc.png", denoise=0.5)
    wf = job["workflow_override"]
    assert wf["li"]["inputs"]["image"] == "init_abc.png"
    assert wf["k"]["inputs"]["denoise"] == 0.5
    # the sampler denoises the ENCODED init image, not an empty latent
    assert wf["k"]["inputs"]["latent_image"] == ["ve", 0]
    assert wf["ve"]["inputs"]["pixels"] == ["li", 0]
    assert wf["p"]["inputs"]["text"] == "A rusty iron key, now golden."


def test_img2img_honours_the_kind_too():
    """A regenerate of a tile must not put the matte back that the first render left out — and it
    must re-render through the tile's own model."""
    wf = build_img2img_job("Worn floorboards.", "init_abc.png", "tile")["workflow_override"]
    assert wf["4"]["inputs"]["ckpt_name"] == "DreamShaperXL_Turbo_v2_1.safetensors"
    assert wf["6"]["inputs"]["text"] == "Worn floorboards."
    assert "47" not in wf
    assert wf["9"]["inputs"]["images"] == ["8", 0]
    assert wf["3"]["inputs"]["latent_image"] == ["51", 0]
    assert wf["50"]["inputs"]["image"] == "init_abc.png"


def test_payload_with_init_image_carries_the_upload():
    payload = build_image_payload("A rusty iron key.", init_image_b64="aGk=")
    assert payload["kind"] == "comfy_image"
    (up,) = payload["uploads"]
    assert up["b64"] == "aGk="
    # the workflow's LoadImage reads exactly the name the worker will upload under
    assert payload["workflow"]["li"]["inputs"]["image"] == up["name"]


def test_payload_without_init_image_has_no_uploads():
    payload = build_image_payload("A rusty iron key.")
    assert "uploads" not in payload
    assert "li" not in payload["workflow"]   # txt2img: empty latent, no LoadImage


def test_a_mesh_subject_renders_through_qwen_with_the_style_fixed():
    """TRELLIS lifts a clean stylized render best; the anime item recipe drifts (measured
    2026-09-03, five subjects in five styles)."""
    wf = build_image_job("A weathered wooden barrel", "mesh")["workflow_override"]
    assert wf["u"]["inputs"]["unet_name"].startswith("qwen_image_2512")
    positive = wf["p"]["inputs"]["text"]
    assert positive.startswith("A weathered wooden barrel.")
    assert positive.endswith(MESH_STYLES[MESH_STYLE])
    assert set(MESH_STYLES) == {"hand-painted", "low-poly"}
    assert "masterpiece" not in positive
    assert "cropped" in wf["n"]["inputs"]["text"]
    assert wf["s"]["inputs"]["images"] == ["m", 0]      # the output reads BiRefNet


def test_a_mesh_subject_seed_varies_per_job():
    a = build_image_job("A barrel", "mesh")["workflow_override"]["k"]["inputs"]["seed"]
    b = build_image_job("A barrel", "mesh")["workflow_override"]["k"]["inputs"]["seed"]
    assert a != b


def test_a_mesh_img2img_seeds_from_the_init_image():
    wf = build_img2img_job("A barrel", "init_x.png", "mesh", denoise=0.4)["workflow_override"]
    assert wf["li"]["inputs"]["image"] == "init_x.png"
    assert wf["k"]["inputs"]["latent_image"] == ["ve", 0]
    assert wf["k"]["inputs"]["denoise"] == 0.4
    assert "l" not in wf
