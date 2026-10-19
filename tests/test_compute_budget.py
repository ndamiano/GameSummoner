"""The compute budget: enqueue is the one gate every producer of GPU work passes through.

A game gets granted_micros when it is charged; workers debit spent_micros as jobs complete. The
hole this closes is the window BETWEEN those two: a build enqueues far faster than workers finish,
so measured spend alone reads near-zero right up to the moment a hundred queued jobs land. Enqueue
therefore reserves each job's per-queue estimate and admits against grant − used − reserved.
"""

import threading

import pytest

from billing.estimates import QUEUE_MICRO_ESTIMATES, calculate_job_cost
from db import games, jobs, workers
from db.errors import InsufficientCompute
from tools.execution_context import run_scope
from workqueue import client as queue_client

RATE = 0.99


def _game(micros: int = 1_000_000, game_id: str = "g1") -> str:
    games.create_game(game_id, "u1")
    games.charge_game(game_id, 1, micros)
    return game_id


def test_remaining_starts_at_the_grant():
    _game(500_000)
    assert games.compute_remaining("g1") == 500_000


def test_pending_jobs_reserve_their_estimate():
    _game()
    jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    # Nothing has executed, so spent_micros is still zero — the reservation is the whole point.
    assert games.game("g1")["spent_micros"] == 0
    assert games.compute_remaining("g1") == 1_000_000 - QUEUE_MICRO_ESTIMATES["llm"]


def test_completion_replaces_the_reservation_with_the_measured_cost():
    _game()
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)
    jobs.complete_job(job_id, "w1", {"ok": True}, None, exec_seconds=12.0)

    assert games.game("g1")["spent_micros"] == calculate_job_cost(12.0, RATE)
    # The estimate is released; only the real 12s is held against the grant.
    assert games.compute_remaining("g1") == 1_000_000 - calculate_job_cost(12.0, RATE)


def _build_row(build_id: str):
    return next(b for b in games.builds_for("g1") if b["id"] == build_id)


def test_a_completed_job_debits_its_build():
    _game()
    build_id = games.create_build("g1")
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id=build_id)
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)

    jobs.complete_job(job_id, "w1", {"ok": True}, None, exec_seconds=12.0)

    assert _build_row(build_id)["spent_micros"] == calculate_job_cost(12.0, RATE)


def test_build_spend_accumulates_across_turns():
    """A build is a chain of turns; its cost is their sum, not the last one."""
    _game()
    build_id = games.create_build("g1")
    workers.worker_created("w1", None, "llm", None, RATE)
    for seconds in (5.0, 7.0, 3.0):
        job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id=build_id)
        jobs.claim_job("llm", "w1", 60)
        jobs.complete_job(job_id, "w1", {"ok": True}, None, exec_seconds=seconds)

    assert _build_row(build_id)["spent_micros"] == sum(calculate_job_cost(s, RATE) for s in (5.0, 7.0, 3.0))


def test_a_failed_job_does_not_debit_its_build():
    """Same rule the game debit follows: only delivered work is billed."""
    _game()
    build_id = games.create_build("g1")
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id=build_id)
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)

    jobs.complete_job(job_id, "w1", None, "boom", exec_seconds=9.0)

    assert _build_row(build_id)["spent_micros"] == 0


def test_a_failed_job_does_not_debit_the_game():
    """The user got nothing out of an OOM or a 500. It burned real GPU time, but that is our cost
    to eat, not theirs to pay."""
    _game()
    job_id = jobs.enqueue_job("image", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "image", None, RATE)
    jobs.claim_job("image", "w1", 60)
    jobs.complete_job(job_id, "w1", None, "Status 500: OOM", exec_seconds=180.0)

    assert games.game("g1")["spent_micros"] == 0
    # The reservation is released either way — the job is terminal, nothing more will be spent.
    assert games.compute_remaining("g1") == 1_000_000


def test_a_failed_job_still_counts_against_the_worker():
    """busy_seconds measures what WE pay the GPU host for, which is real whether or not the user
    got a result. Never conflate it with what the user is billed."""
    _game()
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, RATE)
    workers.worker_seen("w1", "llm")
    jobs.claim_job("llm", "w1", 60)
    jobs.complete_job(job_id, "w1", None, "boom", exec_seconds=42.0)

    (worker,) = workers.live_workers("llm", freshness_seconds=60)
    assert worker["busy_seconds"] == 42.0
    assert games.game("g1")["spent_micros"] == 0


def test_a_partial_failure_is_not_billed_pro_rata():
    """A job that ran a long time and THEN failed is still a non-delivery — the elapsed seconds
    are not a partial entitlement."""
    _game()
    job_id = jobs.enqueue_job("image", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "image", None, RATE)
    jobs.claim_job("image", "w1", 600)
    jobs.complete_job(job_id, "w1", {"partial": True}, "died at 95%", exec_seconds=900.0)

    assert games.game("g1")["spent_micros"] == 0


