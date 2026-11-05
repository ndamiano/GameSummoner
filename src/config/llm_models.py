from typing import Dict

from config.settings_manager import settings_manager


class UnknownModel(Exception):
    pass


def entries() -> Dict[str, Dict]:
    return settings_manager.get_settings()["llm"]


def ours(entry: Dict) -> bool:
    return "api" not in entry


def seat(name: str, private: bool) -> str:
    """The first entry the seat lists that a game of this privacy may call."""
    return next(n for n in settings_manager.get_settings()["seats"][name]
                if not private or ours(entries()[n]))


def entry(name: str) -> Dict:
    found = entries().get(name)
    if found is None:
        raise UnknownModel(f"no llm entry {name!r} in settings")
    return found
