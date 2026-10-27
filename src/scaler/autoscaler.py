"""The control-plane scaling loop."""

import logging
import threading
import time
import uuid
from typing import Callable, Dict, List

from billing.estimates import QUEUE_SECONDS_ESTIMATES
from scaler.policy import (
    MarkWorkerTerminated,
    PodInfo,
    ScalingPolicy,
    StartPod,
    TerminatePod,
    decide,
)
from scaler.pools import Pool, policy, pools
from scaler.providers import Machine, Provider, ProviderError, merge_ladders
from scaler.stats import StatsSource

logger = logging.getLogger("scaler")


class Autoscaler:
    def __init__(self, stats: StatsSource, providers: List[Provider],
                 settings_getter: Callable[[], Dict]):
        self._stats = stats
        self._providers = list(providers)
        self._settings = settings_getter
        self._stop = threading.Event()
        self._thread = None
        self._last_scale_up: Dict[str, float] = {}
        self._pod_first_seen: Dict[str, float] = {}
        self._owner: Dict[str, Provider] = {}

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="autoscaler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        logger.info("autoscaler started")
        while True:
            tick_seconds = self._settings()["scaler"]["tick_seconds"]
            if self._stop.wait(tick_seconds):
                return
            try:
                self.tick()
            except Exception:
                logger.exception("autoscaler tick failed")

    def _machines(self, prefix: str) -> List[Machine]:
        machines: List[Machine] = []
        self._owner = {}
        for provider in self._providers:
            listed = provider.machines(prefix)
            machines += listed
            self._owner.update({m.id: provider for m in listed})
        return machines

    def tick(self) -> None:
        settings = self._settings()
        rp = settings.get("runpod") or {}
        scaler = settings["scaler"]
        token = (settings.get("workqueue") or {}).get("token", "")
        now = time.time()

        pod_prefix = rp.get("pod_prefix", "maestro")
        pods = self._machines(f"{pod_prefix}-")
        listed = {p.id for p in pods}
        for pod_id in listed - self._pod_first_seen.keys():
            self._pod_first_seen[pod_id] = now
        for pod_id in self._pod_first_seen.keys() - listed:
            del self._pod_first_seen[pod_id]

        staleness = scaler["stale_worker_seconds"]
        for pool in pools(settings):
            deadlines = {name: block.get("boot_deadline_seconds", 300)
                         for name, block in pool.providers.items()}
            created = {b.pod_id: b.started_at
                       for b in self._stats.booting_workers(pool.queue, pool.model)}
            qpods = [PodInfo(p.id, p.name, now - created.get(p.id, self._pod_first_seen[p.id]),
                             deadlines.get(self._owner[p.id].name, 0.0))
                     for p in pods if pool.owns(p.name, pod_prefix)]
            own = policy(scaler, pool.name)
            scaling = ScalingPolicy(
                max_workers=own["max_workers"],
                cooldown_seconds=own["cooldown_seconds"],
                boot_seconds=max((block.get("boot_seconds", 150)
                                  for block in pool.providers.values()), default=0.0),
                assumed_job_seconds=QUEUE_SECONDS_ESTIMATES[pool.queue],
                min_jobs_per_pod=own["min_jobs_per_pod"],
            )
            actions = decide(
                pool.name, scaling,
                self._stats.queue_stats(pool.queue, pool.model),
                self._stats.live_workers(pool.queue, pool.model, staleness),
                self._stats.stale_workers(pool.queue, pool.model, staleness, deadlines),
                self._stats.terminated_workers_with_pods(pool.queue, pool.model),
                qpods,
                now - self._last_scale_up.get(pool.name, 0.0),
            )
            for action in actions:
                self._execute(action, pool, rp, token, now)

    def _execute(self, action, pool: Pool, rp: Dict, token: str, now: float) -> None:
        try:
            if isinstance(action, StartPod):
                name = f"{rp.get('pod_prefix', 'maestro')}-{pool.name}-{uuid.uuid4().hex[:8]}"
                env = {
                    "CP_URL": rp.get("cp_url", ""),
                    "WORKER_TOKEN": token,
                    "WORKER_QUEUE": pool.queue,
                }
                if pool.model:
                    env["LLM_MODEL"] = pool.entry["model"]
                    env["LLM_N_CTX"] = str(pool.entry["n_ctx"])
                    env["BASALT_ARGS_EXTRA"] = pool.entry["self_hosted"].get("engine_args") or ""
                    env["WORKER_SLOTS"] = str(pool.entry.get("slots", 1))
                if self._start_pod(name, pool, env, uuid.uuid4().hex):
                    self._last_scale_up[pool.name] = now
            elif isinstance(action, TerminatePod):
                self._owner[action.pod_id].terminate(action.pod_id)
                self._stats.mark_pod_terminated(action.pod_id)
                logger.info("reaped pod %s (%s)", action.pod_id, action.reason)
            elif isinstance(action, MarkWorkerTerminated):
                self._stats.mark_worker_terminated(action.worker_id)
        except ProviderError as e:
            logger.error("action %r on pool %s failed: %s", action, pool.name, e)

    def _start_pod(self, name: str, pool: Pool, env: Dict, worker_id: str) -> bool:
        by_name = {p.name: p for p in self._providers}
        ladders = []
        for provider in self._providers:
            if provider.name not in pool.providers:
                continue
            try:
                ladders.append(provider.rungs(pool.providers[provider.name]))
            except ProviderError as e:
                logger.error("scale-up %s: %s has no ladder: %s", pool.name, provider.name, e)
        refused: List[Dict] = []
        for rung in merge_ladders(ladders):
            block = pool.providers[rung.provider]
            try:
                launched = by_name[rung.provider].launch(
                    rung, block, name,
                    {**env, "IDLE_EXIT_SECONDS": str(block.get("idle_exit_seconds", 10))},
                    worker_id)
            except ProviderError as e:
                refused.append({"provider": rung.provider, **rung.ask, "error": str(e),
                                "stock": e.stock})
                logger.warning("scale-up %s: %s refused %s at $%.4f/hr (%s)", pool.name,
                               rung.provider, rung.ask, rung.usd_per_hour, e)
                continue
            logger.info("scale-up %s: %s launched %s (%s) on %s at $%.4f/hr", pool.name,
                        rung.provider, name, launched.id, rung.ask, rung.usd_per_hour)
            self._stats.record_pod_created(pool.queue, pool.model)
            self._stats.record_worker_created(worker_id, launched.id, pool.queue, pool.model,
                                              launched.gpu_type, launched.usd_per_hour,
                                              rung.provider)
            return True
        if refused:
            kind = "stock" if all(r["stock"] for r in refused) else "other"
            self._stats.record_pod_refusal(pool.queue, pool.model, kind, refused,
                                           refused[-1]["error"])
            logger.error("scale-up %s: every rung refused (%s)", pool.name, kind)
        return False
