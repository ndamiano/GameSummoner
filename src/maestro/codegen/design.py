"""The design stage: one decide job has JEV make the game's decisions, then one llm call writes the
build spec from them, landed as design/design.md in the game folder, with the ask itself as the
fallback prompt."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from db import games, jobs
from llm_clients.connector import get_connector
from llm_clients.message_builder import MessageBuilder

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts"
_SYSTEM = "You design browser games."
MAX_TOKENS = 100000
DESIGN_DIR = "design"
DECISIONS = "decisions.json"
CENTRALITY = "first playable"
CORE, SUPPORT = 0.6, 0.3
SECTIONS = [str(n) for n in range(12)] + ["A"]
CONTINUATIONS = 3
RETRIES = 1
_HEADING = re.compile(r"^#+\s*(\d+|A)\.", re.M)
_ROW = re.compile(r"^\| ([^|]+?) \| ([^|]+?) \|$", re.M)


def _prompt(name: str, **slots) -> str:
    text = (_PROMPTS / "design" / f"{name}.txt").read_text(encoding="utf-8")
    for key, value in slots.items():
        text = text.replace("{" + key + "}", value)
    return text


def _catalogue(name: str) -> List[Dict[str, str]]:
    rows = _ROW.findall((_PROMPTS / "design" / name).read_text(encoding="utf-8"))
    return [{"name": n, "text": f"{n}: {d}"} for n, d in rows if n != "Mechanic"]


def chain(ask: str) -> Dict:
    """The decide job: every decision in decisions.json, each answer carried into the next's state."""
    steps = json.loads((_PROMPTS / "design" / "decisions.json").read_text(encoding="utf-8"))["steps"]
    for step in steps:
        items = step.get("items")
        if isinstance(items, str):
            step["items"] = _catalogue(items)
        elif items is not None:
            step["items"] = [{"name": i, "text": i} for i in items]
    return {"kind": "decide", "state": f"Game prompt: {ask}", "steps": steps}


def tiered_state(decided: Dict) -> str:
    """The decided state with its mechanics banded by JEV's first-playable probabilities."""
    probs = next((s.get("probabilities") or {} for s in decided["steps"] if s["key"] == CENTRALITY), {})
    order = sorted(probs, key=lambda m: -probs[m])
    bands = {"core mechanics": [m for m in order if probs[m] >= CORE],
             "supporting mechanics": [m for m in order if SUPPORT <= probs[m] < CORE],
             "extra mechanics": [m for m in order if probs[m] < SUPPORT]}
    lines = [line for line in decided["state"].splitlines() if not line.startswith("mechanics:")]
    at = next((i for i, line in enumerate(lines) if line.startswith("art style:")), len(lines))
    lines[at:at] = [f"{band}: {', '.join(names) or 'none'}" for band, names in bands.items()]
    return "\n".join(lines)


def _messages(ask: str, state: str, spec_so_far: str = "") -> list:
    body = _prompt("write", state=state, request=ask)
    if spec_so_far:
        body += "\n\n" + _prompt("write_continue", spec_so_far=spec_so_far)
    return MessageBuilder(_SYSTEM).add_user(body).build()


def _design_dir(run_id: str) -> Path:
    from maestro.state import RunState
    d = RunState(run_id).run_dir / DESIGN_DIR
    d.mkdir(exist_ok=True)
    return d


def _state(run_id: str) -> str:
    return tiered_state(json.loads((_design_dir(run_id) / DECISIONS).read_text(encoding="utf-8")))


def missing_sections(text: str) -> List[str]:
    have = set(_HEADING.findall(text))
    return [n for n in SECTIONS if n not in have]


def _reply_text(result: Optional[Dict]) -> str:
    msg = ((result or {}).get("choices") or [{}])[0].get("message", {}) or {}
    return msg.get("content") or ""


def _request(ask: str) -> str:
    return (_PROMPTS / "request.txt").read_text(encoding="utf-8").replace("{ask}", ask).strip()


def _land(run_id: str, request: str) -> str:
    from maestro.codegen.run import set_prompt
    set_prompt(run_id, request, event="prompt_proposed")
    return request


def _land_spec(run_id: str, spec: str, ask: str) -> str:
    from maestro.codegen.staging import spec_path
    from maestro.state import RunState
    spec_path(RunState(run_id).run_dir).write_text(spec, encoding="utf-8")
    return _land(run_id, _request(ask))


def _fallback(run_id: str, build_id: Optional[str], ask: str, why: str) -> None:
    logger.warning("design for %s: %s — the ask is the prompt", run_id, why)
    if build_id:
        games.build_finished(build_id, "failed")
    _land(run_id, ask)
    _start_build(run_id)


def _start_build(run_id: str) -> None:
    from maestro.codegen import build_chain
    try:
        build_chain.kickoff(run_id, kind="build")
    except Exception:
        logger.exception("build for %s could not start after its design landed", run_id)


