# Running Maestro locally

Everything needed to bring the platform up on one box, and the settings that decide how it behaves.
`docs/deploy.md` is the same story for prod.

---

## Bring it up

Four things run: the control plane, the frontend, whatever serves the models, and one worker per
queue (three queues: `decide`, `llm`, `image`). The control plane touches no GPU at all — the
worker-pull queue is the only transport — so a queue with no worker means every job on it times out.

**Backend** — `source venv/bin/activate && python run.py`

**Frontend** — `cd frontend && npm run dev`

**Workers** — `scripts/local_gpu.py` starts them (below), and it is the only way to: a worker
row is created, priced, before the worker runs — the scaler does it for a pod at the pod's rate,
`local_gpu.py` does it for this box at zero — and a claim from a worker id nobody created is
refused, so a bare `python -m worker.agent` never lands a job. `decide` runs the image
repo's own box (`local/decide/run.sh`, docker image `gamesummoner/decide-local`), which carries its
engine, contract server and worker in one container.

**One card, three queues, `auto`** — a world build (`compose_world`, or `worldgen.build.build_world`
by hand) alternates llm and image jobs many times and
blocks until each completes, so there is no point at which the legs can be drained "one at a time
by hand". `scripts/local_gpu.py auto` runs the control plane's other half instead: it polls all
three queues, starts (model + worker) whichever has pending work — favoring the queue it already
holds so a momentary empty doesn't thrash it, except that a queue whose oldest job has waited
ten minutes takes the card, because the control plane fails a job nobody claims in thirty and a
build's turns can keep the llm queue non-empty for an hour — and stops what it started on
SIGINT/SIGTERM. Handing the card over waits up to `HANDOFF_SECONDS` for the worker to finish the
job it holds, because that generation is already paid for and a killed turn re-pays its prefill
when the job is re-driven. A server already on the port is USED rather than refused — it belongs to
whoever started it, and auto never stops it — so another ComfyUI on 8188 no longer costs auto the
queue, or the card it was holding. The llm leg runs Basalt from `BASALT_DIR` (default
`~/Documents/basalt`) with the engine flags in `BASALT_CONFIG` (default
`/var/lib/models/basalt/basalt-iq3s.json`). Run it
in one terminal alongside `python run.py`; `--idle-exit SECONDS` stops and exits once nothing is
pending anywhere for that long (default: never). The single-leg (`decide`/`llm`/`image`) and
`all` modes still exist for draining one queue by hand.

A build that calls `compose_world` needs `auto` for the same reason: the world's stages and the
build's own turns share the llm queue, and its art rides the image queue while the build keeps
writing code.

**CI locally** — `scripts/ci.sh` runs every job in `.github/workflows/ci.yml` (pytest, ruff,
eslint, tsc, vitest, vite build) and prints one verdict per step. Run it before a push.

**Pre-commit** — `git config core.hooksPath scripts/githooks`, once per clone. Git honours ONE
hook path, so everything a commit is checked against lives in that directory. It runs the comment
audit (`comment-audit.py`: every comment line the commit adds, listed to answer for itself against
the standard in `CLAUDE.md`; a terminal answers y/N, a non-interactive commit is blocked and
re-run with `COMMENTS_REVIEWED=1`), then `ci.sh` scoped to what the commit stages: eslint +
typecheck + vitest for `frontend/`, ruff + the backend suite for Python. It deselects `-m browser`
— the dozen tests that launch a real headless chromium — and CI runs those. `--no-verify` skips
the lot.

**Tests** — `python -m pytest tests/ -q -n auto` (`requirements-dev.txt` holds pytest, xdist,
httpx for the TestClient, and ruff; the suite is
process-parallel, 36 s serial against 9 s here), frontend
`cd frontend && npm test` (vitest).

---

## Model servers

### LLM — Basalt (preferred on a 5090)

