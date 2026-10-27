from unittest.mock import MagicMock

import pytest

from scaler.autoscaler import Autoscaler
from scaler.policy import StartPod, TerminatePod
from scaler.pools import Pool, pools
from scaler.providers import Launched, Machine, ProviderError, Rung
from scaler.stats import BootingInfo, QueueStats, WorkerInfo

RP = {"cp_url": "https://cp", "pod_prefix": "gs"}
SCALER = {"tick_seconds": 15, "stale_worker_seconds": 180, "max_workers": 3,
          "min_jobs_per_pod": 6, "cooldown_seconds": 90}
HOST = {"idle_exit_seconds": 30, "boot_seconds": 240, "boot_deadline_seconds": 300}
ENTRY = {"model": "qwen3.8-flash-next-basalt", "n_ctx": 65535,
         "self_hosted": {"providers": {"runpod": HOST, "aws": HOST}}}
LLM = Pool("llm", "qwen-flash-next", ENTRY["self_hosted"]["providers"], ENTRY)


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


def _start(a, pool=LLM):
    a._execute(StartPod(pool.name), pool, RP, "wtoken", now=100.0)


def test_pools_are_the_self_hosted_entries_then_every_other_queue_a_provider_launches_for():
    settings = {
        "llm": {"qwen-flash-next": ENTRY,
                "deepseek-4.1": {"model": "d", "n_ctx": 1, "api": {"base_url": "u"}},
                "local": {"model": "l", "n_ctx": 1, "local_gpu": {}}},
        "runpod": {"queues": {"comfy": {"template_id": "c"}}},
        "aws": {"queues": {"comfy": {"amis": {}}, "decide": {"amis": {}}}}}
    assert [(p.queue, p.model, sorted(p.providers)) for p in pools(settings)] == [
        ("llm", "qwen-flash-next", ["aws", "runpod"]),
        ("comfy", None, ["aws", "runpod"]),
        ("decide", None, ["aws"])]


@pytest.mark.parametrize("pod_name,owner", [
    ("gs-llm-qwen-flash-next-a1b2c3d4", "llm-qwen-flash-next"),
    ("gs-llm-qwen-a1b2c3d4", "llm-qwen"),
    ("gs-comfy-a1b2c3d4", "comfy"),
    ("gs-llm-a1b2c3d4", None),
])
def test_a_pod_belongs_to_the_pool_its_whole_name_says_and_no_other(pod_name, owner):
    candidates = [Pool("llm", "qwen", {}), Pool("llm", "qwen-flash-next", {}), Pool("comfy", None, {})]
    assert [p.name for p in candidates if p.owns(pod_name, "gs")] == ([owner] if owner else [])


def test_an_llm_machine_boots_with_its_entrys_model_window_flags_and_slots_and_no_card_guess():
    """The machine cannot read settings, and the card is the worker's to report"""
    p = _provider("runpod", [1.89])
    a = _scaler(p)
    entry = {**ENTRY, "slots": 2, "self_hosted": {**ENTRY["self_hosted"], "engine_args": "--spec 5"}}
    _start(a, Pool("llm", "qwen-flash-next", {"runpod": HOST}, entry))
    assert p.launch.call_args.args[3] == {
        "CP_URL": "https://cp", "WORKER_TOKEN": "wtoken", "WORKER_QUEUE": "llm",
        "IDLE_EXIT_SECONDS": "30", "LLM_MODEL": "qwen3.8-flash-next-basalt", "LLM_N_CTX": "65535",
        "BASALT_ARGS_EXTRA": "--spec 5", "WORKER_SLOTS": "2"}
    assert p.launch.call_args.args[2].startswith("gs-llm-qwen-flash-next-")
    _start(a)
    env = p.launch.call_args.args[3]
    assert (env["BASALT_ARGS_EXTRA"], env["WORKER_SLOTS"]) == ("", "1")


def test_only_an_llm_machine_hears_about_the_model():
    p = _provider("runpod", [1.01])
    _start(_scaler(p), Pool("comfy", None, {"runpod": HOST}))
    assert p.launch.call_args.args[3].keys() == {
        "CP_URL", "WORKER_TOKEN", "WORKER_QUEUE", "IDLE_EXIT_SECONDS"}
    assert p.launch.call_args.args[2].startswith("gs-comfy-")


def test_scale_up_walks_the_merged_ladder_cheapest_first_with_each_providers_own_block():
    rp_block, aws_block = {**HOST, "idle_exit_seconds": 60}, {**HOST, "idle_exit_seconds": 20}
    rp, aws = _provider("runpod", [2.19]), _provider("aws", [1.37, 2.62])
    aws.launch.side_effect = [ProviderError("no spot", stock=True),
                              Launched("us-west-2/i-1", "g7e.2xlarge", 2.62)]
    rp.launch.side_effect = ProviderError("none", stock=True)
    a = _scaler(rp, aws)
    _start(a, Pool("llm", "qwen-flash-next", {"runpod": rp_block, "aws": aws_block}, ENTRY))
    rp.rungs.assert_called_once_with(rp_block)
    assert [c.args[0].usd_per_hour for c in aws.launch.call_args_list] == [1.37, 2.62]
    assert [c.args[1] for c in aws.launch.call_args_list] == [aws_block, aws_block]
    assert aws.launch.call_args.args[3]["IDLE_EXIT_SECONDS"] == "20"
    assert rp.launch.call_args.args[0].usd_per_hour == 2.19
    assert rp.launch.call_args.args[3]["IDLE_EXIT_SECONDS"] == "60"
    worker_id = aws.launch.call_args.args[4]
    a._stats.record_worker_created.assert_called_once_with(
        worker_id, "us-west-2/i-1", "llm", "qwen-flash-next", "g7e.2xlarge", 2.62, "aws")
    a._stats.record_pod_created.assert_called_once_with("llm", "qwen-flash-next")
    a._stats.record_pod_refusal.assert_not_called()


