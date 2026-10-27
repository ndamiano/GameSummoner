#!/usr/bin/env python3
"""Drain a pool on a one-card box: start the model, start the worker, wait, stop both.
`auto` holds whichever pool has work, swapping the card as the pools need it."""

import argparse
import datetime
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

LOGS = Path(os.environ.get("MAESTRO_LOCAL_LOGS", "/tmp/maestro-local"))

BASALT_BIN = Path(os.environ.get("BASALT_DIR", str(Path.home() / "Documents/basalt"))) / "bin/basalt"
BASALT_CONFIG = Path(os.environ.get("BASALT_CONFIG", "/var/lib/models/basalt/basalt-iq3s.json"))
COMFY_PYTHON = Path(os.environ.get("COMFY_PYTHON",
                                   str(Path.home() / "Documents/Comfy/comfy-env/bin/python")))
COMFY_DIR = Path(os.environ.get("COMFY_DIR", str(Path.home() / "comfy/mess-with-comfy")))
SAFETY_MODEL_DIR = os.environ.get("SAFETY_MODEL_DIR", str(Path.home() / "comfy-models/safety"))
IMAGES_DIR = Path(os.environ.get("IMAGES_REPO_DIR", str(Path.home() / "Documents/gamesummoner-images")))
WORKER_DIR = Path(os.environ.get("WORKER_REPO_DIR", str(Path.home() / "Documents/gamesummoner-workers")))
IMAGE_PORT = 8700
GPU_PYTHON = Path(os.environ.get("GAMESUMMONER_GPU_PYTHON",
                                 str(Path.home() / ".gamesummoner/venv-gpu/bin/python")))


def _worker_python(queue: str) -> str:
    if _llama_swap():
        return str(GPU_PYTHON)
    if queue == "comfy" and COMFY_PYTHON.exists():
        return str(COMFY_PYTHON)
    return sys.executable

IDLE_TICKS = 3          # empty polls before a queue counts as drained; a continuation
                        # is enqueued by the completion that just finished, so "empty
                        # once" is not empty
POLL_SECONDS = 2.0

PRIORITY = {"decide": 0, "llm": 1, "comfy": 2}
# A pending job the control plane's reaper will fail before the held queue drains has to be served
# first: one card, a build whose turns re-fill the llm queue as fast as it empties, and five
# sheets waited 30 minutes behind it for a worker that never came (2026-09-04, two of five lost).
STARVE_SECONDS = 600
# How long a worker may keep the card to finish the job in flight when the card is handed over.
# Longer than a build turn: the alternative is throwing that turn away.
HANDOFF_SECONDS = 300


def _settings() -> dict:
    return json.loads((ROOT / "src" / "config" / "settings.json").read_text())


def _pools() -> list:
    llm = [("llm", name) for name, entry in _settings()["llm"].items() if "local_gpu" in entry]
    return [("decide", None), *llm, ("comfy", None)]


def _label(pool: tuple) -> str:
    queue, model = pool
    return f"{queue}-{model}" if model else queue


def _entry(model: str) -> dict:
    return _settings()["llm"][model]


def _basalt_config() -> Path:
    if BASALT_CONFIG.exists():
        return BASALT_CONFIG
    raise SystemExit(f"no basalt config at {BASALT_CONFIG}")


def _llama_swap() -> dict:
    return ((_settings().get("local") or {}).get("llama_swap")) or {}


def _swap_leg(queue: str, swap: dict) -> dict:
    url = swap["url"].rstrip("/")
    engines = {"llm": url, "decide": url, "comfy": f"{url}/upstream/{swap['comfy']}"}
    return {"port": None, "ready": f"{url}/v1/models", "argv": None, "cwd": None, "env": {},
            "engine": engines[queue],
            "image_args": ["--model", swap["decide"]] if queue == "decide" else []}


