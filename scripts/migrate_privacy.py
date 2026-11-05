#!/usr/bin/env python3
"""Add the private column to games and users, every existing row private."""

import argparse
import sqlite3
import time
from pathlib import Path


def migrate(db: Path, table: str) -> None:
    backup = db.with_name(f"{db.name}.pre-privacy-{time.strftime('%Y%m%d-%H%M%S')}")
    src = sqlite3.connect(db, timeout=30)
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
    print(f"backed up to {backup}")
    with src:
        src.execute(f"ALTER TABLE {table} ADD COLUMN private INTEGER NOT NULL DEFAULT 1")
    n = src.execute(f"SELECT COUNT(*) FROM {table} WHERE private = 1").fetchone()[0]
    print(f"{table}: {n} row(s) private")
    src.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args()
    for name in ("platform.db", "auth.db"):
        if not (args.data_dir / name).is_file():
            raise SystemExit(f"no database at {args.data_dir / name}")
    for name, table in (("platform.db", "games"), ("auth.db", "users")):
        migrate(args.data_dir / name, table)


if __name__ == "__main__":
    main()
