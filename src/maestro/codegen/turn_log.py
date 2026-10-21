"""The build's turn log, runs/<id>/turns.jsonl: a `meta` per build, a `turn` per completed turn with
only the messages it added, a `compact` per compaction. Turn k is system + tools + added[0..k]."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

FILE = "turns.jsonl"


def path(run_dir) -> Path:
    return Path(run_dir) / FILE


def append_meta(run_dir, *, run_id: str, build_id: str, system: str, tools: List[Dict],
                wire: str, model: Optional[str], max_tokens: int, reasoning: Optional[str]) -> None:
    _append(run_dir, {"kind": "meta", "run_id": run_id, "build_id": build_id, "system": system,
                      "tools": tools, "wire": wire, "model": model, "max_tokens": max_tokens,
                      "reasoning": reasoning})


def append_turn(run_dir, *, turn: int, job_id: str, added: List[Dict], response: Optional[Dict],
                usage: Dict, exec_seconds: float, error: Optional[str]) -> None:
    _append(run_dir, {"kind": "turn", "turn": turn, "job_id": job_id, "added": added,
                      "response": response, "usage": usage, "exec_seconds": exec_seconds,
                      "error": error})


def append_compact(run_dir, *, turn: int, trimmed: int, dropped: int, note: Optional[str]) -> None:
    _append(run_dir, {"kind": "compact", "turn": turn, "trimmed": trimmed, "dropped": dropped,
                      "note": note})


def _append(run_dir, record: Dict) -> None:
    with path(run_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def read_turn(run_dir, job_id: str) -> Optional[Dict]:
    """One archived turn with the exact messages its request carried, or None."""
    for meta, record, messages, _chars in _replay(run_dir):
        if record["job_id"] == job_id:
            return {"meta": meta, "messages": list(messages), "response": record["response"],
                    "usage": record["usage"] or {}, "exec_seconds": record["exec_seconds"],
                    "error": record["error"]}
    return None


def _replay(run_dir) -> Iterator[Tuple[Dict, Dict, List[Dict], int]]:
    """Yields (meta, turn record, messages as that turn sent them, their serialized size)."""
    p = path(run_dir)
    if not p.exists():
        return
    meta: Dict = {}
    messages: List[Dict] = []
    chars = 0
    with p.open(encoding="utf-8") as f:
        for line in f:
            record = json.loads(line)
            kind = record["kind"]
            if kind == "meta":
                meta, messages, chars = record, [], 0
            elif kind == "turn":
                messages = messages + record["added"]
                chars += _chars_of(record["added"])
                yield meta, record, messages, chars
            elif kind == "compact":
                messages = _compacted(messages, record)
                chars = _chars_of(messages)


def _compacted(messages: List[Dict], record: Dict) -> List[Dict]:
    """Replay one compaction exactly as build_steps.compact performed it."""
    from maestro.codegen import build_steps
    seeded = [m for m in messages if build_steps.is_seed(m)]
    messages = [m for m in messages if not build_steps.is_note(m) and not build_steps.is_seed(m)]
    messages = build_steps.drop_read_only_rounds(
        build_steps.dedupe_bodies(build_steps.strip_reasoning(messages)))
    if record["trimmed"]:
        messages = build_steps.trim_bodies(messages, record["trimmed"])
    groups = build_steps.rounds(messages)
    kept = [m for g in groups[record["dropped"]:] for m in g]
    return messages[:1] + seeded + [{"role": "user", "content": record["note"]}] + kept


def _chars_of(messages: List[Dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
