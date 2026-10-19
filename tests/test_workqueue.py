"""The worker-pull queue: store-level claim/lease/complete semantics, the /worker HTTP
endpoints (token-gated, outside the user auth gate), and the LLMConnector transport
end-to-end against a fake in-process worker."""

import base64
import threading
import time
from pathlib import Path

import pytest

from api.routers import workqueue as wq
from billing.estimates import calculate_job_cost
from db import connection, games, jobs, workers
from llm_clients.connector import LLMConnector
from llm_clients.rate_limiter import get_llm_rate_limiter
from tools.execution_context import run_scope

RATE = 0.99   # the rate every test worker is created at


@pytest.fixture(autouse=True)
def _refill_rate_limiter():
    # The global LLM rate limiter is a shared token bucket — earlier suite tests can drain it,
    # turning connector calls into rate-limit errors here. Refill it.
    lim = get_llm_rate_limiter()
    lim.tokens = float(lim.capacity)


def test_claim_is_fifo_and_exclusive():
    a = jobs.enqueue_job("llm", {"n": 1})
    b = jobs.enqueue_job("llm", {"n": 2})
    j1 = jobs.claim_job("llm", "w1", lease_seconds=60)
    j2 = jobs.claim_job("llm", "w2", lease_seconds=60)
    assert (j1["id"], j2["id"]) == (a, b)
    assert jobs.claim_job("llm", "w3", lease_seconds=60) is None


def test_queues_are_separate():
    jobs.enqueue_job("comfy", {"prompt": "cat"})
    assert jobs.claim_job("llm", "w1", lease_seconds=60) is None
    assert jobs.claim_job("comfy", "w1", lease_seconds=60)["payload"] == {"prompt": "cat"}


def test_expired_lease_requeues_and_stale_result_is_dropped():
    for worker in ("w1", "w2"):
        workers.worker_created(worker, None, "llm", None, RATE)
    job_id = jobs.enqueue_job("llm", {"n": 1})
    jobs.claim_job("llm", "w1", lease_seconds=0.01)
    time.sleep(0.02)
    rejig = jobs.claim_job("llm", "w2", lease_seconds=60)
    assert rejig["id"] == job_id
    # w1 comes back late — its completion must not clobber w2's claim.
    assert jobs.complete_job(job_id, "w1", {"stale": True}, None, 1.0) is None
    assert jobs.complete_job(job_id, "w2", {"fresh": True}, None, 2.0) is not None
    assert jobs.get_job(job_id)["result"] == {"fresh": True}


def test_heartbeat_extends_only_the_owners_lease():
    job_id = jobs.enqueue_job("llm", {})
    jobs.claim_job("llm", "w1", lease_seconds=60)
    assert jobs.heartbeat_job(job_id, "w1", lease_seconds=60) is True
    assert jobs.heartbeat_job(job_id, "intruder", lease_seconds=60) is False


def test_complete_debits_the_games_budget_and_worker_busy():
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)
    workers.worker_created("w1", None, "llm", None, RATE)
    job_id = jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    jobs.claim_job("llm", "w1", lease_seconds=60)
    jobs.complete_job(job_id, "w1", {"out": 1}, None, exec_seconds=12.5)
    assert games.game("g1")["spent_micros"] == calculate_job_cost(12.5, 0.99)
    job = jobs.get_job(job_id)
    assert (job["status"], job["exec_seconds"]) == ("done", 12.5)


def test_failed_job_carries_the_error():
    workers.worker_created("w1", None, "llm", None, RATE)
    job_id = jobs.enqueue_job("llm", {})
    jobs.claim_job("llm", "w1", lease_seconds=60)
    jobs.complete_job(job_id, "w1", None, "Status 500: boom", 3.0)
    job = jobs.get_job(job_id)
    assert (job["status"], job["error"]) == ("failed", "Status 500: boom")


@pytest.fixture
def client(app_client, monkeypatch):
    monkeypatch.setattr(wq, "_queue_settings",
                        lambda: {"token": "wsecret", "lease_seconds": 60})
    monkeypatch.setattr(wq, "CLAIM_LONG_POLL_SECONDS", 0.2)
    return app_client


