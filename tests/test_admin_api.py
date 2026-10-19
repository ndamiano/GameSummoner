"""The admin queue endpoint: role-gated (a signed-in non-admin gets 403, anonymous 401), and its
snapshot covers every queue — the next jobs in claim order and the fleet with what it holds."""

import time
from unittest.mock import MagicMock

import pytest

from auth import store
from billing.estimates import QUEUE_SECONDS_ESTIMATES
from db import connection, games, jobs, workers


def _token(handle, role):
    store.create_user(handle, "pw-pass1234", role=role, email=f"{handle}@example.com")
    return store.issue_token(store.get_user_by_handle(handle).id)


def test_non_admin_is_forbidden(app_client):
    token = _token("alice", "user")
    r = app_client.get("/api/admin/queues", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_admin_gets_a_snapshot_of_every_queue(app_client):
    token = _token("root", "admin")
    r = app_client.get("/api/admin/queues", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert {q["queue"] for q in body["queues"]} == set(QUEUE_SECONDS_ESTIMATES)
    for q in body["queues"]:
        assert q.keys() >= {"pending", "claimed", "workers_live", "workers_max",
                            "backlog_seconds", "next", "workers"}
    assert body["totals"].keys() == {"pending", "claimed", "workers_live", "backlog_seconds"}


def _queue(app_client, name):
    token = _token("root", "admin")
    body = app_client.get("/api/admin/queues",
                          headers={"Authorization": f"Bearer {token}"}).json()
    return next(q for q in body["queues"] if q["queue"] == name), body["totals"]


def test_next_lists_pending_jobs_in_claim_order_with_their_wait(app_client):
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)
    build = games.create_build("g1")
    first = jobs.enqueue_job("image", {}, game_id="g1", build_id=build)
    second = jobs.enqueue_job("image", {}, game_id="g1", build_id="b1")

    image, totals = _queue(app_client, "image")
    assert [j["id"] for j in image["next"]] == [first, second]
    assert image["next"][0]["game_id"] == "g1"
    assert image["next"][0]["build_id"] == build
    assert 0 <= image["next"][0]["waiting_seconds"] < 5
    assert image["next"][0]["est_seconds"] == QUEUE_SECONDS_ESTIMATES["image"]
    assert (totals["pending"], totals["claimed"]) == (2, 0)


def test_next_is_capped_at_ten(app_client):
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 10_000_000)
    for _ in range(12):
        jobs.enqueue_job("image", {}, game_id="g1", build_id="b1")
    image, _ = _queue(app_client, "image")
    assert (image["pending"], len(image["next"])) == (12, 10)


def test_workers_report_state_and_the_job_they_hold(app_client):
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)
    jid = jobs.enqueue_job("image", {}, game_id="g1", build_id="b1")
    workers.worker_created("busy", "p1", "image", "BIG", 0.99)
    workers.worker_created("idle", None, "image", "NVIDIA GeForce RTX 5090", 0.0)
    workers.worker_seen("busy", "image", gpu_type="BIG", source="runpod")
    workers.worker_seen("idle", "image", gpu_type="NVIDIA GeForce RTX 5090", source="local")
    jobs.claim_job("image", "busy", 60)

    image, totals = _queue(app_client, "image")
    by_id = {w["id"]: w for w in image["workers"]}
    assert by_id["busy"]["state"] == "busy"
    assert by_id["busy"]["pod_id"] == "p1"
    assert by_id["busy"]["job"]["id"] == jid
    assert by_id["busy"]["job"]["game_id"] == "g1"
    assert 0 <= by_id["busy"]["job"]["running_seconds"] < 5
    assert by_id["busy"]["job"]["est_seconds"] == QUEUE_SECONDS_ESTIMATES["image"]
    assert by_id["idle"]["state"] == "idle"
    assert by_id["idle"]["job"] is None
    assert (image["workers_live"], image["claimed"], image["pending"]) == (2, 1, 0)
    assert image["next"] == []
    assert totals["workers_live"] == 2


