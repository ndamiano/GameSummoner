# GameSummoner

Type a game prompt, click go, a playable browser game comes out.

How we work here is defined in `CLAUDE.md` and `docs/design-philosophy.md`, which should help get a
handle on why we made the choices we've made.

## Getting Started

**You need:** Python 3.12, Node 20, git, and a GPU box that can serve a model (see "A GPU").
Without a GPU you can still run the control plane, the frontend and the tests.

```bash
# Backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m playwright install chromium            # the error gate opens games headless
cp src/config/settings.example.json src/config/settings.json
cp .env.example .env
git config core.hooksPath scripts/githooks        # once per clone; comment audit + scoped CI
python run.py                                     # http://localhost:8000

# Frontend
cd frontend && npm install && npm run dev         # http://localhost:5173

# Tests
cd src && python -m pytest ../tests/ -q -n auto
cd frontend && npm test
scripts/ci.sh                                     # everything CI runs, before a push
```

Most things are set up in `src/config/settings.json`, which is git ignored. The required settings for
getting this running locally can be found in `docs/local_dev.md`.

```bash
cd src
python -m auth.cli create <handle> <email> [--role admin]   # prompts for a password
python -m auth.cli grant  <handle> <n>                       # accounts start at 0 credits
```

## A GPU

GameSummoner's logic for creating a game never actually uses a GPU. Instead, it enqueues requests as
they are needed, and processes as workers drain the queues. In order for this to work locally, we
have `scripts/local_gpu.py auto` which automatically handles spinning up workers to drain the
queues, one at a time, as they populate on the local server.

```bash
python scripts/local_gpu.py auto
```

The workers and model servers it starts are not in this repository. They live in two sibling
repos, [`gamesummoner-workers`](https://github.com/ndamiano/gamesummoner-workers) (the pull-side worker agent) and [`gamesummoner-images`](https://github.com/ndamiano/gamesummoner-images) (the image,
mesh and sprite engines), which `local_gpu.py` finds via `WORKER_REPO_DIR` and `IMAGES_REPO_DIR`.
The llm leg serves through [Basalt](https://github.com/jesdga95/basalt) (`BASALT_DIR`). Without
them the control plane, frontend and tests all run, but no build gets past its first queued job.

## First build

Press **Make a new game** in the UI, or from the CLI:

```bash
cd src && python -m maestro.codegen.run "<request>"
```

A built game lives at `runtime/games/<run_id>/`. A 3D one needs http, not `file://`:

```bash
cd runtime/games/<run_id> && python -m http.server
```

To test the change flow:

```bash
python -m maestro.codegen.run --change <run_id> "<note>"
```

The rest of the CLI (`--new`, `--assets`, `--history`, `--restore`, `--evict`) is in
`docs/local_dev.md`.

## Where to read

| doc | for |
|---|---|
| `CLAUDE.md` | how we work |
| `docs/*` | documentation on everything about GameSummoner |
| `labs/` | past experiments in detail; its own repo, pulled separately |

## Repository layout

```
run.py          the control plane entrypoint
src/
  api/          FastAPI routers + WebSocket event bus
  auth/         identity, sessions, play grants, admin CLI
  billing/      credit ledger, packages, Stripe provider, job cost estimates
  config/       settings manager + example
  db/           games/builds/events/jobs/workers + the compute budget
  llm_clients/  connector, message builder, wire translation
  maestro/
    codegen/    the build path: build_chain (driver), build_steps (turn machine), tools,
                staging, assets, error_gate, snapshots, prompts/, run (CLI)
    worldgen/   the 3D world behind compose_world
  scaler/       the autoscaler: rents RunPod and EC2 GPU pods per queue
  tools/        ComfyUI, quilting, S3, DB backup, mailer, safety
  workqueue/    the enqueue-side client + the queue reaper
runtime/
  vendor/       three.js + loaders + world.js + lib/, copied into every game folder at seed
  games/        staged games, served at /play
  demos/        demo snapshots, served at /play/demos
  shares/       shared-by-link games
frontend/       React + Vite UI
tests/          pytest suite
scripts/        local_gpu.py, ci.sh, githooks/, db.py, deploy + provision
docs/           everything else
```

## License

MIT, see `LICENSE`. Vendored code under `runtime/vendor/` keeps its own license headers.
