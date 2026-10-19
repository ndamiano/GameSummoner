# Technology watchlist

New models improve output; new inference shells lower cost. Both compound, and neither announces
itself — so the stack gets re-validated against what has shipped, on a schedule rather than on a
hunch.

A candidate stays here until it has been RUN against real builds. Then the numbers and the ruling go
to `docs/experiments.md` and the entry moves to "Previously investigated" at the bottom — including
the ones we reject, because "why we didn't switch" is the expensive thing to relearn.

## What we run today

| Job | Model | Served by | Defined in |
|---|---|---|---|
| Build turns (the LLM) | Qwen3.8 Flash-Next: NVFP4 in prod, IQ3_S on a 5090 | Pennyroyal SGLang on an RTX PRO 6000; Basalt on a 5090 | `llm.model` in settings.json |
| Tiles | DreamShaperXL Turbo v2.1 → min-cut quilting | ComfyUI + `tools/quilting.py` | `workflows/txt2img_tile.json` |
| Sprites, scenes, mesh subjects, scene-chain subjects | Qwen-Image 2512 | ComfyUI | `workflows/txt2img_subject.json` |
| Scene-chain terrain | DreamShaperXL Turbo v2.1 (masked img2img) | ComfyUI | graph built in `scenegen/paint.py` |
| Scene-chain embed | Qwen-Image-Edit 2511 | ComfyUI | `workflows/imgedit_scene.json` |
| 3D meshes | TRELLIS 2 | home-rolled runtime, `engines/trellis/trellis_server.py` in gamesummoner-images | — |
| Render safety scoring | small timm ViT, locally exported | in the image worker | `scripts/export_safety_model.py` |
| Sound effects, music, voice | **nothing** | — | — |

A `kind` picks the model and the workflow (`tools/comfyui_tools.build_image_job`). Audio is the one
whole category we do not produce at all, and it is wanted.

## Things to validate
- [ ] Inflect-nano-v2 | inflect-micro-v2 for voice gen. I'm not sure if this will be useful as it's only a single voice,
      but it might be valuable either way. We can look into using this as a way to train our own voice models?
      https://www.reddit.com/r/LocalLLaMA/comments/1v5ve6v/i_released_inflect_v2_two_ultratiny_complete_tts/
      https://huggingface.co/owensong/Inflect-Micro-v2
- [ ] TensorSharp. It claims that it gets a speed boost on prefill and TTFT, though slightly lower decode vs llama.cpp
      on CUDA. Worth investigating either way.
      NOTE: the published comparison is against llama.cpp, which is not our 5090 baseline — Basalt
      is. Prefill/TTFT is still the right thing to watch: a build's input IS its transcript, so
      prefill is most of a turn.
      https://www.reddit.com/r/LocalLLaMA/comments/1v6ect8/benchmarks_tensorsharp_vs_llamacpp/
      https://github.com/zhongkaifu/TensorSharp
- [ ] Vibe voice from microsoft for voiceovers
      https://github.com/microsoft/VibeVoice
- [ ] Minimax H3 video generation. Maybe not relevant? Maybe relevant? Worth investigating either way.
      NOTE: already verified to RUN locally on the 5090 (video + audio, in mess-with-comfy) — VRAM
      is tight. So the open question is not "can we run it" but what a game does with video that a
      canvas cannot.
      https://www.reddit.com/r/StableDiffusion/comments/1vdoqb9/repost_because_not_everyone_can_read_blurry/
- [ ] RTX 6000 optimizations
      https://github.com/local-inference-lab/rtx6kpro
- [ ] Scenema voice, seems legit good!
      https://www.reddit.com/r/LocalLLaMA/comments/1vgfmee/scenema_audio_comes_to_comfyui_runs_on_8gb_vram/
      https://huggingface.co/ScenemaAI/scenema-audio
- [ ] Shieldstral, might be good for safety filtering?
      https://huggingface.co/mistralai/Shieldstral-1.0-3B
      NOTE: this is the only candidate on the list that touches a LIVE fail-closed seam. Swapping it
      is not a like-for-like trial — a safety model is judged on its false-NEGATIVE rate at a fixed
      false-positive budget, and the current classifier's threshold was set that way over 290
      renders (`docs/experiments.md`, 2026-08-03). Same method or no swap.
- [ ] For music generation we might want to try minimax music 3
      https://www.reddit.com/r/comfyui/comments/1vnf0p2/comfyorgminimaxmusic3_hugging_face_now_online/
- [ ] trellis.cpp (MIT, GGUF weights). TRELLIS 2 on GGML, CUDA or Vulkan, no spconv / flash-attn /
      flex_gemm. Not a quality lever; a robustness one — every sm_120 build trap disappears. Slow
      (~7 min a mesh on a 5060 Ti), so a fallback backend for the mesh server at most.
      https://github.com/pwilkin/trellis.cpp
