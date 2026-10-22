"""The run dir's off-box copy, and the disk-pressure valve it enables.

Every settled finalize uploads the run dir as one tar.gz — game, art, game.git history, turn
logs, minus a world's build folder — so a finished game exists off-box the moment it exists at
all. `evict` then reclaims
local disk, and it REFUSES unless the remote copy is verified first: an eviction that trusts a
write it never checked is a deletion. `rehydrate` pulls the archive back and re-stages, so an
evicted game is a download away from playable or fixable again.

Archiving is a BOUNDARY like snapshots and the error gate: an unconfigured bucket or a failed
upload logs and stands aside — losing the archive is never a reason to lose (or block) a build.
"""

from __future__ import annotations

import io
import logging
import shutil
import tarfile
from pathlib import Path
from typing import Optional

from maestro.worldgen import compose
from tools import s3

logger = logging.getLogger(__name__)


def _key(run_id: str) -> str:
    return f"runs/{run_id}.tar.gz"


def _shipped(entry: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    """A world's build folder is a quarter of a gigabyte of concept images and refinement views
    that nothing reads back — the world a game plays was published into the game folder, and that
    is what a rehydrated run needs."""
    parts = Path(entry.name).parts
    return None if len(parts) > 1 and parts[1] == compose.BUILD_DIR else entry


def archive(run_id: str) -> bool:
    """Upload the run dir as one object, minus the working material no game reads. True when the
    remote copy is in place."""
    if not s3.configured():
        logger.info("archive %s skipped: s3 not configured", run_id)
        return False
    from maestro.state import RunState
    run_dir = RunState(run_id).run_dir
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(run_dir, arcname=run_id, filter=_shipped)
    data = buf.getvalue()
    try:
        s3.put(_key(run_id), data)
    except Exception as e:
        logger.warning("archive %s failed: %s — build stands, disk copy stays", run_id, e)
        return False
    logger.info("archive %s: %d KB uploaded", run_id, len(data) // 1024)
    return True


def evict(run_id: str) -> None:
    """Reclaim the local disk a run occupies. Refuses without a VERIFIED remote copy, and
    refuses a run that is mid-build — advance would recreate state under it."""
    from maestro.codegen import build_chain
    from maestro.codegen.staging import RUNTIME_DIR
    from maestro.state import RunState
    if build_chain.is_running(run_id):
        raise RuntimeError(f"{run_id} has a build in flight — not evicting under it")
    size = s3.head(_key(run_id)) if s3.configured() else None
    if not size:
        if not archive(run_id):
            raise RuntimeError(f"{run_id} has no verified archive — refusing to delete anything")
        size = s3.head(_key(run_id))
        if not size:
            raise RuntimeError(f"{run_id}: archive upload did not verify — refusing to delete")
    shutil.rmtree(RunState(run_id).run_dir, ignore_errors=True)
    shutil.rmtree(RUNTIME_DIR / "games" / run_id, ignore_errors=True)
    logger.info("evicted %s (archive verified at %d KB)", run_id, size // 1024)


def rehydrate(run_id: str) -> bool:
    """Pull an evicted run back onto disk and re-stage it. True when the game is local again
    (including when it already was)."""
    from maestro.codegen.staging import has_authored_files, stage_for_play
    from maestro.state import RunState
    run_dir = RunState(run_id).run_dir      # mkdirs an empty run dir as a side effect
    if (run_dir / "game").exists():
        return True
    if not s3.configured():
        return False
    try:
        data = s3.get(_key(run_id))
    except KeyError:
        logger.warning("rehydrate %s: no archive in the bucket", run_id)
        return False
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(run_dir.parent, filter="data")
    if has_authored_files(run_dir):
        stage_for_play(run_dir, run_id)
    logger.info("rehydrated %s (%d KB)", run_id, len(data) // 1024)
    return True


def archive_missing() -> int:
    """Upload every run with a game folder the bucket lacks — the one-time backfill for runs that
    predate archiving, and the nightly sweep that catches a finalize whose upload failed."""
    if not s3.configured():
        raise RuntimeError("s3 is not configured")
    from tools.execution_context import resolve_base_path
    runs = resolve_base_path() / "runs"
    count = 0
    for d in sorted(runs.iterdir()) if runs.is_dir() else []:
        if not (d / "game").is_dir():
            continue
        if s3.head(_key(d.name)) is None and archive(d.name):
            count += 1
    return count


def ensure_local(run_id: str) -> None:
    """The one-line hook for any path about to serve or build on a run that may be evicted."""
    from maestro.state import RunState
    if not (RunState(run_id).run_dir / "game").exists():
        rehydrate(run_id)
