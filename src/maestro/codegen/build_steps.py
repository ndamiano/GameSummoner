"""The build's turn machine as a resumable step: apply the completed turn, return the next `Infer`
or `Done`. build_chain enqueues each Infer as one llm job and re-enters on its completion."""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

from llm_clients.message_builder import MessageBuilder
from maestro.codegen import code_map, file_state, turn_log
from maestro.codegen.build_state import BuildCursor
from maestro.codegen.pyexec import runner
from maestro.codegen.staging import game_dir
from maestro.services import parse_args, parse_args_checked
from maestro.tool_calls import parse_tool_calls

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).resolve().parent / "prompts"

_REDRIVE_GIVE_UP = 8
_COMPACT_ROOM = 16_000
_COMPACT_KEEP = 0.33
_DEDUP_ENOUGH = 0.30
_PROMPT_REFUSED = ("context_length_exceeded", "max_context", "max_tokens must be positive")
_NO_CALL_GIVE_UP = 4
_OUT_CAP = 30_000
_MIN_OUT = 2_000
_NUDGES = [
    "Keep going. Call the python tool with a program, or call done() in one if the game is "
    "finished.",
    "That reply carried no python tool call, so nothing ran. Emit an actual tool call whose "
    "`code` is the program.",
    "Still no program landed. Call the python tool with exactly this and nothing else: "
    "write_file(path=\"index.html\", content=\"<!doctype html><title>game</title>\"), then "
    "build the rest.",
]
_NUDGE_EXISTING = ("Still no program landed. Call the python tool with a program that calls "
                   "list_files(), then reads the file you mean to change and edits it.")


def _nudge(streak: int, run_dir) -> str:
    if streak >= len(_NUDGES) and (game_dir(run_dir) / "index.html").exists():
        return _NUDGE_EXISTING
    return _NUDGES[min(streak, len(_NUDGES)) - 1]


_TOO_BIG = ("Write the file in smaller pieces: split the game across several files, or write one "
            "section at a time. Do not repeat a whole large file to change a small part of it.")


@dataclass
class Infer:
    """Suspend here: enqueue one `llm` job with these messages, die, resume on completion."""
    messages: List[dict]
    schemas: List[dict]
    max_tokens: int
    report: Optional[str] = None


@dataclass
class Done:
    report: str


Outcome = Union[Infer, Done]


PYTHON_SCHEMA = {"type": "function", "function": {
    "name": "python",
    "description": ("Run one Python program. It may call list_files, read_file, write_file, "
                    "edit_file, generate_media, compose_world, check_syntax, play, check_off and "
                    "done. Whatever it prints comes back to you."),
    "parameters": {"type": "object",
                   "properties": {"code": {"type": "string"}},
                   "required": ["code"]}}}

SCHEMAS = [PYTHON_SCHEMA]


def _n_ctx() -> int:
    from config.settings_manager import settings_manager
    return int((settings_manager.get_settings().get("llm") or {}).get("n_ctx") or 32768)


