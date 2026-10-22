import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

# Interactive API docs are a dev convenience; in prod they hand an attacker the full route +
# schema map, so they only exist when MAESTRO_DEV=1 (same switch run.py keys reload on).
_dev = os.getenv("MAESTRO_DEV") == "1"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await event_bus.start()
    logging.info("Event bus started")

    # Unconditional, unlike the autoscaler below: a wedged job, a dropped asset finalize, or a
    # stuck build (its driver process died mid-turn) needs reaping on the home box too.
    app.state.reaper = Reaper()
    app.state.reaper.start()
    logging.info("Queue reaper started")
    await asyncio.to_thread(build_chain.recover)

    app.state.db_backup = DbBackup()
    app.state.db_backup.start()

    _settings = settings_manager.get_settings()
    _rp, _aws = _settings.get("runpod") or {}, _settings.get("aws") or {}
    _providers = []
    if _rp.get("enabled") and _rp.get("api_key"):
        _providers.append(RunPodProvider(RunPodClient(_rp["api_key"]),
                                         settings_manager.get_settings))
    if _aws.get("enabled") and _aws.get("access_key"):
        _providers.append(Ec2Provider(Ec2Client(_aws["access_key"], _aws["secret_key"]),
                                      settings_manager.get_settings))
    if _providers:
        app.state.autoscaler = Autoscaler(SqliteStatsSource(), _providers,
                                          settings_manager.get_settings)
        app.state.autoscaler.start()
        logging.info("autoscaler started on %s", ", ".join(p.name for p in _providers))

    yield

    if getattr(app.state, "autoscaler", None):
        app.state.autoscaler.stop()
    app.state.db_backup.stop()
    app.state.reaper.stop()
    await event_bus.shutdown()
    logging.info("Event bus stopped")


app = FastAPI(
    lifespan=_lifespan,
    title="AI Agent API",
    description="API for AI agent management and task execution",
    version="1.0.0",
    docs_url="/docs" if _dev else None,
    redoc_url="/redoc" if _dev else None,
    openapi_url="/openapi.json" if _dev else None,
)

