"""The auth gate as it actually runs on the app: every router rejects an anonymous request,
a valid token gets through, the WebSocket authenticates itself, and the only account-creation
surface is the open /auth/signup."""

import pytest
from starlette.websockets import WebSocketDisconnect

from auth import store


# One path per mounted router; the gate is a single app-level middleware, so covering the
# routers proves the whole surface.
GATED_PATHS = [
    "/api/games",
    "/api/events",
    "/api/billing",
    "/api/admin",
]


@pytest.mark.parametrize("path", GATED_PATHS)
def test_anonymous_request_is_rejected(app_client, path):
    assert app_client.get(path).status_code == 401


def test_health_and_login_are_public(app_client):
    # the SPA shell must bypass the gate; the mount only exists when frontend/dist is
    # built, so the contract here is "not 401", not "200" (404 on an unbuilt checkout is fine).
    assert app_client.get("/").status_code != 401
    # login reached without a token (422 = body validation, i.e. it got PAST the gate).
    assert app_client.post("/auth/login").status_code == 422


def test_valid_token_gets_through_the_gate(app_client):
    store.create_user("alice", "pw-pass1234", email="alice@example.com")
    token = store.issue_token(store.get_user_by_handle("alice").id)
    r = app_client.get("/api/games", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == []


def test_bad_token_is_rejected(app_client):
    r = app_client.get("/api/games", headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


def test_api_docs_do_not_exist_outside_dev(app_client):
    """MAESTRO_DEV is unset in tests, so this asserts the PROD shape: the docs routes are never
    registered (schema enumeration for free) and their paths aren't whitelisted by the gate. The
    SPA fallback may answer these paths with the app shell (or 404 without a dist build) — what
    must never come back is the interactive docs or the schema."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        r = app_client.get(path)
        assert r.status_code in (200, 404), path
        assert "swagger" not in r.text.lower() and "openapi" not in r.text.lower(), path
    paths = {getattr(r, "path", "") for r in app_client.app.routes}
    assert not paths & {"/docs", "/redoc", "/openapi.json"}


def test_signup_is_the_only_account_creation_route(app_client):
    paths = {getattr(r, "path", "") for r in app_client.app.routes}
    assert "/auth/login" in paths
    assert "/auth/signup" in paths
    # signup reached without a token (422 = body validation, i.e. it got PAST the gate).
    assert app_client.post("/auth/signup").status_code == 422
    # /worker/deregister is worker-fleet plumbing, not a signup surface.
    assert not any("register" in p and not p.endswith("/deregister") for p in paths)


def test_websocket_requires_a_token(app_client):
    with pytest.raises(WebSocketDisconnect):
        with app_client.websocket_connect("/api/ws"):
            pass


def test_websocket_accepts_a_valid_token(app_client):
    store.create_user("alice", "pw-pass1234", email="alice2@example.com")
    token = store.issue_token(store.get_user_by_handle("alice").id)
    with app_client.websocket_connect(f"/api/ws?token={token}") as ws:
        assert ws.receive_json()["type"] == "connected"
