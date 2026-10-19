"""The scaling loop over providers: the env a machine boots with, the merged ladder walk,
what a launch and a refusal record, cooldown, ageing and reaping."""

from unittest.mock import MagicMock

import pytest

from scaler.autoscaler import Autoscaler
from scaler.policy import StartPod, TerminatePod
from scaler.providers import Launched, Machine, ProviderError, Rung
from scaler.stats import BootingInfo, QueueStats, WorkerInfo

QCFG = {"idle_exit_seconds": 30}
RP = {"cp_url": "https://cp", "pod_prefix": "gs"}
LLM = {"model": "pennyroyal", "n_ctx": 65535}


def _provider(name, prices, machines=()):
    p = MagicMock()
    p.name = name
    p.rungs.return_value = [Rung(name, usd, {"n": i}) for i, usd in enumerate(prices)]
    p.machines.side_effect = lambda prefix: list(machines)
    p.launch.return_value = Launched(f"{name}-id", "card", prices[0] if prices else None)
    return p


def _scaler(*providers):
    a = Autoscaler(MagicMock(), list(providers), lambda: {})
    a._stats.booting_workers.return_value = []
    return a


def _start(a, llm=LLM, queue="llm"):
    a._execute(StartPod(queue), queue, QCFG, RP, "wtoken", llm, now=100.0)


def test_an_llm_machine_boots_with_the_model_window_flags_and_slots_and_no_card_guess():
    """The machine cannot read settings, and the card is the worker's to report"""
    p = _provider("runpod", [1.89])
    a = _scaler(p)
    _start(a, llm={**LLM, "sglang_args": "--cuda-graph-bs 1", "basalt_args": "--parallel 2",
                   "slots": 2})
    assert p.launch.call_args.args[2] == {
        "CP_URL": "https://cp", "WORKER_TOKEN": "wtoken", "WORKER_QUEUE": "llm",
        "IDLE_EXIT_SECONDS": "30", "LLM_MODEL": "pennyroyal", "LLM_N_CTX": "65535",
        "SGLANG_ARGS_EXTRA": "--cuda-graph-bs 1", "WORKER_SLOTS": "2"}
    assert p.launch.call_args.args[1].startswith("gs-llm-")
    _start(a)
    env = p.launch.call_args.args[2]
    assert (env["SGLANG_ARGS_EXTRA"], env["WORKER_SLOTS"]) == ("", "1")


def test_only_an_llm_machine_hears_about_the_model():
    p = _provider("runpod", [1.01])
    _start(_scaler(p), queue="comfy")
    assert p.launch.call_args.args[2].keys() == {
        "CP_URL", "WORKER_TOKEN", "WORKER_QUEUE", "IDLE_EXIT_SECONDS"}


def test_scale_up_walks_the_merged_ladder_cheapest_first_until_a_rung_takes_it():
    rp, aws = _provider("runpod", [2.19]), _provider("aws", [1.37, 2.62])
    aws.launch.side_effect = [ProviderError("no spot", stock=True),
                              Launched("us-west-2/i-1", "g7e.2xlarge", 2.62)]
    rp.launch.side_effect = ProviderError("none", stock=True)
    a = _scaler(rp, aws)
    _start(a)
    assert [c.args[0].usd_per_hour for c in aws.launch.call_args_list] == [1.37, 2.62]
    assert rp.launch.call_args.args[0].usd_per_hour == 2.19
    worker_id = aws.launch.call_args.args[3]
    a._stats.record_worker_created.assert_called_once_with(
        worker_id, "us-west-2/i-1", "llm", "g7e.2xlarge", 2.62, "aws")
    a._stats.record_pod_created.assert_called_once_with("llm")
    a._stats.record_pod_refusal.assert_not_called()


def test_a_provider_with_no_ladder_does_not_stop_the_other_from_launching():
    rp, aws = _provider("runpod", []), _provider("aws", [1.37])
    rp.rungs.side_effect = ProviderError("graphql down")
    a = _scaler(rp, aws)
    _start(a)
    aws.launch.assert_called_once()


