"""Worker endpoints, the pull side of the inference queue: workers claim jobs, heartbeat their
lease and land results here. Mounted at /worker outside the user-auth gate, behind the shared
workqueue token; an unset token refuses everything, so the queue is fail-closed until configured."""

import asyncio
import base64
import hmac
import re
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from config.settings_manager import settings_manager
from db import jobs, workers
from maestro.codegen import asset_chain, build_chain, design

router = APIRouter()

CLAIM_LONG_POLL_SECONDS = 25.0
_CLAIM_POLL_INTERVAL = 0.05

_BACKGROUND: set = set()


def _queue_settings() -> Dict:
    return settings_manager.get_settings().get("workqueue") or {}


def _require_worker(request: Request) -> Dict:
    """The workqueue settings, iff this request carries the worker token. 403 otherwise."""
    cfg = _queue_settings()
    token = cfg.get("token") or ""
    header = request.headers.get("Authorization", "")
    presented = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    if not token or not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(status_code=403, detail="worker token required")
    return cfg


class ClaimBody(BaseModel):
    queue: str
    worker_id: str
    gpu_type: Optional[str] = None
    source: Optional[str] = None
    wait_seconds: Optional[float] = None


class HeartbeatBody(BaseModel):
    job_id: str
    worker_id: str


class DeregisterBody(BaseModel):
    worker_id: str


class CompleteBody(BaseModel):
    job_id: str
    worker_id: str
    result: Optional[Dict] = None
    error: Optional[str] = None
    exec_seconds: float = 0.0
    gpu_type: Optional[str] = None


@router.post("/claim", response_model=Dict)
async def claim(body: ClaimBody, request: Request):
    """Long-poll for the oldest pending job on the queue. Returns {"job": null} when nothing
    arrives within the window — the agent just calls again."""
    cfg = _require_worker(request)
    lease = cfg.get("lease_seconds", 120)
    if not workers.worker_seen(body.worker_id, body.queue, body.gpu_type, body.source):
        raise HTTPException(status_code=403, detail=f"worker {body.worker_id} was never created")
    window = CLAIM_LONG_POLL_SECONDS if body.wait_seconds is None \
        else max(0.0, min(body.wait_seconds, CLAIM_LONG_POLL_SECONDS))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    first = True
    while True:
        if first or await asyncio.to_thread(jobs.queue_has_work, body.queue):
            job = await asyncio.to_thread(jobs.claim_job, body.queue, body.worker_id, lease)
            if job is not None:
                return {"job": {"id": job["id"], "queue": job["queue"], "payload": job["payload"]}}
        first = False
        if loop.time() >= deadline:
            return {"job": None}
        await asyncio.sleep(_CLAIM_POLL_INTERVAL)


@router.post("/heartbeat", response_model=Dict)
async def heartbeat(body: HeartbeatBody, request: Request):
    cfg = _require_worker(request)
    ok = jobs.heartbeat_job(body.job_id, body.worker_id, cfg.get("lease_seconds", 120))
    workers.touch_worker(body.worker_id)
    return {"ok": ok}


@router.post("/deregister", response_model=Dict)
async def deregister(body: DeregisterBody, request: Request):
    """A worker announcing its own clean exit (idle self-exit or SIGTERM drain). The reaper
    terminates the pod behind any terminated worker row — this is the scale-down handshake."""
    _require_worker(request)
    workers.set_worker_terminated(body.worker_id)
    return {"ok": True}


def _blob_dir() -> Path:
    return Path(settings_manager.get_settings()["data_dir"]).resolve() / "blobs"


_JOB_ID_RE = re.compile(r"[0-9a-f]{16,32}")


def _offload_blobs(job_id: str, result: Optional[Dict]) -> list:
    if not result:
        return []
    has_images = any("b64" in img for img in result.get("images") or [])
    if "glb_b64" not in result and not has_images:
        return []
    if not _JOB_ID_RE.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="bad job id")
    blob_dir = _blob_dir()
    blob_dir.mkdir(parents=True, exist_ok=True)
    written = []
    if "glb_b64" in result:
        path = blob_dir / f"{job_id}.glb"
        path.write_bytes(base64.b64decode(result.pop("glb_b64")))
        result["glb_file"] = str(path)
        written.append(path)
    for i, img in enumerate(result.get("images") or []):
        if "b64" not in img:
            continue
        path = blob_dir / f"{job_id}-{i}.png"
        path.write_bytes(base64.b64decode(img.pop("b64")))
        img["file"] = str(path)
        written.append(path)
    return written


def _prepare(job_id: str, result: Optional[Dict]) -> tuple:
    """Offload the result's binaries, then build the follow-up job from the parent's `then`. Both
    read the same blob paths, so they share one hop off the event loop."""
    blobs = _offload_blobs(job_id, result)
    job = jobs.get_job(job_id) or {}
    metadata = job.get("metadata") or {}
    continuation = asset_chain.build_continuation(metadata, result) if metadata else None
    return blobs, metadata, continuation


def _land(body: "CompleteBody", continuation: Optional[Dict], consumed: bool) -> Optional[Dict]:
    """`consumed` says this route hands the reply on (a build turn, a design, an asset chain), so
    the row can drop it now; a job nobody here consumes has a blocking waiter polling for it,
    which elides the row once it has read it."""
    outcome = jobs.complete_job(body.job_id, body.worker_id, body.result, body.error,
                                    body.exec_seconds, body.gpu_type, continuation)
    if outcome is not None and consumed:
        jobs.elide_job_result(body.job_id)
    return outcome


@router.post("/complete", response_model=Dict)
async def complete(body: CompleteBody, request: Request):
    """Land a result (or failure). ok=false means the lease lapsed and the job was requeued —
    this worker's result was dropped and it should just move on."""
    _require_worker(request)
    blobs, metadata, continuation = await asyncio.to_thread(_prepare, body.job_id, body.result)
    # sqlite, not the event loop: the completion txn also admits and inserts the follow-up.
    outcome = await asyncio.to_thread(_land, body, continuation,
                                      bool(metadata.get("stage") or metadata.get("then")))
    if outcome is None:
        for blob in blobs:
            blob.unlink(missing_ok=True)
        return {"ok": False}

    # Fire-and-forget: the completion's follow-up work must not block the worker's response. For a
    # BUILD turn that work is the whole next advance (tool dispatch, staging, the next enqueue), for
    # a DESIGN it is landing the prompt, for an ASSET job it is the ops + finalize. Either way it's
    # off the event loop; a restart between here and it is what the reaper backstops. to_thread, not
    # a bare task — sync work on the loop stalls every other completion.
    if metadata.get("stage") == "build":
        follow_up = (build_chain.on_completion, metadata["run_id"], metadata.get("build_id"),
                     body.result, body.error, body.job_id, body.exec_seconds)
    elif metadata.get("stage") == "design":
        follow_up = (design.on_complete, metadata["run_id"], outcome["build_id"], body.result,
                     body.error, metadata["step"])
    else:
        follow_up = (asset_chain.on_completion, metadata, body.result, outcome["batch_id"],
                     outcome["batch_complete"])
    task = asyncio.create_task(asyncio.to_thread(*follow_up))
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return {"ok": True}
