"""Auth wiring for the API: the request-gating middleware + the `get_current_user` dependency.

One app-level middleware gates every HTTP route (a valid bearer token is required for anything
that isn't explicitly public). The WebSocket path lives in a different ASGI scope that HTTP
middleware never sees, so it authenticates itself in the endpoint (see routers/websocket.py).
"""

import os
from typing import Optional

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse

from auth import playgrants
from auth.store import User, resolve_token

# Reachable without a token: the health probe, the login + signup endpoints, the
# payment webhook
# (server-to-server — no user token; authed by the provider's signature, verified inside the
# CreditProvider, never by this gate), and /handoff (it authenticates itself by redeeming a
# single-use play token — see auth/playgrants.py). The API docs exist only in dev (see
# api/app.py), so they're public only there.
PUBLIC_PATHS = {"/", "/auth/login", "/auth/signup", "/auth/forgot", "/auth/reset",
                "/api/billing/webhook", "/api/billing/packages", "/api/events/landing", "/handoff"}
# The landing page's demo surface: list + per-game session mint, both read-only and limited
# server-side to the owner-curated `demo_games` list (routers/demos.py).
PUBLIC_PREFIXES = ("/api/demos", "/api/shares")
if os.getenv("MAESTRO_DEV") == "1":
    PUBLIC_PATHS |= {"/docs", "/redoc", "/openapi.json"}

# A staged game is static HTML/JS the browser loads with plain <script>/<img>/fetch — no way to
# attach a Bearer header to those sub-resource requests. So /play alone authenticates by a grant
# cookie the browser sends automatically, minted by /handoff and scoped to Path=/play/games/<id>/
# so it never rides any /api or /auth request (the API stays header-only, cookie-immune, CSRF-
# immune) and never rides another game's requests either. This cookie is the single, deliberate
# deviation from the header-only model — see `_play_gate` below and auth/playgrants.py.
PLAY_COOKIE = "maestro_play"


def bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip() or None
    return None


def get_current_user(request: Request) -> User:
    """FastAPI dependency — returns the user the middleware attached to this request."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def require_admin(request: Request) -> User:
    """`get_current_user` plus the admin role. Gates the operator-only surfaces (queue stats,
    fleet spend). A signed-in non-admin gets a 403, not a 401 — they ARE authenticated, they just
    can't see this."""
    user = get_current_user(request)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def install_auth(app) -> None:
    """Register the auth gate on `app`. Every request except PUBLIC_PATHS (and CORS preflight)
    must carry a valid bearer token; the resolved user is attached to `request.state.user`."""

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in PUBLIC_PATHS \
                or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        # The static game harness — cookie-gated, ownership-checked (a separate model, see below).
        if path == "/play" or path.startswith("/play/"):
            return await _play_gate(request, call_next)
        # The API lives under /api and /auth; everything else is the static SPA shell + its
        # assets, which the browser must fetch (unauthenticated) before it can even show the
        # login form. So gate only the API surfaces — the frontend is served in the clear.
        if not path.startswith("/api") and not path.startswith("/auth"):
            return await call_next(request)
        # Header-only: a token never rides in the URL, so it can't leak into access logs, browser
        # history, or Referer. Browser <img>/download fetches attach the header via authed fetch +
        # blob. The WebSocket (a separate ASGI scope this middleware never sees) authenticates
        # itself from its own `token` query param — see routers/websocket.py.
        token = bearer_token(request)
        user = resolve_token(token)
        if user is None:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        request.state.user = user
        return await call_next(request)

    async def _play_gate(request: Request, call_next):
        """Gate a /play request by the grant cookie (never the Authorization header — the static
        game can't set one on its sub-resource fetches). A grant is minted by /handoff after the
        ownership check, names ONE game, and the browser's own Path scoping means game A's
        requests never even carry game B's grant; the run-id check here is the server-side half
        of that same rule, so a hand-crafted request can't stretch a grant either."""
        parts = request.url.path.split("/")  # ["", "play", "games"|"demos", "<id>", ...]
        if len(parts) < 4 or parts[2] not in playgrants.SURFACES:
            return JSONResponse(status_code=404, content={"detail": "not found"})
        grant = playgrants.resolve_grant(request.cookies.get(PLAY_COOKIE))
        if grant is None:
            return JSONResponse(status_code=401, content={"detail": "authentication required"})
        if grant != f"{parts[2]}/{parts[3]}":
            return JSONResponse(status_code=403, content={"detail": "not this game's grant"})
        return await call_next(request)
