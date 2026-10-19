# Running locally on llama-swap

`GameSummoner` runs on a machine whose models sit behind
[llama-swap](https://github.com/mostlygeek/llama-swap). llama-swap starts and stops each server on
demand, so one card holds whichever model the build needs next; `scripts/local_gpu.py auto` drains
one queue at a time, so the card swaps once per stage rather than once per job. We build and test
on one RTX 5090 with Qwen3.8 Flash-Next; anything else runs, with no promise about the games it
makes. [gamesummoner.com](https://gamesummoner.com) runs the full-size models.

```bash
git clone https://github.com/ndamiano/GameSummoner.git && cd GameSummoner && ./install.sh && GameSummoner
```

`install.sh` looks for llama-swap at its default address, `http://localhost:8080`; set
`LLAMA_SWAP_URL` if yours listens elsewhere. `GameSummoner` opens a terminal view of every game built
on this machine; type a request on its top row to summon one. The README covers the view.

## What llama-swap must serve

Three entries, all required, each reached through llama-swap the same way a client reaches any
model. Any entry names work: `install.sh` lists the models llama-swap serves, asks which is the
LLM and which the decision model, asks for the ComfyUI entry (unlisted is fine) and checks that it
has every node and model file in `src/config/workflows/`. It then lists what it will install and
waits for a yes, and saves the choices in `settings.json`. Rerun it to change them.

| Entry (default name) | Serves | Reached at |
|---|---|---|
| `gamesummoner-llm` | an OpenAI-compatible chat server with tool calls; it designs and writes the game | `/v1/chat/completions`, by model name |
| `gamesummoner-decide` | vLLM serving JEV-27B-VL's decision LoRA as `jev-decision` | `/v1/completions`, by model name |
| `gamesummoner-comfy` | ComfyUI v0.39.2 with ComfyUI-RMBG and the weights in `docs/models.md` | `/upstream/gamesummoner-comfy/` |

Without a terminal, `install.sh` asks nothing: set `GS_LLM_MODEL`, `GS_DECIDE_MODEL` and
`GS_COMFY_MODEL`, or it uses the names already in `settings.json`, then the suggested ones.

## JEV-27B-VL

The decide entry is stock `vllm serve`: the
[unsloth/Qwen3.8-27B-NVFP4](https://huggingface.co/unsloth/Qwen3.8-27B-NVFP4) base (NVFP4 needs a
Blackwell card) with [autotrust/JEV-27B-VL](https://huggingface.co/autotrust/JEV-27B-VL)'s tokenizer
and its `adapter_vllm` loaded as the `jev-decision` LoRA. Each decision is one completion with
`max_tokens: 1` and logprobs over the option tokens; the per-option bias and per-kind temperature
that turn those into probabilities ship with gamesummoner-images (`engines/jev/head/`).

```bash
hf download autotrust/JEV-27B-VL --revision 9e53b8a --local-dir /models/JEV-27B-VL
hf download unsloth/Qwen3.8-27B-NVFP4 --revision 7d6f8d4 --local-dir /models/Qwen3.8-27B-NVFP4
```

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
    useModelName: jev-decision
    cmd: |
      docker run --rm --gpus all --ipc host -p ${PORT}:8000 -v /models:/models
        vllm/vllm-openai:v0.32.0
        --model /models/Qwen3.8-27B-NVFP4 --tokenizer /models/JEV-27B-VL
        --enable-lora --max-lora-rank 32 --lora-modules jev-decision=/models/JEV-27B-VL/adapter_vllm
        --logprobs-mode processed_logprobs --max-logprobs 32 --max-model-len 32768
        --enforce-eager --limit-mm-per-prompt '{"image":0,"video":0}' --load-format runai_streamer
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

The decide contract sends one prompt per request: JEV's LoRA path moves probabilities when
sequences share a batch. A decision generates one token, so the decide entry skips what only
speeds up generation (`--enforce-eager`: no torch.compile, no CUDA graphs) and the vision encoder,
and streams its weights: it boots in about 45 s on a 5090 instead of 146 s.

## Not checked locally

No safety classifier runs on a local install. Art comes back without a verdict, and the control
plane refuses an unscored render, so a build that asks for art draws it in code instead.