def test_a_created_pod_is_booting_until_its_worker_registers(app_client):
    workers.worker_created("w-new", "p-new", "llm", None, 1.89)
    workers.worker_created("w-live", "p-live", "llm", None, 1.89)
    workers.worker_seen("w-live", "llm", gpu_type="BIG", source="runpod")
    llm, _ = _queue(app_client, "llm")

    states = {w["id"]: w["state"] for w in llm["workers"]}
    assert states == {"w-live": "idle", "w-new": "booting"}
    booting = next(w for w in llm["workers"] if w["state"] == "booting")
    assert booting["pod_id"] == "p-new"
    assert booting["uptime_seconds"] == pytest.approx(0, abs=5)
    # The price is the pod's from the create, before any worker has said what card it is on.
    assert booting["usd_per_hour"] == 1.89
    assert booting["gpu_type"] is None
    assert llm["workers_live"] == 1


def test_costs_split_a_reused_pod_id_into_its_lives(app_client, monkeypatch):
    from api.routers import admin
    from billing.estimates import calculate_job_cost

    RATE = 0.99

    now = time.time()
    day = 86400
    # RunPod reused qwieur: one life Tuesday, another Wednesday.
    lives = {"w-tue": ("qwieur", 2.0, now - 3 * day), "w-wed": ("qwieur", 1.0, now - day),
             "w-old": ("p-old", 1.0, now - 10 * day)}
    for worker, (pod, rate, started) in lives.items():
        workers.worker_created(worker, pod, "image", "NVIDIA GeForce RTX 5090", rate)
        workers.worker_seen(worker, "image", gpu_type="NVIDIA GeForce RTX 5090",
                             source="runpod")
        with connection.platform_db() as conn:
            conn.execute("UPDATE workers SET started_at = ? WHERE id = ?", (started, worker))

    def hour(pod, end, total, gpu, disk=0.0):
        return {"pod_id": pod, "end": end, "total": total, "gpu": gpu, "disk": disk}

    monkeypatch.setattr(admin, "_ledger", lambda _since, _now: [
        hour("qwieur", now - 5 * day, 0.25, 0.25),        # before any life we know of: ghost
        hour("qwieur", now - 3 * day + 3600, 2.0, 2.0),
        hour("qwieur", now - day + 3600, 0.6, 0.5, 0.1),
        hour("p-old", now - 6 * day, 3.0, 3.0),           # a life created before the window
        hour("boot-looper", now - 3600, 1.5, 1.5)])
    admin._cost_cache.clear()
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 10_000_000)
    build = games.create_build("g1")
    for worker, game_id, error, secs in [("w-tue", "g1", None, 100.0),
                                         ("w-wed", "g1", None, 360.0),
                                         ("w-wed", "g1", "boom", 36.0),
                                         ("w-wed", None, None, 72.0)]:
        jobs.enqueue_job("image", {}, game_id=game_id, build_id=build)
        claimed = jobs.claim_job("image", worker, 60)
        jobs.complete_job(claimed["id"], worker, None if error else {"ok": True}, error,
                              exec_seconds=secs)
    games.build_finished(build, "built")

    token = _token("root", "admin")
    body = app_client.get("/api/admin/costs?days=7",
                          headers={"Authorization": f"Bearer {token}"}).json()

    assert (body["runpod_reachable"], body["days"]) == (True, 7)
    tracked = {p["worker_id"]: p for p in body["pods"] if p["tracked"]}
    assert set(tracked) == {"w-tue", "w-wed"}
    wed = tracked["w-wed"]
    assert (wed["pod_id"], wed["provider_usd"], wed["disk_usd"]) == ("qwieur", 0.6, 0.1)
    assert wed["billed_seconds"] == pytest.approx(1800.0)
    assert (wed["jobs"], wed["failed"], wed["exec_seconds"]) == (3, 1, 468.0)
    assert wed["customer_usd"] == round(calculate_job_cost(360.0, 1.0) / 1e6, 4)
    tue = tracked["w-tue"]
    assert (tue["pod_id"], tue["provider_usd"], tue["jobs"]) == ("qwieur", 2.0, 1)
    assert tue["billed_seconds"] == pytest.approx(3600.0)
    assert tue["customer_usd"] == round(calculate_job_cost(100.0, 2.0) / 1e6, 4)

    ghosts = {p["pod_id"]: p for p in body["pods"] if not p["tracked"]}
    assert {pid: g["provider_usd"] for pid, g in ghosts.items()} == {"boot-looper": 1.5,
                                                                   "qwieur": 0.25}
    assert all(g["worker_id"] is None and g["jobs"] == 0 for g in ghosts.values())
    assert body["games"]["n"] == 1


