"""Pure scaling decisions: no I/O, no clock, everything injected."""

from dataclasses import dataclass
from typing import List, Optional, Union

from scaler.stats import QueueStats, WorkerInfo


@dataclass(frozen=True)
class ScalingPolicy:
    max_workers: int
    cooldown_seconds: float
    boot_seconds: float
    assumed_job_seconds: float
    min_jobs_per_pod: float


@dataclass(frozen=True)
class PodInfo:
    id: str
    name: str
    age_seconds: float
    boot_deadline_seconds: float


@dataclass(frozen=True)
class StartPod:
    pool: str


@dataclass(frozen=True)
class TerminatePod:
    pod_id: str
    reason: str
    worker_id: Optional[str] = None


@dataclass(frozen=True)
class MarkWorkerTerminated:
    worker_id: str


Action = Union[StartPod, TerminatePod, MarkWorkerTerminated]


def decide(pool: str, cfg: ScalingPolicy, stats: QueueStats,
           live: List[WorkerInfo], stale: List[WorkerInfo], terminated: List[WorkerInfo],
           pods: List[PodInfo], seconds_since_last_scale_up: float) -> List[Action]:
    actions: List[Action] = []
    listed = {p.id for p in pods}
    reaped = set()

    # Pods behind deregistered workers: the worker decided to die; the in-pod self-terminate is
    # best-effort, this is the billing guarantee. Only listed pods — a terminate that already
    # landed must not re-fire every tick.
    for w in terminated:
        if w.pod_id in listed and w.pod_id not in reaped:
            actions.append(TerminatePod(w.pod_id, "worker deregistered"))
            reaped.add(w.pod_id)

    # Stale workers: presumed dead (crash without deregister). Mark the row either way so it
    # stops surfacing; kill the pod if it still exists.
    for w in stale:
        if w.pod_id in listed and w.pod_id not in reaped:
            # The executor marks the row only after the terminate lands, so a failed API call
            # leaves the worker stale and the reap retries next tick.
            actions.append(TerminatePod(w.pod_id, "worker stale", worker_id=w.id))
            reaped.add(w.pod_id)
        else:
            actions.append(MarkWorkerTerminated(w.id))

    # Pods no worker ever registered from, past the boot deadline: wedged (image pull loop,
    # bad env) — reap rather than count as capacity forever.
    known_pod_ids = {w.pod_id for w in (*live, *stale, *terminated) if w.pod_id}
    for p in pods:
        if p.id not in known_pod_ids and p.id not in reaped \
                and p.age_seconds > p.boot_deadline_seconds:
            actions.append(TerminatePod(p.id, "never registered past boot deadline"))
            reaped.add(p.id)

    # Scale-up. A booting pod counts as capacity — without that the same backlog buys a pod on
    # every tick of one boot — and the pod cap counts every surviving managed pod.
    starting = [p for p in pods
                if p.id not in known_pod_ids and p.id not in reaped
                and p.age_seconds <= p.boot_deadline_seconds]
    effective = len(live) + len(starting)
    surviving_pods = len(listed - reaped)

    add = False
    if effective == 0:
        # Scale-from-zero on ANY pending job, no cooldown: nothing is coming to drain it.
        add = stats.pending > 0
    else:
        job = stats.job_seconds if stats.job_seconds is not None else cfg.assumed_job_seconds
        # What the fleet on hand chews while the new pod boots, and what is left for it to
        # share with them when it arrives.
        left = stats.pending - effective * cfg.boot_seconds / job
        owed = left / (effective + 1)
        if owed >= cfg.min_jobs_per_pod:
            add = seconds_since_last_scale_up >= cfg.cooldown_seconds
    if add and surviving_pods < cfg.max_workers:
        actions.append(StartPod(pool))

    return actions