def _hdr(token="wsecret"):
    return {"Authorization": f"Bearer {token}"}


def test_worker_endpoints_refuse_without_the_token(client):
    body = {"queue": "llm", "worker_id": "w1"}
    assert client.post("/worker/claim", json=body).status_code == 403
    assert client.post("/worker/claim", json=body, headers=_hdr("wrong")).status_code == 403


def test_unconfigured_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(wq, "_queue_settings", lambda: {"token": ""})
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"},
                    headers=_hdr(""))
    assert r.status_code == 403


def test_a_claim_never_hands_a_worker_the_jobs_metadata(client):
    """metadata carries the chain (what to enqueue next, what to finalize). It is control-plane
    only — a worker stays a generic executor that knows nothing about assets."""
    workers.worker_created("w1", None, "comfy", None, RATE)
    jobs.enqueue_job("comfy", {"kind": "comfy_image"}, batch_id="b1",
                      metadata={"then": {"enqueue": "mesh_from_image"}, "asset_id": "goblin"})
    r = client.post("/worker/claim", json={"queue": "comfy", "worker_id": "w1"}, headers=_hdr())
    job = r.json()["job"]
    assert set(job) == {"id", "queue", "payload"}


def test_claim_execute_complete_over_http(client):
    workers.worker_created("w1", None, "llm", None, RATE)
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)
    jobs.enqueue_job("llm", {"path": "/v1/responses", "body": {"model": "m"}}, game_id="g1", build_id="b1")

    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    job = r.json()["job"]
    assert job["payload"]["body"] == {"model": "m"}

    hb = client.post("/worker/heartbeat", json={"job_id": job["id"], "worker_id": "w1"},
                     headers=_hdr())
    assert hb.json()["ok"] is True

    done = client.post("/worker/complete", json={
        "job_id": job["id"], "worker_id": "w1", "result": {"output": []},
        "exec_seconds": 4.5}, headers=_hdr())
    assert done.json()["ok"] is True
    assert games.game("g1")["spent_micros"] == calculate_job_cost(4.5, RATE)


def test_complete_offloads_the_glb_to_the_blob_dir(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wq, "_blob_dir", lambda: tmp_path / "blobs")

    workers.worker_created("w1", None, "comfy", None, RATE)
    jid = jobs.enqueue_job("comfy", {"kind": "comfy_mesh"})
    client.post("/worker/claim", json={"queue": "comfy", "worker_id": "w1"}, headers=_hdr())
    glb = b"glTF-binary-bytes"
    r = client.post("/worker/complete", json={
        "job_id": jid, "worker_id": "w1",
        "result": {"glb_b64": base64.b64encode(glb).decode("ascii")}}, headers=_hdr())
    assert r.json()["ok"] is True

    job = jobs.get_job(jid)
    assert "glb_b64" not in job["result"]
    blob = Path(job["result"]["glb_file"])
    assert blob == tmp_path / "blobs" / f"{jid}.glb"
    assert blob.read_bytes() == glb


def test_stale_glb_completion_removes_its_blob(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wq, "_blob_dir", lambda: tmp_path / "blobs")

    jid = jobs.enqueue_job("comfy", {"kind": "comfy_mesh"})
    client.post("/worker/claim", json={"queue": "comfy", "worker_id": "w1"}, headers=_hdr())
    r = client.post("/worker/complete", json={
        "job_id": jid, "worker_id": "not-the-claimant",
        "result": {"glb_b64": base64.b64encode(b"x").decode("ascii")}}, headers=_hdr())
    assert r.json()["ok"] is False
    assert list((tmp_path / "blobs").glob("*")) == []


