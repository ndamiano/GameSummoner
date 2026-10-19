"""The append-only event log and the safety violation record."""

import json
import time
from typing import Dict, List, Optional

from db.connection import platform_db


def record_event(game_id: str, kind: str, payload: Dict, build_id: Optional[str] = None) -> None:
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO events (game_id, build_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (game_id, build_id, kind, json.dumps(payload, ensure_ascii=False, default=str),
             time.time()),
        )


def events_for(game_id: str, after_id: int = 0, limit: int = 500) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE game_id = ? AND user_id IS NULL AND id > ? "
            "ORDER BY id LIMIT ?",
            (game_id, after_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
        out.append(d)
    return out


def record_user_events(user_id: str, rows: List[Dict]) -> None:
    """Batch-insert user-action analytics rows: {kind, payload, game_id?, created_at}."""
    with platform_db() as conn:
        conn.executemany(
            "INSERT INTO events (game_id, user_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r.get("game_id"), user_id, r["kind"],
              json.dumps(r.get("payload") or {}, ensure_ascii=False, default=str),
              r["created_at"]) for r in rows],
        )


def user_event_rollup(since: float) -> Dict:
    with platform_db() as conn:
        kinds = conn.execute(
            "SELECT date(created_at, 'unixepoch') AS day, kind, COUNT(*) AS n "
            "FROM events WHERE user_id IS NOT NULL AND created_at >= ? "
            "GROUP BY day, kind ORDER BY day",
            (since,)).fetchall()
        users = conn.execute(
            "SELECT date(created_at, 'unixepoch') AS day, COUNT(DISTINCT user_id) AS n "
            "FROM events WHERE user_id IS NOT NULL AND user_id != 'anon' AND created_at >= ? "
            "GROUP BY day ORDER BY day",
            (since,)).fetchall()
        active = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM events "
            "WHERE user_id IS NOT NULL AND user_id != 'anon' AND created_at >= ?",
            (since,)).fetchone()[0]
        shares = conn.execute(
            "SELECT game_id, kind, json_extract(payload, '$.referrer') AS host, COUNT(*) AS n, "
            "MAX(created_at) AS last_at FROM events "
            "WHERE kind IN ('share_viewed', 'share_played') AND game_id IS NOT NULL "
            "AND created_at >= ? GROUP BY game_id, kind, host",
            (since,)).fetchall()
    return {"kinds": [dict(r) for r in kinds], "users": [dict(r) for r in users],
            "active": active, "shares": [dict(r) for r in shares]}


def record_violation(user_id: Optional[str], game_id: Optional[str], source: str,
                     category: str, matched: str) -> None:
    """One safety refusal, keyed to the user; the matched terms only, never the text."""
    with platform_db() as conn:
        conn.execute(
            "INSERT INTO violations (user_id, game_id, source, category, matched, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, game_id, source, category, matched, time.time()),
        )


def list_violations(limit: int = 200) -> List[Dict]:
    with platform_db() as conn:
        rows = conn.execute(
            "SELECT * FROM violations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
