"""The control-plane scaling loop: per tick every provider lists its machines, then per queue
stats → decide → execute, a scale-up walking one ladder merged across the providers. A failed
tick is logged and skipped, never fatal."""

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
            tick_seconds = (self._settings().get("runpod") or {}).get("tick_seconds", 15)
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
        token = (settings.get("workqueue") or {}).get("token", "")
        now = time.time()

        pod_prefix = rp.get("pod_prefix", "maestro")
        pods = self._machines(f"{pod_prefix}-")
        listed = {p.id for p in pods}
        for pod_id in listed - self._pod_first_seen.keys():
            self._pod_first_seen[pod_id] = now
        for pod_id in self._pod_first_seen.keys() - listed:
            del self._pod_first_seen[pod_id]

        for queue, qcfg in (rp.get("queues") or {}).items():
            prefix = f"{pod_prefix}-{queue}-"
            created = {b.pod_id: b.started_at for b in self._stats.booting_workers(queue)}
            qpods = [PodInfo(p.id, p.name, now - created.get(p.id, self._pod_first_seen[p.id]))
                     for p in pods if p.name.startswith(prefix)]
            staleness = rp.get("stale_worker_seconds", 180)
            policy = ScalingPolicy(
                max_workers=qcfg.get("max_workers", 2),
                cooldown_seconds=qcfg.get("cooldown_seconds", 90),
                boot_deadline_seconds=qcfg.get("boot_deadline_seconds", 300),
                boot_seconds=qcfg.get("boot_seconds", 150),
                assumed_job_seconds=QUEUE_SECONDS_ESTIMATES[queue],
                min_jobs_per_pod=qcfg.get("min_jobs_per_pod", 6),
            )
            actions = decide(
                queue, policy,
                self._stats.queue_stats(queue),
                self._stats.live_workers(queue, staleness),
                self._stats.stale_workers(queue, staleness, policy.boot_deadline_seconds),
                self._stats.terminated_workers_with_pods(queue),
                qpods,
                now - self._last_scale_up.get(queue, 0.0),
            )
            for action in actions:
                self._execute(action, queue, qcfg, rp, token, settings["llm"], now)

    def _execute(self, action, queue: str, qcfg: Dict, rp: Dict, token: str, llm: Dict,
                 now: float) -> None:
        try:
            if isinstance(action, StartPod):
                name = f"{rp.get('pod_prefix', 'maestro')}-{queue}-{uuid.uuid4().hex[:8]}"
                env = {
                    "CP_URL": rp.get("cp_url", ""),
                    "WORKER_TOKEN": token,
                    "WORKER_QUEUE": queue,
                    "IDLE_EXIT_SECONDS": str(qcfg.get("idle_exit_seconds", 10)),
                }
                if queue == "llm":
                    env["LLM_MODEL"] = llm["model"]
                    env["LLM_N_CTX"] = str(llm["n_ctx"])
                    env["BASALT_ARGS_EXTRA"] = llm.get("basalt_args") or ""
                    env["WORKER_SLOTS"] = str(llm.get("slots", 1))
                if self._start_pod(name, queue, env, uuid.uuid4().hex):
                    self._last_scale_up[queue] = now
            elif isinstance(action, TerminatePod):
                self._owner[action.pod_id].terminate(action.pod_id)
                self._stats.mark_pod_terminated(action.pod_id)
                logger.info("reaped pod %s (%s)", action.pod_id, action.reason)
            elif isinstance(action, MarkWorkerTerminated):
                self._stats.mark_worker_terminated(action.worker_id)
        except ProviderError as e:
            logger.error("action %r on queue %s failed: %s", action, queue, e)

    def _start_pod(self, name: str, queue: str, env: Dict, worker_id: str) -> bool:
        by_name = {p.name: p for p in self._providers}
        ladders = []
        for provider in self._providers:
            try:
                ladders.append(provider.rungs(queue))
            except ProviderError as e:
                logger.error("scale-up %s: %s has no ladder: %s", queue, provider.name, e)
        refused: List[Dict] = []
        for rung in merge_ladders(ladders):
            try:
                launched = by_name[rung.provider].launch(rung, name, env, worker_id)
            except ProviderError as e:
                refused.append({"provider": rung.provider, **rung.ask, "error": str(e),
                                "stock": e.stock})
                logger.warning("scale-up %s: %s refused %s at $%.4f/hr (%s)", queue,
                               rung.provider, rung.ask, rung.usd_per_hour, e)
                continue
            logger.info("scale-up %s: %s launched %s (%s) on %s at $%.4f/hr", queue,
                        rung.provider, name, launched.id, rung.ask, rung.usd_per_hour)
            self._stats.record_pod_created(queue)
            self._stats.record_worker_created(worker_id, launched.id, queue, launched.gpu_type,
                                              launched.usd_per_hour, rung.provider)
            return True
        if refused:
            kind = "stock" if all(r["stock"] for r in refused) else "other"
            self._stats.record_pod_refusal(queue, kind, refused, refused[-1]["error"])
            logger.error("scale-up %s: every rung refused (%s)", queue, kind)
        return False
