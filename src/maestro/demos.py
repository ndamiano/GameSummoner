"""The demo snapshots — the landing page's playable games, each a copy that nothing touches.

A demo is a folder `runtime/demos/<run_id>/`: the game files as they were staged at the moment
the owner took the snapshot, plus `_demo.json` carrying the words that made it. It is copied
ONCE and read from there alone — no run dir, no database row, no staging — so a later build of
the same run (a fix, a change, a rebuild) restages `runtime/games/<run_id>/` and the demo does
not move. Listing which snapshots the page shows is still `demo_games` in settings.

    python -m maestro.demos snapshot <run_id> [--replace]   # copy the staged game into a demo
    python -m maestro.demos list
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Dict, Optional

from maestro.codegen.staging import ENTRY, RUNTIME_DIR, title_of

DEMOS_DIR = RUNTIME_DIR / "demos"
META = "_demo.json"


def demo_dir(run_id: str) -> Path:
    return DEMOS_DIR / run_id


def exists(run_id: str) -> bool:
    return (demo_dir(run_id) / ENTRY).is_file()


def meta(run_id: str) -> Dict:
    try:
        return json.loads((demo_dir(run_id) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def title(run_id: str) -> Optional[str]:
    """The name the model gave the game — the snapshot's <title> — else the run's own title."""
    return title_of(demo_dir(run_id)) or meta(run_id).get("title") or None


def snapshot(run_id: str, replace: bool = False) -> Path:
    """Copy the run's staged game into the demos tree, with the person's ask beside it. Refuses
    to overwrite a snapshot that exists unless told to — a demo that quietly became a different
    game is the failure this tree exists to rule out."""
    from db import games
    from maestro.state import RunState

    src = RUNTIME_DIR / "games" / run_id
    if not (src / ENTRY).is_file():
        raise FileNotFoundError(f"{run_id} is not staged")
    dst = demo_dir(run_id)
    if dst.exists():
        if not replace:
            raise FileExistsError(f"demo {run_id} exists; --replace to overwrite it")
        shutil.rmtree(dst)
    spec = RunState(run_id).read_spec() or {}
    row = games.game(run_id) or {}
    shutil.copytree(src, dst)
    (dst / META).write_text(json.dumps({
        "run_id": run_id,
        "prompt": spec.get("ask") or spec.get("request", ""),
        "title": row.get("title") or spec.get("title", ""),
    }, indent=1), encoding="utf-8")
    return dst


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("run_id")
    snap.add_argument("--replace", action="store_true")
    sub.add_parser("list")
    args = ap.parse_args(argv)
    if args.cmd == "snapshot":
        try:
            print(snapshot(args.run_id, replace=args.replace))
        except (FileNotFoundError, FileExistsError) as e:
            sys.exit(str(e))
    else:
        for d in sorted(DEMOS_DIR.glob("*/")) if DEMOS_DIR.is_dir() else []:
            if (d / ENTRY).is_file():
                print(d.name, "|", title(d.name) or "", "|", (meta(d.name).get("prompt") or "")[:80])


if __name__ == "__main__":
    main()