def test_a_provider_the_pool_has_no_block_for_is_never_asked():
    rp, aws = _provider("runpod", [1.01]), _provider("aws", [1.37])
    _start(_scaler(rp, aws), Pool("comfy", None, {"aws": HOST}))
    rp.rungs.assert_not_called()
    aws.launch.assert_called_once()


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
    queue, model, kind, attempts, error = a._stats.record_pod_refusal.call_args.args
    assert (queue, model, kind, error) == ("llm", "qwen-flash-next", "stock", "none available")
    assert attempts == [
        {"provider": "aws", "n": 0, "error": "InsufficientInstanceCapacity", "stock": True},
        {"provider": "runpod", "n": 0, "error": "none available", "stock": True}]
    a._stats.record_worker_created.assert_not_called()
    a._stats.record_pod_created.assert_not_called()

    rp.launch.side_effect = ProviderError("401 unauthorized")
    _start(a)
    assert a._stats.record_pod_refusal.call_args.args[2] == "other"
    assert a._stats.record_pod_refusal.call_count == 2


def _settings(llm=None, **scaler):
    return {"llm": {"qwen-flash-next": ENTRY} if llm is None else llm,
            "workqueue": {"token": "wtoken"}, "scaler": {**SCALER, **scaler},
            "runpod": {**RP, "queues": {}}}


def _busy(a, **scaler):
    a._settings = lambda: _settings(**scaler)
    a._stats.queue_stats.return_value = QueueStats(pending=50, job_seconds=30.0)
    a._stats.live_workers.return_value = [WorkerInfo("w0", "p0")]
    a._stats.stale_workers.return_value = []
    a._stats.terminated_workers_with_pods.return_value = []


def _tick(a, monkeypatch, now):
    monkeypatch.setattr("scaler.autoscaler.time.time", lambda: now)
    a.tick()


def test_one_pools_backlog_starts_its_own_machine_and_leaves_the_other_pool_alone():
    p = _provider("aws", [1.37])
    a = _scaler(p)
    other = {**ENTRY, "model": "other-basalt"}
    a._settings = lambda: _settings(llm={"qwen-flash-next": ENTRY, "other": other})
    a._stats.queue_stats.side_effect = lambda queue, model: QueueStats(
        pending=1 if model == "qwen-flash-next" else 0)
    a._stats.live_workers.return_value = []
    a._stats.stale_workers.return_value = []
    a._stats.terminated_workers_with_pods.return_value = []
    a.tick()
    p.machines.assert_called_once_with("gs-")
    assert p.launch.call_count == 1
    name, env = p.launch.call_args.args[2], p.launch.call_args.args[3]
    assert name.startswith("gs-llm-qwen-flash-next-")
    assert env["LLM_MODEL"] == "qwen3.8-flash-next-basalt"
    assert a._stats.record_worker_created.call_args.args[2:4] == ("llm", "qwen-flash-next")


@pytest.mark.parametrize("boots,adds", [((120, 120), True), ((120, 600), False)])
def test_the_add_a_machine_math_takes_the_slower_providers_boot(boots, adds):
    p = _provider("runpod", [1.89], [Machine("p0", "gs-llm-qwen-flash-next-p0")])
    a = _scaler(p)
    _busy(a)
    hosts = {"runpod": {**HOST, "boot_seconds": boots[0]}, "aws": {**HOST, "boot_seconds": boots[1]}}
    a._settings = lambda: _settings(llm={"qwen-flash-next": {
        **ENTRY, "self_hosted": {"providers": hosts}}}, min_jobs_per_pod=20)
    a.tick()
    assert p.launch.called is adds


def test_a_launch_starts_the_cooldown_and_a_refusal_does_not(monkeypatch):
    machines = [Machine("p0", "gs-llm-qwen-flash-next-p0")]
    p = _provider("runpod", [1.89], machines)
    a = _scaler(p)
    _busy(a, max_workers=4)
    p.launch.side_effect = ProviderError("no capacity", stock=True)
    _tick(a, monkeypatch, 100.0)
    _tick(a, monkeypatch, 101.0)
    assert p.launch.call_count == 2

    p.launch.side_effect = None
    _tick(a, monkeypatch, 102.0)
    machines.append(Machine("runpod-id", "gs-llm-qwen-flash-next-p1"))
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
    rp = _provider("runpod", [2.19], [Machine("p0", "gs-llm-qwen-flash-next-p0")])
    aws = _provider("aws", [1.37], [Machine("eu-north-1/i-1", "gs-llm-qwen-flash-next-p1")])
    a = _scaler(rp, aws)
    _busy(a)
    a._stats.booting_workers.return_value = [BootingInfo("eu-north-1/i-1", started_at=100.0)]
    _tick(a, monkeypatch, 1100.0)
    aws.terminate.assert_called_once_with("eu-north-1/i-1")
    rp.terminate.assert_not_called()
    a._stats.mark_pod_terminated.assert_called_once_with("eu-north-1/i-1")
    assert a._stats.stale_workers.call_args.args == (
        "llm", "qwen-flash-next", 180, {"runpod": 300, "aws": 300})


def test_a_failed_terminate_leaves_the_row_for_the_next_tick():
    p = _provider("aws", [1.37])
    p.terminate.side_effect = ProviderError("TerminateInstances 503")
    a = _scaler(p)
    a._owner = {"eu-north-1/i-1": p}
    a._execute(TerminatePod("eu-north-1/i-1", "worker stale", worker_id="w1"), LLM, RP, "t", 0.0)
    a._stats.mark_pod_terminated.assert_not_called()
