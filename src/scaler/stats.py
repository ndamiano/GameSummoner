"""Pool and fleet facts: the only scaler module that touches storage."""

import time
from typing import Dict, List, NamedTuple, Optional, Protocol

from db import jobs, workers

_WEEK = 7 * 24 * 3600


class QueueStats(NamedTuple):
    pending: int
    # One job's cost to a worker, measured over the last week; None where the week holds none.
    job_seconds: Optional[float] = None


class WorkerInfo(NamedTuple):
    id: str
    pod_id: Optional[str]


class BootingInfo(NamedTuple):
    pod_id: str
    started_at: float


class StatsSource(Protocol):
    def queue_stats(self, queue: str, model: Optional[str]) -> QueueStats: ...
    def live_workers(self, queue: str, model: Optional[str],
                     freshness_seconds: float) -> List[WorkerInfo]: ...
    def stale_workers(self, queue: str, model: Optional[str], staleness_seconds: float,
                      boot_deadlines: Dict[str, float]) -> List[WorkerInfo]: ...
    def booting_workers(self, queue: str, model: Optional[str]) -> List[BootingInfo]: ...
    def terminated_workers_with_pods(self, queue: str, model: Optional[str]) -> List[WorkerInfo]: ...
    def mark_worker_terminated(self, worker_id: str) -> None: ...
    def mark_pod_terminated(self, pod_id: str) -> None: ...
    def record_worker_created(self, worker_id: str, pod_id: str, queue: str, model: Optional[str],
                              gpu_type: Optional[str], usd_per_hour: Optional[float],
                              source: str) -> None: ...
    def record_pod_refusal(self, queue: str, model: Optional[str], kind: str,
                           attempts: List[Dict], error: str) -> None: ...
    def record_pod_created(self, queue: str, model: Optional[str]) -> None: ...


class SqliteStatsSource:
    def queue_stats(self, queue: str, model: Optional[str]) -> QueueStats:
        s = jobs.queue_stats(queue, model)
        since = time.time() - _WEEK
        return QueueStats(s["pending"], jobs.recent_job_seconds(queue, model, since))

    def live_workers(self, queue: str, model: Optional[str],
                     freshness_seconds: float) -> List[WorkerInfo]:
        return [WorkerInfo(w["id"], w["pod_id"])
                for w in workers.live_workers(queue, model, freshness_seconds)]

    def stale_workers(self, queue: str, model: Optional[str], staleness_seconds: float,
                      boot_deadlines: Dict[str, float]) -> List[WorkerInfo]:
        return [WorkerInfo(w["id"], w["pod_id"])
                for w in workers.stale_workers(queue, model, staleness_seconds, boot_deadlines)]

    def booting_workers(self, queue: str, model: Optional[str]) -> List[BootingInfo]:
        return [BootingInfo(w["pod_id"], w["started_at"])
                for w in workers.booting_workers(queue, model)]

    def terminated_workers_with_pods(self, queue: str, model: Optional[str]) -> List[WorkerInfo]:
        return [WorkerInfo(w["id"], w["pod_id"])
                for w in workers.terminated_workers_with_pods(queue, model)]

    def mark_worker_terminated(self, worker_id: str) -> None:
        workers.set_worker_terminated(worker_id)

    def mark_pod_terminated(self, pod_id: str) -> None:
        workers.set_pod_terminated(pod_id)

    def record_worker_created(self, worker_id: str, pod_id: str, queue: str, model: Optional[str],
                              gpu_type: Optional[str], usd_per_hour: Optional[float],
                              source: str) -> None:
        workers.worker_created(worker_id, pod_id, queue, gpu_type, usd_per_hour, source, model)

    def record_pod_refusal(self, queue: str, model: Optional[str], kind: str,
                           attempts: List[Dict], error: str) -> None:
        workers.record_pod_refusal(queue, model, kind, attempts, error)

    def record_pod_created(self, queue: str, model: Optional[str]) -> None:
        workers.record_pod_created(queue, model)
