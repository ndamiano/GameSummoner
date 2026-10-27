from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Pool:
    queue: str
    model: Optional[str]
    providers: Dict[str, Dict]
    entry: Dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.queue}-{self.model}" if self.model else self.queue

    def owns(self, pod_name: str, prefix: str) -> bool:
        return pod_name.rsplit("-", 1)[0] == f"{prefix}-{self.name}"


def policy(scaler: Dict, pool: str) -> Dict:
    shared = {k: v for k, v in scaler.items() if k != "pools"}
    return {**shared, **(scaler.get("pools") or {}).get(pool, {})}


def pools(settings: Dict) -> List[Pool]:
    out = [Pool("llm", name, entry["self_hosted"].get("providers") or {}, entry)
           for name, entry in settings["llm"].items() if "self_hosted" in entry]
    blocks = {provider: (settings.get(provider) or {}).get("queues") or {}
              for provider in ("runpod", "aws")}
    for queue in sorted({q for queues in blocks.values() for q in queues}):
        out.append(Pool(queue, None, {provider: queues[queue]
                                      for provider, queues in blocks.items() if queue in queues}))
    return out