# Allowed browser origins come from MAESTRO_CORS_ORIGINS (comma-separated); default to the local
# dev servers. Auth is a Bearer header, not a cookie, so credentials are off — a pinned origin
# list is what keeps other sites from scripting the API on a logged-in user's behalf.
_cors_origins = [
    o.strip() for o in
    os.getenv("MAESTRO_CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from api.routers import (admin, billing, demos, events, games, grades, shares, websocket,
                         workqueue)
from api.websocket.event_bus import event_bus
from auth.deps import install_auth
from auth.router import router as auth_router
from config.settings_manager import settings_manager
from maestro.codegen import build_chain
from workqueue.reaper import Reaper
from tools.db_backup import DbBackup
from scaler.autoscaler import Autoscaler
from scaler.ec2_client import Ec2Client
from scaler.providers import Ec2Provider, RunPodProvider
from scaler.runpod_client import RunPodClient
from scaler.stats import SqliteStatsSource

# Gate every route behind a valid bearer token (public paths + the WebSocket handle themselves).
install_auth(app)

@app.get("/healthz", response_model=Dict[str, Any])
async def healthz():
    """Liveness probe. (`/` serves the SPA in a deployed build, so health lives here.)"""
    return {"status": "healthy", "server": "running"}

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(websocket.router, prefix="/api", tags=["websocket"])
app.include_router(games.router, prefix="/api/games", tags=["games"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
# Public by design (auth/deps.py PUBLIC_PREFIXES) — the landing page's demo games.
app.include_router(demos.router, prefix="/api/demos", tags=["demos"])
app.include_router(shares.router, prefix="/api/shares", tags=["shares"])
app.include_router(billing.router, prefix="/api/billing", tags=["billing"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(grades.router, prefix="/api/admin/grades", tags=["admin"])
# Outside the /api user gate on purpose — workers auth with the shared workqueue token.
app.include_router(workqueue.router, prefix="/worker", tags=["workqueue"])

# Serve the built frontend same-origin (one process, one Funnel port, no CORS). Mounted LAST so
# the API routers above win; skipped when dist/ is absent (dev runs the Vite server instead).
import re
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from auth import playgrants
from auth.deps import PLAY_COOKIE
from maestro import shares as share_store

# Serve staged games so the SPA can open a built one (mounted before the SPA catch-all).
# /play runs MODEL-AUTHORED JS, so every response carries a CSP that pins scripted loads and network
# to this origin. This is NOT full exfiltration protection: top-level navigation is governed by no
# CSP directive (`navigate-to` was specified and abandoned), so a `location =` to an external URL
# still leaves. form-action must be set explicitly — it does NOT fall back to default-src.
# 'unsafe-inline' is for index.html's own bootstrap script (and the injected reporter);
# eval stays blocked. blob: + data: in img-src (blob: in connect-src too) are for GLTFLoader's
# embedded GLB textures: it decodes them through same-document object URLs (ImageBitmapLoader
# fetches them, so connect-src governs as well), and EXT_texture_webp's support DETECTION loads a
# 1-px data: probe image — blocking either rejects the whole GLB and the game silently plays as
# bare boxes (shipped, twice: first blob:, then the data: probe). Neither grants anything
# cross-origin — the bytes already live in the page.
# frame-ancestors is the app origin and nothing else: the SPA's game page frames the game, and no
# other site may. With `play.origin` set, games live on their own registrable domain where 'self'
# would be the WRONG origin — the app origin is named explicitly; unset, the two are one origin.
_PLAY_CSP_BASE = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                  "connect-src 'self' blob:; img-src 'self' blob: data:; "
                  "style-src 'self' 'unsafe-inline'; "
                  "object-src 'none'; base-uri 'none'; frame-ancestors {frame_ancestors}; "
                  "form-action 'none'")


def _play_settings() -> tuple[str, str]:
    play = settings_manager.get_settings().get("play") or {}
    return (play.get("origin", "") or "").rstrip("/"), (play.get("app_origin", "") or "").rstrip("/")


def _play_csp_header() -> str:
    _, app_origin = _play_settings()
    return _PLAY_CSP_BASE.format(frame_ancestors=app_origin or "'self'")


@app.middleware("http")
async def _play_csp(request, call_next):
    response = await call_next(request)
    if request.url.path == "/play" or request.url.path.startswith("/play/"):
        response.headers["Content-Security-Policy"] = _play_csp_header()
    return response


# With games on their own domain, one process serves two hostnames — and the split only isolates
# if each host serves ONLY its own surface. A game's fetch('/api/…') resolves to the GAME host, so
# the game host must hold no API; and the app host must serve no game, or the isolation is opt-in.
@app.middleware("http")
async def _host_split(request, call_next):
    play_origin, _ = _play_settings()
    if play_origin:
        path = request.url.path
        on_game_surface = (path == "/handoff" or path == "/play" or path.startswith("/play/"))
        host = request.headers.get("host", "").lower()
        if host == urlsplit(play_origin).netloc.lower():
            if not on_game_surface and path != "/healthz":
                return Response(status_code=404)
        elif on_game_surface:
            return Response(status_code=404)
    return await call_next(request)


_GRANT_COOKIE_TMPL = (PLAY_COOKIE + "={token}; Max-Age={max_age}; Path=/play/{target}/; "
                      "HttpOnly; {context}")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _grant_cookie_context(host: str) -> str:
    """`Secure; SameSite=None; Partitioned` is what an iframed game on its own domain needs, and a
    browser stores none of it over plain http — which is every localhost dev box, where the game is
    same-site anyway and Lax is both sufficient and storable."""
    if host.split(":")[0].lower() in _LOCAL_HOSTS:
        return "SameSite=Lax"
    return "Secure; SameSite=None; Partitioned"


@app.get("/handoff", include_in_schema=False)
async def handoff(request: Request, t: str = ""):
    """The game origin's front door: redeem a single-use play token (minted on the app origin by
    POST /api/games/<id>/play-session, after the ownership check) and 302 into the game, setting
    the grant cookie its sub-resource loads will ride. The cookie is HttpOnly + host-only +
    Path-scoped to this one game — the game origin never holds a credential its JS can read, and
    game A never sends game B's grant. SameSite=None + Partitioned because the game runs in an
    iframe on the app origin: a cross-site subresource context, where Lax/Strict cookies are
    never sent and unpartitioned third-party cookies are blocked outright."""
    target = playgrants.redeem_handoff(t)
    if target is None:
        raise HTTPException(status_code=403, detail="expired or invalid play token")
    grant = playgrants.issue_grant(target)
    if grant is None:
        raise HTTPException(status_code=429, detail="too many open sessions — try again shortly")
    response = RedirectResponse(f"/play/{target}/index.html", status_code=302)
    response.headers.append("set-cookie", _GRANT_COOKIE_TMPL.format(
        token=grant, max_age=playgrants.GRANT_TTL_SECONDS, target=target,
        context=_grant_cookie_context(request.headers.get("host", ""))))
    return response


_runtime = Path(__file__).resolve().parents[2] / "runtime"
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_HEAD_TAG = re.compile(r"<head[^>]*>", re.IGNORECASE)
_reporter_js = (Path(__file__).parent / "static" / "report.js").read_text(encoding="utf-8")


@app.get("/play/{surface}/{run_id}/index.html", include_in_schema=False)
async def play_index(surface: str, run_id: str):
    """Serve a game's index.html with the console reporter injected on the way out. The model
    cannot be relied on to include the tag, and staging stays "no bundle, no transform" — the
    game folder on disk is exactly what the model wrote. Registered before the /play static
    mount, so this route wins for index.html and the mount serves everything else."""
    if surface not in playgrants.SURFACES or not _RUN_ID.match(run_id):
        raise HTTPException(status_code=404, detail="not found")
    index = _runtime / surface / run_id / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="not found")
    html = index.read_text(encoding="utf-8", errors="replace")
    tag = f"<script>{_reporter_js}</script>"
    match = _HEAD_TAG.search(html)
    if match:
        html = html[:match.end()] + tag + html[match.end():]
    else:
        html = tag + html
    return HTMLResponse(html)


if _runtime.is_dir():
    app.mount("/play", StaticFiles(directory=_runtime, html=True), name="play")

_OG_META = re.compile(r'\s*<meta (?:property="og:(?:title|url|image)"|name="twitter:image")[^>]*>')


def share_card(html: str, share_id: str, base: str) -> str:
    base = base.rstrip("/")
    title = escape(share_store.meta(share_id).get("title") or "A GameSummoner game", quote=True)
    tags = [f'<meta property="og:title" content="{title} — made with GameSummoner" />',
            f'<meta property="og:url" content="{base}/g/{share_id}" />']
    if share_store.thumb(share_id):
        image = f"{base}/api/shares/{share_id}/thumb"
        tags += [f'<meta property="og:image" content="{image}" />',
                 f'<meta name="twitter:image" content="{image}" />']
    return _OG_META.sub("", html).replace("</head>", "    " + "\n    ".join(tags) + "\n  </head>", 1)


_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.is_dir():
    from fastapi.responses import FileResponse

    _dist_root = _frontend_dist.resolve()

    @app.get("/g/{share_id}", include_in_schema=False)
    async def shared_game_page(share_id: str, request: Request):
        html = (_dist_root / "index.html").read_text(encoding="utf-8")
        if not share_store.exists(share_id):
            return HTMLResponse(html, status_code=404)
        return HTMLResponse(share_card(html, share_id, _play_settings()[1] or str(request.base_url)))

    # The SPA routes by URL (/game/<id>, /settings, …), so a deep link or reload must get
    # index.html back, not a 404 — a plain StaticFiles mount only serves paths that exist on disk.
    @app.get("/{spa_path:path}", include_in_schema=False)
    async def spa(spa_path: str):
        if spa_path.split("/", 1)[0] in {"api", "auth", "worker", "play"}:
            raise HTTPException(status_code=404, detail="not found")
        candidate = (_dist_root / spa_path).resolve() if spa_path else _dist_root
        if candidate.is_file() and candidate.is_relative_to(_dist_root):
            return FileResponse(candidate)
        return FileResponse(_dist_root / "index.html")

__all__ = ["app"]
