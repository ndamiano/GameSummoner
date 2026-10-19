# Running locally on llama-swap

`GameSummoner summon` runs on a machine whose models sit behind
[llama-swap](https://github.com/mostlygeek/llama-swap). llama-swap starts and stops each server on
demand, so one card holds whichever model the build needs next; `scripts/local_gpu.py auto` drains
one queue at a time, so the card swaps once per stage rather than once per job. We build and test
on one RTX 5090 with Qwen3.8 Flash-Next; anything else runs, with no promise about the games it
makes. [gamesummoner.com](https://gamesummoner.com) runs the full-size models.

```bash
LLAMA_SWAP_URL=http://localhost:8080 JEV_DIR=~/models/jev ./install.sh
GameSummoner summon "A game about frogs eating bugs"
```

## What llama-swap must serve

Three entries, all required. `install.sh` checks each one and stops on the first that is missing.

| Entry (default name) | Serves | Reached at |
|---|---|---|
| `gamesummoner-llm` | an OpenAI-compatible chat server with tool calls; it designs and writes the game | `/v1/chat/completions`, by model name |
| `gamesummoner-decide` | SGLang serving JEV-27B-VL with its decision LoRA and hidden states | `/upstream/gamesummoner-decide/` |
| `gamesummoner-comfy` | ComfyUI v0.39.2 with ComfyUI-RMBG and the weights in `docs/models.md` | `/upstream/gamesummoner-comfy/` |

Other names work: set `GS_LLM_MODEL`, `GS_DECIDE_MODEL` and `GS_COMFY_MODEL` for `install.sh`.

## JEV-27B-VL

The decide entry needs the decision head prepared from JEV and its NVFP4 base (NVFP4 needs a
Blackwell card):

```bash
hf download autotrust/JEV-27B-VL --revision 9e53b8a --local-dir ~/models/JEV-27B-VL
hf download unsloth/Qwen3.8-27B-NVFP4 --revision 7d6f8d4 --local-dir ~/models/Qwen3.8-27B-NVFP4
docker run --rm -v ~/models:/models -v ~/.gamesummoner/images:/gs -w /gs \
  lmsysorg/sglang:v0.5.21-cu130-runtime \
  python3 -m engines.jev.prep --jev /models/JEV-27B-VL --base /models/Qwen3.8-27B-NVFP4 --out /models/jev
```

`--out` is the `JEV_DIR` for `install.sh`: it holds the model SGLang serves, the adapter, and the
head files the decide contract reads. The images repo is cloned by `install.sh`; clone it first
(`git clone https://github.com/ndamiano/gamesummoner-images ~/.gamesummoner/images`) to run the prep
before installing.

## Example config

One group with `swap: true`, so loading one model unloads the others.

```yaml
healthCheckTimeout: 600

models:
  gamesummoner-llm:
    cmd: |
      llama-server --host 127.0.0.1 --port ${PORT} --jinja -fa on -c 131072
        -m /models/Qwen3.8-Flash-Next-IQ3_S.gguf
    checkEndpoint: /health

  gamesummoner-decide:
    cmd: |
      docker run --rm --gpus all --network host -v /models:/models
        lmsysorg/sglang:v0.5.21-cu130-runtime python3 -m sglang.launch_server
        --model-path /models/jev/model --served-model-name gamesummoner-decide
        --enable-lora --max-lora-rank 32 --lora-paths jev-decision=/models/jev/adapter_sglang
        --lora-strict-loading --max-loras-per-batch 2 --enable-return-hidden-states
        --max-running-requests 1 --host 127.0.0.1 --port ${PORT}
    checkEndpoint: /health

  gamesummoner-comfy:
    cmd: |
      /path/to/comfy-venv/bin/python /path/to/ComfyUI/main.py
        --listen 127.0.0.1 --port ${PORT} --use-pytorch-cross-attention
    checkEndpoint: /system_stats

groups:
  gpu:
    swap: true
    exclusive: true
    members: [gamesummoner-llm, gamesummoner-decide, gamesummoner-comfy]
```

`--max-running-requests 1` keeps JEV's answers stable: two co-batched requests moved a probability by
up to 0.05.

## Not checked locally

No safety classifier runs on a local install. Art comes back without a verdict, and the control
plane refuses an unscored render, so a build that asks for art draws it in code instead.