def step(spec, run_dir, tools, cursor, result, error: Optional[str] = None) -> Outcome:
    """`result` None re-asks the turn, {} is a turn that returned nothing, `error` never arrived."""
    if not cursor.started:
        cursor.started = True
        cursor.system = (_PROMPTS / "build.txt").read_text(encoding="utf-8")
        cursor.history = [{"role": "user", "content": cursor.request or _request_from(spec)}]
        _seed(tools, cursor, run_dir)
        return _infer(run_dir, cursor)

    if error:
        if any(m in error for m in _PROMPT_REFUSED):
            cursor.turn += 1
            before = cursor.compacted
            cursor.prompt_tokens = _n_ctx()
            nxt = _infer(run_dir, cursor, report="the prompt outgrew the window; trimmed it")
            if cursor.compacted == before:
                return Done("stalled: the prompt is over the window and nothing is left to trim")
            return nxt
        cursor.turn += 1
        cursor.no_call_streak += 1
        cursor.history.append({"role": "user", "content":
                               "The inference server could not read your last reply, so nothing "
                               f"was saved. It reported: {_clip(error, 200)}\n\nA reply too large "
                               f"to parse is the usual cause. {_TOO_BIG}"})
        if cursor.no_call_streak >= _NO_CALL_GIVE_UP:
            return Done(f"stalled: {cursor.no_call_streak} turns the server could not read")
        return _infer(run_dir, cursor, report=f"turn rejected by the server: {_clip(error, 80)}")

    if result is None:
        cursor.redriven += 1
        if cursor.redriven >= _REDRIVE_GIVE_UP:
            return Done(f"stalled: {cursor.redriven} turns never ran")
        return _infer(run_dir, cursor, report="re-sent the turn that never ran")

    usage = result.get("usage") or {}
    cursor.prompt_tokens = usage.get("prompt_tokens") or cursor.prompt_tokens
    if usage.get("prompt_tokens"):
        sent = len(cursor.system) + sum(len(json.dumps(m)) for m in cursor.history)
        cursor.chars_per_token = sent / usage["prompt_tokens"]
    message = (result.get("choices") or [{}])[0].get("message", {}) or {}
    content = message.get("content") or ""
    calls = [tc for tc in (message.get("tool_calls") or []) if tc.get("function", {}).get("name")]
    if not calls:
        calls = parse_tool_calls(content, SCHEMAS)
    cursor.turn += 1

    if not calls and (usage.get("completion_tokens") or 0) >= cursor.out_cap - 32:
        cursor.no_call_streak += 1
        if cursor.no_call_streak >= _NO_CALL_GIVE_UP:
            return Done(f"stalled: {cursor.no_call_streak} turns cut off by the output limit")
        return _infer(run_dir, cursor, report="the reply ran out of room — sending the turn again",
                      full_window=True)

    reply = {"role": "assistant", "content": content}
    if message.get("reasoning_content"):
        reply["reasoning_content"] = message["reasoning_content"]

    if not calls:
        cursor.no_call_streak += 1
        cursor.history.append(reply)
        cursor.history.append({"role": "user", "content":
                               _nudge(cursor.no_call_streak, run_dir)})
        if cursor.no_call_streak >= _NO_CALL_GIVE_UP:
            return Done(f"stalled: {cursor.no_call_streak} turns with no tool call")
    else:
        if len(content) > 2000:
            reply["content"] = "[…analysis truncated…]\n" + content[-2000:]
        cursor.no_call_streak = 0
        cursor.history.append({**reply, "tool_calls": calls[:1]})
        _apply(tools, cursor, calls[0], run_dir)
        if len(calls) > 1:
            cursor.history.append({"role": "user", "content":
                                   f"Only the first program ran; the other {len(calls) - 1} did "
                                   "not. Send one program per reply — what the first one printed "
                                   "is above, so write the next one knowing it."})
        if cursor.finished:
            return Done(f"done after {cursor.turn} turn(s): {cursor.summary}")

    if cursor.turn >= cursor.max_steps:
        return Done(f"hit the {cursor.max_steps}-turn cap")
    return _infer(run_dir, cursor)


def _clip(text: str, n: int) -> str:
    """Cut to `n` chars on a word boundary."""
    text = " ".join(str(text).split())
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


def _opening_line(cursor) -> str:
    sent = _clip(cursor.history[0]["content"] if cursor.history else "", 300)
    label = {"fix": "sent the fix note", "change": "sent the change note"}.get(cursor.kind, "sent the prompt")
    return f"{label}: {sent}" if sent else label


def _actions_of(outcome) -> List[str]:
    """One turn's program as build-feed lines, repeated calls folded."""
    if outcome.refused:
        return [f"program refused: {_clip(outcome.refused, 110)}"]
    verb = {"write_file": "wrote", "edit_file": "edited", "read_file": "read",
            "generate_media": "asked for art", "compose_world": "built a world",
            "list_files": "listed files", "check_syntax": "checked the syntax",
            "play": "played", "check_off": "checked off", "done": "done"}
    lines, seen = [], {}
    for name, target, ok in outcome.ledger:
        if name == "done":
            lines.append("said it is done")
            continue
        key = (verb.get(name, name), ok)
        seen.setdefault(key, [])
        if target and target not in seen[key]:
            seen[key].append(target)
        elif not target:
            seen[key].append("")
    for (word, ok), targets in seen.items():
        named = [x for x in targets if x]
        failed = "" if ok else " — failed"
        if len(named) > 3:
            noun = "asks" if word.startswith("asked") else "files"
            lines.append(f"{word} {len(named)} {noun}" + (failed and " — all failed"))
        elif named:
            lines.append(f"{word} {', '.join(named)}{failed}")
        else:
            lines.append(f"{word}{failed}")
    if outcome.timed_out:
        lines.append("the program ran out of time and was stopped")
    elif outcome.failed:
        lines.append("the program raised")
    return lines or ["ran a program that called nothing"]


def _repeat_note(cursor, code: str, outcome) -> Optional[str]:
    if not (outcome.refused or outcome.failed or any(not ok for _, _, ok in outcome.ledger)):
        cursor.repeat_counts.pop(_sig(code), None)
        return None
    sig = _sig(code)
    n = cursor.repeat_counts.get(sig, 0) + 1
    cursor.repeat_counts[sig] = n
    if n < 2:
        return None
    return (f"\n\nYou have now sent this exact program {n} times and it has gone wrong every "
            "time. Try something new.")


