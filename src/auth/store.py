"""Persistent user + session store (sqlite).
pbkdf2 passwords and opaque bearer tokens stored as sha256 — a leaked DB yields neither.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from db.connection import auth_db

_PBKDF2_ROUNDS = 600_000

# Length is the whole password rule. Composition requirements (a symbol, a digit, mixed case)
# shrink the search space an attacker must cover and push users toward one predictable shape.
MIN_PASSWORD_LENGTH = 10


# A session token stops resolving this long after it was issued, so a leaked token can't be
# used forever — a re-login mints a fresh one.
SESSION_TTL_SECONDS = 7 * 24 * 3600

# A reset link is a password-equivalent credential that arrives over mail, so it lives briefly.
RESET_TTL_SECONDS = 3600


@dataclass(frozen=True)
class User:
    id: str
    handle: str
    role: str = "user"
    email: str = ""


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, expected = stored.split("$", 1)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), expected)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_user(row: sqlite3.Row) -> User:
    return User(id=row["id"], handle=row["handle"], role=row["role"], email=row["email"])


class HandleTakenError(ValueError):
    pass


class EmailTakenError(ValueError):
    pass


# Deliberately permissive: something@something.tld and no spaces. A stricter pattern rejects
# addresses that deliver, and the only proof an address is real is mail arriving at it.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(email: str) -> str:
    email = (email or "").strip()
    if not _EMAIL.match(email):
        raise ValueError("that email address doesn't look right")
    return email


def check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def _insert_user(conn, handle: str, password: str, role: str, email: str) -> User:
    """The one place the handle/password/email rules live."""
    handle = handle.strip()
    if not handle:
        raise ValueError("handle is required")
    check_password(password)
    email = clean_email(email)
    user = User(id=uuid.uuid4().hex[:12], handle=handle, role=role, email=email)
    try:
        conn.execute(
            "INSERT INTO users (id, handle, email, password_hash, role, credits, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.id, user.handle, user.email, _hash_password(password), role, 0, time.time()),
        )
    except sqlite3.IntegrityError as e:
        # Two unique constraints reach here; the caller answers them differently.
        raise (EmailTakenError(f"email {email!r} already has an account")
               if "email" in str(e) else HandleTakenError(f"handle {handle!r} already exists"))
    from tools.db_backup import mark_dirty
    mark_dirty()
    return user


def create_user(handle: str, password: str, role: str = "user", email: str = "") -> User:
    with auth_db() as conn:
        return _insert_user(conn, handle, password, role, email)


def signup(handle: str, password: str, email: str = "") -> User:
    with auth_db() as conn:
        return _insert_user(conn, handle, password, "user", email)


def set_password(handle: str, password: str) -> None:
    """Set a password and END EVERY SESSION the account has."""
    check_password(password)
    with auth_db() as conn:
        cur = conn.execute("UPDATE users SET password_hash = ? WHERE handle = ?",
                           (_hash_password(password), handle))
        if cur.rowcount == 0:
            raise ValueError(f"no user {handle!r}")
        conn.execute("DELETE FROM sessions WHERE user_id = "
                     "(SELECT id FROM users WHERE handle = ?)", (handle,))
    from tools.db_backup import mark_dirty
    mark_dirty()


def set_email(user_id: str, email: str) -> str:
    email = clean_email(email)
    with auth_db() as conn:
        try:
            cur = conn.execute("UPDATE users SET email = ? WHERE id = ?", (email, user_id))
        except sqlite3.IntegrityError:
            raise EmailTakenError(f"email {email!r} already has an account")
        if cur.rowcount == 0:
            raise ValueError(f"no user {user_id!r}")
    from tools.db_backup import mark_dirty
    mark_dirty()
    return email


def get_user_by_handle(handle: str) -> Optional[User]:
    with auth_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(email: str) -> Optional[User]:
    with auth_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE lower(email) = lower(?)",
                           ((email or "").strip(),)).fetchone()
    return _row_to_user(row) if row else None


def list_users() -> List[User]:
    with auth_db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    return [_row_to_user(r) for r in rows]


def signup_times() -> List[float]:
    with auth_db() as conn:
        return [r[0] for r in conn.execute("SELECT created_at FROM users").fetchall()]


def authenticate(handle: str, password: str) -> Optional[User]:
    with auth_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE handle = ?", (handle,)).fetchone()
    if row is None or not _verify_password(password, row["password_hash"]):
        return None
    return _row_to_user(row)


def issue_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    with auth_db() as conn:
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
                     (_token_hash(token), user_id, time.time()))
    return token


def resolve_token(token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    cutoff = time.time() - SESSION_TTL_SECONDS
    with auth_db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.created_at > ?",
            (_token_hash(token), cutoff),
        ).fetchone()
    return _row_to_user(row) if row else None


def revoke_token(token: str) -> None:
    with auth_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


# ── Password resets: a mailed, single-use, short-lived credential ────────────────────────────


def issue_reset_token(user_id: str) -> str:
    """A fresh reset token; every earlier one for this account dies."""
    token = secrets.token_urlsafe(32)
    with auth_db() as conn:
        conn.execute("DELETE FROM password_resets WHERE user_id = ? AND used_at IS NULL",
                     (user_id,))
        conn.execute("INSERT INTO password_resets (token_hash, user_id, created_at) "
                     "VALUES (?, ?, ?)", (_token_hash(token), user_id, time.time()))
    return token


def consume_reset_token(token: str) -> Optional[User]:
    if not token:
        return None
    cutoff = time.time() - RESET_TTL_SECONDS
    with auth_db() as conn:
        cur = conn.execute(
            "UPDATE password_resets SET used_at = ? "
            "WHERE token_hash = ? AND used_at IS NULL AND created_at > ?",
            (time.time(), _token_hash(token), cutoff))
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT u.* FROM password_resets r JOIN users u ON u.id = r.user_id "
            "WHERE r.token_hash = ?", (_token_hash(token),)).fetchone()
    return _row_to_user(row) if row else None
