# Deploy runbook

Deploying Maestro is deploying **one CPU-only container**: API + SPA (same-origin, single uvicorn
worker) + the job queue. Parameterized by `.env` — no code edits to deploy.

GPU capacity is attached separately and is not a deploy step; see [Attaching
capacity](#attaching-capacity).[^1]

[^1]: The control plane enqueues; workers claim over `/worker`. No GPU host appears in any
control-plane config, and prod never dials out. Contract details: `docs/architecture.md`.

---

## Files

- `Dockerfile` — multi-stage: node builds `frontend/dist`, a second node stage resolves the mesh
  toolchain, then a `python:3.12-slim` runtime installs deps and copies `run.py` + `src/` +
  `runtime/` + the built SPA, and runs as non-root.
- `docker-compose.yml` — the `app` service (build, `.env`, the two named volumes, the
  `settings.json` bind mount, healthcheck).
- `.env.example` — every knob; copy to `.env` and edit.
- `scripts/provision.sh` — one-time host setup.
- `scripts/deploy.sh` — ship dev → prod.

`settings.json` is dockerignored and rides a host bind mount, so `runpod.queues` (which env
can't express) is editable without a rebuild — but the file must exist before the first `up`.

## The data invariant (critical)

Durable state lives on **four named Docker volumes**, all outside the rsync'd source tree:

- `maestro-data` → `/data`: `runs/` (via `WORKING_DIRECTORY=/data`) + `auth.db` (accounts, credit
  ledger) + `platform.db` (games, builds, jobs, events) (via `MAESTRO_DATA_DIR=/data`, set in
  docker-compose.yml).
- `maestro-games` → `/app/runtime/games`: staged playable bundles served at `/play/games`.
- `maestro-demos` → `/app/runtime/demos`: the landing page's demo snapshots, served at
  `/play/demos`. A snapshot is a copy taken once; a rebuild, fix or change of the same run
  restages `runtime/games` and leaves the demo as it was.
- `maestro-shares` → `/app/runtime/shares`: games their owners shared, served at `/play/shares`
  and linked as `/g/<share_id>`. Frozen the same way; the owner refreshes or removes them.

All survive image rebuilds and `deploy.sh` runs. Never point `WORKING_DIRECTORY` or
`MAESTRO_DATA_DIR` off `/data`, and never `docker volume rm` any of them — that wipes accounts and
games.

Surviving the BOX is `docs/backups.md`: a control-plane thread snapshots both DBs to the bucket
(every 15 minutes, sooner on account/credit writes), every settled run uploads its own archive
with a nightly `--archive-all` sweep behind it, and the restore drill there is the proof either
one works.

## Node in the image

Games are plain HTML/CSS/JS: staging copies the folder straight to `/play`. Node is in the image for
one subprocess — `runtime/decimate.mjs`, which cuts a finished TRELLIS GLB down to game weight. It
needs `runtime/node_modules` (gltf-transform + meshoptimizer), npm-ci'd in a linux build stage so the
platform-specific binaries resolve.

---

## Brand-new box (provision → configure → deploy)

On the **prod box**:

```bash
./scripts/provision.sh        # one-time host setup: Docker + the compose plugin
```

Two files must exist on the prod box before the first deploy — `deploy.sh` never ships either. In
the app dir (default `/opt/maestro`):

```bash
cp .env.example .env                  # then edit
touch src/config/settings.json        # the compose bind-mount target; fill in runpod.queues
```

Then from the **dev box**:

```bash
PROD_HOST=user@prod-box ./scripts/deploy.sh
```

`deploy.sh` rsyncs only what the box needs to build + run (source, runtime, frontend,
compose/Docker files, scripts — repo paperwork like docs/tasks/tests and the pod-side worker
Dockerfiles stay home), runs `docker compose build && docker compose up -d`, and curls prod
`/healthz` — failing loudly if any step errors. `rsync --delete` does not remove excluded paths, so
leftovers from older deploys need a one-time manual sweep.

**Redeploy:** the same `deploy.sh` command.

## Editing `.env` after the first boot

A plain `docker compose up -d` does **not** re-read a changed `.env` for an already-running container
— it keeps the env baked in at its last (re)creation, so your edit silently has no effect. Force it:

```bash
docker compose up -d --force-recreate
docker compose exec app printenv <VAR>     # confirm the value landed
```

(This bites `CP_URL` and `WORKQUEUE_TOKEN` in particular — a stale value leaves workers unable to
claim while `.env` on disk looks correct.)

## Accounts (open signup, or manual)

Self-serve signup is open at `/login` ("Create an account"), throttled per client IP. Manual
provisioning still works; the CLI lives at
`/app/src/auth/cli.py`; the container WORKDIR is `/app`, so run it from `/app/src`:

```bash
docker compose exec -w /app/src app python -m auth.cli create <handle> <email> [--role admin]   # prompts for a password
docker compose exec -w /app/src app python -m auth.cli grant  <handle> <n>
```

Demos are snapshots (`maestro/demos.py`): copy a staged game into the demos volume, then list its
id under `demo_games` in settings.json. `--replace` is the only way an existing snapshot changes.

```bash
docker compose exec -w /app/src app python -m maestro.demos snapshot <run_id> [--replace]
docker compose exec -w /app/src app python -m maestro.demos list
```

The other subcommands: `passwd <handle>` (reset a password), `email <handle> <email>` (change the
recovery address), `refund <handle> <n>` (a manual refund), `list`. Accounts start at 0 credits
either way.

## Smoke test (do this before handing out the URL)

```bash
BASE=https://<prod-host>
curl -s -o /dev/null -w "%{http_code}\n" $BASE/healthz          # 200
curl -s -o /dev/null -w "%{http_code}\n" $BASE/                 # 200 (SPA)
curl -s -o /dev/null -w "%{http_code}\n" $BASE/api/games/       # 401 (gated)
```

Then in a browser: log in as a test account → request a game → confirm it builds → press Play on
the game page (the iframe rides the /handoff flow). The build needs an `llm` worker running or its
first turn times out.

## Public domains: gamesummoner.com + gamesummonerusercontent.com

Two REGISTRABLE domains, deliberately: the app lives on `gamesummoner.com`, games (model-authored
JS) are served from `gamesummonerusercontent.com`. Separate registrable domains — not a subdomain —
because cookies are domain-scoped, and a subdomain of the app's domain could toss a `Domain=`
cookie onto the app (session fixation). One process serves both hostnames; the host-split
middleware (`api/app.py` `_host_split`) makes each host serve ONLY its own surface: the game host
answers `/play` + `/handoff` + `/healthz` and 404s the rest (a game's `fetch('/api/…')` resolves
to a host with no API on it — that IS the isolation), and the app host 404s `/play`.

Serving them means direct inbound 443 — Tailscale Funnel serves only `ts.net` names. Box side is
DONE (2026-08-01): ufw active (OpenSSH + 80 + 443 + 41641/udp), caddy 2.6 from Ubuntu universe installed and running,
and the app container binds `127.0.0.1:8000` (compose) — a `0.0.0.0` bind would serve the app
to the internet on :8000, because Docker's iptables chain bypasses ufw entirely. Funnel and
caddy both proxy via loopback, so nothing else changed.

`/etc/caddy/Caddyfile` as deployed — `default_bind` pins caddy to the PUBLIC IP because tailscaled
(Funnel) already owns 443 on the tailnet IP and a wildcard `:443` bind collides with it:

```
{
    default_bind 137.184.59.143
}
gamesummoner.com {
    reverse_proxy 127.0.0.1:8000
}
gamesummonerusercontent.com {
    reverse_proxy 127.0.0.1:8000
}
```

DNS (the one registrar-side step): A records for both apexes → `137.184.59.143`. Caddy is already
running and retries ACME on its own, so certs appear without a touch once DNS propagates. Then
point the app at the split in the bind-mounted `settings.json` — `play.origin` =
`https://gamesummonerusercontent.com`, `play.app_origin` = `https://gamesummoner.com`
(`docs/local_dev.md` "Settings") — and `docker compose restart` (settings load at process start).

Leave both empty (the default) and everything rides one origin — dev and the funnel-only alpha
deploy keep working unchanged. Split smoke test:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://gamesummoner.com/healthz               # 200
curl -s -o /dev/null -w "%{http_code}\n" https://gamesummoner.com/play/games/x/index.html   # 404 (no games on the app host)
curl -s -o /dev/null -w "%{http_code}\n" https://gamesummonerusercontent.com/api/games  # 404 (no API on the game host)
curl -s -o /dev/null -w "%{http_code}\n" https://gamesummonerusercontent.com/handoff    # 403 (alive, refuses a bare visit)
```

---

# Attaching capacity

One worker process per queue, run wherever the GPU is. It dials out, so it needs no inbound port:

```bash
python -m worker.agent --server http://<control-plane>:8000 --token <WORKQUEUE_TOKEN> \
    --queue llm   --target http://localhost:8090     # Basalt on the local 5090; :8001 on an RTX PRO 6000 pod
python -m worker.agent ... --queue image --target http://localhost:8188   # ComfyUI
python -m worker.agent ... --queue mesh  --target http://localhost:8189   # TRELLIS
```

`--target` is the worker's own inference server and should stay bound to `127.0.0.1` — a reachable
one is an unauthenticated GPU. `--worker-id` must name a row the scaler created for the pod: a
worker row carries the pod's price, and a claim from an id nobody created is refused.

A queue with no worker means every job on it times out.

## RunPod worker images

The images live in [gamesummoner-images](../../gamesummoner-images) (`runpod/README.md` there:
build and ship, the volumes and what they hold, provisioning, boot order). The deployed tag is
whatever each RunPod template names; the templates are the only record of it. Bump the tag on
every push, because RunPod caches images per host.

## Autoscaler (queue-driven pods)

With a provider enabled (`runpod.enabled` + `runpod.api_key`, `aws.enabled` + `aws.access_key`,
or both) and the workqueue on, the control plane runs a scaling
loop (`src/scaler/`): per tick it reaps dead pods and adds at most one pod per queue when the queue
is backed up. Workers own scale-DOWN: `IDLE_EXIT_SECONDS` (delivered at pod create) is the
worker's idle window — once every slot has been empty that long since its last job ended, the
worker deregisters and exits 0. The claim long-poll is capped server-side at 25 s, so the window
is the worker's own clock across polls (until 2026-09-04 a single null claim was taken as the
verdict, and every queue idled out at 25 s whatever the setting).

Settings block (`settings.json` → `runpod`, the full key list in `docs/local_dev.md` "Settings"):

- `cp_url` — the control-plane URL pods dial back to; must be reachable from RunPod
  (`https://gamesummoner.com`, not localhost).
- `tick_seconds` (15) — scaling-loop cadence. `stale_worker_seconds` (180) — a worker row silent
  this long is dead.
  A create RunPod refuses on every volume/card combination is recorded durably (`pod_refusals`,
  one row per refused scale-up, kept 30 days) with the combos tried and the provider's text;
- `queues.<name>` — per-queue policy: `template_id`, `gpu_type_ids` (a PRIORITY-ORDERED list: the
  scaler creates with the first entry alone, and retries with the whole list only if RunPod refuses
  — asking for all of them at once gets whichever card RunPod prefers to hand out, and the cards are
  not substitutes), `network_volume_ids` (the queue's own volumes, one per datacenter its weights
  are copied to, tried in order; absent, the queue rides `runpod.network_volume_id`),
  `allowed_cuda_versions` (the host-driver floor the queue's engine needs),
  `max_workers`, `cooldown_seconds`,
  `boot_seconds` (what a boot costs this queue — a flat estimate, never measured),
  `min_jobs_per_pod` (jobs a new pod must be owed when it lands, or it is not worth its boot),
  `idle_exit_seconds`
  (linger tuning: raise for chatty queues, 0 = never exit), `boot_deadline_seconds` (a pod this
  old that no worker has registered from is reaped as wedged). The `queues` dict in `settings.json` replaces the
  default wholesale — carry complete blocks.
- `aws` — the EC2 provider's own block (key list in `docs/local_dev.md`): credentials, the
  security group and instance profile, and per queue the `instance_types`, `markets` and `amis`
  by region. Policy stays in `runpod.queues`; a queue with a block in both is served by both.
  The IAM user needs `ec2:RunInstances`, `CreateTags`, `TerminateInstances`,
  `CancelSpotInstanceRequests`, `DescribeInstances`, `DescribeSpotPriceHistory`,
  `DescribeInstanceTypeOfferings`, `DescribeSpotInstanceRequests`, `pricing:GetProducts`, and
  `iam:PassRole` on `gs-gpu-worker`.
- A scale-up walks ONE ladder. Each provider lists its rungs priced and in its own order of
  preference (RunPod: per volume, the head card alone then the list, at RunPod's list price;
  EC2: every zone that offers the type, spot and on-demand, cheapest first), the ladders merge
  by always taking the cheapest head, and the first rung that takes the launch wins. No provider
  knows its stock, only its prices, so the cheapest available machine is found by asking. A
  provider whose prices cannot be read keeps its rungs, last. Every rung refused is one
  `pod_refusals` row, `stock` only if every refusal was.
- An EC2 box ends itself (the worker's idle exit powers it off, and it was launched to terminate
  on shutdown). When the scaler reaps one instead, it cancels the instance's spot request before
  terminating it, because a request left live holds the spot vCPU quota.
- A provider that cannot list its machines skips the whole tick: a fleet counted short buys
  machines it already has.
- A machine's age is its worker row's `started_at`, so it survives a control-plane restart. A
  listed machine under the prefix with no row is aged from the tick that first saw it, and a
  restart only delays reaping it by one `boot_deadline_seconds`.
- Scale-from-zero fires on ANY pending job with no cooldown. Past zero, a pod is added only when
  it is owed `min_jobs_per_pod` when it lands: the workers on hand (live and booting) eat
  `boot_seconds` ÷ seconds-per-job each while it boots, and what is left splits across the fleet
  it joins. Seconds per job is the last week's, from `jobs`, with the billing estimate standing
  in until the week has data; `boot_seconds` is configured and never measured, because a worker
  that registers under an id no create made a row for reads as an instant boot and dragged the
  average to a fraction of the truth. A booting pod counts as capacity, so a 5-minute boot can't
  trigger add-forever. Age triggers nothing — an old pending job means the fleet is still
  chewing, and the pod bought for it lands after that job is gone.

Create-time `env` OVERRIDES the template's (verified 2026-09-04: every fleet pod dialed the
`cp_url` in settings while the templates still named the retired funnel host). The template env
is what a pod started by hand gets, so keep it current anyway.

First full cycle to watch (mesh, `max_workers: 1`): enqueue a mesh job → pod appears in the RunPod
console with its worker row already `booting` → the worker registers under the worker id it was started with → job done → queue drains → worker exits + deregisters → pod
disappears (self-terminate or reaper within `stale_worker_seconds`).

A run renting cards nobody wants is ended at its source, not in the console: a pending job is what
the scaler is answering, so terminating pods only buys the next tick. `POST /api/admin/games/<run
id>/stop` (admin token) ends any run whoever owns it — every job it holds on every queue fails and
the fleet drains itself. The owner's own `POST /api/games/<run id>/stop` does the same over their
own runs.

---

## Known deferred risks (accepted for private alpha — trusted testers)

Flagged in the pre-open security audit; hardened 2026-07-23.

- **Untrusted generated JS in the browser** — contained everywhere, ISOLATED once `play.origin`
  is set. Every `/play` response carries a CSP (`api/app.py` `_PLAY_CSP_BASE`) pinning scripted
  loads + network to its own origin, framed only by the app origin: generated code can't pull
  external scripts, and fetch/XHR/WS can't leave. Exfiltration is NOT fully closed — no CSP
  directive governs top-level navigation, so a `location =` to an external URL still leaves. The
  play surface holds no readable credential in ANY mode (the grant cookie is HttpOnly and
  path-scoped to one game — see `auth/playgrants.py`), but with `play.origin` unset the game still
  shares the app origin's localStorage, where the session bearer token lives; the ownership check
  at play-session mint is what keeps that token the game's own. Before any game is viewable by a
  NON-owner, deploy the split domains (above) — on the split, game code runs on a host that serves
  no API and holds no app storage.
- **Login throttle is the only rate limit.** `src/auth/ratelimit.py` caps online password guessing
  per handle (429 + `Retry-After`). Nothing else is capped: there is no conversational surface,
  builds charge credits before enqueue, every GPU job admits against the game's compute budget, the
  autoscaler absorbs depth, and accounts start at 0 credits — so build load is paid load, not an
  attack.
- **API surface closed.** `/docs`/`/redoc`/`/openapi.json` exist only under `MAESTRO_DEV=1`;
  session TTL is 7 days.
