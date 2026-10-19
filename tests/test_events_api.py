"""The analytics intake (POST /api/events) and the admin rollup (GET /api/admin/analytics):
batched writes attributed to the authed user, allowlist + cap enforced by dropping rows (never
400ing the batch), and the user-action stream kept disjoint from the build/spec event log."""

import sqlite3
import time

from auth import store
from billing import ledger
from db import events


def _token(handle="alice", role="user"):
    store.create_user(handle, "pw-pass1234", role=role, email=f"{handle}@example.com")
    return store.issue_token(store.get_user_by_handle(handle).id)


def _post(client, token, rows):
    return client.post("/api/events", json=rows,
                       headers={"Authorization": f"Bearer {token}"})


def _raw_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "platform.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def test_batch_lands_attributed_to_the_caller(app_client, isolated_dbs):
    token = _token()
    user_id = store.get_user_by_handle("alice").id
    now_ms = time.time() * 1000
    r = _post(app_client, token, [
        {"kind": "page_view", "payload": {"path": "/"}, "ts": now_ms},
        {"kind": "game_played", "payload": {"mode": "embed"}, "ts": now_ms, "run_id": "run-1"},
    ])
    assert r.status_code == 200
    assert r.json() == {"accepted": 2}

    rows = _raw_rows(isolated_dbs)
    assert [row["kind"] for row in rows] == ["page_view", "game_played"]
    assert all(row["user_id"] == user_id for row in rows)
    assert rows[0]["game_id"] is None
    assert rows[1]["game_id"] == "run-1"


def test_bad_rows_are_dropped_not_400(app_client, isolated_dbs):
    token = _token()
    r = _post(app_client, token, [
        {"kind": "not_a_kind", "payload": {}},
        "not even a dict",
        {"kind": "page_view", "payload": "not a dict"},
        {"kind": "page_view", "payload": {"big": "x" * 3000}},
        {"kind": "change_sent", "payload": {"length": 12}},
    ])
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}
    assert [row["kind"] for row in _raw_rows(isolated_dbs)] == ["change_sent"]


def test_batch_is_capped(app_client, isolated_dbs):
    token = _token()
    r = _post(app_client, token, [{"kind": "page_view", "payload": {}}] * 150)
    assert r.json() == {"accepted": 100}
    assert len(_raw_rows(isolated_dbs)) == 100


def test_insane_client_timestamp_takes_server_time(app_client, isolated_dbs):
    token = _token()
    _post(app_client, token, [
        {"kind": "page_view", "ts": 12345},                       # 1970, in ms
        {"kind": "page_view", "ts": (time.time() + 9999) * 1000}, # far future
    ])
    now = time.time()
    for row in _raw_rows(isolated_dbs):
        assert abs(row["created_at"] - now) < 60


def test_bad_run_id_is_stored_unattached(app_client, isolated_dbs):
    token = _token()
    _post(app_client, token, [{"kind": "build_started", "run_id": "../etc/passwd"}])
    assert _raw_rows(isolated_dbs)[0]["game_id"] is None


def test_user_rows_never_leak_into_the_game_event_log():
    events.record_event("g1", "build_step", {"step": 1})
    events.record_user_events("u1", [
        {"kind": "game_played", "payload": {}, "game_id": "g1", "created_at": time.time()}])
    assert [e["kind"] for e in events.events_for("g1")] == ["build_step"]


def test_rollup_fills_fixed_columns_per_day_and_counts_users_and_purchases(app_client):
    now = time.time()
    events.record_user_events("u1", [
        {"kind": "page_view", "payload": {}, "created_at": now},
        {"kind": "build_started", "payload": {}, "created_at": now},
        {"kind": "game_played", "payload": {}, "created_at": now - 86400},
    ])
    events.record_user_events("u2", [{"kind": "page_view", "payload": {}, "created_at": now}])
    events.record_user_events("anon", [
        {"kind": "demo_played", "payload": {}, "game_id": "d1", "created_at": now},
        {"kind": "demo_played", "payload": {}, "game_id": "d1", "created_at": now - 86400},
        {"kind": "landing_view", "payload": {}, "created_at": now - 40 * 86400},
    ])
    events.record_event("g1", "build_step", {"step": 1})

    token = _token("root", "admin")
    buyer = store.get_user_by_handle("root").id
    paid, refunded, _abandoned = (ledger.create_purchase(buyer, "p", 10, cents)
                                  for cents in (500, 900, 2000))
    ledger.complete_purchase(paid.id)
    ledger.complete_purchase(refunded.id)
    ledger.refund_purchase(refunded.id)
    body = app_client.get("/api/admin/analytics?days=3",
                          headers={"Authorization": f"Bearer {token}"}).json()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    zero = {"landing": 0, "demo_plays": 0, "share_views": 0, "share_plays": 0,
            "own_plays": 0, "builds": 0}
    assert [d["day"] for d in body["days"]][0] == today and len(body["days"]) == 3
    assert body["days"][0] == {"day": today, **zero, "signups": 1, "active": 2,
                               "demo_plays": 1, "builds": 1}
    assert body["days"][1]["own_plays"] == 1 and body["days"][1]["active"] == 1
    assert body["days"][2] == {**body["days"][2], **zero, "signups": 0, "active": 0}
    assert body["totals"] == {**zero, "users": 1, "signups": 1, "active": 2,
                              "demo_plays": 2, "own_plays": 1, "builds": 1,
                              "checkouts": 3, "purchases": 1, "revenue_cents": 500}
    assert body["shares"] == []


def test_rollup_is_admin_only(app_client):
    token = _token()
    r = app_client.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_landing_view_needs_no_token_and_keeps_only_the_referrer_host(app_client, isolated_dbs):
    r = app_client.post("/api/events/landing",
                        json={"referrer": "https://news.ycombinator.com/item?id=1&x=secret",
                              "kind": "purchase_started", "payload": {"junk": "x" * 5000}})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1}
    rows = _raw_rows(isolated_dbs)
    assert [(row["kind"], row["user_id"]) for row in rows] == [("landing_view", "anon")]
    assert rows[0]["payload"] == '{"referrer": "news.ycombinator.com"}'


def test_landing_view_counts_but_not_as_a_user(app_client, isolated_dbs):
    token = _token(role="admin")
    app_client.post("/api/events/landing", json={"referrer": ""})
    app_client.post("/api/events/landing", json=None)
    _post(app_client, token, [{"kind": "page_view", "payload": {}}])
    r = app_client.get("/api/admin/analytics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    day = r.json()["days"][0]
    assert (day["landing"], day["active"]) == (2, 1)