def test_abandoned_job_releases_its_reservation():
    _game()
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    assert games.compute_remaining("g1") < 1_000_000
    assert jobs.abandon_job(job_id, "timed out") is True
    assert games.compute_remaining("g1") == 1_000_000
    assert jobs.abandon_job(job_id, "again") is False


def test_worker_completing_an_abandoned_job_is_dropped():
    """The enqueuer gave up and the reservation was released; a late completion must not then
    debit the game for work nobody is waiting on."""
    _game()
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)
    jobs.abandon_job(job_id, "timed out")

    assert jobs.complete_job(job_id, "w1", {"ok": True}, None, exec_seconds=99.0) is None
    assert games.game("g1")["spent_micros"] == 0


@pytest.mark.parametrize("queue", sorted(QUEUE_MICRO_ESTIMATES))
def test_only_enqueues_with_sufficient_balance(queue):
    cost = QUEUE_MICRO_ESTIMATES[queue]
    _game(cost, game_id="covers")
    jobs.enqueue_job(queue, {}, game_id="covers", build_id="b1")

    _game(cost - 1, game_id="short")
    with pytest.raises(InsufficientCompute) as exc:
        jobs.enqueue_job(queue, {}, game_id="short", build_id="b1")
    assert (exc.value.game_id, exc.value.remaining, exc.value.needed) == ("short", cost - 1, cost)


def test_reservations_accumulate_until_the_grant_is_gone():
    budget = QUEUE_MICRO_ESTIMATES["llm"] * 3
    _game(budget)
    for _ in range(3):
        jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    assert games.compute_remaining("g1") == 0
    with pytest.raises(InsufficientCompute):
        jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")


def test_measured_overrun_can_push_remaining_negative_but_still_refuses():
    """Estimates are not caps: a job that runs long overdraws. The gate must read the overdraft
    rather than treating a negative balance as headroom."""
    grant = QUEUE_MICRO_ESTIMATES["llm"]   # exactly one llm job's worth, and it runs long
    _game(grant)
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)
    jobs.complete_job(job_id, "w1", {}, None, exec_seconds=500.0)

    assert games.compute_remaining("g1") == grant - calculate_job_cost(500.0, RATE)
    with pytest.raises(InsufficientCompute):
        jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")


def test_concurrent_enqueues_cannot_all_take_the_same_headroom():
    """Several threads enqueue at once. A check-then-insert that is not one transaction lets every
    thread read the same remaining seconds and each spend it."""
    fits = 4
    _game(QUEUE_MICRO_ESTIMATES["llm"] * fits)
    admitted, refused = [], []
    start = threading.Barrier(12)

    def attempt():
        start.wait()
        try:
            admitted.append(jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1"))
        except InsufficientCompute:
            refused.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(admitted) == fits
    assert len(refused) == 12 - fits
    assert games.compute_remaining("g1") == 0


def test_jobs_outside_a_run_scope_are_ungated():
    """Chat and spec drafting are platform cost, not a game's — no game_id, so no budget to
    check and nothing to refuse."""
    job_id = jobs.enqueue_job("llm", {}, game_id=None)
    assert jobs.get_job(job_id)["status"] == "pending"


def test_unknown_game_has_no_budget():
    with pytest.raises(InsufficientCompute):
        jobs.enqueue_job("llm", {}, game_id="ghost", build_id="b1")


def test_run_scope_attributes_and_gates_every_producer(monkeypatch):
    """The scope is what ties a job to a game. queue_client reads it for EVERY queue, so image and
    mesh work is metered exactly like llm work."""
    _game()
    seen = []
    monkeypatch.setattr(jobs, "enqueue_job",
                        lambda q, p, **kw: seen.append((q, kw.get("game_id"))) or "j1")
    monkeypatch.setattr(jobs, "get_job",
                        lambda jid: {"status": "done", "result": {}})

    with run_scope("g1", "b1"):
        for queue in ("llm", "image", "decide"):
            queue_client.run_job(queue, {})

    assert seen == [("llm", "g1"), ("image", "g1"), ("decide", "g1")]


def test_refused_job_comes_back_as_a_failed_job_not_an_exception():
    """Callers branch on one shape. A budget refusal must degrade like a timeout — the image and
    mesh stages fall back to shapes, and a build fails its gate rather than crashing the loop."""
    _game(1)
    with run_scope("g1", "b1"):
        job = queue_client.run_job("llm", {})

    assert job["status"] == "failed"
    assert "compute budget exhausted" in job["error"]


def test_timed_out_job_is_abandoned_so_it_stops_holding_budget(monkeypatch):
    _game()
    monkeypatch.setattr(queue_client, "_POLL_INTERVAL", 0.01)
    with run_scope("g1", "b1"):
        job = queue_client.run_job("llm", {}, timeout_seconds=0.05)

    assert job["status"] == "failed" and "timed out" in job["error"]
    assert games.compute_remaining("g1") == 1_000_000
