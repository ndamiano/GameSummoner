"""The error gate — open the finished game in a headless browser and hear what throws.

Three rules make the fix loop converge (docs/experiments.md, "The error gate"). ONE error per
fix build — several errors in one note close none of them. Every error carries an ADDRESS — a
browser SyntaxError arrives with an empty stack, so node supplies the file and line, and a
cross-file declaration scan supplies the pair for a redeclaration neither tool can locate alone.
A parse error's note says to fix the WHOLE file — a parser reports one error per file, so a note
aimed at the line named fixes one instance per round and grinds.

An uncaught exception is one of the few signals that satisfies the BROKEN-not-bad guardrail:
`this._doIdle is not a function` can only be met by defining it. The gate does not simulate
play — it loads the page, shows the model one screenshot and presses what the model says the
title screen offers (prompts/probe_targets.txt), each on a fresh page, and whether the game PLAYS
right stays a human question. A model that cannot be asked leaves the fixed poke (centre click,
Enter, Space) in place — losing the model is never a reason to lose the gate. 404s are excluded
deliberately: art lands after the code that draws it, and a missing file is not a broken game (a
crash while DRAWING the missing art still throws, and that is caught).

The gate is a BOUNDARY like snapshots: a probe that cannot run is logged and the build stands —
losing the gate is never a reason to lose a game.
"""

from __future__ import annotations

import base64
import functools
import http.server
import json
import logging
import re
import socketserver
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional

from llm_clients.connector import get_connector
from llm_clients.message_builder import MessageBuilder
from maestro.codegen.staging import RUNTIME_DIR

logger = logging.getLogger(__name__)

MAX_ROUNDS = 8            # measured convergence was <= 6 rounds on the worst cell
PROBE_SECONDS = 6.0       # settle time after load + pokes; boot errors land well inside it
STATE_FILE = "error_gate.json"
MAX_TARGETS = 6           # a title screen offers a few things; more is the model listing the HUD
VIEWPORT = {"width": 1280, "height": 720}

VENDORED = {p.name for p in (RUNTIME_DIR / "vendor").glob("*.js")}

_NOTE = (Path(__file__).parent / "prompts" / "error_gate_note.txt")
_TARGETS = (Path(__file__).parent / "prompts" / "probe_targets.txt")

# The model names keys the way a screen prints them; playwright wants its own names.
_KEY_NAMES = {"esc": "Escape", "escape": "Escape", "return": "Enter", "enter": "Enter",
              "space": "Space", "spacebar": "Space", "tab": "Tab", "shift": "Shift",
              "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
              "up arrow": "ArrowUp", "down arrow": "ArrowDown", "left arrow": "ArrowLeft",
              "right arrow": "ArrowRight", "arrowup": "ArrowUp", "arrowdown": "ArrowDown",
              "arrowleft": "ArrowLeft", "arrowright": "ArrowRight", "any key": "Enter"}
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

_SYNTAXY = re.compile(r"SyntaxError|redeclaration|already been declared|Unexpected (?:token|identifier|string|number|end of input)|missing [)}\]] after", re.IGNORECASE)
_REDECL = re.compile(r"redeclaration of (?:const|let|var|class|function)?\s*['\"]?(\w+)"
                     r"|Identifier '(\w+)' has already been declared")
_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


# ---------------------------------------------------------------- the probe