def _leg(pool: tuple) -> dict:
    queue, model = pool
    swap = _llama_swap()
    if swap:
        return _swap_leg(queue, swap)
    if queue == "decide":
        return {
            "port": IMAGE_PORT,
            "ready": f"http://127.0.0.1:{IMAGE_PORT}/health",
            "argv": [str(IMAGES_DIR / "local" / "decide" / "run.sh"), str(_worker_env(pool))],
            "cwd": None,
            "env": {},
            "boxed": True,
        }
    if queue == "llm":
        entry = _entry(model)
        return {
            "port": 8090,
            "ready": "http://127.0.0.1:8090/v1/models",
            "argv": [str(BASALT_BIN), "serve", "--config", str(_basalt_config()),
                     "--alias", entry["model"], "--host", "127.0.0.1", "--port", "8090",
                     "--vision", "dynamic", "--fit-max-tokens",
                     *shlex.split(entry["local_gpu"].get("engine_args") or "")],
            "cwd": None,
            "env": {},
        }
    if queue == "comfy":
        return {
            "port": 8188,
            "ready": "http://127.0.0.1:8188/system_stats",
            "argv": [str(COMFY_PYTHON), "main.py", "--port", "8188",
                     "--use-pytorch-cross-attention"],
            "cwd": str(COMFY_DIR),
            "env": {},
        }
    raise SystemExit(f"unknown queue {queue!r}")


def _up(url: str, timeout: float = 2.0) -> bool:
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            return True
    except (urllib.error.URLError, OSError):
        return False


def _ready(pool: tuple, url: str, timeout: float = 2.0) -> bool:
    queue, model = pool
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except (urllib.error.URLError, OSError):
        return False
    if queue != "llm":
        return True
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    ids = {m.get("id") for m in data.get("data", [])}
    return _entry(model)["model"] in ids


def _spawn(name: str, argv: list, cwd, env: dict) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = (LOGS / f"{name}.log").open("ab")
    print(f"[{name}] starting — log {LOGS / f'{name}.log'}", flush=True)
    return subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                            env={**os.environ, **env}, start_new_session=True)


def _spawn_image(pool: tuple, leg: dict) -> subprocess.Popen:
    queue = pool[0]
    engine = leg.get("engine") or f"http://127.0.0.1:{leg['port']}"
    argv = [_worker_python(queue), "-m", f"local.{queue}.server", "--image-id", "local",
            "--engine-url", engine, "--port", str(IMAGE_PORT), *leg.get("image_args", [])]
    return _spawn(f"{_label(pool)}-image", argv, str(IMAGES_DIR),
                  {"PYTHONPATH": str(IMAGES_DIR), "SAFETY_MODEL_DIR": SAFETY_MODEL_DIR})


def _worker_env(pool: tuple) -> Path:
    from db import workers
    queue, model = pool
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    workers.worker_created(worker_id, None, queue, None, 0.0, model=model)
    settings = _settings()
    LOGS.mkdir(parents=True, exist_ok=True)
    env_file = LOGS / f"{_label(pool)}-env.json"
    env_file.write_text(json.dumps({
        "CP_URL": "http://localhost:8000",
        "WORKER_TOKEN": (settings.get("workqueue") or {}).get("token") or "",
        "WORKER_QUEUE": queue,
        "WORKER_ID": worker_id,
        "WORKER_SLOTS": str(settings["llm"][model].get("slots", 1) if model else 1),
        "IDLE_EXIT_SECONDS": "31536000",
        "JOB_TIMEOUT_SECONDS": str((settings.get("workqueue") or {}).get("job_timeout_seconds", 1800)),
    }))
    return env_file


def _spawn_worker(pool: tuple) -> subprocess.Popen:
    env_file = _worker_env(pool)
    return _spawn(f"{_label(pool)}-worker",
                  [_worker_python("llm"), "-m", "gamesummoner_worker", "--env-file", str(env_file),
                   "--image", f"http://127.0.0.1:{IMAGE_PORT}"],
                  None, {"PYTHONPATH": str(WORKER_DIR / "src")})


def _stop(name: str, process: subprocess.Popen, grace: float = 25) -> None:
    if process.poll() is not None:
        return
    print(f"[{name}] stopping", flush=True)
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def hand_over(held: Optional[tuple], server, worker, image=None) -> None:
    """Give the card up: the worker first, with time to finish its job, then the server."""
    name = _label(held) if held else None
    if worker is not None:
        _stop(f"{name}-worker", worker, grace=HANDOFF_SECONDS)
    if image is not None:
        _stop(f"{name}-image", image)
    if server is not None:
        _stop(f"{name}-server", server)


