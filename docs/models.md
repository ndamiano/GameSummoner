# Models

Every model weight GameSummoner runs in production, by queue, with where it comes from, what it
costs to host, and what its license asks of us. `scripts/provision_volume.sh` is the authority for
what sits on the art volume; `docs/deploy.md` for the llm volume. Sizes are on-disk as provisioned.
Licenses were read from the upstream repos on 2026-09-12.

| Queue | Model | Role | Disk | License |
|---|---|---|---|---|
| llm | Qwen3.8 Flash-Next (NVFP4) | design + game build | 126 GiB | Qwen Community 1.0 |
| image | Qwen-Image-2512 (fp8) | sprites, scenes, anim stills, mesh subjects | 19 GB | Apache 2.0 |
| image | Qwen-Image-Edit-2511 (fp8) | scene embed | 19 GB | Apache 2.0 |
| image | Qwen2.5-VL-7B text encoder (fp8) | shared by both Qwen graphs | 8.7 GB | Apache 2.0 |
| image | DreamShaperXL Turbo v2.1 | tile textures, scene-chain terrain | 6.5 GB | CreativeML OpenRAIL++-M |
| image | BiRefNet | matte (background removal) | ~1 GB | MIT |
| image | Marqo NSFW detector | safety classifier | 22 MB | Apache 2.0 |
| mesh | TRELLIS.2-4B | image → 3D | ~10 GB | MIT |
| mesh | DINOv3 ViT-L/16 | TRELLIS.2 image conditioner | 1.2 GB | DINOv3 License |
| mesh | Kimodo-SOMA-RP v1.1 | text → skeletal motion behind anim sheets | ~1 GB | NVIDIA Open Model |

## Language

### Qwen3.8 Flash-Next (NVFP4)