def probe(game_dir: Path) -> List[Dict[str, str]]:
    """Serve the game folder, load it headless, and return the uncaught errors in arrival order.

    Each error is {"message": ..., "stack": ...}. The loaded page is screenshotted and the model
    names what the title screen offers (`_targets`); each click and key is pressed on its own
    fresh page — enough to get past "press to start", deliberately no more. A script the page asked
    for and did not get is an error too: a module import that 404s stops the whole module graph
    without throwing, so nothing else would ever report it. An environment with no browser
    answers [] and logs why, per the boundary rule. The page has NO network egress: every request
    that is not the game's own ephemeral server is aborted and logged.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("error gate: playwright not installed, probe skipped")
        return []

    errors: List[Dict[str, str]] = []
    blocked: List[str] = []
    with _serve(game_dir) as base_url:
        try:
            with sync_playwright() as pw:
                browser = _browser(pw)
                page = _open(browser, base_url, errors, blocked)
                actions = _targets(page.screenshot()) if page is not None else None
                if page is not None:
                    page.close()
                if actions is None:
                    w, h = VIEWPORT["width"], VIEWPORT["height"]
                    actions = [("click", (w // 2, h // 2)), ("key", "Enter"), ("key", "Space")]
                # Each press on its own fresh page: a title that leaves on the first press would
                # otherwise hide what every later press does, and a crash on one press would be
                # blamed on the next.
                for kind, what in actions:
                    page = _open(browser, base_url, errors, blocked)
                    if page is None:
                        break
                    try:
                        if kind == "click":
                            page.mouse.click(*what)
                        else:
                            page.keyboard.press(what)
                    except Exception:
                        pass
                    page.wait_for_timeout(PROBE_SECONDS * 500)
                    page.close()
                browser.close()
        except Exception as e:
            logger.warning("error gate: probe could not run (%s) — build stands", e)
            return []
    if blocked:
        logger.warning("error gate: %s: blocked %d external request(s): %s",
                       game_dir, len(blocked), ", ".join(sorted(set(blocked))[:5]))
    return dedup(errors)


def _browser(pw):
    return pw.chromium.launch(
        proxy={"server": "http://127.0.0.1:9", "bypass": "127.0.0.1"},
        args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp"])


def _open(browser, base_url: str, errors: List[Dict[str, str]], blocked: List[str],
          settle: bool = True):
    """One fresh page on the game, wired to report: uncaught errors, a script the page asked for
    and did not get, every request that tried to leave. None when the page could not load — the
    failure to load is itself the error, and it is already recorded."""
    page = browser.new_page(viewport=VIEWPORT)

    def _no_egress(route):
        url = route.request.url
        if url.startswith(f"{base_url}/"):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    page.route("**/*", _no_egress)
    page.on("pageerror", lambda e: errors.append(
        {"message": str(e), "stack": getattr(e, "stack", "") or ""}))

    def _missing_script(response):
        req = response.request
        if response.status < 400 or req.resource_type != "script":
            return
        path = req.url[len(base_url) + 1:]
        referrer = (req.headers.get("referer") or "")[len(base_url) + 1:]
        who = f" ({referrer} asked for it)" if referrer and referrer != "index.html" else ""
        errors.append({"message": f"the page asked for {path} and it does not exist "
                                  f"(HTTP {response.status}){who} — the path in the "
                                  f"script tag or import that names it is wrong",
                       "stack": ""})

    page.on("response", _missing_script)
    try:
        page.goto(f"{base_url}/index.html", timeout=15_000, wait_until="load")
    except Exception as e:
        # A page that cannot finish loading (script hangs the parser, endless loop in module
        # init) is itself the finding when nothing threw first.
        if not errors:
            errors.append({"message": f"page did not finish loading: {e}", "stack": ""})
        page.close()
        return None
    if settle:
        page.wait_for_timeout(PROBE_SECONDS * 500)
    return page


def _targets(png: bytes) -> Optional[List[tuple]]:
    """What the title screen offers, as the model reads it from a screenshot: a list of
    ("click", (x, y)) and ("key", name), at most MAX_TARGETS of each. None when the model could not
    be asked or did not answer in shape — the caller falls back to the fixed poke."""
    prompt = _TARGETS.read_text(encoding="utf-8").format(**VIEWPORT)
    content = [{"type": "text", "text": prompt},
               {"type": "image_url", "image_url": {
                   "url": "data:image/png;base64," + base64.b64encode(png).decode()}}]
    try:
        reply = get_connector().generate_with_tools(
            MessageBuilder("You read screenshots of browser games.").add_user(content).build(),
            [], max_tokens=4000, reasoning="low")
        msg = (reply.get("choices") or [{}])[0].get("message", {}) or {}
        return _parse_targets(msg.get("content") or "")
    except Exception as e:
        logger.warning("error gate: could not ask where to press (%s) — fixed poke", e)
        return None


def _parse_targets(text: str) -> Optional[List[tuple]]:
    try:
        data = json.loads(_FENCE.sub("", text.strip()))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    actions: List[tuple] = []
    for t in (data.get("targets") or [])[:MAX_TARGETS]:
        try:
            x, y = int(t["x"]), int(t["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]:
            actions.append(("click", (x, y)))
    for k in (data.get("keys") or [])[:MAX_TARGETS]:
        if not isinstance(k, str) or not k.strip():
            continue
        name = _KEY_NAMES.get(k.strip().lower(), k.strip())
        if len(name) == 1:
            name = name.lower() if name.isalpha() else name
        actions.append(("key", name))
    return actions


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # a probe's 404s are expected, not log noise
        pass


class _serve:
    """An ephemeral static server for one probe: `with _serve(dir) as url:`."""

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self) -> str:
        handler = functools.partial(_Quiet, directory=str(self.root))
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False


def dedup(errors: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """A render loop re-throws the same error every frame; the gate wants each once, first-seen
    order kept (the first error is usually the cause and the rest its wake)."""
    seen, out = set(), []
    for e in errors:
        key = e["message"]
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


# ---------------------------------------------------------------- the address

def address(error: Dict[str, str], game_dir: Path) -> str:
    """Where the error lives, as text for the note. A runtime error carries its own stack; a
    SyntaxError reaches the browser with an empty one, so node re-parses the authored files to
    name the file and line, and a redeclaration adds every declaration site of the identifier."""
    lines: List[str] = []
    stack = (error.get("stack") or "").strip()
    if stack and not _SYNTAXY.search(error["message"]):
        lines.append(stack.splitlines()[0] if len(stack.splitlines()) == 1 else stack)
    else:
        lines.extend(_parse_addresses(game_dir))
        redecl = _REDECL.search(error["message"])
        if redecl:
            name = redecl.group(1) or redecl.group(2)
            lines.extend(_declaration_sites(name, game_dir))
    return "\n".join(lines)


def _authored_js(game_dir: Path) -> List[Path]:
    """Every .js the model wrote, wherever it put it — games keep code under game/ or js/ as
    often as at the root. The vendored renderer and lib/ beside the game are not the model's."""
    return [p for p in sorted(game_dir.rglob("*.js"))
            if not (p.parent == game_dir and p.name in VENDORED)
            and "lib" not in p.relative_to(game_dir).parts[:-1]]