def _enqueue_step(run_id: str, build_id: str, ask: str, step: str, spec_so_far: str = "") -> None:
    metadata = {"stage": "design", "run_id": run_id, "step": step}
    if step == "decide":
        jobs.enqueue_job("decide", chain(ask), game_id=run_id, build_id=build_id, metadata=metadata)
        return
    payload, model = get_connector().build_llm_job(_messages(ask, _state(run_id), spec_so_far), [],
                                                   MAX_TOKENS)
    jobs.enqueue_job("llm", payload, game_id=run_id, build_id=build_id, model=model,
                     metadata=metadata)


def enqueue(run_id: str, ask: str) -> None:
    build_id = games.create_build(run_id, kind="design")
    games.build_started(build_id)
    try:
        _enqueue_step(run_id, build_id, ask, "decide")
    except Exception:
        logger.exception("design for %s could not be enqueued", run_id)
        games.build_finished(build_id, "failed")
        _land(run_id, ask)


def _retry(run_id: str, step: str) -> bool:
    d = _design_dir(run_id)
    marker = d / f".retried-{step}"
    n = int(marker.read_text()) if marker.is_file() else 0
    if n >= RETRIES:
        return False
    marker.write_text(str(n + 1))
    return True


def on_complete(run_id: str, build_id: Optional[str], result: Optional[Dict],
                error: Optional[str], step: str) -> None:
    from maestro.state import RunState
    ask = (RunState(run_id).read_spec() or {}).get("ask", "")
    d = _design_dir(run_id)
    if step == "decide":
        if error is None and result and result.get("state"):
            (d / DECISIONS).write_text(json.dumps(result, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
            _next(run_id, build_id, ask, "write")
        elif _retry(run_id, step):
            _next(run_id, build_id, ask, "decide")
        else:
            _fallback(run_id, build_id, ask, f"decide failed ({error or 'no state'})")
        return
    text = _reply_text(result) if error is None else ""
    spec = d / "spec.md"
    if not text.strip():
        if error is None and _retry(run_id, step):
            _next(run_id, build_id, ask, step,
                  spec_so_far=spec.read_text(encoding="utf-8") if spec.is_file() else "")
            return
        _fallback(run_id, build_id, ask, f"{step} landed with nothing ({error or 'empty reply'})")
        return
    text = (spec.read_text(encoding="utf-8") if spec.is_file() else "") + text
    spec.write_text(text, encoding="utf-8")
    rounds = int((d / ".rounds").read_text()) if (d / ".rounds").is_file() else 0
    if missing_sections(text) and rounds < CONTINUATIONS:
        (d / ".rounds").write_text(str(rounds + 1))
        _next(run_id, build_id, ask, "write", spec_so_far=text)
        return
    if build_id:
        games.build_finished(build_id, "succeeded", steps=1)
    _land_spec(run_id, text, ask)
    _start_build(run_id)


def _next(run_id: str, build_id: Optional[str], ask: str, step: str, spec_so_far: str = "") -> None:
    try:
        _enqueue_step(run_id, build_id or "", ask, step, spec_so_far)
    except Exception:
        logger.exception("design for %s: %s could not be enqueued", run_id, step)
        _fallback(run_id, build_id, ask, f"{step} refused")


def _decide(ask: str) -> Dict:
    from workqueue.client import run_job
    for _ in range(RETRIES + 1):
        job = run_job("decide", chain(ask))
        if job["status"] == "done" and (job.get("result") or {}).get("state"):
            return job["result"]
    raise ValueError(f"decide failed: {job.get('error') or 'no state'}")


def _write(ask: str, state: str, spec_so_far: str) -> str:
    for _ in range(RETRIES + 1):
        text = _reply_text(get_connector().generate_with_tools(
            _messages(ask, state, spec_so_far), [], max_tokens=MAX_TOKENS))
        if text.strip():
            return text
    raise ValueError("write came back empty")


def generate(run_id: str, ask: str) -> str:
    from tools.execution_context import run_scope
    build_id = games.create_build(run_id, kind="design")
    games.build_started(build_id)
    d = _design_dir(run_id)
    try:
        with run_scope(run_id, build_id):
            decided = _decide(ask)
            (d / DECISIONS).write_text(json.dumps(decided, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
            state, spec = tiered_state(decided), ""
            for _ in range(CONTINUATIONS + 1):
                spec += _write(ask, state, spec)
                if not missing_sections(spec):
                    break
        (d / "spec.md").write_text(spec, encoding="utf-8")
        games.build_finished(build_id, "succeeded", steps=1)
        return _land_spec(run_id, spec, ask)
    except Exception as e:
        logger.exception("design for %s failed (%s) — the ask is the prompt", run_id, e)
    games.build_finished(build_id, "failed", steps=1)
    return _land(run_id, ask)
