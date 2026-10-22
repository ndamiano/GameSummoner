#!/usr/bin/env python3

import argparse
import json
import sqlite3
import time
from pathlib import Path

CURSOR_KINDS = ("build", "fix", "change")
RETIRED = ("build_id", "kind", "phase", "paused")


def new_state(status: str, cursor: dict | None, busy: bool) -> tuple:
    if status == "building":
        if cursor is None:
            return "building", "failed"
        if cursor.get("phase") == "done":
            return "checking", "succeeded" if cursor.get("ok") else "failed"
        stage = "checking" if cursor.get("phase") == "checking" else "building"
        return stage, "running" if busy and not cursor.get("paused") else "paused"
    return {"draft": ("designing", "paused"), "ready": ("building", "paused"),
            "built": ("checking", "succeeded"), "failed": ("building", "failed"),
            "held": ("building", "held"), "stopped": ("building", "paused"),
            "paused": ("building", "paused")}.get(
                status, ("building", "failed"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--runs", required=True, type=Path)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"no database at {args.db}")
    backup = args.db.with_name(f"{args.db.name}.pre-stage-{time.strftime('%Y%m%d-%H%M%S')}")
    db = sqlite3.connect(args.db, timeout=30)
    db.row_factory = sqlite3.Row
    with sqlite3.connect(backup) as dst:
        db.backup(dst)
    print(f"backed up to {backup}")

    cursors = {}
    for f in args.runs.glob("*/build_state.json"):
        cursors[f.parent.name] = (f, json.loads(f.read_text(encoding="utf-8")))
    with db:
        db.execute("ALTER TABLE games ADD COLUMN stage TEXT NOT NULL DEFAULT 'designing'")
        db.execute("ALTER TABLE builds ADD COLUMN cursor TEXT")
        moved = 0
        for game_id, (_, cursor) in cursors.items():
            state = {k: v for k, v in cursor.items() if k not in RETIRED}
            moved += db.execute("UPDATE builds SET cursor = ? WHERE id = ? AND game_id = ?",
                                (json.dumps(state, ensure_ascii=False), cursor.get("build_id"),
                                 game_id)).rowcount
        print(f"cursors: {moved} moved of {len(cursors)} files")

        counts: dict = {}
        busy = {r[0] for r in db.execute(
            "SELECT DISTINCT game_id FROM jobs WHERE status IN ('pending', 'claimed')")}
        for game in db.execute("SELECT id, status FROM games").fetchall():
            stage, status = new_state(game["status"], (cursors.get(game["id"]) or (None, None))[1],
                                      game["id"] in busy)
            db.execute("UPDATE games SET stage = ?, status = ? WHERE id = ?",
                       (stage, status, game["id"]))
            key = f"{game['status']} -> {stage}/{status}"
            counts[key] = counts.get(key, 0) + 1
        for key, n in sorted(counts.items()):
            print(f"games: {key}: {n}")

        kinds = ",".join("?" * len(CURSOR_KINDS))
        latest = (f"(SELECT b2.id FROM builds b2 WHERE b2.game_id = builds.game_id AND b2.kind IN "
                  f"({kinds}) ORDER BY b2.queued_at DESC, b2.rowid DESC LIMIT 1)")
        live = "(SELECT status FROM games g WHERE g.id = builds.game_id) IN ('running', 'paused')"
        n = db.execute(
            "UPDATE builds SET status = 'stopped', finished_at = ? "
            "WHERE status IN ('queued', 'running') "
            f"AND NOT (kind IN ({kinds}) AND id = {latest} AND {live}) "
            "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.build_id = builds.id "
            "  AND j.status IN ('pending', 'claimed'))",
            (time.time(), *CURSOR_KINDS, *CURSOR_KINDS)).rowcount
        print(f"builds left open with nothing behind them: {n} ended")
    db.close()

    for f, _ in cursors.values():
        f.unlink()
    print(f"deleted {len(cursors)} build_state.json")


if __name__ == "__main__":
    main()