def _sig(code: str) -> str:
    return hashlib.sha1(code.encode("utf-8")).hexdigest()


def _result_of(outcome) -> str:
    """What the turn hears back: what the program printed, then the calls it made."""
    if outcome.refused:
        return outcome.refused
    body = outcome.stdout.strip() or "(the program ran and printed nothing)"
    if not outcome.ledger:
        return body
    lines = [f"  {name} {target}".rstrip() + ("" if ok else "  — FAILED")
             for name, target, ok in outcome.ledger]
    failed = sum(1 for _, _, ok in outcome.ledger if not ok)
    if len(lines) > 40:
        lines = lines[:20] + [f"  … {len(lines) - 40} more …"] + lines[-20:]
    tail = "\n\n[what the program called:\n" + "\n".join(lines) + "\n]"
    if failed:
        tail += (f"\n[{failed} of those FAILED. A failed call returns its error as a string "
                 "instead of doing anything; everything after it still ran.]")
    return body + tail


def _cut_off(tc) -> bool:
    """Did this call's argument JSON stop mid-write at the output cap?"""
    return not parse_args_checked(tc["function"].get("arguments"))[1]


def _apply(tools, cursor, tc, run_dir) -> bool:
    """Run one program and record what it did. Answers whether the build is finished."""
    if _cut_off(tc):
        cursor.actions.append("the program was cut off by the output limit")
        cursor.history.append({"role": "tool", "tool_call_id": tc.get("id"),
                               "content": "Your reply hit the output token limit part-way through "
                                          f"the program, so nothing ran. {_TOO_BIG}"})
        return False
    code = parse_args(tc["function"].get("arguments")).get("code")
    if not isinstance(code, str) or not code.strip():
        cursor.actions.append("no program in the call")
        cursor.history.append({"role": "tool", "tool_call_id": tc.get("id"),
                               "content": "That call carried no `code`, so nothing ran. Send the "
                                          "program as the `code` argument."})
        return False

    outcome = runner.run(code, tools, game_dir(run_dir))
    cursor.actions.extend(_actions_of(outcome))
    content = _result_of(outcome) + (_repeat_note(cursor, code, outcome) or "")

    finished = [t for name, t, ok in outcome.ledger if name == "done" and ok]
    cursor.history.append({"role": "tool", "tool_call_id": tc.get("id"), "content": content})
    if finished:
        cursor.finished = True
        cursor.summary = _clip(finished[0], 400)
    return cursor.finished


def _estimate(cursor) -> int:
    """Tokens the next prompt will cost: the server's last count plus what was added since."""
    chars = lambda ms: sum(len(json.dumps(m)) for m in ms)
    if cursor.prompt_tokens:
        return cursor.prompt_tokens + chars(cursor.history[cursor.logged:]) // 3
    return (len(cursor.system) + chars(cursor.history)) // 3


def _compact(run_dir, cursor, ctx: int) -> None:
    # A build transcript runs ~3 chars a token until the server has counted one.
    if compact(run_dir, cursor, int(ctx * _COMPACT_KEEP * (cursor.chars_per_token or 3))):
        cursor.compacted += 1
        cursor.prompt_tokens = 0


def _infer(run_dir, cursor, report: Optional[str] = None, *, full_window: bool = False) -> Infer:
    ctx = _n_ctx()
    if _estimate(cursor) > ctx - _COMPACT_ROOM or (
            full_window and _estimate(cursor) > ctx * _COMPACT_KEEP):
        _compact(run_dir, cursor, ctx)
    if ctx - _estimate(cursor) < _MIN_OUT:
        cursor.prompt_tokens = ctx
        _compact(run_dir, cursor, ctx)
    msgs = MessageBuilder(cursor.system).extend(cursor.history).build()
    report = report or ", ".join(cursor.actions) or (
        _opening_line(cursor) if cursor.turn == 0 else "no tool call — asked again")
    if cursor.compacted:
        report += f" (compacted {cursor.compacted}×)"
    cursor.actions = []
    room = max(_MIN_OUT, ctx - _estimate(cursor))
    cursor.out_cap = room if full_window else min(_OUT_CAP, room)
    return Infer(msgs, SCHEMAS, cursor.out_cap, report=report)


def rounds(history: List[dict]) -> List[List[dict]]:
    """The transcript after the request as rounds: an assistant message and its tool results."""
    out, cur = [], []
    for m in history[1:]:
        if m["role"] != "tool" and cur:
            out.append(cur)
            cur = []
        cur.append(m)
    if cur:
        out.append(cur)
    return out


