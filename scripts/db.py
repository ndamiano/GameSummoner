#!/usr/bin/env python3
"""Read the platform DB from a terminal. Stdlib only, so it runs over ssh on the prod droplet."""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "platform.db"

QUERIES = {
    "cost": """
        SELECT substr(g.id,1,8) AS game, g.status,
               g.credits_spent AS credits,
               round(g.spent_micros/1e6, 3) AS usd,
               round(100.0*g.spent_micros/nullif(g.granted_micros,0), 1) AS pct_budget,
               (SELECT count(*) FROM builds b WHERE b.game_id=g.id) AS builds,
               (SELECT group_concat(DISTINCT j.model) FROM jobs j
                 WHERE j.game_id=g.id AND j.queue='llm') AS models
          FROM games g
         WHERE g.created_at > :since AND g.spent_micros > 0
         ORDER BY g.spent_micros DESC
    """,
    "builds": """
        SELECT substr(b.id,1,8) AS build, substr(b.game_id,1,8) AS game, b.kind, b.status,
               b.steps, round(b.spent_micros/1e6, 3) AS usd,
               round((b.finished_at - b.started_at)/60.0, 1) AS wall_min
          FROM builds b
         WHERE b.queued_at > :since
         ORDER BY b.queued_at DESC
    """,
    "models": """
        SELECT j.model, j.queue, j.gpu_type, count(*) AS jobs,
               round(sum(j.exec_seconds)/3600.0, 2) AS hours,
               round(avg(j.exec_seconds), 1) AS avg_sec
          FROM jobs j
         WHERE j.created_at > :since AND j.exec_seconds > 0
         GROUP BY j.model, j.queue, j.gpu_type
         ORDER BY hours DESC
    """,
    "failures": """
        SELECT queue, count(*) AS jobs,
               sum(status='failed') AS failed,
               round(100.0*sum(status='failed')/count(*), 1) AS pct
          FROM jobs
         WHERE created_at > :since AND status IN ('done','failed')
         GROUP BY queue
    """,
    "turns": """
        SELECT substr(j.game_id,1,8) AS game,
               substr(j.build_id,1,8) AS build,
               j.created_at,
               j.exec_seconds,
               json_extract(j.payload, '$.prompt_chars') AS prompt_chars,
               json_extract(j.payload, '$.n_messages') AS n_messages,
               json_extract(j.result, '$.usage.completion_tokens') AS completion_tokens,
               json_extract(j.result, '$.finish_reason') AS finish_reason,
               json_extract(j.result, '$.tool_names') AS tool_names
          FROM jobs j
         WHERE j.queue = 'llm' AND j.created_at > :since
         ORDER BY j.created_at DESC
    """,
}


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    return [c[0] for c in cur.description], cur.fetchall()


def _print(cols, rows):
    if not rows:
        print("(no rows)")
        return
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    width = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(width[i]) for i, c in enumerate(cols)))
    print("  ".join("-" * w for w in width))
    for row in cells:
        print("  ".join(row[i].ljust(width[i]) for i in range(len(cols))))
    print(f"\n{len(rows)} rows")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=[*QUERIES, "sql"])
    ap.add_argument("query", nargs="?", help="SQL text, for the `sql` command")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="named queries only (default: 7 days ago)")
    args = ap.parse_args(argv)

    if not args.db.exists():
        sys.exit(f"no such database: {args.db}")
    conn = sqlite3.connect(args.db)

    if args.command == "sql":
        if not args.query:
            sys.exit("sql needs a query: scripts/db.py sql \"select ...\"")
        return _print(*_rows(conn, args.query))

    since = (time.mktime(time.strptime(args.since, "%Y-%m-%d")) if args.since
             else time.time() - 7 * 86400)
    _print(*_rows(conn, QUERIES[args.command], {"since": since}))


if __name__ == "__main__":
    main()