def test_costs_without_a_reachable_ledger_still_reports_our_half(app_client, monkeypatch):
    from api.routers import admin

    monkeypatch.setattr(admin, "_ledger", lambda _since, _now: None)
    admin._cost_cache.clear()
    workers.worker_created("w1", "p1", "llm", "NVIDIA GeForce RTX 5090", 1.0)
    workers.worker_seen("w1", "llm", gpu_type="NVIDIA GeForce RTX 5090", source="runpod")
    jid = jobs.enqueue_job("llm", {"p": 1})
    jobs.claim_job("llm", "w1", lease_seconds=120)
    jobs.complete_job(jid, "w1", {"ok": True}, None, exec_seconds=60)

    token = _token("root2", "admin")
    body = app_client.get("/api/admin/costs",
                          headers={"Authorization": f"Bearer {token}"}).json()
    assert (body["runpod_reachable"], body["days"]) == (False, 7)
    [pod] = body["pods"]
    assert (pod["worker_id"], pod["pod_id"], pod["provider_usd"]) == ("w1", "p1", None)
    assert pod["billed_seconds"] is None
    assert (pod["jobs"], pod["exec_seconds"], pod["customer_usd"]) == (1, 60.0, 0.0)


def test_costs_price_an_ec2_life_at_its_launch_rate_with_no_ledger(app_client, monkeypatch):
    from api.routers import admin

    now = time.time()
    monkeypatch.setattr(admin, "_ledger", lambda _since, _now: [])
    monkeypatch.setattr(admin.time, "time", lambda: now)
    admin._cost_cache.clear()
    lives = {"w-done": ("eu-north-1/i-done", 1.20, now - 7200, now - 5400),
             "w-live": ("us-west-2/i-live", 3.36, now - 900, None)}
    for worker, (machine, rate, started, ended) in lives.items():
        workers.worker_created(worker, machine, "llm", "g7e.2xlarge", rate, source="aws")
        with connection.platform_db() as conn:
            conn.execute("UPDATE workers SET started_at = ?, terminated_at = ? WHERE id = ?",
                         (started, ended, worker))

    token = _token("root3", "admin")
    body = app_client.get("/api/admin/costs",
                          headers={"Authorization": f"Bearer {token}"}).json()
    pods = {p["worker_id"]: p for p in body["pods"]}
    assert {w: (p["source"], p["pod_id"], p["provider_usd"], p["disk_usd"], p["billed_seconds"])
            for w, p in pods.items()} == {
        "w-done": ("aws", "eu-north-1/i-done", 0.6, 0.0, pytest.approx(1800.0)),
        "w-live": ("aws", "us-west-2/i-live", 0.84, 0.0, pytest.approx(900.0))}


def test_ledger_reads_runpods_hour_buckets_and_fails_soft(monkeypatch):
    import calendar

    from api.routers import admin

    monkeypatch.setattr(admin.settings_manager, "get_settings",
                        lambda: {"runpod": {"api_key": "k"}})
    buckets = []

    class Client:
        def __init__(self, key):
            pass

        def billing_pods(self, start, end, bucket):
            buckets.append(bucket)
            return [{"podId": "p1", "startTime": "2026-09-01T10:00:00Z",
                     "endTime": "2026-09-01T11:00:00Z", "totalAmount": 0.6, "gpuAmount": 0.5,
                     "cpuAmount": 0, "diskAmount": 0.1}]

    monkeypatch.setattr(admin, "RunPodClient", Client)
    assert admin._ledger(0.0, 30 * 86400.0) == [
        {"pod_id": "p1", "end": calendar.timegm((2026, 9, 1, 11, 0, 0)),
         "total": 0.6, "gpu": 0.5, "disk": 0.1}]
    assert buckets == ["hour"]

    def refused(self, start, end, bucket):
        raise RuntimeError("429")

    monkeypatch.setattr(Client, "billing_pods", refused)
    assert admin._ledger(0.0, 1.0) is None


def test_a_queue_never_refused_carries_an_empty_stock_out_block(app_client):
    llm, _ = _queue(app_client, "llm")
    assert llm["stockouts"] == {
        "last_1h": 0,
        "last_24h": 0,
        "last_7d": 0,
        "last_at": None
    }