[Basalt](https://github.com/jesdga95/basalt), a Strata fork for Blackwell, serves the prod model,
Qwen3.8 Flash-Next, on the 5090 as the GSQ-RCO IQ3_S pack (`docs/models.md`): routed experts
stream through a GPU expert cache, the rest stays resident. Locally it is the closest thing to
prod's Pennyroyal: the same model at a lower-bit quantization.

```
<basalt>/bin/basalt serve \
  --config /var/lib/models/basalt/basalt-iq3s.json \
  --alias <llm.model> \
  --host 127.0.0.1 --port 8090 \
  --vision dynamic \
  --fit-max-tokens
```

The config holds the engine flags: the `.basalt` file, `spec` 7, int8 KV, `max-context` 262144,
the expert cache sized to the free VRAM. Flags on the command line win over it. `--alias` must
match `llm.model` in settings.json: every request names it. `--vision dynamic` borrows
expert-cache slots while it reads an image, so the worldgen refine stages can show the model its
renders without a standing vision allocation. The window sits above `n_ctx` (131072), and
`--fit-max-tokens` trims a request's `max_tokens` rather than refusing it, because Basalt counts a
build's prompt about 1.5K tokens longer than the harness does and answers 400 to a request that
overflows (measured 2026-10-15, `labs/basalt-6000`). Prod's Pennyroyal runs 524288 against the
same budget for the same reason. Thinking is `llm.reasoning` (below), sent as `reasoning_effort`
on every call.

### LLM — llama.cpp

```
llama-server \
  --models-dir <model dir> \
  --host 0.0.0.0 --port 8080 \
  -ngl 99 \
  -c 131072 \
  -fa on \
  --cache-type-k q8_0 --cache-type-v q8_0 \
  --jinja \
  --reasoning-budget 0 \
  --chat-template-kwargs '{"enable_thinking":false}' \
  --spec-type draft-mtp \
  -np 4
```

`--chat-template-kwargs '{"enable_thinking":false}'` is load-bearing and `--reasoning-budget 0`
alone is a no-op: without it Qwen3.6 thinks in `content` and authoring turns truncate at the output
cap before the tool call. `-fa on` plus q8_0 KV keeps the cache from spilling out of VRAM into host
RAM.

Set `-c` and `llm.n_ctx` to the same number — see the settings note below for why both directions
hurt.

### Images — ComfyUI

Local: `comfy-start`. Over the network: run `main.py --listen 0.0.0.0` from the Comfy checkout.

Meshes render here too: `src/config/workflows/img2mesh.json` is TRELLIS.2 on ComfyUI's native
nodes, which need v0.39.2 and the four `Comfy-Org/TRELLIS.2` weights (`docs/models.md`).

---

## Builds from the CLI

```
cd src && python -m maestro.codegen.run "<request>"     # prompt → build
```

- `--new "<request>"` stops with the prompt on disk so it can be edited first
- `--build <run_id>` builds that stored prompt
- `--change <run_id> "<note>"` changes a built game from a play note
- `--assets <run_id>` re-renders the art the game asked for and never got
- `--history <run_id>` lists a run's snapshots; `--restore <run_id> <ref>` puts the game back to one
- `--evict <run_id>` / `--rehydrate <run_id>` move a run dir to and from S3
- `--archive-all` uploads every settled run that has no archive yet (the nightly sweep in
  `docs/backups.md`)

**Playing a build:** `runtime/games/<run_id>/index.html`. A game is plain browser files, but a 3D
one needs http, not `file://` — `<script type="module">` is CORS-blocked from a file origin.

## Builds on RunPod pods from this box

The prod model on prod pods, driven by the local control plane, through the exact prod path. A
pod never listens; it boots the worker and polls `CP_URL/worker/claim`, so the only thing
this box has to add is a URL a pod can reach.

1. `sudo tailscale funnel --bg 8000` (once: `sudo tailscale set --operator=$USER` lets you do
   it without sudo). `tailscale funnel status` prints the URL. It is a public HTTPS door to
   :8000 — the worker path is bearer-gated like prod, the landing page is not — so
   `sudo tailscale funnel off` when done.
2. In settings.json: prod's `runpod` block (`queues`, templates, volumes; take it from the
   droplet's settings.json) with `enabled: true` and `cp_url` = the funnel URL; `llm` =
   what the pod serves — `model` `pennyroyal`, `n_ctx` 131072, `slots` 2 — because the pod
   refuses to boot on a model mismatch and every request names it. `max_workers: 1` per queue
   bounds spend. `workqueue.job_timeout_seconds` ≥ the longest single call you will queue (a
   100K-token design at xhigh runs past the 1800 s default).
3. Stop `scripts/local_gpu.py auto` and any hand-started worker: a local worker claims the job
   before the pod boots.
   Prod's autoscaler shares the RunPod account and reaps every `maestro-<queue>-*` pod that
   never registered with PROD inside `boot_deadline_seconds` — a pod that registered here dies
   at exactly five minutes. Set `runpod.pod_prefix` to something else (`gsdev`), and then prod
   cannot see these pods and this scaler cannot see prod's.
4. Restart `run.py`; the log says `autoscaler started on runpod`. Then build exactly as usual —
   `python -m maestro.codegen.run "<ask>"`, or a pinned prompt through `run_build`. Scale-from-
   zero fires on the first pending job (the `min_jobs_per_pod` rule only gates a second pod).
   Rent → claim is ~3 min on a warm image; a stock-out shows as `scale-up llm: ... refused`
   every tick and the pending job dies at the reaper's 30 min unless something re-enqueues it.

The pod's own view: `GET /pods/<id>` on the RunPod API (`scaler.runpod_client`) has its env,
uptime and GPU utilisation, and the control-plane log shows its claims and heartbeats arriving
from a RunPod address.

The same from EC2: add the `aws` block (below) with `enabled: true`, keep the llm policy block
in `runpod.queues` (it needs no `template_id` or cards when `runpod.enabled` is false), and the
log says `autoscaler started on aws`. A box is ~3 min from launch to its first claim and ends
itself on idle; `pod_prefix` keeps this scaler and prod's apart in a shared account the same way.

---

## Frontend notes

The palette is CSS variables in `frontend/src/index.css`; `tailwind.config.js` only names them, so
a colour change hot-reloads. Editing the config itself (a new name, a font) needs the dev server
restarted — Node caches the ESM config, and a stale one drops every custom class silently.

---

## Settings

`src/config/settings.json`, gitignored. Copy from `settings.example.json`.

### `llm`

`model`, `n_ctx`, `max_tokens`, `reasoning`, `slots`, `sglang_args`, `basalt_args`. There is no
endpoint — inference rides the queue.

`slots` is how many jobs one llm worker runs at once against its engine (`worker/agent.py
--slots`); an llm pod reads it at create. The scaler counts workers, not slots, so the llm
queue's `min_jobs_per_pod` is per pod and scales with it: two slots, twice the backlog before a
second card is rented.

`n_ctx` is the INPUT budget: when the prompt ABOUT TO BE SENT — the last one the server counted
plus the rounds added since — leaves less than 16K tokens of it, `build_steps.compact`
stubs every superseded file body and drops the rounds that only read, then stubs the bodies out
of the oldest rounds until a third of the window holds the rest and, only if that is not enough,
drops the oldest whole rounds; every compaction re-grounds the model on the code map and on which
files it has already read unchanged (`CLAUDE.md`). Nothing else trims.

The two `_args` strings are appended to an engine's launch line, one per engine: `basalt_args` by
`scripts/local_gpu.py` for Flash-Next IQ3_S on the local 5090, `sglang_args` by the pod
entrypoint for Flash-Next NVFP4 on an RTX PRO 6000 (`docs/deploy.md`). Prod and the local box run
the same model at different quantizations on different cards; the build harness is the same.

Set it to the server's `-c`. The local router never reports its window, so both errors are yours to
avoid, and only one of them announces itself:

- **Too large** — nothing trims until the prompt has already overflowed.
- **Too small** — silent. A build whose files no longer fit one read-everything round compacts every
  turn, forgets, re-reads, and grinds to the turn cap. Measured 2026-08-01: a visual novel at
  `-c 32768` spent 31 compactions and ~100 of 120 turns re-reading its own five files.

`max_tokens` is only the connector's default ceiling — the build path passes its own.

`reasoning` is the thinking effort of every llm call, the build included, sent to the engine as
`reasoning_effort`. The Qwen3.8 template takes `none`, `low`, `medium` and `xhigh`; `high` is
rejected (`reasoning_effort_not_supported`). `none` DISABLES thinking, and a non-thinking build is
the "something kinda close" arm of `docs/experiments.md` (2026-08-25), so a build server runs at
`medium` or above. Prod's `LLM_REASONING` default is `none` and ships in the same deploy as the
wire that forwards it — deploying the wire alone turns prod's thinking off.

### The wire format is the worker's, not a setting

The control plane enqueues a CANONICAL chat request; the worker translates it for whatever its own
target serves (`worker.agent --api chat|responses`, default `chat`; see `llm_clients/wire.py`).
`responses` honors `reasoning.effort` as written; `chat` carries it as `reasoning_effort`, which
llama.cpp ignores.

On `chat` everything else passes through — so **sampling is a launch flag on the target server,
not a request field**, and on llama.cpp so is thinking (see the invocations above). Adding an engine is a
branch in `worker/handlers.llm`, never a settings change.

### `workqueue`

`token` is the worker bearer secret. There is no `enabled` flag and no GPU endpoint on this side.

exec_seconds are debited to the owning game by the `game_id` on the job row, and a job with none is
neither metered nor gated. Build and asset enqueues pass it directly; the one blocking path
(`workqueue/client.py`’s `run_job`, what the connector's `generate_with_tools` rides) reads it off the
`run_scope` contextvar instead — a call there outside a scope spends GPU nobody is charged for.

### `data_dir`

Env `MAESTRO_DATA_DIR`, default `<repo>/data`. Where platform.db + auth.db live — control-plane
state, deliberately not under `working_directory`.

### `play.origin` / `play.app_origin`

Set BOTH to serve games from their own registrable domain; empty means one origin. settings.json
is the only place these live — prod's values are in `docs/deploy.md` "Public domains".

Either way /play auth is the handoff flow: the SPA mints a single-use token
(`POST /api/games/<id>/play-session`), `/handoff` redeems it into a per-game path-scoped grant
cookie, and the served `index.html` gets the console reporter injected on the way out (the game
folder stays pristine). See `docs/deploy.md` "Public domains".

### `smtp`

host/port/username/password/from_address — outbound mail, `tools/mailer.py`. One caller: the
password-reset link, whose address comes from `play.app_origin`. Unconfigured, the forgot-password
endpoint still answers ok (it must not report who has an account) and logs that it could not send —
so a box without it has no account recovery.

### `s3`

endpoint/region/bucket/keys — the off-box run-dir archive. An unconfigured bucket logs and stands
aside.

### `runpod`

The autoscaler (`src/scaler/` + `docs/deploy.md`): `enabled`, `api_key`, `network_volume_id`,
`cp_url` (the pod-reachable control-plane URL), `tick_seconds`, `stale_worker_seconds`, and
per-queue `queues.<name>` scaling blocks (template_id, network_volume_ids, gpu_type_ids,
allowed_cuda_versions, max_workers, thresholds, cooldown, idle_exit_seconds,
boot_deadline_seconds). The `queues` dict
replaces the default wholesale — carry complete blocks. settings.json is the only place the block
lives; `docs/deploy.md` describes what each knob does to a pod.

`gpu_type_ids` is PRIORITY-ORDERED: the scaler asks for the head alone and widens to the whole list
only when RunPod refuses that create, since the cards are not substitutes. Which card a pod GOT is the worker's to report, from the device — a control plane that
records its own request records the first list entry forever (measured 2026-08-01: 879 prod jobs
stamped 5090, the bill entirely RTX PRO 4500).

A pod's price is the provider's own (`workers.usd_per_hour`, written at create: RunPod's
`costPerHr`, or the EC2 rung's spot or on-demand price),
and it is what every job that pod runs debits (`docs/finance_information.md`); there is no rate
table to keep. The admin fleet view shows it beside each queue's provider stock-outs
(`pod_refusals`, see `docs/deploy.md` "Autoscaler"): the outage under way, the count over the last
seven days, and the never-pruned requests-vs-stock-refusals ratio over 60 days and all time;
nothing when zero.

`llm.model` must be what the engine answers to: it reaches the pod as `LLM_MODEL`, and the
autoscaled llm image serves `pennyroyal` and refuses to boot under any other name.

### `aws`

The scaler's second provider (`scaler.ec2_client`, `docs/deploy.md` "Autoscaler"): `enabled`,
`access_key`, `secret_key`, `security_group` and `instance_profile` (both `gs-gpu-worker`, by
name, in the default VPC), and `queues.<name>` launch blocks: `instance_types`, `markets`
(`spot`, `on-demand`), and `amis` by region. Only a region with an ami is priced, launched in
or listed. Scaling policy is not here: a queue scales by its `runpod.queues` block whichever
provider its machines come from. `workers.source` says which provider a row's machine is on, and
an EC2 row's `pod_id` is `<region>/<instance id>`.

### `payments`

`stripe_secret_key`, `stripe_webhook_secret` — the storefront (`api/routers/billing.py`). Both
are required: a box missing either 500s every billing call, and the webhook signs under the
second, so it is never empty where the webhook is reachable. Checkout returns the payer to
`play.app_origin`. Credits also arrive by `auth.cli grant`.

### `demo_games`

`showcase` and `oneshot`, each a list of run ids the landing page serves unauthenticated
(`api/routers/demos.py`). What is served is the snapshot `python -m maestro.demos snapshot <run_id>`
copied into `runtime/demos/` — a listed id with no snapshot is skipped, and no later build of the
run touches the snapshot. Curation is a deploy-time decision: nothing a build does can put a game
here. Both empty means no demo surface.

---

## Environment variables

Everything structured is in settings.json; the env carries only what `.env.example` lists (server
bind, `WORKING_DIRECTORY`, `WORKQUEUE_TOKEN` + its timeouts, the `LLM_*` defaults) plus:

- `MAESTRO_DATA_DIR` — above.
- `MAESTRO_DEV=1` — uvicorn reload, `/docs` + `/redoc` served.
- `MAESTRO_STALE_PENDING_SECONDS` (default 1800) — `workqueue/reaper.py`: a job pending this long with
  nobody claiming it is failed rather than left to hold its budget reservation forever.
- `MAESTRO_LOCAL_LOGS` (default `/tmp/maestro-local`) — where `scripts/local_gpu.py` writes the
  model-server and worker logs it starts.

---

## Reading the platform DB

`scripts/db.py` — stdlib only, runs anywhere the file is: named queries (`cost`, `builds`,
`models`, `failures`, `turns`) and `sql "<text>"` for the rest; `--db` points it at another file, `--since`
at a date. Read it from a snapshot, never the live file (`docs/backups.md` "Query hygiene").
