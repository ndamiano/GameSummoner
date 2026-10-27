import pytest

from config.llm_models import UnknownModel
from db import games, jobs, workers


def _job(model, queue="llm", worker_model=None, build_id="b1") -> str:
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 10_000_000)
    job_id = jobs.enqueue_job(queue, {}, game_id="g1", build_id=build_id, model=model)
    workers.worker_created("w1", None, queue, None, 0.99, model=worker_model or model)
    jobs.claim_job(queue, "w1", 60)
    return job_id


def test_an_llm_job_for_no_settings_entry_is_refused_at_enqueue():
    with pytest.raises(UnknownModel, match="no llm entry 'deepseek-4.1' in settings"):
        jobs.enqueue_job("llm", {}, model="deepseek-4.1")
    with pytest.raises(UnknownModel):
        jobs.enqueue_job("llm", {})


@pytest.mark.parametrize("result,error", [
    ({"model": "/workspace/models/Qwen3.8-Flash-Next-IQ4_XS.gguf"}, None),
    ({"model": "qwen3.8-flash-next-basalt"}, None),
    (None, "Status 500"),
])
def test_the_entry_a_job_was_enqueued_for_is_what_its_row_keeps(result, error):
    job_id = _job("qwen-flash-next")
    jobs.complete_job(job_id, "w1", result, error, exec_seconds=1.0)
    assert jobs.get_job(job_id)["model"] == "qwen-flash-next"


API = {"base_url": "http://api", "usd_per_mtok": {"input": 0.28, "cached_input": 0.028,
                                                  "output": 0.42}}


@pytest.mark.parametrize("usage,prices,billed", [
    ({"prompt_tokens": 100_000, "completion_tokens": 2_000, "cached_tokens": 90_000},
     API["usd_per_mtok"], 6_160),
    ({"prompt_tokens": 100_000, "completion_tokens": 2_000, "cached_tokens": 0},
     API["usd_per_mtok"], 28_840),
    ({"prompt_tokens": 7, "completion_tokens": 1}, API["usd_per_mtok"], 3),
    ({"prompt_tokens": 100_000, "completion_tokens": 2_000, "cached_tokens": 90_000},
     {"input": 0, "cached_input": 0, "output": 0}, 0),
])
def test_an_api_entrys_job_debits_its_tokens_at_the_entrys_prices(llm_entries, usage, prices,
                                                                  billed):
    llm_entries["deepseek-4.1"] = {"model": "deepseek-v4.1-flash", "n_ctx": 131072,
                                   "api": {**API, "usd_per_mtok": prices}}
    job_id = _job("deepseek-4.1")
    jobs.complete_job(job_id, "w1", {"choices": [], "usage": usage}, None, exec_seconds=600.0)
    assert jobs.get_job(job_id)["billed_micros"] == billed
    assert games.game("g1")["spent_micros"] == billed


def _priced_job(usd_per_hour, game="g1", build_id="b1") -> str:
    """A job claimed by a pod the scaler priced at `usd_per_hour` (None: never priced)."""
    worker = f"w-{usd_per_hour}"
    if games.game(game) is None:
        games.create_game(game, "u1")
        games.charge_game(game, 1, 10_000_000)
    workers.worker_created(worker, f"p-{usd_per_hour}", "llm", None, usd_per_hour, model="qwen-flash-next")
    job_id = jobs.enqueue_job("llm", {}, game_id=game, build_id=build_id, model="qwen-flash-next")
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