- [ ] SymTRELLIS (arXiv 2606.04108). A sampling-time symmetry constraint on TRELLIS 2, no retraining,
      no code released. Small enough to reimplement from the paper; vehicles, furniture and weapons
      are where it would show.
      NOTE: TRELLIS 2's image conditioner is DINOv3 under Meta's DINOv3 License, not MIT —
      commercial use allowed, "Built with DINOv3" attribution and license redistribution required.
      Pixal3D inherits the same encoder.
- [ ] https://www.tripo3d.ai/blog/game-industry-character-workflow
      A blog about how to create characters to game industry standards.
- [ ] https://www.reddit.com/r/TopologyAI/comments/1wt9hxb/new_opensource_texttoanimation_ai_for_humans/
      Open source text to animation thing. Worth taking a look at.
- [ ] https://www.reddit.com/r/LocalLLaMA/comments/1ww09ab/new_architecture_from_percepta_spotlight/
      New architecture from percepta? Can it maybe be used?
- [ ] Hunyuan 3? https://www.reddit.com/r/StableDiffusion/comments/1wzclz5/hunyuanimage_30_80b_running_natively_in_comfyui/
- [ ] https://www.reddit.com/r/TopologyAI/comments/1wzlljg/new_opensource_ai_texturing_for_3d_models_at_2k/
      better textures for 3d?


## Previously investigated

- **Inference shell on the 5090: ninfer → Basalt** (adopted 2026-10-16). Basalt serves the prod
  model, Flash-Next, as an IQ3_S pack on the same card, where ninfer served the 27B stand-in.
- **Inference shell: llama.cpp → ninfer** (adopted 2026-08-01). ~3x: 194.5 vs 63.3 tok/s on the
  same weights on a 5090. ninfer serves Qwen only, on 5090s only, and speaks `chat` only, so llama.cpp
  stays the fallback everywhere else. Thinking and sampling are launch flags on both — see
  `docs/local_dev.md`.
- **Image models: flux1-schnell-fp8 and animaOfficial retired** (bake-off 2026-08-06). Replaced by
  per-kind routing: Qwen-Image 2512 for sprites, scenes and objects,
  DreamShaperXL Turbo for tile textures. Both run at a real cfg, so the negative prompt is live on
  every kind. animaOfficial's weights are non-commercial, so its orphaned workflow was deleted
  outright (2026-08-08) rather than left to be picked up again — every model in the table above is
  permissively licensed on the WEIGHTS, not just the code.
- **Image-to-3D: TripoSplat, LATO.2, img2threejs — run and not adopted** (2026-09-03, five
  subjects against the TRELLIS 2 `512` pipeline, `docs/experiments.md`). TripoSplat is the most
  faithful image of the three by a wide margin at 7 s per subject, and is out as a class: a
  gaussian splat is not a mesh, so nothing collides with it, and its lighting is baked from the
  photo, so it cannot be relit in a scene. LATO.2 regenerates a mesh's topology from a voxel
  scaffold and returned filled-in frames, faceted noise and non-watertight surfaces at both 2000
  and 5000 vertices. img2threejs is a Claude Code skill, not a model — a frontier agent writing
  procedural three.js behind vision gates — and eleven minutes of it made a 0.62 barrel by its own
  score. Splat generators are disqualified until one ships a lit, collidable mesh. A web sweep
  the same day (HF, GitHub, arXiv, Mar–Sep 2026) found no permissive image-to-mesh model that
  beats TRELLIS 2; the field's 2026 gains (Meta AssetGen and MeshFlow, Seed3D 2.0, Hunyuan3D 3.x)
  are closed or non-commercial, and TriFlow's weights are non-commercial too. What survived is
  on the list above.
- **Pixal3D — run and not adopted** (2026-09-03, `docs/experiments.md`). TRELLIS 2's backbone at a
  1024 cascade: finer geometry than our `512` tier, kept through a 15K decimation, at ~55 s a
  mesh resident against ~10 s, no 512 tier, one subject in five exported fully metallic, and a
  gated non-commercial background remover in its default pipeline. Multi-view conditioning
  (Sep 2026) needs several posed views of one object, which nothing upstream of the mesh queue
  produces. Its `--low_vram` staging is the useful part: it fits a 1024 cascade on a 5090,
  which our own server cannot — see the verdict in `docs/experiments.md`.
- **Audio: surveyed and measured, deliberately not implemented** (2026-08-01, 58 generations —
  `docs/experiments.md`). ACE-Step 1.5 (MIT, code + weights) does 60 s of music in 1.8 s with the
  planner off; MOSS-SoundEffect v2.0 (Apache 2.0) does SFX in 2–3 s at 19.6 GB resident. Ruled out
  on WEIGHTS licensing, whatever the code says: Stable Audio, MusicGen/AudioGen/AudioCraft,
  AudioLDM 2, Tango 2. Music ships before SFX, because MOSS needs a full TRELLIS-shaped lift while a
  WebAudio oscillator beats a diffusion model at a 0.2 s arcade blip.

  **Read that entry before trialling any audio candidate above** — it already holds the license
  method, the integration shape, and three environment traps (Blackwell needs
  `--use-pytorch-cross-attention`, the box has no ffmpeg, and ComfyUI's node cache will fake your
  numbers unless you unload between arms).
