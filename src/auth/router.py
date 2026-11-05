"""Auth endpoints. Login, plus open self-serve signup — anyone can create an account; admins can
also provision one by hand (`python -m auth.cli`)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import store
from auth.deps import bearer_token, get_current_user
from auth.ratelimit import login_throttle, reset_throttle, signup_throttle
from auth.store import User
from billing import ledger
from config.settings_manager import settings_manager
from tools import mailer

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    handle: str
    password: str


@router.post("/login")
async def login(body: LoginRequest):
    key = body.handle.strip().lower()
    wait = login_throttle.retry_after(key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="too many login attempts, try again later",
            headers={"Retry-After": str(wait)},
        )
    user = store.authenticate(body.handle, body.password)
    if user is None:
        login_throttle.record_failure(key)
        raise HTTPException(status_code=401, detail="invalid handle or password")
    login_throttle.clear(key)
    token = store.issue_token(user.id)
    return {
        "token": token,
        "user": {"id": user.id, "handle": user.handle, "role": user.role},
    }


class SignupRequest(BaseModel):
    handle: str
    password: str
    email: str


@router.post("/signup")
async def signup(body: SignupRequest, request: Request):
    """Create an account and sign it in. Throttled per client IP (the login throttle's shape —
    see auth/ratelimit.py): open signup's exposure is bulk account creation, and the throttle is
    what slows a scripted registrar."""
    key = request.client.host if request.client else "unknown"
    wait = signup_throttle.retry_after(key)
    if wait:
        raise HTTPException(
            status_code=429,
            detail="too many signup attempts, try again later",
            headers={"Retry-After": str(wait)},
        )
    if not body.handle.strip():
        raise HTTPException(status_code=400, detail="handle is required")
    try:
        user = store.signup(body.handle, body.password, body.email)
    except store.HandleTakenError:
        signup_throttle.record_failure(key)
        raise HTTPException(status_code=409, detail="that handle is already taken")
    except store.EmailTakenError:
        signup_throttle.record_failure(key)
        raise HTTPException(status_code=409, detail="that email already has an account")
    except ValueError as e:
        # A malformed email or a short password: the form's own fault, not the code's, so the
        # throttle stays out of it — a typo must not cost someone their signup attempts.
        raise HTTPException(status_code=400, detail=str(e))
    signup_throttle.clear(key)
    token = store.issue_token(user.id)
    return {
        "token": token,
        "user": {"id": user.id, "handle": user.handle, "role": user.role},
    }


@router.post("/logout")
async def logout(request: Request, user: User = Depends(get_current_user)):
    """Revoke the presented session token server-side, so it can't be reused after logout
    (dropping it client-side alone would leave it live until its TTL). Play grants are not
    touched: they are per-game, path-scoped, and expire on their own short TTL."""
    token = bearer_token(request)
    if token:
        store.revoke_token(token)
    return {"ok": True}


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/password")
async def change_password(body: PasswordChangeRequest, user: User = Depends(get_current_user)):
    """Change the signed-in user's password, gated on the current one — a stolen bearer token
    alone must not be enough to take the account over. Wrong guesses share the login throttle,
    so the current-password check can't be brute-forced faster than the login form."""
    key = user.handle.strip().lower()
    wait = login_throttle.retry_after(key)
    if wait:
        raise HTTPException(status_code=429, detail="too many attempts, try again later",
                            headers={"Retry-After": str(wait)})
    if store.authenticate(user.handle, body.current_password) is None:
        login_throttle.record_failure(key)
        raise HTTPException(status_code=403, detail="current password is wrong")
    login_throttle.clear(key)
    try:
        store.set_password(user.handle, body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # set_password ends every session including this one — the caller changed their password,
    # they did not ask to be signed out of the tab they did it in.
    return {"ok": True, "token": store.issue_token(user.id)}


class EmailChangeRequest(BaseModel):
    password: str
    email: str


@router.post("/email")
async def change_email(body: EmailChangeRequest, user: User = Depends(get_current_user)):
    """Change the address a reset link would go to — gated on the password, since an attacker
    holding only a session token could otherwise point recovery at themselves."""
    key = user.handle.strip().lower()
    wait = login_throttle.retry_after(key)
    if wait:
        raise HTTPException(status_code=429, detail="too many attempts, try again later",
                            headers={"Retry-After": str(wait)})
    if store.authenticate(user.handle, body.password) is None:
        login_throttle.record_failure(key)
        raise HTTPException(status_code=403, detail="password is wrong")
    login_throttle.clear(key)
    try:
        email = store.set_email(user.id, body.email)
    except store.EmailTakenError:
        raise HTTPException(status_code=409, detail="that email already has an account")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "email": email}


class ForgotRequest(BaseModel):
    email: str


@router.post("/forgot")
async def forgot_password(body: ForgotRequest):
    """Mail a reset link. The answer is the SAME whether or not the address has an account —
    a differing response (or a differing latency shape) turns this into a membership oracle for
    the whole user list. Throttled per address so it cannot be used to mail-bomb someone."""
    email = (body.email or "").strip()
    key = f"forgot:{email.lower()}"
    if reset_throttle.retry_after(key):
        return {"ok": True}
    reset_throttle.record_failure(key)

    user = store.get_user_by_email(email)
    if user is not None and mailer.configured():
        token = store.issue_reset_token(user.id)
        try:
            mailer.send(user.email, "Reset your GameSummoner password", _reset_body(token))
        except Exception:
            logger.exception("reset mail failed for user %s", user.id)
    elif user is not None:
        logger.error("password reset requested but no smtp is configured")
    return {"ok": True}


def _reset_body(token: str) -> str:
    origin = (settings_manager.get_settings().get("play") or {}).get("app_origin") or ""
    link = f"{origin.rstrip('/')}/reset?t={token}"
    return (
        "Someone asked to reset the password on your GameSummoner account.\n\n"
        f"{link}\n\n"
        f"The link works once and expires in {store.RESET_TTL_SECONDS // 60} minutes.\n"
        "If this wasn't you, ignore this message — nothing has changed.\n"
    )


class ResetRequest(BaseModel):
    token: str
    new_password: str


@router.post("/reset")
async def reset_password(body: ResetRequest):
    """Spend a mailed reset token and set the new password. The token is single-use in the
    store, and setting the password ends every existing session — a reset is exactly the moment
    someone else's session must die."""
    try:
        store.check_password(body.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    user = store.consume_reset_token(body.token)
    if user is None:
        raise HTTPException(status_code=400, detail="that reset link is invalid or has expired")
    store.set_password(user.handle, body.new_password)
    return {"token": store.issue_token(user.id),
            "user": {"id": user.id, "handle": user.handle, "role": user.role}}


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    """The authed user + their live credit balance — the header's balance source. Gated: no token
    (or a bad one) never reaches here, the middleware 401s first."""
    return {
        "id": user.id,
        "handle": user.handle,
        "role": user.role,
        "email": user.email,
        "private": user.private,
        "balance": ledger.balance(user.id),
    }


class PrivacyRequest(BaseModel):
    private: bool


@router.post("/privacy")
async def change_privacy(body: PrivacyRequest, user: User = Depends(get_current_user)):
    """The default a new game starts with; each game keeps the choice it was created with."""
    store.set_private(user.id, body.private)
    return {"ok": True, "private": body.private}