def _await_ready(name: str, pool: tuple, url: str, process: subprocess.Popen, timeout: float) -> None:
    started = time.time()
    while time.time() - started < timeout:
        if process.poll() is not None:
            raise SystemExit(f"[{name}] died during startup — see {LOGS / f'{name}.log'}")
        if _ready(pool, url):
            print(f"[{name}] ready in {time.time() - started:.0f}s", flush=True)
            return
        time.sleep(1.0)
    raise SystemExit(f"[{name}] not ready after {timeout:.0f}s")


def _waiting(pool: tuple) -> int:
    from db import connection
    with connection.platform_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE queue = ? AND model IS ? "
            "AND status IN ('pending','claimed')",
            pool,
        ).fetchone()
    return int(row["n"])


def _oldest_wait(pool: tuple) -> float:
    from db import connection
    with connection.platform_db() as conn:
        row = conn.execute(
            "SELECT MIN(created_at) AS t FROM jobs WHERE queue = ? AND model IS ? "
            "AND status = 'pending'",
            pool,
        ).fetchone()
    return time.time() - row["t"] if row["t"] else 0.0


def drain(pool: tuple, *, keep: bool, ready_timeout: float) -> None:
    name = _label(pool)
    leg = _leg(pool)
    if leg["argv"] is None:
        raise SystemExit("llama-swap serves the models: run `auto`")
    if _up(leg["ready"]):
        raise SystemExit(
            f"[{name}] something is already serving port {leg['port']}. Stop it first — this "
            f"script only stops what it started."
        )

    waiting = _waiting(pool)
    print(f"[{name}] {waiting} job(s) waiting", flush=True)

    server = _spawn(f"{name}-server", leg["argv"], leg["cwd"], leg["env"])
    worker = image = None
    try:
        _await_ready(f"{name}-server", pool, leg["ready"], server, ready_timeout)
        if not leg.get("boxed"):
            image = _spawn_image(pool, leg)
            worker = _spawn_worker(pool)

        idle = 0
        while idle < IDLE_TICKS:
            time.sleep(POLL_SECONDS)
            if server.poll() is not None:
                raise SystemExit(f"[{name}] server exited — see {LOGS / f'{name}-server.log'}")
            if worker is not None and worker.poll() is not None:
                raise SystemExit(f"[{name}] worker exited — see {LOGS / f'{name}-worker.log'}")
            if image is not None and image.poll() is not None:
                raise SystemExit(f"[{name}] contract server exited — see {LOGS / f'{name}-image.log'}")
            left = _waiting(pool)
            if left:
                idle = 0
                if left != waiting:
                    print(f"[{name}] {left} left", flush=True)
                    waiting = left
            else:
                idle += 1
        print(f"[{name}] drained", flush=True)
    finally:
        if worker is not None:
            _stop(f"{name}-worker", worker)
        if image is not None:
            _stop(f"{name}-image", image)
        if keep:
            print(f"[{name}] leaving the model up (--keep)", flush=True)
        else:
            _stop(f"{name}-server", server)


# ---------------------------------------------------------------------------
# auto mode
# ---------------------------------------------------------------------------

def choose_action(held: Optional[str], pending: dict, idle_ticks: dict,
                   idle_tick_limit: int, waits: Optional[dict] = None) -> Optional[str]:
    starving = {q: w for q, w in (waits or {}).items() if w >= STARVE_SECONDS and q != held}
    if starving:
        return max(starving, key=starving.get)
    if held is not None:
        if pending.get(held, 0) > 0 or idle_ticks.get(held, 0) < idle_tick_limit:
            return held
    candidates = [p for p in pending if pending[p] > 0]
    if not candidates:
        return None
    candidates.sort(key=lambda p: PRIORITY[p[0]])
    return candidates[0]


def _log_auto(msg: str, logfile) -> None:
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    logfile.write(line + "\n")
    logfile.flush()


