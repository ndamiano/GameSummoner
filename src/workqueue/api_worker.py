"""A worker for one `api` llm entry: its jobs go to the provider's OpenAI-compatible API."""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger("api_worker")

QUEUE = "llm"
CLAIM_WAIT_SECONDS = 2.0
HEARTBEAT_SECONDS = 45.0
RESTART_SECONDS = 10.0
STOP_GRACE_SECONDS = 30.0
RETRY_BACKOFF_SECONDS = (2.0, 5.0)
TRANSIENT = (requests.ConnectionError, requests.exceptions.ChunkedEncodingError)


def engine_body(body: dict, api: dict) -> dict:
    out = {k: v for k, v in body.items() if k != "reasoning"}
    effort = body.get("reasoning")
    if api.get("effort"):
        effort = api["effort"].get(effort)
    if effort:
        out["reasoning_effort"] = effort
    return out


def with_cached_tokens(result: dict) -> dict:
    usage = result.get("usage")
    if not usage:
        return result
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
              or usage.get("prompt_cache_hit_tokens") or 0)
    return {**result, "usage": {**usage, "cached_tokens": cached}}


def call(api: dict, body: dict, timeout: float) -> tuple[Optional[dict], Optional[str]]:
    headers = {"Authorization": f"Bearer {api['api_key']}"} if api.get("api_key") else {}
    for backoff in (*RETRY_BACKOFF_SECONDS, None):
        try:
            r = requests.post(f"{api['base_url'].rstrip('/')}/chat/completions", timeout=timeout,
                              json=engine_body(body, api), headers=headers)
        except requests.RequestException as e:
            error = f"api unreachable: {e.__class__.__name__}: {e}"
            if backoff is None or not isinstance(e, TRANSIENT):
                return None, error
        else:
            if r.status_code < 500:
                break
            error = f"Status {r.status_code}: {r.text[:2000]}"
            if backoff is None:
                return None, error
        logger.warning("%s, retrying in %.0fs", error, backoff)
        time.sleep(backoff)
    if not 200 <= r.status_code < 300:
        return None, f"Status {r.status_code}: {r.text[:2000]}"
    try:
        result = r.json()
    except ValueError:
        return None, f"api answered {r.status_code} with no JSON: {r.text[:500]}"
    return with_cached_tokens(result), None


class Worker:
    def __init__(self, cp_url: str, token: str, entry: str, api: dict, slots: int,
                 job_timeout: float):
        self.cp_url = cp_url.rstrip("/")
        self.entry = entry
        self.api = api
        self.slots = slots
        self.job_timeout = job_timeout
        self.worker_id = f"api-{entry}-{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.stopping = threading.Event()
        self._running: set[str] = set()
        self._lock = threading.Condition()

    def _post(self, path: str, body: dict, timeout: float) -> dict:
        r = self.session.post(f"{self.cp_url}{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def execute(self, job: dict) -> None:
        t0 = time.perf_counter()
        try:
            result, error = call(self.api, job["payload"].get("body") or {}, self.job_timeout)
            body = {"worker_id": self.worker_id, "job_id": job["id"], "result": result,
                    "error": error, "exec_seconds": time.perf_counter() - t0}
            for attempt in (1, 2):
                try:
                    if not self._post("/worker/complete", body, timeout=120).get("ok"):
                        logger.warning("job %s: lease lapsed, result dropped", job["id"])
                    break
                except requests.RequestException as e:
                    logger.warning("job %s: complete failed (%d/2): %s", job["id"], attempt, e)
                    time.sleep(2)
            else:
                logger.error("job %s: result dropped", job["id"])
            if error:
                logger.warning("job %s failed after %.1fs: %s", job["id"], body["exec_seconds"], error)
        finally:
            with self._lock:
                self._running.discard(job["id"])
                self._lock.notify_all()

    def _heartbeat_loop(self) -> None:
        while not self.stopping.wait(HEARTBEAT_SECONDS):
            with self._lock:
                running = list(self._running)
            for job_id in running:
                try:
                    self._post("/worker/heartbeat", {"job_id": job_id, "worker_id": self.worker_id},
                               timeout=10)
                except requests.RequestException as e:
                    logger.warning("job %s: heartbeat failed: %s", job_id, e)

    def run(self) -> None:
        from db import workers
        workers.worker_created(self.worker_id, None, QUEUE, None, 0.0, source="api",
                               model=self.entry)
        logger.info("worker %s serving %s from %s (%d slots)", self.worker_id, self.entry,
                    self.api["base_url"], self.slots)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        while not self.stopping.is_set():
            with self._lock:
                if not self._lock.wait_for(lambda: len(self._running) < self.slots, timeout=1.0):
                    continue
            try:
                job = self._post("/worker/claim", {"worker_id": self.worker_id, "queue": QUEUE,
                                                   "wait_seconds": CLAIM_WAIT_SECONDS, "source": "api"},
                                 timeout=CLAIM_WAIT_SECONDS + 10).get("job")
            except requests.RequestException as e:
                logger.warning("claim failed (%s), retrying in 5s", e)
                self.stopping.wait(5)
                continue
            if job is not None:
                with self._lock:
                    self._running.add(job["id"])
                threading.Thread(target=self.execute, args=(job,), daemon=True).start()
        with self._lock:
            self._lock.wait_for(lambda: not self._running)
        try:
            self._post("/worker/deregister", {"worker_id": self.worker_id}, timeout=10)
        except requests.RequestException as e:
            logger.warning("deregister failed: %s", e)


class ApiWorkerProcess:
    def __init__(self, port: int, entry: str) -> None:
        self.port = port
        self.entry = entry
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervise, name=f"api-worker-{self.entry}",
                                        daemon=True)
        self._thread.start()

    def _supervise(self) -> None:
        src = Path(__file__).resolve().parents[1]
        while not self._stop.is_set():
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "workqueue.api_worker", self.entry], cwd=src,
                env={**os.environ, "PYTHONPATH": str(src), "CP_URL": f"http://127.0.0.1:{self.port}"})
            code = self._proc.wait()
            if not self._stop.is_set():
                logger.error("api worker for %s exited %s, restarting in %.0fs", self.entry, code,
                             RESTART_SECONDS)
                self._stop.wait(RESTART_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.send_signal(signal.SIGTERM)
            try:
                self._proc.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._thread:
            self._thread.join(timeout=5)


def main() -> int:
    from config import llm_models
    from config.settings_manager import settings_manager
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    name = sys.argv[1]
    entry = llm_models.entry(name)
    queue_cfg = settings_manager.get_settings().get("workqueue") or {}
    worker = Worker(os.environ["CP_URL"], queue_cfg.get("token") or "", name, entry["api"],
                    int(entry.get("slots", 1)), float(queue_cfg.get("job_timeout_seconds", 1800)))
    signal.signal(signal.SIGTERM, lambda *_: worker.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stopping.set())
    worker.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
