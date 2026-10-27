from unittest.mock import MagicMock

import pytest
import requests

from scaler.ec2_client import Ec2Error, Instance, Offer
from scaler.providers import (Ec2Provider, Launched, Machine, ProviderError, Rung,
                              RunPodProvider, merge_ladders, runpod_stock_refusal)
from scaler.runpod_client import RunPodError

WK, SE = "NVIDIA RTX PRO 6000 Blackwell Workstation", "NVIDIA RTX PRO 6000 Blackwell Server"
RP = {"cloud_type": "SECURE", "network_volume_id": "vol1"}
RP_BLOCK = {"template_id": "tpl1", "gpu_type_ids": [WK, SE], "allowed_cuda_versions": ["13.0"]}
AWS = {"security_group": "sg", "instance_profile": "profile"}
AWS_BLOCK = {"instance_types": ["g7e.2xlarge"], "markets": ["spot"],
             "amis": {"eu-north-1": "ami-eu", "us-west-2": "ami-us"}}


def _runpod():
    client = MagicMock()
    client.gpu_prices.return_value = {WK: 1.89, SE: 2.19}
    return RunPodProvider(client, lambda: {"runpod": RP}), client


def _ec2(settings=None):
    client = MagicMock()
    settings = settings or {"aws": AWS, "llm": {}}
    return Ec2Provider(client, lambda: settings), client


def test_merge_takes_the_cheapest_head_and_never_reorders_inside_a_provider():
    a = [Rung("runpod", 2.19, {"n": 1}), Rung("runpod", 1.89, {"n": 2})]
    b = [Rung("aws", 1.37, {"n": 3}), Rung("aws", 2.62, {"n": 4}), Rung("aws", 3.36, {"n": 5})]
    assert [r.ask["n"] for r in merge_ladders([a, b, []])] == [3, 1, 2, 4, 5]


def test_runpod_ladder_is_the_preferred_card_alone_then_the_list_volume_by_volume():
    provider, _ = _runpod()
    block = {**RP_BLOCK, "network_volume_ids": ["vol-eu", "vol-us"]}
    assert [(r.ask["volume"], r.ask["gpu_type_ids"], r.usd_per_hour)
            for r in provider.rungs(block)] == [
        ("vol-eu", [WK], 1.89), ("vol-eu", [WK, SE], 2.19),
        ("vol-us", [WK], 1.89), ("vol-us", [WK, SE], 2.19)]


def test_a_block_with_host_facts_but_no_machines_is_served_only_by_the_other_providers():
    provider, client = _runpod()
    assert provider.rungs({"boot_seconds": 200}) == []
    client.gpu_prices.assert_not_called()
    ec2, ec2_client = _ec2()
    assert ec2.rungs({"boot_seconds": 200}) == []
    ec2_client.offers.assert_not_called()


def test_runpod_with_no_prices_still_offers_its_rungs_last():
    provider, client = _runpod()
    client.gpu_prices.side_effect = RunPodError("graphql down")
    rungs = provider.rungs(RP_BLOCK)
    assert [r.usd_per_hour for r in rungs] == [float("inf")] * 2
    assert merge_ladders([rungs, [Rung("aws", 4.85, {})]])[0].provider == "aws"


def test_runpod_launch_sends_the_pools_block_and_reads_the_pods_own_price():
    provider, client = _runpod()
    client.create_pod.return_value = {"id": "pod1", "cost": 1.91, "gpu": {"id": WK}}
    rung = provider.rungs(RP_BLOCK)[0]
    assert provider.launch(rung, RP_BLOCK, "gs-llm-a1", {"CP_URL": "u"}, "w1") == Launched(
        "pod1", WK, 1.91)
    client.create_pod.assert_called_once_with(
        name="gs-llm-a1", template_id="tpl1", gpu_type_ids=[WK], network_volume_id="vol1",
        env={"CP_URL": "u"}, args="--worker-id w1", cloud_type="SECURE",
        allowed_cuda_versions=["13.0"])
    client.create_pod.return_value = {"id": "pod2"}
    assert provider.launch(rung, RP_BLOCK, "n", {}, "w2") == Launched("pod2", None, None)