def auto(idle_exit: Optional[float], ready_timeout: float) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    auto_log = (LOGS / "auto.log").open("a")
    held: Optional[tuple] = None
    server: Optional[subprocess.Popen] = None
    worker: Optional[subprocess.Popen] = None
    image: Optional[subprocess.Popen] = None
    pools = _pools()
    idle_ticks = {p: 0 for p in pools}
    all_idle_since: Optional[float] = None

    stopping = {"flag": False}

    def _signal(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _signal)
    signal.signal(signal.SIGTERM, _signal)

    def _stop_current():
        nonlocal server, worker, image, held
        hand_over(held, server, worker, image)
        worker = server = image = None
        held = None

    def _start(pool: tuple):
        nonlocal server, worker, image, held
        name = _label(pool)
        leg = _leg(pool)
        if _up(leg["ready"]):
            # Someone else's server on the port is the server to use, not a reason to stop: auto
            # would otherwise abandon a queue it cannot start and exit, and the card it was holding
            # goes with it. It stays THEIRS — nothing here stops what it did not start.
            print(f"[{name}] using the server already at {leg['ready']}", flush=True)
            server = None
        else:
            server = _spawn(f"{name}-server", leg["argv"], leg["cwd"], leg["env"])
            _await_ready(f"{name}-server", pool, leg["ready"], server, ready_timeout)
        if not leg.get("boxed"):
            image = _spawn_image(pool, leg)
            worker = _spawn_worker(pool)
        held = pool

    try:
        while not stopping["flag"]:
            pending = {p: _waiting(p) for p in pools}
            for p in pools:
                if pending[p] > 0:
                    idle_ticks[p] = 0
                else:
                    idle_ticks[p] += 1

            if any(pending.values()):
                all_idle_since = None
            elif all_idle_since is None:
                all_idle_since = time.time()

            if idle_exit is not None and all_idle_since is not None \
                    and time.time() - all_idle_since >= idle_exit:
                _log_auto(f"idle {idle_exit:.0f}s, nothing pending {pending} — exiting", auto_log)
                break

            waits = {p: _oldest_wait(p) for p in pools}
            action = choose_action(held, pending, idle_ticks, IDLE_TICKS, waits)

            if action != held:
                _log_auto(f"switch {held!r} -> {action!r} pending={pending}", auto_log)
                _stop_current()
                if action is not None:
                    t0 = time.time()
                    _start(action)
                    _log_auto(f"[{_label(action)}] ready in {time.time() - t0:.0f}s", auto_log)

            name = _label(held) if held else None
            if server is not None and server.poll() is not None:
                raise SystemExit(f"[{name}] server exited — see {LOGS / f'{name}-server.log'}")
            if worker is not None and worker.poll() is not None:
                raise SystemExit(f"[{name}] worker exited — see {LOGS / f'{name}-worker.log'}")
            if image is not None and image.poll() is not None:
                raise SystemExit(f"[{name}] contract server exited — see {LOGS / f'{name}-image.log'}")

            time.sleep(POLL_SECONDS)
    finally:
        _log_auto(f"stopping, held={held!r}", auto_log)
        _stop_current()
        auto_log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("queues", nargs="+",
                        help="decide, llm (every local_gpu entry), llm-<entry>, comfy, all, or auto")
    parser.add_argument("--keep", action="store_true",
                        help="leave the model server up after the queue empties")
    parser.add_argument("--ready-timeout", type=float, default=300.0)
    parser.add_argument("--idle-exit", type=float, default=None,
                        help="auto mode only: stop and exit after this many seconds with "
                             "nothing pending anywhere (default: never)")
    args = parser.parse_args()

    if args.queues == ["auto"]:
        if not _up("http://localhost:8000/docs") and not _up("http://localhost:8000/"):
            print("warning: nothing answering on :8000 — start `python run.py` first", flush=True)
        auto(args.idle_exit, args.ready_timeout)
        return 0

    pools = _pools() if args.queues == ["all"] else [
        p for arg in args.queues for p in _pools() if arg in (p[0], _label(p))]
    if not _up("http://localhost:8000/docs") and not _up("http://localhost:8000/"):
        print("warning: nothing answering on :8000 — start `python run.py` first, or the "
              "worker has no queue to pull from", flush=True)
    for pool in pools:
        drain(pool, keep=args.keep and pool == pools[-1], ready_timeout=args.ready_timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
