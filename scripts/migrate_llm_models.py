#!/usr/bin/env python3

import argparse
import sqlite3
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--entry", required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        raise SystemExit(f"no database at {args.db}")
    backup = args.db.with_name(f"{args.db.name}.pre-llm-models-{time.strftime('%Y%m%d-%H%M%S')}")
    src = sqlite3.connect(args.db, timeout=30)
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
    print(f"backed up to {backup}")
    entry = args.entry
    src.isolation_level = None
    src.execute("BEGIN")
    with src:
        src.execute("ALTER TABLE workers ADD COLUMN model TEXT")
        src.execute("ALTER TABLE pod_refusals ADD COLUMN model TEXT")
        src.execute("DROP INDEX IF EXISTS idx_jobs_claim")
        src.execute("DROP INDEX IF EXISTS idx_pod_refusals_queue")
        src.execute("CREATE INDEX idx_jobs_pool ON jobs(queue, model, status, created_at)")
        src.execute("CREATE INDEX idx_pod_refusals_pool ON pod_refusals(queue, model, kind, created_at)")
        src.execute("""CREATE TABLE pod_request_days_new (
            queue           TEXT NOT NULL,
            model           TEXT NOT NULL DEFAULT '',
            day             TEXT NOT NULL,
            attempts        INTEGER NOT NULL DEFAULT 0,
            stock_refusals  INTEGER NOT NULL DEFAULT 0,
            other_refusals  INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (queue, model, day))""")
        n = src.execute(
            "INSERT INTO pod_request_days_new (queue, model, day, attempts, stock_refusals, "
            "other_refusals) SELECT queue, CASE WHEN queue = 'llm' THEN ? ELSE '' END, day, "
            "attempts, stock_refusals, other_refusals FROM pod_request_days", (entry,)).rowcount
        src.execute("DROP TABLE pod_request_days")
        src.execute("ALTER TABLE pod_request_days_new RENAME TO pod_request_days")
        print(f"pod_request_days: {n} row(s) rekeyed")
        n = src.execute("UPDATE jobs SET model = CASE WHEN queue = 'llm' THEN ? END",
                        (entry,)).rowcount
        print(f"jobs: {n} row(s)")
        for table in ("workers", "pod_refusals"):
            n = src.execute(f"UPDATE {table} SET model = ? WHERE queue = 'llm'", (entry,)).rowcount
            print(f"{table}: {n} llm row(s)")
    src.close()


if __name__ == "__main__":
    main()
