"""What a completed job records about which model served it. `jobs.model` was written at ENQUEUE
from what the caller asked for and never corrected, so a pod serving one model filed its jobs under
another — 387 of them, on a run that cost real money.
"""

import pytest

from db import games, jobs, workers


def _job(queue: str = "llm", model=None, build_id="b1") -> str:
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 10_000_000)
    job_id = jobs.enqueue_job(queue, {}, game_id="g1", build_id=build_id, model=model)
    workers.worker_created("w1", None, queue, None, 0.99)
    jobs.claim_job(queue, "w1", 60)
    return job_id


def test_the_served_model_overwrites_what_was_asked_for():
    job_id = _job(model="qwen3.6_27b")

    jobs.complete_job(job_id, "w1", {"model": "DeepSeek-V4-Flash-0731"}, None, exec_seconds=1.0)

    assert jobs.get_job(job_id)["model"] == "DeepSeek-V4-Flash-0731"


def test_a_served_model_reported_as_a_path_is_stored_as_its_filename():
    """llama.cpp answers with the gguf path it was launched on; hosted engines answer with a
    name. Storing the basename makes a grouped cost query comparable across both."""
    job_id = _job(model="qwen3.6_27b")

    jobs.complete_job(job_id, "w1", {"model": "/workspace/models/LLM/MiniMax-M3-UD.gguf"},
                       None, exec_seconds=1.0)

    assert jobs.get_job(job_id)["model"] == "MiniMax-M3-UD.gguf"


def test_a_result_without_a_model_keeps_the_requested_one():
    """Image and mesh results carry no model field, and must not blank the column."""
    job_id = _job(queue="comfy", model="sdxl")

    jobs.complete_job(job_id, "w1", {"images": []}, None, exec_seconds=1.0)

    assert jobs.get_job(job_id)["model"] == "sdxl"


def test_a_failed_job_keeps_the_requested_model():
    job_id = _job(model="qwen3.6_27b")

    jobs.complete_job(job_id, "w1", None, "status 500", exec_seconds=1.0)

    assert jobs.get_job(job_id)["model"] == "qwen3.6_27b"


def _priced_job(usd_per_hour, game="g1", build_id="b1") -> str:
    """A job claimed by a pod the scaler priced at `usd_per_hour` (None: never priced)."""
    worker = f"w-{usd_per_hour}"
    if games.game(game) is None:
        games.create_game(game, "u1")
        games.charge_game(game, 1, 10_000_000)
    workers.worker_created(worker, f"p-{usd_per_hour}", "llm", None, usd_per_hour)
    job_id = jobs.enqueue_job("llm", {}, game_id=game, build_id=build_id)
    jobs.claim_job("llm", worker, 60)
    return job_id


def _finish(job_id, usd_per_hour, exec_seconds, error=None):
    jobs.complete_job(job_id, f"w-{usd_per_hour}", None if error else {"out": 1}, error,
                       exec_seconds=exec_seconds)


def test_a_job_debits_its_pods_own_rate_rounded_up_to_the_micro():
    job_id = _priced_job(2.21)
    _finish(job_id, 2.21, 47.0)
    assert games.game("g1")["spent_micros"] == 28_858
    assert jobs.get_job(job_id)["billed_micros"] == 28_858


def test_a_pricier_pod_debits_more_for_the_same_seconds():
    _finish(_priced_job(2.19, game="g1"), 2.19, 100.0)
    _finish(_priced_job(0.99, game="g2"), 0.99, 100.0)
    ratio = games.game("g1")["spent_micros"] / games.game("g2")["spent_micros"]
    assert ratio == pytest.approx(2.19 / 0.99, rel=2e-3)


def test_the_builds_debit_matches_the_games():
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 10_000_000)
    build_id = games.create_build("g1")
    _finish(_priced_job(2.21, build_id=build_id), 2.21, 47.0)
    assert games.builds_for("g1")[0]["spent_micros"] == 28_858


def test_a_failed_job_debits_nothing_but_its_row_keeps_the_price():
    job_id = _priced_job(2.21)
    _finish(job_id, 2.21, 47.0, error="status 500")
    job = jobs.get_job(job_id)
    assert games.game("g1")["spent_micros"] == 0
    assert (job["exec_seconds"], job["billed_micros"]) == (47.0, 28_858)
