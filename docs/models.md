# Models

Every model weight GameSummoner runs in production, by queue, with where it comes from, what it
costs to host, and what its license asks of us. `runpod/provision_volume.sh` in
[gamesummoner-images](https://github.com/ndamiano/gamesummoner-images) is the authority for
what sits on the art volume; `docs/deploy.md` for the llm volume. Sizes are on-disk as provisioned.
Licenses were read from the upstream repos on 2026-09-12.

| Queue | Model | Role | Disk | License |
|---|---|---|---|---|
| llm | Qwen3.8 Flash-Next (UD-Q4_K_XL) | design + game build | 106.8 GiB | Qwen Community 1.0 |
| decide | JEV-27B-VL on Qwen3.8-27B (NVFP4) | design decisions | 0.9 + 23.4 GB | Apache 2.0 |
| image | Qwen-Image-2512 (fp8) | sprites, scenes, mesh subjects | 19 GB | Apache 2.0 |
| image | Qwen-Image-Edit-2511 (fp8) | scene embed | 19 GB | Apache 2.0 |
| image | Qwen2.5-VL-7B text encoder (fp8) | shared by both Qwen graphs | 8.7 GB | Apache 2.0 |
| image | DreamShaperXL Turbo v2.1 | tile textures, scene-chain terrain | 6.5 GB | CreativeML OpenRAIL++-M |
| image | BiRefNet | matte (background removal) | ~1 GB | MIT |
| image | Marqo NSFW detector | safety classifier | 22 MB | Apache 2.0 |
| image | TRELLIS.2-4B (bf16) + shape and texture VAEs | image → 3D | 12.4 GB | MIT |
| image | DINOv3 ViT-L/16 | TRELLIS.2 image conditioner | 1.2 GB | DINOv3 License |

## Language

### Qwen3.8 Flash-Next (UD-Q4_K_XL)

- Weights: the UD-Q4_K_XL GGUF from
  [unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)
  (quantization of [Qwen/Qwen3.8-Flash-Next](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)),
  converted to the Basalt pack `Qwen3.8-FN-UD-Q4_K_XL-bf16key-mtptuned.basalt`
- License: [Qwen Community License 1.0](https://huggingface.co/Qwen/Qwen3.8-Flash-Next/blob/main/LICENSE)

The one model that designs and writes the game. A 125B-parameter mixture-of-experts with 6B active
per token, hybrid attention, gated residuals and an n-gram embedding; text and vision in, 262K
native context (1M extended). The pack is 106.8 GiB: the UD-Q4_K_XL weights with the PLE key in
BF16 and the tuned MTP draft layer. `labs/basalt-6000/userdata2.sh` is the conversion recipe; it
needs the converter's `NATIVE_PLE_KEY` edited first.

Served by Basalt on the RTX PRO 6000 (SM120) at a 262144 window, `reasoning_effort=medium`. An EC2
box pulls the pack from the region's bucket onto instance store at boot; a RunPod pod serves it
from the llm volume (`aws/README.md` and `runpod/README.md` in gamesummoner-images).

Requires: RTX PRO 6000 (96 GB, SM120), Workstation or Server Edition; the driver floor in
`queues.llm.allowed_cuda_versions`.

License terms that touch us: redistribution allowed with the notice intact. The model name must
be shown on the UI only above 100M monthly active users or US$20M monthly revenue. A "Model as a
Service" business — giving third parties inference access with meaningful control over inputs and
parameters — needs a separate license from Qwen. GameSummoner exposes no prompt or parameter
control over the model; the user types a game idea and gets a game. See [License obligations]
(#license-obligations).

## Decision

### JEV-27B-VL on Qwen3.8-27B (NVFP4)

- Weights: [autotrust/JEV-27B-VL](https://huggingface.co/autotrust/JEV-27B-VL) at `9e53b8a` (the
  LoRA, the decision head, the tokenizer and templates) over
  [unsloth/Qwen3.8-27B-NVFP4](https://huggingface.co/unsloth/Qwen3.8-27B-NVFP4) at `7d6f8d4`
  (NVFP4 of [Qwen/Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B))
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0), adapter and base

A System One decision model: a LoRA on Qwen3.8-27B, its decision head an `lm_head` LoRA, that turns
a state, a question and its options into calibrated probabilities. The design stage asks it every
decision in `prompts/design/decisions.json` as one `decide` job. Served by stock vLLM 0.32.0 with
`adapter_vllm` as the `jev-decision` LoRA; `contract/decide.py` in gamesummoner-images reads each
decision from the option tokens' logprobs over the OpenAI completions API and applies JEV's bias
and temperature (`engines/jev/head/`).

Requires: one 5090-class card (`local/decide/`, `runpod/decide/`; `aws/decide/` on a g7e.2xlarge).
One prompt per request: prompts sharing a batch moved a probability by up to 0.085.

## Image

### Qwen-Image-2512 (fp8)

- Weights: [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI)
  `split_files/diffusion_models/qwen_image_2512_fp8_e4m3fn.safetensors` (repack of
  [Qwen/Qwen-Image-2512](https://huggingface.co/Qwen/Qwen-Image-2512))
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

20B-parameter text-to-image diffusion transformer. Draws sprites, scenes and the subject renders
that feed TRELLIS. Won the character and object bake-offs against NetaYume and
HiDream (`docs/experiments.md`). Runs at a real cfg, so the negative prompt is live.

Requires: the Qwen2.5-VL-7B text encoder and `qwen_image_vae` below; ~20 GB VRAM at fp8 in
ComfyUI v0.39.2.

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

0.2B-parameter dichotomous segmentation model. Cuts the matte on every sprite and subject render,
which is also the cut-out TRELLIS.2 lifts.

The `1038lab` repack carries no license field of its own; the license is the upstream MIT.

### Marqo NSFW detector

- Weights: [Marqo/nsfw-image-detection-384](https://huggingface.co/Marqo/nsfw-image-detection-384),
  exported by `scripts/export_safety_model.py` to `comfy/models/safety/` (22 MB)
- License: [Apache 2.0](https://www.apache.org/licenses/LICENSE-2.0)

timm image classifier at 384px. Gates every image before it reaches a game. Its
threshold was set on the false-negative rate at a fixed false-positive budget over 290 renders
(`docs/experiments.md`, 2026-08-03); a replacement is judged the same way or not at all.

Requires: CPU is enough.

## 3D

### TRELLIS.2-4B

- Weights: [Comfy-Org/TRELLIS.2](https://huggingface.co/Comfy-Org/TRELLIS.2)
  `diffusion_models/trellis_2_bf16.safetensors`, `vae/trellis_2_shape_vae_bf16.safetensors`,
  `vae/trellis_2_texture_vae_bf16.safetensors` (ComfyUI repack of
  [microsoft/TRELLIS.2-4B](https://huggingface.co/microsoft/TRELLIS.2-4B))
- License: [MIT](https://huggingface.co/microsoft/TRELLIS.2-4B/blob/main/LICENSE)

4B-parameter flow-matching transformer over sparse voxels; one image in, a PBR-textured mesh out.
Every 3D asset and every world subject comes through it. Runs on ComfyUI's native nodes
(`src/config/workflows/img2mesh.json`) at the 512 tier: remesh, decimate to 50k, unwrap, and a
1024 base-colour and metallic/roughness bake, 8-9 s a mesh on a 5090 under v0.34.4 (`labs/trellis-comfy`). A
finished GLB is cut to game weight, webp textures and single-sided, by `runtime/decimate.mjs`.

Requires: ComfyUI v0.39.2 launched with `--use-pytorch-cross-attention` (DINOv3 has no xformers
SM120 kernel), and an alpha channel on the input image, whose mask drives the crop.

### DINOv3 ViT-L/16

- Weights: [Comfy-Org/TRELLIS.2](https://huggingface.co/Comfy-Org/TRELLIS.2)
  `clip_vision/dino_v3_vit_l.safetensors` (ComfyUI repack of the gated
  [facebook/dinov3-vitl16-pretrain-lvd1689m](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m))
- License: [DINOv3 License](https://ai.meta.com/resources/models-and-libraries/dinov3-license)

303M-parameter vision transformer; TRELLIS.2's image conditioner, so it is not optional while
TRELLIS.2 is. 1.2 GB.

License terms that touch us: commercial use allowed, royalty-free. Redistribution must carry a
copy of the agreement; publications must acknowledge DINO. Prohibited: military, nuclear,
espionage, weapons and ITAR-controlled uses. Filing an IP claim against Meta over DINOv3 ends the
license. Pixal3D and every other TRELLIS.2 derivative inherit the same encoder.

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

## Engines

The servers the weights above run in. None is shipped: the worker images are private, built from
public code.

| Engine | Serves | License |
|---|---|---|
| [ComfyUI](https://github.com/Comfy-Org/ComfyUI) v0.39.2 | the comfy queue: images and TRELLIS.2 | GPL-3.0 |
| [ComfyUI-RMBG](https://github.com/1038lab/ComfyUI-RMBG) 3.0.0 | the BiRefNet matte inside ComfyUI | GPL-3.0 |
| [vLLM](https://github.com/vllm-project/vllm) 0.32.0 | the decide queue | Apache 2.0 |
| [Basalt](https://github.com/ndamiano/basalt) `gamesummoner-v1` | the llm queue, in production and on a local 5090 | MIT |

## License obligations

What the licenses above require of the product as shipped, in one place.

| Obligation | From | Status |
|---|---|---|
| DINOv3 license copy with any redistribution; acknowledge DINO in publications | DINOv3 | Nothing ships the weights; no publication. Nothing owed today. |
| "Model as a Service" needs a separate Qwen license | Qwen Community 1.0 | Not a MaaS: users control a game prompt, not model inputs or parameters. Watch if a raw prompt or parameter surface is ever exposed. |
| Qwen model name on UI above 100M MAU or US$20M monthly revenue | Qwen Community 1.0 | Below threshold. |
| OpenRAIL++ use restrictions passed to downstream weight recipients | DreamShaperXL | Nothing ships the weights. Nothing owed today. |
| Source offer and the GPL to anyone given a ComfyUI build | ComfyUI, ComfyUI-RMBG | The ComfyUI image is private and never distributed. Nothing owed today; owed the day an image or an installer bundling ComfyUI ships. |
