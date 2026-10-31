from typing import Dict

from config.settings_manager import settings_manager


class UnknownModel(Exception):
    pass


def entries() -> Dict[str, Dict]:
    return settings_manager.get_settings()["llm"]


def seat(name: str) -> str:
    return settings_manager.get_settings()["seats"][name][0]


def entry(name: str) -> Dict:
    found = entries().get(name)
    if found is None:
        raise UnknownModel(f"no llm entry {name!r} in settings")
    return found