- Weights: [RadixArk/Qwen3.8-Flash-Next-NVFP4](https://huggingface.co/RadixArk/Qwen3.8-Flash-Next-NVFP4)
  (quantized by NVIDIA Model Optimizer from
  [Qwen/Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next))
- License: [Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE)

The one model that designs and writes the game. A 125B-parameter mixture-of-experts with 6B active
per token, hybrid attention, gated residuals and an n-gram embedding; text and vision in, 262K
native context (1M extended). NVFP4 W4A4 on the routed experts brings the checkpoint from 360 GB
to ~135 GB and fits one 96 GB card.

Served by Pennyroyal, the SGLang fork for the RTX PRO 6000 (SM120), at a 524288 window,
`reasoning_effort=medium`. The llm volume holds it prepacked, not as safetensors, so a pod boots
in 20–55 s instead of a 110 s CPU-bound repack (`docs/deploy.md`).

Requires: RTX PRO 6000 (96 GB, SM120), Workstation or Server Edition; the driver floor in
`queues.llm.allowed_cuda_versions`.

License terms that touch us: redistribution allowed with the notice intact. The model name must
be shown on the UI only above 100M monthly active users or US$20M monthly revenue. A "Model as a
Service" business — giving third parties inference access with meaningful control over inputs and
parameters — needs a separate license from Qwen. GameSummoner exposes no prompt or parameter
control over the model; the user types a game idea and gets a game. See [License obligations]
(#license-obligations).

## Image

### Qwen-Image-2512 (fp8)

- Weights: [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI)
  `split_files/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors` (repack of
  [Qwen/Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512))
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

20B-parameter text-to-image diffusion transformer. Draws sprites, scenes, anim stills and the
subject renders that feed TRELLIS. Won the character and object bake-offs against NetaYume and
HiDream (`docs/experiments.md`). Runs at a real cfg, so the negative prompt is live.

Requires: the Qwen2.5-VL-7B text encoder and `qwen_image_vae` below; ~20 GB VRAM at fp8 in
ComfyUI 0.30.1.

### Qwen-Image-Edit-2511 (fp8)

- Weights: [Comfy-Org/Qwen-Image-Edit_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image-Edit_ComfyUI)
  `split_files/diffusion_models/qwen_image_edit_2511_fp8mixed.safetensors` (repack of
  [Qwen/Qwen-Image-Edit-2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511))
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

20B-parameter image-editing sibling of Qwen-Image; edits an input image while holding subject
identity. Does the scene embed: composes rendered subjects into a painted world
(`compose_scene`, `compose_world`).

Requires: same encoder and VAE as Qwen-Image-2512; ~20 GB VRAM.

### Qwen2.5-VL-7B text encoder + Qwen-Image VAE

- Weights: [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI)
  `split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors`,
  `split_files/vae/qwen_image_vae.safetensors` (encoder is
  [Qwen/Qwen2.5-VL-7B-Instruct](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct))
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

The 7B vision-language model both Qwen image graphs use to encode the prompt, and the VAE they
share. 8.7 GB + 0.25 GB.

### DreamShaperXL Turbo v2.1

- Weights: [Lykon/dreamshaper-xl-v2-turbo](https://huggingface.co/Lykon/dreamshaper-xl-v2-turbo)
  `DreamShaperXL_Turbo_v2_1.safetensors`
- License: [CreativeML OpenRAIL++-M](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/blob/main/LICENSE.md)
  (inherited from SDXL base)

SDXL fine-tune, ~3B parameters, tuned for few-step sampling. Paints tile textures and the terrain
in the scene chain; won the tile bake-off where the Qwen models over-detail at 48px.

Requires: ~7 GB VRAM; bundled CLIP encoders and VAE, nothing shared.

License terms that touch us: commercial use allowed; a list of use-based restrictions (no illegal
use, no harm to minors, no medical advice, no automated decisions affecting legal rights, and so
on) that any downstream distribution must pass on. We distribute outputs, not the weights.

### BiRefNet

- Weights: [1038lab/BiRefNet](https://huggingface.co/1038lab/BiRefNet) (ComfyUI RMBG repack;
  upstream [ZhengPeng7/BiRefNet](https://huggingface.co/ZhengPeng7/BiRefNet))
- License: [MIT](https://github.com/ZhengPeng7/BiRefNet/blob/main/LICENSE)

0.2B-parameter dichotomous segmentation model. Cuts the matte on every sprite and subject render.
The same weights ship a second time under `encoders/BiRefNet` on the mesh volume, where TRELLIS.2
uses them in place of the non-commercial `briaai/RMBG-2.0` its pipeline config names.

The `1038lab` repack carries no license field of its own; the license is the upstream MIT.

### Marqo NSFW detector

- Weights: [Marqo/nsfw-image-detection-384](https://huggingface.co/Marqo/nsfw-image-detection-384),
  exported by `scripts/export_safety_model.py` to `comfy/models/safety/` (22 MB)
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

timm image classifier at 384px. Gates every image and video still before it reaches a game. Its
threshold was set on the false-negative rate at a fixed false-positive budget over 290 renders
(`docs/experiments.md`, 2026-08-03); a replacement is judged the same way or not at all.

Requires: CPU is enough. A mesh pod refuses to boot without it.

## Video

No video model is in use. The `video` queue, its worker, workflow and weights stay in place for
the next model; nothing enqueues on it.

### MiniMax-H3 (image-to-video, int8) — not in use

- Weights: [Comfy-Org/MiniMax-H3](https://huggingface.co/Comfy-Org/MiniMax-H3)
  `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors` (21 GB),
  `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` (16 GB),
  `vae/minimax_h3_video_vae_fp16.safetensors` (5 GB); upstream
  [MiniMaxAI/MiniMax-H3](https://huggingface.co/MiniMaxAI/MiniMax-H3)
- License: [MiniMax H3 Community License Agreement](https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE)

33B-parameter omni-modal video generator; the image-to-video path (fl2va) at 768p turns one
still into a 73-frame pinned clip per facing, which the sheet cutter turns into an animation
sheet. Withdrawn from the pipeline on 2026-09-12 over the territory clause below; a silhouette no
skeleton fits now ships as its still. The text encoder is a MiniMax-tuned Qwen3-VL-32B. Runs as core ComfyUI nodes (0.30.1+),
not a separate server, on the `video` queue.

Requires: a 5090-class or PRO 6000 card; ~42 GB of weights, so pinned host RAM must be capped
(`--disable-dynamic-vram --disable-pinned-memory` on a 60 GB box).

License terms that rule it out: **the license is limited to an "Applicable Territory" that
excludes the United States, the European Union, the United Kingdom and South Korea** (§I.5,
§II). Use, distribution or display of the weights *or their outputs* outside that territory "is
not authorized by this Agreement" (§IV.4). A license for the excluded territories is available on
request (api@minimax.io). Inside the territory it would also want "Powered by MiniMax H3" on the
product, "MiniMax H3" on the UI, written authorization above US$20M yearly revenue, and
machine-generated content disclosed.

Candidate replacement, license read 2026-09-12: [Lightricks/LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5)
under the [LTX-2.x Community License](https://github.com/Lightricks/LTX-2/blob/main/LICENSE-2_x)
— worldwide, royalty-free below US$10M annual revenue (paid license above), no UI attribution,
outputs ours; its Gemma 4 12B text encoder rides the
[Gemma Terms of Use](https://ai.google.dev/gemma/terms) (commercial, worldwide). Unmeasured.

## 3D

### TRELLIS.2-4B

- Weights: [microsoft/TRELLIS.2-4B](https://huggingface.co/microsoft/TRELLIS.2-4B)
- License: [MIT](https://huggingface.co/microsoft/TRELLIS.2-4B/blob/main/LICENSE)

4B-parameter flow-matching transformer over sparse voxels; one image in, a PBR-textured mesh out.
Every 3D asset and every world subject comes through it. Served by `src/tools/trellis_server.py`
at the 512 pipeline (10 s a mesh on a 5090); a finished GLB is cut to game weight by
`runtime/decimate.mjs`.

Requires: 24 GB VRAM minimum; SM120 needs the sdpa patch in `docker/Dockerfile.worker-mesh`
(the upstream only knows xformers/flash-attn). `--stage-dir ""` and ptype 512 are mandatory on a
60 GB host or it takes the desktop down.

### DINOv3 ViT-L/16

- Weights: [camenduru/dinov3-vitl16-pretrain-lvd1689m](https://huggingface.co/camenduru/dinov3-vitl16-pretrain-lvd1689m)
  (public mirror of the gated
  [facebook/dinov3-vitl16-pretrain-lvd1689m](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m))
- License: [DINOv3 License](https://ai.meta.com/resources/models-and-libraries/dinov3-license)

303M-parameter vision transformer; TRELLIS.2's image conditioner, so it is not optional while
TRELLIS.2 is. 1.2 GB. Staged to tmpfs on the mesh pod at boot.

License terms that touch us: commercial use allowed, royalty-free. Redistribution must carry a
copy of the agreement; publications must acknowledge DINO. Prohibited: military, nuclear,
espionage, weapons and ITAR-controlled uses. Filing an IP claim against Meta over DINOv3 ends the
license. Pixal3D and every other TRELLIS.2 derivative inherit the same encoder.

## Motion

### Kimodo-SOMA-RP v1.1

- Weights: [nvidia/Kimodo-SOMA-RP-v1.1](https://huggingface.co/nvidia/Kimodo-SOMA-RP-v1.1)
- License: [NVIDIA Open Model License](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/)

282M-parameter diffusion transformer on the SOMA skeleton; a verb in, a BVH clip out. Behind the
text-to-sprite-sheet path: TRELLIS mesh → template rig → Kimodo clip → retarget → sheet, ~90 s.
Its text conditioning is a lookup in `src/tools/sprite_server.py`, because the encoder Kimodo was
trained with is a gated Llama. Lives on the mesh volume under `kimodo/`; `CHECKPOINT_DIR` names
the folder so nothing resolves a hub name at run time.

Requires: small; runs beside TRELLIS on the mesh pod.

License terms that touch us: commercial use, royalty-free. Redistribution needs "Licensed by
NVIDIA Corporation under the NVIDIA Open Model License" in a notice file. Rights end
automatically if a safety guardrail is bypassed without a comparable replacement, or on IP
litigation against the model.

## Local testing

### Qwen3.8 Flash-Next (IQ3_S)

- Weights: the GSQ-RCO IQ3_S quantization of
  [Qwen/Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) from
  [ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF](https://huggingface.co/ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-GGUF),
  served as the `.basalt` pack from
  [jesdga/Qwen3.8-Flash-Next-Basalt](https://huggingface.co/jesdga/Qwen3.8-Flash-Next-Basalt)
- License: [Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE)
  for the base, Apache 2.0 for the quantization

The prod model at a lower-bit quantization, so it fits a 5090: 81.0 GiB on disk, 89.4% the same
top-1 token as Q8_0 on Basalt's wikitext-2 bench. `settings.json` names it by the alias Basalt
serves it under, on :8090 (`docs/local_dev.md`). Never in production.
Everything else in this file runs locally through the same code path as prod (`docs/local_dev.md`).

## License obligations

What the licenses above require of the product as shipped, in one place.

| Obligation | From | Status |
|---|---|---|
| DINOv3 license copy with any redistribution; acknowledge DINO in publications | DINOv3 | Nothing ships the weights; no publication. Nothing owed today. |
| NVIDIA notice file with any redistribution | Kimodo | Nothing ships the weights. Nothing owed today. |
| "Model as a Service" needs a separate Qwen license | Qwen Community 1.0 | Not a MaaS: users control a game prompt, not model inputs or parameters. Watch if a raw prompt or parameter surface is ever exposed. |
| Qwen model name on UI above 100M MAU or US$20M monthly revenue | Qwen Community 1.0 | Below threshold. |
| OpenRAIL++ use restrictions passed to downstream weight recipients | DreamShaperXL | Nothing ships the weights. Nothing owed today. |
