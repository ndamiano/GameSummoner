"""Pure scaling-policy decisions: the reap rules and the scale-up work guarantee."""

from scaler.policy import (
    MarkWorkerTerminated,
    PodInfo,
    ScalingPolicy,
    StartPod,
    TerminatePod,
    decide,
)
from scaler.stats import QueueStats, WorkerInfo

CFG = ScalingPolicy(max_workers=2, cooldown_seconds=90,
                    boot_seconds=120, assumed_job_seconds=13, min_jobs_per_pod=6)

IDLE = QueueStats(pending=0)


def _decide(stats=IDLE, live=(), stale=(), terminated=(), pods=(), since=1e9, cfg=CFG):
    return decide("mesh", cfg, stats, list(live), list(stale), list(terminated),
                  list(pods), since)


def test_scale_from_zero_on_a_single_pending_job():
    assert _decide(stats=QueueStats(1)) == [StartPod("mesh")]


def test_scale_from_zero_ignores_the_cooldown():
    assert _decide(stats=QueueStats(1), since=0.0) == [StartPod("mesh")]


def test_empty_queue_adds_nothing():
    assert _decide() == []


def test_a_booting_pod_counts_as_capacity():
    # The add-forever-during-boot case: the pod on its way owes the five itself.
    pod = PodInfo("p1", "maestro-mesh-a1", age_seconds=60, boot_deadline_seconds=900)
    assert _decide(stats=QueueStats(5), pods=[pod]) == []
def test_a_backlog_that_owes_the_new_pod_its_minimum_adds_one():
    # 40 jobs, one worker: it eats 120/13 ~ 9 while the new pod boots, and the 31 left split
    # two ways is 15 each — well past the six a boot has to earn.
    live = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    assert _decide(stats=QueueStats(40), live=live, pods=pods) == [StartPod("mesh")]


def test_a_backlog_the_fleet_will_have_eaten_adds_nothing():
    # The 2026-09-07 image burst at its last spawn: 14 pending, five pods on them. They chew
    # about 46 before a sixth could land — it would wake to nothing, as the real one did.
    live = [WorkerInfo(f"w{i}", f"p{i}") for i in range(5)]
    pods = [PodInfo(f"p{i}", f"maestro-mesh-a{i}", 600, 900) for i in range(5)]
    cfg = ScalingPolicy(max_workers=8, cooldown_seconds=90,
                        boot_seconds=120, assumed_job_seconds=13, min_jobs_per_pod=6)
    assert _decide(stats=QueueStats(14), live=live, pods=pods, cfg=cfg) == []


def test_a_backlog_that_owes_the_new_pod_too_little_adds_nothing():
    # 20 jobs, two workers: 18 eaten during the boot, and the two left split three ways is
    # under one job each. A boot is billed whole — those two do not carry it.
    live = [WorkerInfo("w1", "p1"), WorkerInfo("w2", "p2")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900), PodInfo("p2", "maestro-mesh-a2", 300, 900)]
    assert _decide(stats=QueueStats(20), live=live, pods=pods) == []


def test_measured_job_seconds_overrides_the_assumed_one():
    live = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    # 20 jobs the week says cost 30 s each: the worker eats 4 in the boot and 8 are owed.
    assert _decide(stats=QueueStats(20, job_seconds=30.0),
                   live=live, pods=pods) == [StartPod("mesh")]
    # The same twenty at the week's 5 s each are gone before the pod lands.
    assert _decide(stats=QueueStats(20, job_seconds=5.0), live=live, pods=pods) == []


def test_cooldown_blocks_the_add():
    live = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    assert _decide(stats=QueueStats(10), live=live, pods=pods, since=10.0) == []


def test_max_workers_caps_the_fleet():
    live = [WorkerInfo("w1", "p1"), WorkerInfo("w2", "p2")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900), PodInfo("p2", "maestro-mesh-a2", 600, 900)]
    assert _decide(stats=QueueStats(500), live=live, pods=pods) == []


def test_a_long_wait_alone_buys_nothing():
    # One job waiting behind a busy worker is the fleet chewing, not starvation: a pod bought
    # for it lands after that job is gone. True starvation is the no-capacity arm.
    live = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    assert _decide(stats=QueueStats(1), live=live, pods=pods) == []


def test_at_most_one_start_pod_per_tick():
    actions = _decide(stats=QueueStats(100))
    assert actions == [StartPod("mesh")]


def test_wedged_pod_reaped_past_the_boot_deadline_and_capacity_recovers():
    pod = PodInfo("p1", "maestro-mesh-a1", age_seconds=1000, boot_deadline_seconds=900)
    actions = _decide(stats=QueueStats(1), pods=[pod])
    assert TerminatePod("p1", "never registered past boot deadline") in actions
    # the wedged pod no longer counts as capacity, so scale-from-zero fires
    assert StartPod("mesh") in actions


def test_each_pod_is_held_to_its_own_providers_boot_deadline():
    pods = [PodInfo("slow", "maestro-mesh-a1", 400, boot_deadline_seconds=600),
            PodInfo("fast", "maestro-mesh-a2", 400, boot_deadline_seconds=300)]
    actions = _decide(stats=QueueStats(1), pods=pods)
    assert actions == [TerminatePod("fast", "never registered past boot deadline")]


def test_terminated_workers_listed_pod_is_reaped():
    t = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 60, 900)]
    assert _decide(terminated=t, pods=pods) == [TerminatePod("p1", "worker deregistered")]


def test_terminated_workers_missing_pod_is_not_re_reaped():
    # pod already gone from list_pods — a terminate must not re-fire every tick
    assert _decide(terminated=[WorkerInfo("w1", "p1")]) == []


def test_stale_workers_pod_is_reaped_and_row_marked_via_the_action():
    s = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    assert _decide(stale=s, pods=pods) == [TerminatePod("p1", "worker stale", worker_id="w1")]


def test_stale_worker_with_no_pod_left_is_just_marked():
    assert _decide(stale=[WorkerInfo("w1", "p1")]) == [MarkWorkerTerminated("w1")]


def test_reaped_stale_pod_frees_capacity_for_scale_from_zero():
    s = [WorkerInfo("w1", "p1")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900)]
    actions = _decide(stats=QueueStats(3), stale=s, pods=pods)
    assert TerminatePod("p1", "worker stale", worker_id="w1") in actions
    assert StartPod("mesh") in actions


def test_reap_respects_the_cap_when_counting_survivors():
    # 2 pods, one being reaped → 1 survivor < max_workers, so an add is allowed
    s = [WorkerInfo("w1", "p1")]
    live = [WorkerInfo("w2", "p2")]
    pods = [PodInfo("p1", "maestro-mesh-a1", 600, 900), PodInfo("p2", "maestro-mesh-a2", 600, 900)]
    actions = _decide(stats=QueueStats(50), stale=s, live=live, pods=pods)
    assert StartPod("mesh") in actions
