"""The credit ledger and the storefront's purchase records."""

import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from db.connection import auth_db


def _log_txn(conn, user_id: str, delta: int, reason: str, run_id: Optional[str]) -> None:
    conn.execute(
        "INSERT INTO credit_transactions (id, user_id, delta, reason, run_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex[:16], user_id, delta, reason, run_id, time.time()),
    )
    from tools.db_backup import mark_dirty
    mark_dirty()


def balance(user_id: str) -> int:
    with auth_db() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["credits"] if row else 0


def _apply_grant(conn, user_id: str, n: int, reason: str, run_id: Optional[str]) -> int:
    conn.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (n, user_id))
    _log_txn(conn, user_id, n, reason, run_id)
    row = conn.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
    return row["credits"] if row else 0


def grant(user_id: str, n: int, reason: str = "grant", run_id: Optional[str] = None) -> int:
    with auth_db() as conn:
        return _apply_grant(conn, user_id, n, reason, run_id)


def deduct(user_id: str, n: int, reason: str, run_id: Optional[str] = None) -> bool:
    """Atomically remove `n` credits, refusing to go negative."""
    with auth_db() as conn:
        cur = conn.execute(
            "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?",
            (n, user_id, n),
        )
        if cur.rowcount != 1:
            return False
        _log_txn(conn, user_id, -n, reason, run_id)
    return True


def refund(user_id: str, n: int, reason: str, run_id: Optional[str] = None) -> int:
    return grant(user_id, n, reason, run_id)


@dataclass(frozen=True)
class Purchase:
    id: str
    user_id: str
    package_id: str
    credits: int
    usd_cents: int
    provider_ref: Optional[str]
    payment_intent: Optional[str]
    status: str
    created_at: float
    completed_at: Optional[float]


def _row_to_purchase(row: sqlite3.Row) -> Purchase:
    return Purchase(id=row["id"], user_id=row["user_id"], package_id=row["package_id"],
                    credits=row["credits"], usd_cents=row["usd_cents"],
                    provider_ref=row["provider_ref"], payment_intent=row["payment_intent"],
                    status=row["status"],
                    created_at=row["created_at"], completed_at=row["completed_at"])


def create_purchase(user_id: str, package_id: str, credits: int, usd_cents: int) -> Purchase:
    purchase = Purchase(id=uuid.uuid4().hex[:16], user_id=user_id, package_id=package_id,
                        credits=credits, usd_cents=usd_cents, provider_ref=None,
                        payment_intent=None, status="started", created_at=time.time(),
                        completed_at=None)
    with auth_db() as conn:
        conn.execute(
            "INSERT INTO purchases (id, user_id, package_id, credits, usd_cents, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (purchase.id, purchase.user_id, purchase.package_id, purchase.credits,
             purchase.usd_cents, purchase.status, purchase.created_at),
        )
    return purchase


def set_purchase_ref(purchase_id: str, provider_ref: str) -> None:
    with auth_db() as conn:
        conn.execute("UPDATE purchases SET provider_ref = ? WHERE id = ?",
                     (provider_ref, purchase_id))


def set_payment_intent(purchase_id: str, payment_intent: str) -> None:
    with auth_db() as conn:
        conn.execute("UPDATE purchases SET payment_intent = ? WHERE id = ?",
                     (payment_intent, purchase_id))


def purchase_by_payment_intent(payment_intent: str) -> Optional[Purchase]:
    with auth_db() as conn:
        row = conn.execute("SELECT * FROM purchases WHERE payment_intent = ?",
                           (payment_intent,)).fetchone()
    return _row_to_purchase(row) if row else None


def refund_purchase(purchase_id: str) -> Optional[int]:
    with auth_db() as conn:
        cur = conn.execute(
            "UPDATE purchases SET status = 'refunded' WHERE id = ? AND status = 'completed'",
            (purchase_id,),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT user_id, credits FROM purchases WHERE id = ?",
                           (purchase_id,)).fetchone()
        conn.execute("UPDATE users SET credits = credits - ? WHERE id = ?",
                     (row["credits"], row["user_id"]))
        _log_txn(conn, row["user_id"], -row["credits"], "purchase_refund", None)
        bal = conn.execute("SELECT credits FROM users WHERE id = ?",
                           (row["user_id"],)).fetchone()
        return bal["credits"] if bal else 0


def get_purchase(purchase_id: str) -> Optional[Purchase]:
    with auth_db() as conn:
        row = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    return _row_to_purchase(row) if row else None


def complete_purchase(purchase_id: str) -> Optional[int]:
    with auth_db() as conn:
        cur = conn.execute(
            "UPDATE purchases SET status = 'completed', completed_at = ? "
            "WHERE id = ? AND status = 'started'",
            (time.time(), purchase_id),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT user_id, credits FROM purchases WHERE id = ?",
                           (purchase_id,)).fetchone()
        return _apply_grant(conn, row["user_id"], row["credits"], "purchase", None)


def list_purchases(user_id: str) -> List[Purchase]:
    with auth_db() as conn:
        rows = conn.execute("SELECT * FROM purchases WHERE user_id = ? ORDER BY created_at DESC",
                            (user_id,)).fetchall()
    return [_row_to_purchase(r) for r in rows]


def purchase_totals(since: float) -> Dict[str, int]:
    with auth_db() as conn:
        started = conn.execute("SELECT COUNT(*) FROM purchases WHERE created_at >= ?",
                               (since,)).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(usd_cents), 0) FROM purchases "
            "WHERE status = 'completed' AND completed_at >= ?", (since,)).fetchone()
    return {"checkouts": started, "purchases": done[0], "revenue_cents": done[1]}
