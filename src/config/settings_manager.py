import json
import logging
import os
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
            "llm": {
                "model": os.getenv("LLM_MODEL", "local-model"),
                # Reaches the engine as `reasoning_effort`, so "none" DISABLES thinking — and a
                # build without thinking makes "something kinda close" (docs/experiments.md).
                "reasoning": os.getenv("LLM_REASONING", "medium"),
                "max_tokens": 50000,
                "basalt_args": os.getenv("BASALT_ARGS", ""),
                # Jobs one llm worker runs at once; the pod's engine batches them. The scaler
                # counts workers, not slots, so depth_per_worker and max age scale with it.
                "slots": int(os.getenv("LLM_SLOTS", "1")),
                "n_ctx": int(os.getenv("LLM_N_CTX", "32768"))
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
            # Games playable by ANYONE from the landing page — the owner's curation, not a flag
            # any build can set. Two tiers: "showcase" is what the machine can achieve, "oneshot"
            # is a single prompt's first result kept untouched. Entries are
            # {"id": run_id, "thumb": "assets/….webp"} — thumb optional, a file of the game's own,
            # the card's face. Both tiers empty ⇒ no demo surface.
            "demo_games": {"showcase": [], "oneshot": []},
            # EXCEPT "queues", which it replaces wholesale: the file must carry complete queue blocks.
            "runpod": {
                "enabled": False,
                "api_key": "",
                "network_volume_id": "",
                "cloud_type": "SECURE",
                "cp_url": "",
                "tick_seconds": 15,
                "stale_worker_seconds": 180,
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