@pytest.mark.parametrize("error,stock", [
    ("POST /v2/pods -> 500: create pod: There are no instances currently available", True),
    ("POST /v2/pods -> 500: could not find any pods with required specifications", True),
    ('POST https://api.runpod.io/v2/pods -> 400: {"detail":"There are no longer any instances '
     'available with the requested specifications. Please refresh and try again."}', True),
    ("POST /v2/pods -> 401: unauthorized", False)])
def test_runpod_refusal_is_stock_only_by_its_wording(error, stock):
    assert runpod_stock_refusal(error) is stock
    provider, client = _runpod()
    client.create_pod.side_effect = RunPodError(error)
    with pytest.raises(ProviderError) as e:
        provider.launch(provider.rungs(RP_BLOCK)[0], RP_BLOCK, "n", {}, "w")
    assert e.value.stock is stock


def test_runpod_lists_only_machines_under_the_prefix():
    provider, client = _runpod()
    client.list_pods.return_value = [{"id": "p1", "name": "gs-llm-a"}, {"id": "p2", "name": None},
                                     {"id": "p3", "name": "maestro-llm-b"}]
    assert provider.machines("gs-") == [Machine("p1", "gs-llm-a")]


def test_ec2_ladder_is_the_clients_offers_over_the_regions_that_have_an_ami():
    provider, client = _ec2()
    client.offers.return_value = [Offer("eu-north-1", "eu-north-1a", "g7e.2xlarge", "spot", 1.37)]
    assert provider.rungs(AWS_BLOCK) == [Rung("aws", 1.37, {
        "region": "eu-north-1", "zone": "eu-north-1a",
        "instance_type": "g7e.2xlarge", "market": "spot", "usd_per_hour": 1.37})]
    client.offers.assert_called_once_with(AWS_BLOCK["amis"], ["g7e.2xlarge"], ["spot"])


def test_ec2_launch_tells_the_box_who_it_is_and_the_id_carries_the_region():
    provider, client = _ec2()
    offer = Offer("us-west-2", "us-west-2c", "g7e.2xlarge", "spot", 2.62)
    client.offers.return_value = [offer]
    client.launch.return_value = "i-0abc"
    launched = provider.launch(provider.rungs(AWS_BLOCK)[0], AWS_BLOCK, "gs-llm-a1",
                               {"CP_URL": "u"}, "w1")
    assert launched == Launched("us-west-2/i-0abc", "g7e.2xlarge", 2.62)
    client.launch.assert_called_once_with(
        offer, "gs-llm-a1", "ami-us",
        {"CP_URL": "u", "WORKER_ID": "w1", "WORKER_SOURCE": "aws"}, "sg", "profile")
    provider.terminate("us-west-2/i-0abc")
    client.terminate.assert_called_once_with("us-west-2", "i-0abc")


def test_ec2_lists_every_region_any_pool_has_an_ami_in_once_under_region_qualified_ids():
    entry = {"model": "m", "n_ctx": 1, "self_hosted": {"providers": {"aws": AWS_BLOCK}}}
    provider, client = _ec2({
        "aws": {**AWS, "queues": {"comfy": {"amis": {"us-east-1": "ami-c", "eu-north-1": "x"}}}},
        "llm": {"qwen-flash-next": entry, "local": {"model": "m", "n_ctx": 1, "local_gpu": {}}}})
    client.list_instances.return_value = [
        Instance("i-1", "eu-north-1", "eu-north-1a", "gs-llm-a", "g7e.2xlarge", "spot", 0.0)]
    assert provider.machines("gs-") == [Machine("eu-north-1/i-1", "gs-llm-a")]
    client.list_instances.assert_called_once_with(["eu-north-1", "us-east-1", "us-west-2"], "gs-")


@pytest.mark.parametrize("raised,stock", [
    (Ec2Error("InsufficientInstanceCapacity", "none"), True),
    (Ec2Error("UnauthorizedOperation", "no"), False),
    (requests.ConnectionError("dns"), False)])
def test_ec2_refusal_carries_whether_it_was_stock(raised, stock):
    provider, client = _ec2()
    client.offers.return_value = [Offer("eu-north-1", "eu-north-1a", "g7e.2xlarge", "spot", 1.37)]
    client.launch.side_effect = raised
    with pytest.raises(ProviderError) as e:
        provider.launch(provider.rungs(AWS_BLOCK)[0], AWS_BLOCK, "n", {}, "w")
    assert e.value.stock is stock
