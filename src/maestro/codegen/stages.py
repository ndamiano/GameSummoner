"""Staged builds: a planner cuts the design into playable stages, each its own build on the same
folder; a broken stage reruns from its snapshot and the best attempt carries on."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from maestro.codegen import code_map, snapshots
from maestro.codegen.staging import DESIGN_DIR, game_dir, is_vendored, spec_path

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).parent / "prompts" / "stage"
PLAN_FILE = "stages.json"
STAGES = 9
PLAN_BUDGET = 25
BUDGETS = [20, 20, 20, 25, 25, 30, 30, 35, 35]
RETRIES = 2
LAST = 3
SHARED_READS = ["# 2. CONTRACTS", "# 8. DEBUG API", "# 9. TESTS"]
FILES_CAP = 100_000
ROOM = 18_000
TEXT = {".js", ".mjs", ".html", ".css", ".json"}


def _text(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8").strip()


def _plan_path(run_dir) -> Path:
    return Path(run_dir) / DESIGN_DIR / PLAN_FILE


def load(run_dir) -> Optional[Dict]:
    p = _plan_path(run_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _save(run_dir, plan: Dict) -> None:
    _plan_path(run_dir).parent.mkdir(exist_ok=True)
    _plan_path(run_dir).write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")


def plan_messages(design: str) -> List[Dict]:
    from llm_clients.message_builder import MessageBuilder
    prompt = _text("plan.txt").format(stages=STAGES, budget=PLAN_BUDGET, design=design)
    return MessageBuilder(_text("plan_system.txt")).add_user(prompt).build()


def _headings(design: str) -> List[str]:
    return [line for line in design.splitlines() if line.startswith("#")]


def match_reads(reads: List[str], design: str) -> List[str]:
    """Each read as a heading line the design has: exact, else the heading its § number names."""
    heads = _headings(design)
    out = []
    for r in reads:
        r = r.strip()
        hit = r if r in heads else None
        if hit is None:
            num = re.search(r"(\d+(?:\.\d+)*)", r)
            if num:
                hit = next((h for h in heads
                            if re.match(rf"#+ {re.escape(num.group(1))}[. ]", h + " ")), None)
        if hit and hit not in out:
            out.append(hit)
    return out


def parse(text: str) -> List[Dict]:
    m = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m[-1] if m else text[text.find("["):text.rfind("]") + 1]
    stages = json.loads(body)
    if not isinstance(stages, list) or not stages:
        raise ValueError("the plan is not a list of stages")
    return stages


def land_plan(run_dir, text: str) -> bool:
    design = spec_path(run_dir).read_text(encoding="utf-8")
    try:
        raw = parse(text)
        stages = [{"name": s["name"].strip().lower(), "stage": s["stage"].strip(),
                   "done": s["done"].strip(), "reads": match_reads(s.get("reads") or [], design)}
                  for s in raw]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning("stage plan for %s did not parse: %s", run_dir, e)
        return False
    _save(run_dir, {"stages": stages, "budgets": budgets(len(stages)), "attempts": {}, "before": {}})
    return True


def budgets(count: int) -> List[int]:
    return [BUDGETS[min(i * len(BUDGETS) // count, len(BUDGETS) - 1)] for i in range(count)]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def request(ask: str, plan: Dict, n: int, repair: Optional[List[str]] = None) -> str:
    s = plan["stages"][n - 1]
    carry = "\n" + _text("carry.txt").format(prev=n - 1) + "\n" if n > 1 else ""
    listing = " · ".join(f"{i} {st['name']}" for i, st in enumerate(plan["stages"], 1))
    text = _text("frame.txt").format(ask=ask, carry=carry, listing=listing, n=n,
                                     stage=_cap(s["stage"]), done=_cap(s["done"]),
                                     budget=plan["budgets"][n - 1])
    if repair:
        text = _text("repair.txt").format(errors="; ".join(repair)) + "\n\n" + text
    return text


def system(build_prompt: str) -> str:
    """build.txt with its opening paragraph (the whole game, by tier) swapped for the stage one."""
    return _text("system_intro.txt") + "\n\n" + build_prompt.split("\n\n", 1)[1]


def turns_left(cursor) -> str:
    left = cursor.max_steps - cursor.turn
    return _text("left_last.txt" if left <= LAST else "left.txt").format(left=left, n=cursor.stage)


def _section_range(design: str, start: str) -> tuple:
    i = design.index(start)
    level = len(start) - len(start.lstrip("#"))
    ends = [design.find("\n" + "#" * n + " ", i + 1) for n in range(1, level + 1)]
    ends = [e for e in ends if e > 0]
    return i, min(ends) if ends else len(design)


def design_programs(design: str, starts: List[str], room: int = ROOM) -> List[str]:
    """Programs that print the named sections, each program's output under the runner's cap."""
    ranges = []
    for s in starts:
        if s in design:
            a, b = _section_range(design, s)
            while a < b:
                ranges.append((a, min(b, a + room - 200)))
                a += room - 200
    packs, cur, used = [], [], 0
    for a, b in ranges:
        if cur and used + (b - a) + 2 > room:
            packs.append(cur)
            cur, used = [], 0
        cur.append((a, b))
        used += b - a + 2
    packs += [cur] if cur else []
    return [_text("read_design.txt").format(ranges=repr(p)) for p in packs]


def _own_files(root: Path) -> List[str]:
    own = sorted(str(p.relative_to(root)) for p in root.rglob("*")
                 if p.is_file() and p.suffix in TEXT and not is_vendored(p, root))
    if sum(len((root / f).read_text(encoding="utf-8", errors="replace")) for f in own) > FILES_CAP:
        return []
    return own


def file_programs(root: Path, files: List[str], room: int = ROOM) -> List[str]:
    packs, cur, used = [], [], 0
    for f in files:
        size = len((root / f).read_text(encoding="utf-8", errors="replace"))
        start = 0
        while start < size:
            take = min(size - start, room - used - 100)
            if take <= 0:
                packs.append(cur)
                cur, used = [], 0
                continue
            cur.append((f, start, start + take))
            used += take + 100
            start += take
    packs += [cur] if cur else []
    return [_text("read_files.txt").format(pieces=repr(p)) for p in packs]


def preread(tools, cursor, run_dir) -> None:
    """Seeded rounds before the nudge, pinned through compaction like the build seeds."""
    from maestro.codegen import build_steps
    from maestro.codegen.build_state import BuildCursor

    plan = load(run_dir)
    s = plan["stages"][cursor.stage - 1]
    starts = s["reads"] + [r for r in SHARED_READS if r not in s["reads"]]
    root = game_dir(run_dir)
    programs = design_programs(spec_path(run_dir).read_text(encoding="utf-8"), starts)
    if root.exists():
        programs += file_programs(root, _own_files(root))
    nudge = cursor.history.pop()
    for i, code in enumerate(programs):
        tc = {"id": f"{build_steps._SEEDED}stage_{cursor.stage}_{i}", "type": "function",
              "function": {"name": "python", "arguments": json.dumps({"code": code})}}
        scratch = BuildCursor(build_id=cursor.build_id, started=True)
        build_steps._apply(tools, scratch, tc, run_dir)
        if i == len(programs) - 1 and root.exists():
            scratch.history[-1]["content"] += ("\n\n" + _text("codemap_head.txt") + "\n"
                                               + code_map.render(root))
        cursor.history += [{"role": "assistant", "content": "", "tool_calls": [tc]}] + scratch.history
    cursor.history.append(nudge)


def broken(check: Dict) -> bool:
    return not check["boots"] or bool(check["errors"])


def _rank(attempt: Dict) -> tuple:
    return (attempt["boots"], -len(attempt["errors"]), attempt["attempt"])


def begin(run_dir, n: int, attempt: int) -> None:
    """Note where the stage starts, so a retry can go back there."""
    if attempt:
        return
    plan = load(run_dir)
    plan["before"][str(n)] = snapshots.head(run_dir)
    _save(run_dir, plan)


def after_stage(run_id: str, cursor) -> bool:
    """False when the chain is done and the gates should run."""
    from maestro.codegen import build_chain, build_state, error_gate
    from maestro.state import RunState
    from tools.execution_context import run_scope

    rs = RunState(run_id)
    plan = load(rs.run_dir)
    n, attempt = cursor.stage, cursor.attempt
    with run_scope(run_id, cursor.build_id):
        check = error_gate.boot_check(game_dir(rs.run_dir))
    tried = plan["attempts"].setdefault(str(n), [])
    if not any(t["attempt"] == attempt for t in tried):
        tried.append({"attempt": attempt, "ref": snapshots.head(rs.run_dir), **check})
        _save(rs.run_dir, plan)
    logger.info("build %s: stage %d attempt %d boots=%s errors=%d", run_id, n, attempt,
                check["boots"], len(check["errors"]))
    ask = rs.read_spec()["ask"]
    if broken(check) and attempt < RETRIES:
        before = plan["before"].get(str(n))
        if before:
            snapshots.restore(rs.run_dir, before)
        build_chain.kickoff(run_id, kind="build", after=cursor.build_id, fresh=before is None,
                            stage=n, attempt=attempt + 1, request=request(ask, plan, n))
        return True
    best = max(tried, key=_rank)
    if best["ref"] and best["ref"] != snapshots.head(rs.run_dir):
        snapshots.restore(rs.run_dir, best["ref"])
        snapshots.take(rs.run_dir, f"kept stage {n} attempt {best['attempt']}")
    if n == len(plan["stages"]):
        cursor.ok = best["boots"]
        build_state.save(cursor)
        return False
    repair = (best["errors"] or ["the page does not boot"]) if broken(best) else None
    build_chain.kickoff(run_id, kind="build", after=cursor.build_id, stage=n + 1,
                        request=request(ask, plan, n + 1, repair))
    return True
