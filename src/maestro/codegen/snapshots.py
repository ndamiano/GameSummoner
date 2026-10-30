"""The game folder's history, kept in git.

A fix re-edits code that already worked, so the last playable version is exactly what a fix round
can destroy. One commit is taken at every finalize that produced a playable game, and one before a
fix starts editing.

The repository is `<run_dir>/game.git` and its work tree is `<run_dir>/game`, so nothing appears
inside the folder the model lists, staging copies, and `/play` serves. Everything in the folder is
committed — art and the vendored renderer included — because a restore that leaves half the game at
another version is not a restore.

git is a boundary, not a dependency of the build: a snapshot that cannot be taken is logged and the
build carries on. Losing history is not a reason to lose a game.
"""

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from maestro.codegen.staging import entry_path, game_dir

logger = logging.getLogger(__name__)

REPO_DIR = "game.git"
# Nothing about the box the build ran on: a run dir moves between machines.
_IDENTITY = ["-c", "user.name=maestro", "-c", "user.email=maestro@localhost"]


def _repo(run_dir) -> Path:
    return Path(run_dir) / REPO_DIR


def _git(run_dir, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "--git-dir", str(_repo(run_dir)), "--work-tree", str(game_dir(run_dir)),
         *_IDENTITY, *args],
        capture_output=True, text=True, check=check)


def _init(run_dir) -> None:
    repo = _repo(run_dir)
    if (repo / "HEAD").exists():
        return
    subprocess.run(["git", "init", "--quiet", "--bare", str(repo)], check=True,
                   capture_output=True, text=True)
    # Bare only in the sense that the repo sits outside the tree — it tracks one.
    _git(run_dir, "config", "core.bare", "false")


def take(run_dir, label: str) -> Optional[str]:
    """Commit the game folder as it stands. Returns the commit, or None when there is no game yet
    and when nothing has changed since the last one."""
    if not entry_path(run_dir).exists():
        return None
    try:
        _init(run_dir)
        _git(run_dir, "add", "--all")
        done = _git(run_dir, "commit", "--quiet", "-m", label, check=False)
        if done.returncode != 0:
            return None                      # nothing changed since the last snapshot
        return _git(run_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        logger.warning("no snapshot for %s: %s", run_dir, e)
        return None


def head(run_dir) -> Optional[str]:
    if not (_repo(run_dir) / "HEAD").exists():
        return None
    out = _git(run_dir, "rev-parse", "--short", "--verify", "--quiet", "HEAD", check=False)
    return out.stdout.strip() or None


def list_snapshots(run_dir) -> List[Dict]:
    """Oldest first: {id, label, at}. Empty when the run has no history."""
    if not (_repo(run_dir) / "HEAD").exists():
        return []
    out = _git(run_dir, "log", "--reverse", "--format=%h%x00%s%x00%cI", check=False)
    if out.returncode != 0:
        return []                            # a repo with no commits yet
    rows = []
    for line in out.stdout.splitlines():
        commit, label, at = line.split("\0")
        rows.append({"id": commit, "label": label, "at": at})
    return rows


def restore(run_dir, ref: str) -> None:
    """Put the game folder back as that commit left it, including files added since — a fix that
    broke the game by ADDING one is only undone by taking it away."""
    if not (_repo(run_dir) / "HEAD").exists():
        raise ValueError("this run has no snapshots")
    if _git(run_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
            check=False).returncode != 0:
        raise ValueError(f"no snapshot {ref!r} for this run")
    _git(run_dir, "checkout", ref, "--", ".")
    _git(run_dir, "clean", "--quiet", "-fd")