def test_complete_offloads_each_image_to_the_blob_dir(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wq, "_blob_dir", lambda: tmp_path / "blobs")

    workers.worker_created("w1", None, "comfy", None, RATE)
    jid = jobs.enqueue_job("comfy", {"kind": "comfy_image"})
    client.post("/worker/claim", json={"queue": "comfy", "worker_id": "w1"}, headers=_hdr())
    r = client.post("/worker/complete", json={
        "job_id": jid, "worker_id": "w1",
        "result": {"prompt_id": "p1", "images": [
            {"filename": "a.png", "b64": base64.b64encode(b"png-a").decode("ascii")},
            {"filename": "b.png", "b64": base64.b64encode(b"png-b").decode("ascii")},
        ]}}, headers=_hdr())
    assert r.json()["ok"] is True

    imgs = jobs.get_job(jid)["result"]["images"]
    assert all("b64" not in img for img in imgs)
    assert [img["filename"] for img in imgs] == ["a.png", "b.png"]
    assert Path(imgs[0]["file"]).read_bytes() == b"png-a"
    assert Path(imgs[1]["file"]).read_bytes() == b"png-b"


def test_glb_completion_refuses_a_path_shaped_job_id(client, tmp_path, monkeypatch):
    monkeypatch.setattr(wq, "_blob_dir", lambda: tmp_path / "blobs")
    r = client.post("/worker/complete", json={
        "job_id": "../../etc/passwd", "worker_id": "w1",
        "result": {"glb_b64": base64.b64encode(b"x").decode("ascii")}}, headers=_hdr())
    assert r.status_code == 400


def test_empty_claim_long_polls_then_returns_null(client):
    workers.worker_created("w1", None, "llm", None, RATE)
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    assert r.json() == {"job": None}


def test_wait_seconds_shortens_the_long_poll_window(client, monkeypatch):
    workers.worker_created("w1", None, "llm", None, RATE)
    monkeypatch.setattr(wq, "CLAIM_LONG_POLL_SECONDS", 30.0)
    t0 = time.time()
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1",
                                           "wait_seconds": 0.1}, headers=_hdr())
    assert r.json() == {"job": None}
    assert time.time() - t0 < 5   # honored the request, not the 30s server max


def test_wait_seconds_is_capped_at_the_server_max(client):
    workers.worker_created("w1", None, "llm", None, RATE)
    # server max is 0.2 in this fixture; asking for 60 must not hold the request for 60s
    t0 = time.time()
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1",
                                           "wait_seconds": 60}, headers=_hdr())
    assert r.json() == {"job": None}
    assert time.time() - t0 < 5


def test_claim_keeps_the_pod_id_the_scaler_recorded(client):
    workers.worker_created("w1", "pod-1", "llm", None, RATE)
    client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    assert workers.live_workers("llm", 60)[0]["pod_id"] == "pod-1"


def test_heartbeat_bumps_worker_last_seen(client):
    workers.worker_created("w1", None, "llm", None, RATE)
    jobs.enqueue_job("llm", {})
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    job = r.json()["job"]
    with connection.platform_db() as conn:
        conn.execute("UPDATE workers SET last_seen_at = last_seen_at - 999 WHERE id = 'w1'")
    assert workers.live_workers("llm", 60) == []
    client.post("/worker/heartbeat", json={"job_id": job["id"], "worker_id": "w1"},
                headers=_hdr())
    assert [w["id"] for w in workers.live_workers("llm", 60)] == ["w1"]


def test_deregister_terminates_the_worker_row(client):
    workers.worker_created("w1", "pod-1", "llm", None, RATE)
    client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    r = client.post("/worker/deregister", json={"worker_id": "w1"}, headers=_hdr())
    assert r.json() == {"ok": True}
    assert workers.live_workers("llm", 60) == []
    assert workers.terminated_workers_with_pods("llm")[0]["id"] == "w1"


def test_a_worker_nobody_created_is_refused_at_claim(client):
    jobs.enqueue_job("llm", {})
    r = client.post("/worker/claim", json={"queue": "llm", "worker_id": "stray"}, headers=_hdr())
    assert r.status_code == 403
    assert "never created" in r.json()["detail"]
    assert workers.live_workers("llm", 60) == []
    assert jobs.claim_job("llm", "w1", 60) is not None


def test_deregister_requires_the_token(client):
    assert client.post("/worker/deregister", json={"worker_id": "w1"}).status_code == 403


