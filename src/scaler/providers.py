"""What the autoscaler asks of a GPU provider: its machines, a priced ladder of rungs in its own
order of preference, launch one, terminate one. Providers price but none knows its stock, so the
first rung of the merged ladder that takes a launch is the cheapest available."""

from dataclasses import asdict, dataclass
import logging
from typing import Callable, Dict, List, Optional, Protocol

import requests

from scaler.ec2_client import Ec2Client, Ec2Error, Offer
from scaler.runpod_client import RunPodClient, RunPodError

logger = logging.getLogger("scaler")

_RUNPOD_ERRORS = (RunPodError, requests.RequestException)
_EC2_ERRORS = (Ec2Error, requests.RequestException)

_RUNPOD_STOCK_MARKERS = ("no instances currently available",
                         "no longer any instances available",
                         "could not find any pods with required specifications")


@dataclass(frozen=True)
class Machine:
    id: str
    name: str


@dataclass(frozen=True)
class Rung:
    provider: str
    usd_per_hour: float
    ask: Dict


@dataclass(frozen=True)
class Launched:
    id: str
    gpu_type: Optional[str]
    usd_per_hour: Optional[float]


class ProviderError(Exception):
    def __init__(self, message: str, stock: bool = False):
        super().__init__(message)
        self.stock = stock


class Provider(Protocol):
    name: str

    def machines(self, prefix: str) -> List[Machine]: ...
    def rungs(self, queue: str) -> List[Rung]: ...
    def launch(self, rung: Rung, name: str, env: Dict[str, str], worker_id: str) -> Launched: ...
    def terminate(self, machine_id: str) -> None: ...


def merge_ladders(ladders: List[List[Rung]]) -> List[Rung]:
    """One ladder, always taking the cheapest head, so each provider keeps its own order."""
    heads = [list(ladder) for ladder in ladders if ladder]
    out: List[Rung] = []
    while heads:
        cheapest = min(heads, key=lambda ladder: ladder[0].usd_per_hour)
        out.append(cheapest.pop(0))
        heads = [ladder for ladder in heads if ladder]
    return out


def runpod_stock_refusal(error: str) -> bool:
    return any(m in error.lower() for m in _RUNPOD_STOCK_MARKERS)


class RunPodProvider:
    name = "runpod"

    def __init__(self, client: RunPodClient, settings_getter: Callable[[], Dict]):
        self._client = client
        self._settings = settings_getter

    def _block(self) -> Dict:
        return self._settings().get("runpod") or {}

    def machines(self, prefix: str) -> List[Machine]:
        try:
            pods = self._client.list_pods()
        except _RUNPOD_ERRORS as e:
            raise ProviderError(str(e)) from e
        return [Machine(p["id"], p["name"]) for p in pods
                if (p.get("name") or "").startswith(prefix)]

    def rungs(self, queue: str) -> List[Rung]:
        """Per volume the head card alone, then the list, priced at its dearest card or last"""
        rp = self._block()
        qcfg = (rp.get("queues") or {}).get(queue) or {}
        ids = list(qcfg.get("gpu_type_ids") or [])
        if not ids:
            return []
        try:
            prices = self._client.gpu_prices(rp.get("cloud_type", "SECURE"))
        except _RUNPOD_ERRORS as e:
            logger.warning("runpod prices unavailable, its rungs go last: %s", e)
            prices = {}
        asks = [ids[:1], ids] if len(ids) > 1 else [ids]
        volumes = qcfg.get("network_volume_ids") or [rp.get("network_volume_id", "")]
        return [Rung(self.name, max(prices.get(g, float("inf")) for g in ask),
                     {"queue": queue, "volume": volume, "gpu_type_ids": ask})
                for volume in volumes for ask in asks]

    def launch(self, rung: Rung, name: str, env: Dict[str, str], worker_id: str) -> Launched:
        rp = self._block()
        qcfg = rp["queues"][rung.ask["queue"]]
        try:
            pod = self._client.create_pod(
                name=name,
                template_id=qcfg["template_id"],
                gpu_type_ids=rung.ask["gpu_type_ids"],
                network_volume_id=rung.ask["volume"],
                env=env,
                args=f"--worker-id {worker_id}",
                cloud_type=rp.get("cloud_type", "SECURE"),
                allowed_cuda_versions=qcfg.get("allowed_cuda_versions"),
            )
        except _RUNPOD_ERRORS as e:
            raise ProviderError(str(e), runpod_stock_refusal(str(e))) from e
        rate = pod.get("cost")
        return Launched(pod["id"], (pod.get("gpu") or {}).get("id"),
                        float(rate) if rate is not None else None)

    def terminate(self, machine_id: str) -> None:
        try:
            self._client.terminate_pod(machine_id)
        except _RUNPOD_ERRORS as e:
            raise ProviderError(str(e)) from e


class Ec2Provider:
    """A machine id is `<region>/<instance id>`: an instance id alone does not say where it is."""
    name = "aws"

    def __init__(self, client: Ec2Client, settings_getter: Callable[[], Dict]):
        self._client = client
        self._settings = settings_getter

    def _block(self) -> Dict:
        return self._settings().get("aws") or {}

    def machines(self, prefix: str) -> List[Machine]:
        regions = sorted({region for qcfg in (self._block().get("queues") or {}).values()
                          for region in qcfg["amis"]})
        try:
            return [Machine(f"{i.region}/{i.id}", i.name)
                    for i in self._client.list_instances(regions, prefix)]
        except _EC2_ERRORS as e:
            raise ProviderError(str(e)) from e

    def rungs(self, queue: str) -> List[Rung]:
        qcfg = (self._block().get("queues") or {}).get(queue)
        if not qcfg:
            return []
        try:
            offers = self._client.offers(qcfg["amis"], qcfg["instance_types"],
                                         qcfg.get("markets", ["spot", "on-demand"]))
        except _EC2_ERRORS as e:
            raise ProviderError(str(e)) from e
        return [Rung(self.name, o.usd_per_hour, {"queue": queue, **asdict(o)}) for o in offers]

    def launch(self, rung: Rung, name: str, env: Dict[str, str], worker_id: str) -> Launched:
        aws = self._block()
        offer = Offer(**{k: v for k, v in rung.ask.items() if k != "queue"})
        try:
            instance_id = self._client.launch(
                offer, name, aws["queues"][rung.ask["queue"]]["amis"][offer.region],
                {**env, "WORKER_ID": worker_id, "WORKER_SOURCE": self.name},
                aws["security_group"], aws["instance_profile"])
        except _EC2_ERRORS as e:
            raise ProviderError(str(e), getattr(e, "stock", False)) from e
        return Launched(f"{offer.region}/{instance_id}", offer.instance_type, offer.usd_per_hour)

    def terminate(self, machine_id: str) -> None:
        region, instance_id = machine_id.split("/")
        try:
            self._client.terminate(region, instance_id)
        except _EC2_ERRORS as e:
            raise ProviderError(str(e)) from e