def _parse_addresses(game_dir: Path) -> List[str]:
    """file:line for every authored file node cannot parse, inline scripts included. A file is
    tried as a module first (authored games import three), then as a script; only a file both
    parsers refuse is broken."""
    out = []
    for path in _authored_js(game_dir):
        err = _node_check(path.read_text(encoding="utf-8", errors="replace"))
        if err:
            out.append(f"{path.relative_to(game_dir)}: {err}")
    entry = game_dir / "index.html"
    if entry.exists():
        for i, script in enumerate(_INLINE_SCRIPT.findall(
                entry.read_text(encoding="utf-8", errors="replace"))):
            err = _node_check(script)
            if err:
                out.append(f"index.html inline script #{i + 1}: {err}")
    return out


def _node_check(source: str) -> Optional[str]:
    """The module parser's first error line, or None if either parser accepts the source. Games
    are modules, so its line is the one reported; the script pass only decides whether a file
    that is not a module is fine as a script."""
    first_err = None
    # .cjs, not .js: node 22 detects `export` in a .js file and answers a CommonJS parse failure
    # with "retry as a module" — reported as a pass, so every module file checked as .js is clean.
    for suffix in (".mjs", ".cjs"):
        with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
            f.write(source)
            tmp = f.name
        try:
            res = subprocess.run(["node", "--check", tmp], capture_output=True, text=True,
                                 timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("error gate: node --check unavailable (%s)", e)
            return None
        finally:
            Path(tmp).unlink(missing_ok=True)
        if res.returncode == 0:
            return None
        if first_err is None:
            first = next((ln for ln in res.stderr.splitlines() if ln.strip()), res.stderr)
            first_err = re.sub(re.escape(tmp), "", first).strip(" :")
    return first_err


def _node_message(source: str) -> Optional[str]:
    """The parse failure as a person would read it — "line 2: Unexpected token ';'" — or None when
    the source parses. `_node_check` answers with the location alone because its caller supplies
    the rest; a model reading the answer itself needs the message with it."""
    line = _node_check(source)
    if line is None:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(source)
        tmp = f.name
    try:
        res = subprocess.run(["node", "--check", tmp], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return f"line {line}"
    finally:
        Path(tmp).unlink(missing_ok=True)
    said = next((ln.strip() for ln in res.stderr.splitlines()
                 if ln.strip().startswith("SyntaxError:")), "")
    return f"line {line}: {said[len('SyntaxError:'):].strip()}" if said else f"line {line}"


def _declaration_sites(name: str, game_dir: Path) -> List[str]:
    pattern = re.compile(rf"\b(?:const|let|var|function|class)\s+{re.escape(name)}\b")
    out = []
    for path in _authored_js(game_dir) + [game_dir / "index.html"]:
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                out.append(f"{path.relative_to(game_dir)}:{n}: {line.strip()}")
    return out


# ---------------------------------------------------------------- the note

def note_for(error: Dict[str, str], game_dir: Path) -> str:
    template = _NOTE.read_text(encoding="utf-8")
    where = address(error, game_dir)
    parse_line = ""
    if _SYNTAXY.search(error["message"]):
        parse_line = ("A file stops parsing at its first error, so fix every occurrence of the "
                      "problem in that file, not only the line named.")
    return template.format(message=error["message"].strip(),
                           address=where or "(no location available — find it by reading the files)",
                           parse_note=parse_line).strip() + "\n"


# ---------------------------------------------------------------- the loop

def _state_path(run_dir) -> Path:
    return Path(run_dir) / STATE_FILE

def _load_state(run_dir) -> Dict:
    p = _state_path(run_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"rounds": 0, "last_message": None}

def _save_state(run_dir, state: Dict) -> None:
    _state_path(run_dir).write_text(json.dumps(state, indent=2), encoding="utf-8")


def after_build(run_id: str, build_id: str) -> bool:
    """Probe a just-finalized playable build; a thrown error re-enters the fix machine with ONE
    error's note. Runs outside the run's advance lock (kickoff blocks until it is free).
    Returns whether a fix build was started — a caller with its own next step (a stage advance)
    must stand down while the gate is still converging.

    Two stops besides a clean probe: MAX_ROUNDS spent (a game the gate cannot converge belongs
    to a human), and the same first error twice running (a fix that changed nothing will not
    change next round either)."""
    from maestro.codegen import build_chain     # late import — build_chain imports this module
    from maestro.state import RunState
    from maestro.codegen.staging import game_dir
    from tools.execution_context import run_scope

    rs = RunState(run_id)
    gdir = game_dir(rs.run_dir)
    state = _load_state(rs.run_dir)

    with run_scope(run_id, build_id):
        errors = probe(gdir)
    if not errors:
        if state["rounds"]:
            logger.info("error gate: %s clean after %d fix round(s)", run_id, state["rounds"])
        state["last_message"] = None
        _save_state(rs.run_dir, state)
        return False

    first = errors[0]
    logger.info("error gate: %s threw %d error(s); first: %s", run_id, len(errors),
                first["message"].splitlines()[0][:200])
    if state["rounds"] >= MAX_ROUNDS:
        logger.warning("error gate: %s still throwing after %d rounds — leaving it to a human",
                       run_id, state["rounds"])
        return False
    if state["last_message"] == first["message"]:
        logger.warning("error gate: %s repeated the same error two rounds running — stopping",
                       run_id)
        return False

    state["rounds"] += 1
    state["last_message"] = first["message"]
    _save_state(rs.run_dir, state)
    fix = build_chain.kickoff(run_id, kind="fix", note=note_for(first, gdir), after=build_id)
    return fix is not None