_SUPERSEDED = "a newer copy is later in this transcript"
_ON_DISK = "on disk; not repeated here"
_BODY = 200


def _program_of(tc: dict) -> Optional[str]:
    if (tc.get("function") or {}).get("name") != "python":
        return None
    code = parse_args(tc["function"].get("arguments")).get("code")
    return code if isinstance(code, str) else None


def _written_in(code: str) -> List[tuple]:
    """Every literal file body the program writes, as (path, span, chars)."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    stores: Dict[str, int] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            stores[n.id] = stores.get(n.id, 0) + 1
    bound = {t.id: n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}

    def literals(node, through=frozenset()) -> Optional[list]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = literals(node.left, through), literals(node.right, through)
            return None if left is None or right is None else left + right
        if (isinstance(node, ast.Name) and stores.get(node.id) == 1 and node.id in bound
                and node.id not in through):
            return literals(bound[node.id], through | {node.id})
        return None

    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "write_file"):
            continue
        args = {kw.arg: kw.value for kw in node.keywords}
        path = node.args[0] if node.args else args.get("path")
        body = node.args[1] if len(node.args) > 1 else args.get("content")
        if not (isinstance(path, ast.Constant) and isinstance(path.value, str)) or body is None:
            continue
        for lit in literals(body) or []:
            if len(lit.value) >= _BODY:
                out.append((path.value, (lit.lineno, lit.col_offset,
                                         lit.end_lineno, lit.end_col_offset), len(lit.value)))
    return out


def _splice(code: str, spans: List[tuple]) -> str:
    """The program with each span replaced, cut from the end so earlier offsets stay true."""
    lines = code.split("\n")
    for (l1, c1, l2, c2), text in sorted(spans, key=lambda s: s[0], reverse=True):
        head = lines[l1 - 1][:c1]
        tail = lines[l2 - 1][c2:]
        lines[l1 - 1:l2] = [head + text + tail]
    return "\n".join(lines)


def _stub_call(tc: dict, why: str) -> Optional[dict]:
    """The program with its written-out file bodies replaced by a stub saying where they are."""
    code = _program_of(tc)
    if not code:
        return None
    spans = list({span: (span, json.dumps(f"[the {n} chars written to {path} — {why}]"))
                  for path, span, n in reversed(_written_in(code))}.values())
    if not spans:
        return None
    args = parse_args(tc["function"].get("arguments"))
    args["code"] = _splice(code, spans)
    return {**tc, "function": {**tc["function"],
                               "arguments": json.dumps(args, ensure_ascii=False)}}


def _write_keys(tc: dict) -> List[tuple]:
    return [("write", path) for path, _, _ in _written_in(_program_of(tc) or "")]


_LOOKING = {"read_file", "list_files", "check_syntax", "play"}


def _called_in(code: str) -> set:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def _read_only(group: List[dict]) -> bool:
    """A round whose programs called only tools that change nothing."""
    calls = group[0].get("tool_calls") or []
    if not calls or any(_program_of(tc) is None for tc in calls):
        return False
    named = set().union(*(_called_in(_program_of(tc)) for tc in calls))
    return bool(named & _LOOKING) and not (named - _LOOKING - {"print", "len", "range",
                                                               "sorted", "enumerate", "str",
                                                               "int", "repr", "set", "list"})


def drop_read_only_rounds(history: List[dict]) -> List[dict]:
    groups = rounds(history)
    return history[:1] + [m for g in groups if not _read_only(g) for m in g]


def trim_bodies(history: List[dict], rounds_to_trim: int) -> List[dict]:
    """The oldest `rounds_to_trim` rounds with their file bodies stubbed."""
    groups = rounds(history)
    out = list(history[:1])
    for i, g in enumerate(groups):
        for m in g:
            if i < rounds_to_trim and m.get("tool_calls"):
                m = {**m, "tool_calls": [_stub_call(tc, _ON_DISK) or tc for tc in m["tool_calls"]]}
            out.append(m)
    return out


def strip_reasoning(history: List[dict]) -> List[dict]:
    return [{k: v for k, v in m.items() if k != "reasoning_content"} for m in history]


def dedupe_bodies(history: List[dict]) -> List[dict]:
    """Every write older than the newest write of its path becomes a stub."""
    seen: set = set()
    out = []
    for m in reversed(history[1:]):
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                keys = _write_keys(tc)
                if keys and all(k in seen for k in keys):
                    tc = _stub_call(tc, _SUPERSEDED) or tc
                seen.update(keys)
                calls.append(tc)
            m = {**m, "tool_calls": calls}
        out.append(m)
    return history[:1] + out[::-1]


_NOTE_HEAD = ("[Earlier steps were trimmed to save room. Every file is on disk exactly as you last "
              "wrote or read it. Below is the whole project: each file, what it imports, and every "
              "declaration with its line range. Read a file only to edit it, and read the lines "
              "you need with offset and lines rather than the whole file.]\n\n")


def is_note(m: dict) -> bool:
    return m.get("role") == "user" and str(m.get("content", "")).startswith(_NOTE_HEAD)


_SEED = _PROMPTS / "build_seed"
_SEEDED = "call_seeded_"


@functools.lru_cache(maxsize=1)
def _nudge_after_seed() -> str:
    return (_SEED / "nudge.txt").read_text(encoding="utf-8").strip()


def _seed(tools, cursor, run_dir) -> None:
    """Open the build on prompts/build_seed/ programs, really run, then the nudge after them."""
    scratch = BuildCursor(build_id=cursor.build_id, started=True)
    for i, path in enumerate(sorted(_SEED.glob("[0-9]*.txt"))):
        code = path.read_text(encoding="utf-8").strip()
        tc = {"id": f"{_SEEDED}{i}", "type": "function",
              "function": {"name": "python", "arguments": json.dumps({"code": code})}}
        cursor.history.append({"role": "assistant", "content": "", "tool_calls": [tc]})
        scratch.history = []
        _apply(tools, scratch, tc, run_dir)
        cursor.history.extend(scratch.history)
    cursor.history.append({"role": "user", "content": _nudge_after_seed()})


def is_seed(m: dict) -> bool:
    ids = [tc.get("id", "") for tc in m.get("tool_calls") or []] + [m.get("tool_call_id") or ""]
    return (any(i.startswith(_SEEDED) for i in ids)
            or (m.get("role") == "user" and m.get("content") == _nudge_after_seed()))


def compact(run_dir, cursor, keep_chars: int) -> int:
    """Returns rounds trimmed or dropped, 1 when only the dedupe pass changed anything, else 0."""
    seeded = [m for m in cursor.history if is_seed(m)]
    cursor.history = [m for m in cursor.history if not is_seed(m)]
    try:
        return _compact_rounds(run_dir, cursor, keep_chars)
    finally:
        cursor.history = cursor.history[:1] + seeded + cursor.history[1:]


def _compact_rounds(run_dir, cursor, keep_chars: int) -> int:
    size = lambda msgs: sum(len(json.dumps(m)) for m in msgs)
    was = size(cursor.history)
    base = [m for m in cursor.history if not is_note(m)]
    history = drop_read_only_rounds(dedupe_bodies(strip_reasoning(base)))
    deduped = history != base
    if deduped and size(history) <= was * (1 - _DEDUP_ENOUGH):
        keep_chars = max(keep_chars, size(history))
    groups = rounds(history)
    whole = [size(g) for g in groups]
    stubbed = [size(g) for g in rounds(trim_bodies(history, len(groups)))]
    total, trimmed = sum(whole), 0
    while total > keep_chars and trimmed < len(groups):
        total += stubbed[trimmed] - whole[trimmed]
        trimmed += 1
    if whole[:trimmed] == stubbed[:trimmed]:
        trimmed = 0
    history = trim_bodies(history, trimmed) if trimmed else history
    groups = rounds(history)
    dropped, removed = 0, 0
    while groups and total > keep_chars:
        g = groups.pop(0)
        total -= size(g)
        dropped += 1
        removed += len(g)
    if not deduped and not trimmed and not dropped:
        return 0
    kept = [m for g in groups for m in g]
    root = game_dir(run_dir)
    listing = "\n".join(
        f"{p.relative_to(root)} ({p.stat().st_size} bytes)"
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.startswith("_") and str(p.relative_to(root)).startswith("assets/"))
    state = file_state.render(run_dir, root)
    note = {"role": "user", "content": _NOTE_HEAD
            + (code_map.render(root) or "(no source files yet)")
            + (f"\n\n{state}" if state else "")
            + (f"\n\nArt on disk:\n{listing}" if listing else "")}
    cursor.history = history[:1] + [note] + kept
    turn_log.append_compact(run_dir, turn=cursor.turn, trimmed=trimmed, dropped=dropped,
                            note=note["content"])
    cursor.logged = max(1, cursor.logged - removed + 1)
    return trimmed + dropped or 1


def _request_from(spec) -> str:
    return str((spec or {}).get("request") or "").strip() or "Make a small, playable browser game."
