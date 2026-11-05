import json
import logging
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any, Dict

logger = logging.getLogger(__name__)


def _merge(defaults: Dict[str, Any], loaded: Dict[str, Any]) -> Dict[str, Any]:
    """`loaded` over `defaults`, one level deep — the settings blocks (llm, comfyui, …) are
    flat dicts, so a file that names a block need not repeat every key in it."""
    out = dict(defaults)
    for k, v in loaded.items():
        base = out.get(k)
        out[k] = {**base, **v} if isinstance(base, dict) and isinstance(v, dict) else v
    return out


_ENTRY_NAME = re.compile(r"[a-z0-9.-]{1,32}")
_KINDS = ("self_hosted", "api", "local_gpu")
_PROVIDERS = {"runpod", "aws"}
_PRICES = ("input", "cached_input", "output")
SEATS = ("design", "build", "gate", "assets", "worldgen")


def validate_llm(llm: Dict[str, Any]) -> None:
    for name, entry in llm.items():
        where = f"settings llm.{name}"
        if not _ENTRY_NAME.fullmatch(name):
            raise ValueError(f"{where}: a name is [a-z0-9.-], at most 32 characters")
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: an entry is an object")
        if not entry.get("model") or not entry.get("n_ctx"):
            raise ValueError(f"{where}: needs model and n_ctx")
        kinds = [k for k in _KINDS if k in entry]
        if len(kinds) != 1:
            raise ValueError(f"{where}: needs exactly one of {', '.join(_KINDS)}, has {kinds}")
        if "self_hosted" in entry:
            unknown = set((entry["self_hosted"].get("providers") or {})) - _PROVIDERS
            if unknown:
                raise ValueError(f"{where}: unknown providers {sorted(unknown)}")
        if "api" in entry:
            api = entry["api"]
            prices = api.get("usd_per_mtok") or {}
            if not api.get("base_url") or any(
                    not isinstance(prices.get(k), (int, float)) for k in _PRICES):
                raise ValueError(f"{where}: an api entry needs base_url and usd_per_mtok "
                                 f"{{{', '.join(_PRICES)}}}")


def validate_seats(seats: Dict[str, Any], llm: Dict[str, Any]) -> None:
    for seat in SEATS:
        names = seats.get(seat)
        if not isinstance(names, list) or not names:
            raise ValueError(f"settings seats.{seat}: a seat is a non-empty list of llm entries")
        unknown = [n for n in names if n not in llm]
        if unknown:
            raise ValueError(f"settings seats.{seat}: no llm entry {unknown}")
        if all("api" in llm[n] for n in names):
            raise ValueError(f"settings seats.{seat}: a private game needs an entry that is not api")
    extra = set(seats) - set(SEATS)
    if extra:
        raise ValueError(f"settings seats: unknown seats {sorted(extra)}")


class SettingsManager:

    def __init__(self):
        self._settings_lock = Lock()

        project_root = Path(__file__).parent.parent
        self.settings_path = project_root / "config" / "settings.json"

        default_working_dir = os.getenv("WORKING_DIRECTORY", "outputs")
        if not Path(default_working_dir).is_absolute():
            default_working_dir = str((project_root / default_working_dir).resolve())

        # Control-plane state (the sqlite dbs), resolved against the repo root — deliberately
        # apart from working_directory, which holds generated artifacts.
        default_data_dir = os.getenv("MAESTRO_DATA_DIR", "data")
        if not Path(default_data_dir).is_absolute():
            default_data_dir = str((project_root.parent / default_data_dir).resolve())

        self.defaults = {
            "working_directory": default_working_dir,
            "data_dir": default_data_dir,
            "llm": {},
            "seats": {},
            "scaler": {
                "tick_seconds": 15,
                "stale_worker_seconds": 180,
                "max_workers": 3,
                "min_jobs_per_pod": 6,
                "cooldown_seconds": 300
            },
            "workqueue": {
                "token": os.getenv("WORKQUEUE_TOKEN", ""),
                "job_timeout_seconds": int(os.getenv("WORKQUEUE_JOB_TIMEOUT", "1800")),
                "lease_seconds": int(os.getenv("WORKQUEUE_LEASE", "120"))
            },
            # _merge is one level deep, so a settings.json "runpod" block merges over these keys —
            # Where games are served FROM (the untrusted origin) and the one origin allowed to
            # frame them. Both empty ⇒ games ride the app origin, framed by 'self'. Set BOTH to
            # split: origin must be a separate REGISTRABLE domain (cookies are domain-scoped — a
            # subdomain of the app's domain shares the jar and can toss cookies onto it).
            "play": {
                "origin": "",
                "app_origin": "",
            },
            "demo_games": [],
            # EXCEPT "queues", which it replaces wholesale: the file must carry complete queue blocks.
            "runpod": {
                "enabled": False,
                "api_key": "",
                "network_volume_id": "",
                "cloud_type": "SECURE",
                "cp_url": "",
                "queues": {}
            },
            "aws": {
                "enabled": False,
                "access_key": "",
                "secret_key": "",
                "security_group": "gs-gpu-worker",
                "instance_profile": "gs-gpu-worker",
                "queues": {}
            }
        }

        self._settings = self._load_settings()
        validate_llm(self._settings["llm"])
        validate_seats(self._settings["seats"], self._settings["llm"])

    def _load_settings(self) -> Dict[str, Any]:
        try:
            if self.settings_path.exists():
                with open(self.settings_path, 'r') as f:
                    settings = json.load(f)
                    logger.info(f"Settings loaded from {self.settings_path}")
                    # Callers subscript this dict directly, so a key the file omits is a KeyError
                    # rather than the schema default it looks like — layer the defaults under it.
                    return _merge(self.defaults, settings)
            else:
                logger.info(f"Settings file not found, creating with defaults at {self.settings_path}")
                self._save_settings(self.defaults)
                return self.defaults.copy()
        except Exception as e:
            logger.error(f"Error loading settings: {e}, using defaults")
            return self.defaults.copy()

    def _save_settings(self, settings: Dict[str, Any]) -> None:
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.settings_path, 'w') as f:
                json.dump(settings, f, indent=2)

            logger.info(f"Settings saved to {self.settings_path}")
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            raise

    def get_settings(self) -> Dict[str, Any]:
        with self._settings_lock:
            return self._settings.copy()


settings_manager = SettingsManager()
