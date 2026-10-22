from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from db import games

DEFAULT_MAX_STEPS = 200


@dataclass
class BuildCursor:
    build_id: str
    kind: str = "build"
    step: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
    t0: float = 0.0
    ok: Optional[bool] = None
    request: str = ""                             # overrides the spec-rendered request (a gate fix or a change note)
    started: bool = False
    turn: int = 0
    # what the SERVER reported for the last turn — an estimate of ours that drifts low would hit the
    # context window with no warning.
    prompt_tokens: int = 0
    # chars per token of the last prompt the server counted: compaction measures chars, and its
    # target is a share of the window in tokens.
    chars_per_token: float = 0.0
    compacted: int = 0                            # compactions so far
    no_call_streak: int = 0
    redriven: int = 0
    out_cap: int = 0
    # hashed failing tool call -> times sent. Per call, not just the last one: a stuck model
    # re-reads between retries, and that read must not clear the failing edit's count.
    repeat_counts: Dict[str, int] = field(default_factory=dict)
    actions: List[str] = field(default_factory=list)   # last turn's tool calls, for the build feed
    finished: bool = False                        # the model called `done`
    summary: str = ""
    system: str = ""
    history: List[Dict] = field(default_factory=list)
    # The turn log's place in that history: how many messages have been archived to turns.jsonl,
    # and whether this build's `meta` record is down. Both durable — a control plane that restarts
    # mid-build must not re-append what a previous process already wrote.
    logged: int = 0
    meta_logged: bool = False


def load(game_id: str) -> Optional[BuildCursor]:
    build = games.current_build(game_id)
    if build is None or build["cursor"] is None:
        return None
    return BuildCursor(build_id=build["id"], kind=build["kind"], **json.loads(build["cursor"]))


def save(cursor: BuildCursor) -> None:
    state = asdict(cursor)
    del state["build_id"], state["kind"]
    games.save_cursor(cursor.build_id, json.dumps(state, ensure_ascii=False))
