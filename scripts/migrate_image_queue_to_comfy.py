#!/usr/bin/env python3
"""One-shot: back up the platform DB, then rename the `image` queue to `comfy` in every table
that names a queue. Stdlib only, so it runs over ssh on the droplet.

    scripts/migrate_image_queue_to_comfy.py --db <platform.db>
"""

import argparse
import sqlite3
import time
from pathlib import Path

TABLES = ("jobs", "workers", "pod_refusals", "pod_request_days")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    db = parser.parse_args().db
    if not db.is_file():
        raise SystemExit(f"no database at {db}")
    backup = db.with_name(f"{db.name}.pre-comfy-{time.strftime('%Y%m%d-%H%M%S')}")
    src = sqlite3.connect(db, timeout=30)
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
    print(f"backed up to {backup}")
    with src:
        for table in TABLES:
            n = src.execute(f"UPDATE {table} SET queue = 'comfy' WHERE queue = 'image'").rowcount
            print(f"{table}: {n} row(s)")
    src.close()


if __name__ == "__main__":
    main()
