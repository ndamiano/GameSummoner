"""The admin-view queries over the jobs table: the head of a queue in claim order, what each
worker holds, and the projected backlog."""

import itertools
import time

from billing.estimates import QUEUE_SECONDS_ESTIMATES, calculate_job_cost
from db import connection, games, jobs, workers

RATE = 0.99   # the rate every test worker is created at
_worker_seq = itertools.count()


def _game():
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)


def test_pending_head_is_claim_order_capped():
    _game()
    ids = [jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1") for _ in range(4)]
    head = jobs.pending_jobs_head("llm", 3)
    assert [j["id"] for j in head] == ids[:3]
    assert head[0].keys() == {"id", "game_id", "build_id", "created_at"}
    assert head[0]["game_id"] == "g1"


def test_claimed_jobs_leave_the_head_and_name_their_worker():
    _game()
    first = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    second = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.claim_job("llm", "w1", 60)

    assert [j["id"] for j in jobs.pending_jobs_head("llm", 10)] == [second]
    held = jobs.claimed_jobs("llm")
    assert [(j["id"], j["worker_id"]) for j in held] == [(first, "w1")]
    assert held[0]["started_at"] <= time.time()
    assert jobs.claimed_jobs("comfy") == []


def test_backlog_sums_the_estimates_of_unfinished_jobs():
    _game()
    jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    assert jobs.backlog_seconds("llm") == 2 * QUEUE_SECONDS_ESTIMATES["llm"]

    workers.worker_created("w1", None, "llm", None, RATE)
    claimed = jobs.claim_job("llm", "w1", 60)
    jobs.complete_job(claimed["id"], "w1", {"ok": True}, None, exec_seconds=12.0)
    assert jobs.backlog_seconds("llm") == QUEUE_SECONDS_ESTIMATES["llm"]


def _finish(queue, exec_seconds, *, game_id, gpu_type=None, build_id="b1"):
    jobs.enqueue_job(queue, {}, game_id=game_id, build_id=build_id)
    worker = f"w{next(_worker_seq)}"
    workers.worker_created(worker, None, queue, None, RATE)
    claimed = jobs.claim_job(queue, worker, 60)
    jobs.complete_job(claimed["id"], worker, {"ok": True}, None,
                       exec_seconds=exec_seconds, gpu_type=gpu_type)


def test_pod_ledger_counts_each_pods_jobs_and_what_its_games_were_debited():
    _game()
    workers.worker_created("wp", "p1", "llm", "A", 1.0)
    workers.worker_seen("wp", "llm", gpu_type="A", source="runpod")
    for game_id, error, secs in [("g1", None, 36.0), ("g1", "boom", 18.0), (None, None, 9.0)]:
        jobs.enqueue_job("llm", {}, game_id=game_id, build_id="b1")
        claimed = jobs.claim_job("llm", "wp", 60)
        jobs.complete_job(claimed["id"], "wp", None if error else {"ok": True}, error,
                           exec_seconds=secs)
    _finish("llm", 5.0, game_id="g1")

    [row] = workers.pod_ledger(0.0)
    assert (row["worker_id"], row["pod_id"]) == ("wp", "p1")
    assert (row["queue"], row["gpu_type"], row["usd_per_hour"]) == ("llm", "A", 1.0)
    assert (row["jobs"], row["failed"], row["exec_seconds"]) == (3, 1, 63.0)
    assert row["customer_micros"] == calculate_job_cost(36.0, 1.0)
    assert workers.pod_ledger(time.time() + 10) == []
    assert workers.pod_lives() == {"p1": [(row["started_at"], "wp")]}


def test_pod_ledger_is_one_row_per_life_when_runpod_reuses_a_pod_id():
    _game()
    workers.worker_created("tue", "p1", "llm", "A", 2.0)
    workers.worker_created("wed", "p1", "llm", "A", 1.0)
    with connection.platform_db() as conn:
        conn.execute("UPDATE workers SET started_at = started_at - 86400 WHERE id = 'tue'")
    for worker in ("tue", "wed"):
        workers.worker_seen(worker, "llm", gpu_type="A", source="runpod")
        jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
        claimed = jobs.claim_job("llm", worker, 60)
        jobs.complete_job(claimed["id"], worker, {"ok": True}, None, exec_seconds=10.0)

    rows = workers.pod_ledger(0.0)
    assert [(r["worker_id"], r["pod_id"], r["usd_per_hour"], r["jobs"]) for r in rows] == [
        ("wed", "p1", 1.0, 1), ("tue", "p1", 2.0, 1)]
    assert [r["worker_id"] for r in workers.pod_ledger(time.time() - 3600)] == ["wed"]
    assert [wid for _, wid in workers.pod_lives()["p1"]] == ["tue", "wed"]


def test_games_exec_seconds_is_everything_the_games_ever_ran():
    _game()
    games.create_game("g2", "u1")
    games.charge_game("g2", 1, 1_000_000)
    _finish("llm", 10.0, game_id="g1", gpu_type="A")
    _finish("comfy", 4.0, game_id="g1", gpu_type="B")
    _finish("llm", 3.0, game_id="g2", gpu_type="A")
    _finish("llm", 1.0, game_id=None, gpu_type="A")

    cost = lambda *s: sum(calculate_job_cost(x, RATE) for x in s)
    assert games.games_exec_seconds_by_gpu(["g1"]) == {
        "A": {"seconds": 10.0, "micros": cost(10.0)}, "B": {"seconds": 4.0, "micros": cost(4.0)}}
    assert games.games_exec_seconds_by_gpu(["g1", "g2"]) == {
        "A": {"seconds": 13.0, "micros": cost(10.0, 3.0)}, "B": {"seconds": 4.0, "micros": cost(4.0)}}
    assert games.games_exec_seconds_by_gpu([]) == {}


def test_games_built_since_counts_a_game_once_on_its_full_build():
    _game()
    games.create_game("g2", "u1")
    games.create_game("g3", "u1")
    b1 = games.create_build("g1")
    b2 = games.create_build("g1")
    games.create_build("g2")
    only_change = games.create_build("g3", kind="change")
    games.build_finished(b1, "built")
    games.build_finished(b2, "failed")
    games.build_finished(only_change, "built")

    assert games.games_built_since(0.0) == ["g1"]
    assert games.games_built_since(time.time() + 10) == []


def test_games_change_count_is_the_games_change_rounds_ever():
    _game()
    games.create_game("g2", "u1")
    games.create_build("g1")
    games.create_build("g1", kind="change")
    games.create_build("g1", kind="change")
    games.create_build("g1", kind="fix")
    games.create_build("g2", kind="change")

    assert games.games_change_count(["g1"]) == 2
    assert games.games_change_count(["g1", "g2"]) == 3
    assert games.games_change_count([]) == 0


def test_pod_refusals_count_stock_only_per_window_and_keep_the_latest():
    workers.record_pod_refusal("llm", "other", [{"volume": "v", "gpu_type_ids": ["a"],
                                              "error": "401 unauthorized"}], "401 unauthorized")
    workers.record_pod_refusal("llm", "stock", [{"volume": "v", "gpu_type_ids": ["a"],
                                              "error": "no instances"}], "no instances")
    workers.record_pod_refusal("comfy", "stock", [], "no instances")
    now = time.time()
    with connection.platform_db() as conn:
        conn.execute("UPDATE pod_refusals SET created_at = ? WHERE queue = 'comfy'",
                     (now - 2 * 24 * 3600,))
    s = workers.pod_stockout_stats("llm", now)
    assert (s["last_1h"], s["last_24h"], s["last_7d"]) == (1, 1, 1)
    assert abs(s["last_at"] - now) < 5
    image = workers.pod_stockout_stats("comfy", now)
    assert (image["last_1h"], image["last_24h"], image["last_7d"]) == (0, 0, 1)
    assert workers.pod_stockout_stats("mesh", now)["last_at"] is None


def test_pod_refusals_older_than_thirty_days_are_pruned_on_insert():
    workers.record_pod_refusal("llm", "stock", [], "old")
    with connection.platform_db() as conn:
        conn.execute("UPDATE pod_refusals SET created_at = ?", (time.time() - 31 * 24 * 3600,))
    workers.record_pod_refusal("llm", "stock", [], "new")
    with connection.platform_db() as conn:
        assert [r["error"] for r in conn.execute("SELECT error FROM pod_refusals")] == ["new"]

