"""Run one model-written program and answer with what it printed and what it did.

The program is a build turn's whole action, so this is the boundary the build's trust ends at: it
runs in a separate interpreter that has confined itself with seccomp, holds no environment, and
reaches the project only by asking THIS process to do it. The tools it asks for are the same
`build_tools` the build has always used, so the path jail and every reported failure are unchanged.

A static check runs first. It is not the security boundary — the filter is — but it refuses the
obvious reaches before any code runs, so the model reads "there is no os module here" instead of a
traceback from inside a library it did not know it was calling.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from maestro.codegen.pyexec import rpc

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120.0
MAX_STDOUT = 20_000
MAX_CALLS = 400          # a program that calls a tool this many times is looping, not building

# Pure-Python and side-effect free. Anything that opens a path, starts a process or reaches the
# network is absent, and absent is what the filter enforces.
SAFE_IMPORTS = frozenset({
    "json", "math", "random", "re", "itertools", "collections", "statistics", "string",
    "textwrap", "functools", "operator", "copy", "heapq", "bisect", "difflib", "hashlib",
    "unicodedata", "datetime", "decimal", "fractions", "base64"})
BANNED_CALLS = frozenset({
    "open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "globals", "locals", "vars", "input", "breakpoint"})


@dataclass
class Outcome:
    stdout: str
    ledger: List[Tuple[str, str, bool]] = field(default_factory=list)   # (tool, target, ok)
    refused: Optional[str] = None      # the static check's reason; the program never ran
    timed_out: bool = False
    failed: bool = False               # the program raised


def check(code: str) -> Optional[str]:
    """Why this program cannot run, or None. Deliberately shallow: it names what the model must
    change, and the filter catches whatever it misses."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return (f"the program is not valid Python: {e.msg} (line {e.lineno}). Nothing ran — "
                "send it again, fixed.")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in SAFE_IMPORTS:
                    return _no_module(alias.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in SAFE_IMPORTS:
                return _no_module(node.module or ".")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in BANNED_CALLS:
                return (f"{node.func.id}() is not available here. Nothing ran. Files are reached "
                        "only through read_file, write_file and edit_file.")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            return f"{node.attr} is not available here. Nothing ran."
        elif isinstance(node, ast.Name) and node.id.startswith("__"):
            return f"{node.id} is not available here. Nothing ran."
    return None


def _no_module(name: str) -> str:
    return (f"there is no module {name!r} in this session, and nothing in the program ran. "
            "The project is reached only through the tools; the standard modules that ARE "
            f"importable are {', '.join(sorted(SAFE_IMPORTS))}.")


def run(code: str, tools: Dict, timeout: float = TIMEOUT_SECONDS) -> Outcome:
    refusal = check(code)
    if refusal:
        return Outcome(stdout="", refused=refusal)

    parent, child = socket.socketpair()
    env = {"PYEXEC_FD": str(child.fileno()), "PATH": "/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join(os.path.abspath(p) for p in sys.path if p), "PYTHONHASHSEED": "0",
           "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-B", "-s", "-m", "maestro.codegen.pyexec.child"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, pass_fds=(child.fileno(),), cwd="/", start_new_session=True)
    child.close()

    ledger: List[Tuple[str, str, bool]] = []
    result = Outcome(stdout="")
    answered = False        # the child said it was finished; anything else is a death
    deadline = time.monotonic() + timeout
    try:
        proc.stdin.write(code.encode("utf-8"))
        proc.stdin.close()
        parent.settimeout(timeout)
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                result.timed_out = True
                break
            parent.settimeout(left)
            msg = rpc.recv(parent)
            if msg is None:
                break
            if msg.get("done"):
                answered = True
                result.stdout = msg.get("stdout") or ""
                result.failed = bool(msg.get("status"))
                break
            name, kwargs = msg.get("tool"), msg.get("kwargs") or {}
            if len(ledger) >= MAX_CALLS:
                rpc.send(parent, {"value": f"ERROR: this program has already made {MAX_CALLS} "
                                           "tool calls, which is more than a build turn should."})
                continue
            value, ok, target = _serve(tools, name, kwargs)
            ledger.append((name, target, ok))
            rpc.send(parent, {"value": value})
    except (socket.timeout, TimeoutError):
        result.timed_out = True
    finally:
        result.ledger = ledger
        if answered:
            # It has said everything it had to say; let it exit on its own rather than counting
            # our own kill as its death.
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            proc.kill()
        proc.stdin = None      # already closed; communicate() would flush it again
        try:
            _, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            err = b""
        parent.close()
        if not answered and not result.timed_out and proc.returncode != 3:
            logger.error("pyexec: child exited %s: %s", proc.returncode,
                         err.decode("utf-8", "replace")[:500])
            result.refused = ("the session could not be started, so nothing ran. Try the same "
                              "program again.")
        if proc.returncode == 3:
            # The filter is not optional: a turn that could not be confined is a turn that did
            # not run, and the build hears it as a tool failure rather than silently running free.
            logger.error("pyexec: %s", err.decode("utf-8", "replace")[:500])
            result.refused = ("the session could not be started safely, so nothing ran. "
                              "Try the same program again.")

    if result.timed_out:
        result.stdout += (f"\n\n[the program was still running after {int(timeout)} seconds and "
                          "was stopped. Anything it did before that stands. Do less in one "
                          "program, and never wait or loop without an end.]")
    result.stdout = result.stdout[:MAX_STDOUT]
    return result


def _serve(tools: Dict, name: str, kwargs: Dict):
    """One tool call, as a VALUE the program can use. A failure comes back as a string beginning
    ERROR, never as an exception: a raise abandons every statement after it, and a program that
    writes ten files must not lose nine of them to the first bad path (measured 2026-09-07: 5 of
    16 edits applied, 10 never attempted)."""
    fn = tools.get(name)
    target = str(kwargs.get("path") or kwargs.get("id") or kwargs.get("summary") or "")
    if fn is None:
        return f"ERROR: there is no tool called {name!r}.", False, target
    res = fn(**kwargs)
    if isinstance(res, dict) and res.get("ok") is False:
        extra = {k: v for k, v in res.items() if k not in ("ok", "error") and v}
        tail = " " + json.dumps(extra, ensure_ascii=False) if extra else ""
        return "ERROR: " + str(res.get("error")) + tail, False, target
    if not isinstance(res, dict):
        return res, True, target
    # A tool that answers with ONE thing hands the program that thing, so a call reads the way
    # build.txt says it does: `for path, verdict in check_syntax().items()`.
    for key in ("files", "content", "checked"):
        if key in res:
            return res[key], True, target
    if res.get("pending"):
        return res.get("note"), True, target
    return {k: v for k, v in res.items() if k != "ok"}, True, target