def _fake_worker(stop, respond):
    """Claim from the store directly and complete with `respond(payload)`."""
    workers.worker_created("fake", None, "llm", None, RATE)
    while not stop.is_set():
        job = jobs.claim_job("llm", "fake", lease_seconds=60)
        if job is None:
            time.sleep(0.01)
            continue
        result, error = respond(job["payload"])
        jobs.complete_job(job["id"], "fake", result, error, exec_seconds=1.0)


def _connector(timeout=10):
    return LLMConnector(model="test-model", job_timeout_seconds=timeout)


@pytest.fixture
def fake_worker():
    stop = threading.Event()
    holder = {}

    def start(respond):
        t = threading.Thread(target=_fake_worker, args=(stop, respond), daemon=True)
        holder["t"] = t
        t.start()

    yield start
    stop.set()
    if "t" in holder:
        holder["t"].join(timeout=2)


def test_connector_round_trip(fake_worker):
    def respond(payload):
        assert payload["kind"] == "llm"
        assert payload["body"]["model"] == "test-model"
        assert "messages" in payload["body"]        # canonical, not a dialect
        # a worker returns CANONICAL chat — it already translated for its own target
        return {"choices": [{"message": {"role": "assistant", "content": "hello"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1}}, None

    fake_worker(respond)
    result = _connector().generate_with_tools(
        [{"role": "user", "content": "hi"}], [])
    assert result["choices"][0]["message"]["content"] == "hello"
    assert result["usage"]["completion_tokens"] == 1


def test_connector_attributes_jobs_to_the_run_scope(fake_worker):
    games.create_game("g9", "u1")
    games.charge_game("g9", 1, 1_000_000)
    fake_worker(lambda p: ({"choices": [{"message": {"content": ""}}]}, None))
    with run_scope("g9", "b1"):
        _connector().generate_with_tools([{"role": "user", "content": "hi"}], [])
    assert games.game("g9")["spent_micros"] == calculate_job_cost(1.0, RATE)


def test_a_blocking_callers_reply_is_whole_until_read_then_elided(fake_worker):
    """Every job lands through the same route. One with a stage or a `then` is consumed there and
    its row can drop the reply at once; a bare job has a caller polling for it, and that caller
    elides the row after reading — so the connector sees the words and the row does not keep them."""
    fake_worker(lambda p: ({"choices": [{"message": {"content": "the whole reply"},
                                         "finish_reason": "stop"}],
                            "usage": {"completion_tokens": 3}}, None))
    result = _connector().generate_with_tools([{"role": "user", "content": "hi"}], [])
    assert result["choices"][0]["message"]["content"] == "the whole reply"
    with connection.platform_db() as conn:
        (jid,) = [r["id"] for r in conn.execute("SELECT id FROM jobs")]
    assert jobs.get_job(jid)["result"] == {"usage": {"completion_tokens": 3},
                                            "finish_reason": "stop", "tool_names": []}


def test_a_consumed_jobs_reply_is_elided_by_the_route(client):
    workers.worker_created("w1", None, "llm", None, RATE)
    jid = jobs.enqueue_job("llm", {"body": {"model": "m", "messages": []}},
                            metadata={"then": {"operations": []}})
    client.post("/worker/claim", json={"queue": "llm", "worker_id": "w1"}, headers=_hdr())
    client.post("/worker/complete", json={
        "job_id": jid, "worker_id": "w1",
        "result": {"choices": [{"message": {"content": "words"}, "finish_reason": "stop"}]}},
        headers=_hdr())
    assert jobs.get_job(jid)["result"] == {"usage": None, "finish_reason": "stop",
                                            "tool_names": []}


def test_connector_surfaces_worker_errors(fake_worker):
    fake_worker(lambda p: (None, "Status 500: model exploded"))
    result = _connector().generate_with_tools([{"role": "user", "content": "hi"}], [])
    assert "model exploded" in result["error"]


def test_connector_times_out_without_a_worker():
    result = _connector(timeout=0.3).generate_with_tools(
        [{"role": "user", "content": "hi"}], [])
    assert "timed out" in result["error"]
