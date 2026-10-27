"""Which commit built this: `<short sha>`, with `-dirty` for an edited tree. Prod has no .git, so
deploy.sh writes the commit to `src/REVISION`."""

import logging
import subprocess
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO = Path(__file__).resolve().parents[2]
_REVISION = _REPO / "src" / "REVISION"


@lru_cache(maxsize=1)
def maestro_rev() -> str:
    if _REVISION.is_file():
        return _REVISION.read_text().strip()
    try:
        sha = subprocess.run(["git", "-C", str(_REPO), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if sha.returncode != 0:
            return "unknown"
        dirty = subprocess.run(["git", "-C", str(_REPO), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=5)
        suffix = "-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else ""
        return sha.stdout.strip() + suffix
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not resolve the maestro revision: %s", exc)
        return "unknown"