def test_every_rung_refused_is_recorded_once_and_is_stock_only_if_all_were():
    rp, aws = _provider("runpod", [2.19]), _provider("aws", [1.37])
    aws.launch.side_effect = ProviderError("InsufficientInstanceCapacity", stock=True)
    rp.launch.side_effect = ProviderError("none available", stock=True)
    a = _scaler(rp, aws)
    _start(a)
    queue, kind, attempts, error = a._stats.record_pod_refusal.call_args.args
    assert (queue, kind, error) == ("llm", "stock", "none available")
    assert attempts == [
        {"provider": "aws", "n": 0, "error": "InsufficientInstanceCapacity", "stock": True},
        {"provider": "runpod", "n": 0, "error": "none available", "stock": True}]
    a._stats.record_worker_created.assert_not_called()
    a._stats.record_pod_created.assert_not_called()

    rp.launch.side_effect = ProviderError("401 unauthorized")
    _start(a)
    assert a._stats.record_pod_refusal.call_args.args[1] == "other"
    assert a._stats.record_pod_refusal.call_count == 2


def _busy(a, max_workers=3):
    """One live worker already on a machine, and a queue deep enough to ask for another."""
    a._settings = lambda: {"llm": LLM, "workqueue": {"token": "wtoken"},
                           "runpod": {**RP, "queues": {"llm": {**QCFG, "max_workers": max_workers}}}}
    a._stats.queue_stats.return_value = QueueStats(pending=50, job_seconds=30.0)
    a._stats.live_workers.return_value = [WorkerInfo("w0", "p0")]
    a._stats.stale_workers.return_value = []
    a._stats.terminated_workers_with_pods.return_value = []


def _tick(a, monkeypatch, now):
    monkeypatch.setattr("scaler.autoscaler.time.time", lambda: now)
    a.tick()


def test_tick_lists_under_the_prefix_and_launches_from_zero_with_settings_env():
    p = _provider("aws", [1.37])
    a = _scaler(p)
    _busy(a)
    a._stats.live_workers.return_value = []
    a._stats.queue_stats.return_value = QueueStats(pending=1)
    a.tick()
    p.machines.assert_called_once_with("gs-")
    assert p.launch.call_args.args[2]["LLM_MODEL"] == "pennyroyal"


def test_a_launch_starts_the_cooldown_and_a_refusal_does_not(monkeypatch):
    machines = [Machine("p0", "gs-llm-p0")]
    p = _provider("runpod", [1.89], machines)
    a = _scaler(p)
    _busy(a, max_workers=4)
    p.launch.side_effect = ProviderError("no capacity", stock=True)
    _tick(a, monkeypatch, 100.0)
    _tick(a, monkeypatch, 101.0)
    assert p.launch.call_count == 2

    p.launch.side_effect = None
    _tick(a, monkeypatch, 102.0)
    machines.append(Machine("runpod-id", "gs-llm-p1"))
    _tick(a, monkeypatch, 103.0)
    assert p.launch.call_count == 3
    _tick(a, monkeypatch, 102.0 + 90)
    assert p.launch.call_count == 4


def test_a_provider_that_cannot_list_skips_the_tick_rather_than_count_the_fleet_short():
    rp, aws = _provider("runpod", [2.19]), _provider("aws", [1.37])
    aws.machines.side_effect = ProviderError("DescribeInstances 503")
    a = _scaler(rp, aws)
    _busy(a)
    with pytest.raises(ProviderError):
        a.tick()
    rp.launch.assert_not_called()
    a._stats.mark_worker_terminated.assert_not_called()


def test_a_booting_machine_is_aged_from_its_create_and_reaped_through_its_own_provider(monkeypatch):
    """A control-plane restart forgets first-seen; the row's started_at does not."""
    rp = _provider("runpod", [2.19], [Machine("p0", "gs-llm-p0")])
    aws = _provider("aws", [1.37], [Machine("eu-north-1/i-1", "gs-llm-p1")])
    a = _scaler(rp, aws)
    _busy(a)
    a._stats.booting_workers.return_value = [BootingInfo("eu-north-1/i-1", started_at=100.0)]
    _tick(a, monkeypatch, 1100.0)
    aws.terminate.assert_called_once_with("eu-north-1/i-1")
    rp.terminate.assert_not_called()
    a._stats.mark_pod_terminated.assert_called_once_with("eu-north-1/i-1")


def test_a_failed_terminate_leaves_the_row_for_the_next_tick():
    p = _provider("aws", [1.37])
    p.terminate.side_effect = ProviderError("TerminateInstances 503")
    a = _scaler(p)
    a._owner = {"eu-north-1/i-1": p}
    a._execute(TerminatePod("eu-north-1/i-1", "worker stale", worker_id="w1"),
               "llm", QCFG, RP, "t", LLM, 0.0)
    a._stats.mark_pod_terminated.assert_not_called()
