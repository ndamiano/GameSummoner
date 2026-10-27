import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api.routers import workqueue as wq
from db import jobs
from workqueue import api_worker

REPLY = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1_000, "completion_tokens": 10, "prompt_cache_hit_tokens": 900}}


@pytest.fixture
def upstream():
    seen = []
    answer = {"status": 200, "body": REPLY}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "body": body})
            out = json.dumps(answer["body"]).encode()
            self.send_response(answer["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen, answer
    server.shutdown()


def _serve_until_finished(app_client, monkeypatch, name, entry, job_id):
    monkeypatch.setattr(wq, "_queue_settings", lambda: {"token": "wsecret", "lease_seconds": 60})
    monkeypatch.setattr(wq, "CLAIM_LONG_POLL_SECONDS", 0.2)
    worker = api_worker.Worker("", "wsecret", name, entry["api"], 1, job_timeout=10)
    app_client.headers["Authorization"] = "Bearer wsecret"
    worker.session = app_client
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while jobs.get_job(job_id)["status"] in ("pending", "claimed") and time.time() < deadline:
        time.sleep(0.05)
    worker.stopping.set()
    thread.join(timeout=10)


@pytest.mark.parametrize("api_key,status,auth,outcome", [
    ("sk-test", 200, "Bearer sk-test", ("done", None)),
    (None, 401, None, ("failed", 'Status 401: {"error": "no key"}')),
])
def test_an_api_worker_serves_only_its_entry_with_its_model_effort_and_key(
        app_client, monkeypatch, llm_entries, upstream, api_key, status, auth, outcome):
    url, seen, answer = upstream
    answer.update(status=status, body=REPLY if status == 200 else {"error": "no key"})
    api = {"base_url": url, "effort": {"medium": "high"},
           "usd_per_mtok": {"input": 1.0, "cached_input": 0.1, "output": 2.0}}
    if api_key:
        api["api_key"] = api_key
    llm_entries["deepseek-4.1"] = {"model": "deepseek-v4.1-flash", "n_ctx": 131072, "api": api}
    qwen = jobs.enqueue_job("llm", {"kind": "llm", "body": {"model": "qwen", "messages": []}},
                            model="qwen-flash-next")
    body = {"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}],
            "reasoning": "medium", "max_tokens": 50}
    ds = jobs.enqueue_job("llm", {"kind": "llm", "body": body}, model="deepseek-4.1")

    _serve_until_finished(app_client, monkeypatch, "deepseek-4.1", llm_entries["deepseek-4.1"], ds)

    assert [(s["path"], s["auth"]) for s in seen] == [("/v1/chat/completions", auth)]
    assert seen[0]["body"] == {"model": "deepseek-v4.1-flash", "max_tokens": 50,
                               "messages": [{"role": "user", "content": "hi"}],
                               "reasoning_effort": "high"}
    job = jobs.get_job(ds)
    assert (job["status"], job["error"]) == outcome
    assert job["model"] == "deepseek-4.1"
    if status == 200:
        assert job["result"]["usage"]["cached_tokens"] == 900
        assert job["billed_micros"] == 100 + 90 + 20
    assert jobs.get_job(qwen)["status"] == "pending"


@pytest.mark.parametrize("usage,cached", [
    ({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}}, 8),
    ({"prompt_tokens": 10, "prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 3}, 7),
    ({"prompt_tokens": 10}, 0),
])
def test_cache_hits_are_read_from_either_providers_field(usage, cached):
    assert api_worker.with_cached_tokens({"usage": usage})["usage"]["cached_tokens"] == cached
