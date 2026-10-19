"""The `GameSummoner` TUI's backend on this box: the control-plane database, run folders and
codegen CLI. The view calls only LocalBackend's methods, so an HTTP API backend can replace it."""
import functools
import html
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / "venv" / "bin" / "python")
sys.path.insert(0, str(ROOT / "src"))

from db import connection  # noqa: E402

STALE_SECONDS = 300
TAIL_BYTES = 256_000
FEED = 5
TOOLS = re.compile(r"\b(write_file|edit_file|read_file|generate_media|compose_world|check_syntax|play|done)"
                   r"\(\s*(?:\w+\s*=\s*)?(?:['\"]([^'\"\n]{1,80})['\"])?")
PHRASES = {"write_file": "wrote {}", "edit_file": "edited {}", "read_file": "read {}",
           "generate_media": "conjuring art: {}", "compose_world": "composing a world",
           "check_syntax": "checked the code", "play": "play-tested the game",
           "done": "declared it finished"}
SKIP_ROWS = ("game prompt",)


def describe(name: str, arg: str | None) -> str:
    phrase = PHRASES[name]
    return phrase.format(arg) if "{}" in phrase and arg else phrase.replace(" {}", "").replace(": ", "")


def turn_events(record: dict) -> tuple[list[str], str | None]:
    response = record.get("response") or {}
    acts = []
    for call in response.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name", "")
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        if name == "python":
            acts += [describe(n, a or None) for n, a in TOOLS.findall(args.get("code", ""))
                     if n != "read_file"]
        elif name in PHRASES and name != "read_file":
            acts.append(describe(name, args.get("path") or args.get("id")))
    thought = next((ln.strip() for ln in (response.get("reasoning_content") or "").splitlines()
                    if ln.strip()), None)
    return acts, thought


def design_summary(design: Path) -> tuple[int, list[str]]:
    text = design.read_text(errors="replace")
    picks = []
    for cell in re.findall(r"^\|\s*([^|:]+):\s*([^|]+?)\s*\|", text, re.M):
        if cell[0].strip().lower() not in SKIP_ROWS:
            picks.append(cell[1].strip())
    return len(text.split()), picks


def game_title(game: Path) -> str | None:
    try:
        m = re.search(r"<title>([^<]+)</title>", (game / "index.html").read_text(errors="replace"))
    except OSError:
        return None
    return html.unescape(m.group(1)).strip() if m else None


class _Turns:
    def __init__(self):
        self.offset = None
        self.feed: list = []
        self.thought = None

    def read(self, path: Path) -> None:
        try:
            with open(path, "rb") as f:
                if self.offset is None:
                    f.seek(0, os.SEEK_END)
                    self.offset = max(0, f.tell() - TAIL_BYTES)
                f.seek(self.offset)
                data = f.read()
        except OSError:
            return
        cut = data.rfind(b"\n") + 1
        self.offset += cut
        for line in data[:cut].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("kind") != "turn":
                continue
            acts, thought = turn_events(record)
            for act in acts:
                if self.feed and self.feed[-1][0] == act:
                    self.feed[-1][1] += 1
                else:
                    self.feed.append([act, 1])
            del self.feed[:-FEED]
            self.thought = thought or self.thought


class _Games(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        root = Path(self.directory)
        parts = Path(super().translate_path(path)).relative_to(root).parts
        return str(root / parts[0] / "game" / Path(*parts[1:])) if parts else str(root)

    def log_message(self, *args):
        pass


class LocalBackend:
    def __init__(self, port: int, logs: Path):
        settings = json.loads((ROOT / "src" / "config" / "settings.json").read_text())
        self.runs = Path(settings["working_directory"]).expanduser() / "runs"
        self.logs = logs
        self.base = self._serve(port)
        self.procs: list = []
        self._log_lock = threading.Lock()
        self._turns: dict[str, _Turns] = {}
        self._titles: dict[str, str | None] = {}
        self._designs: dict[str, str] = {}

    def _serve(self, port: int) -> str:
        handler = functools.partial(_Games, directory=str(self.runs))
        try:
            httpd = socketserver.ThreadingTCPServer(("0.0.0.0", port), handler)
        except OSError:
            httpd = socketserver.ThreadingTCPServer(("0.0.0.0", 0), handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return f"http://localhost:{httpd.server_address[1]}"

    def games(self) -> list[dict]:
        with connection.platform_db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id, title, status, created_at FROM games ORDER BY created_at DESC")]
            builds: dict = {}
            for b in c.execute("SELECT game_id, kind, status, queued_at, started_at, finished_at FROM builds"):
                builds.setdefault(b["game_id"], []).append(dict(b))
            stirring = {r[0] for r in c.execute(
                "SELECT game_id FROM jobs WHERE game_id IN (SELECT id FROM games WHERE status = 'building') "
                "GROUP BY game_id HAVING SUM(status IN ('pending', 'claimed')) > 0 "
                "OR MAX(MAX(created_at, COALESCE(finished_at, 0))) > ?", (time.time() - STALE_SECONDS,))}
        out = []
        for row in rows:
            live = row["id"] in stirring
            if live or row["id"] not in self._titles:
                self._titles[row["id"]] = game_title(self.runs / row["id"] / "game")
            out.append({"id": row["id"], "request": row["title"], "status": row["status"],
                        "created_at": row["created_at"], "live": live,
                        "title": self._titles[row["id"]], "builds": builds.get(row["id"], []),
                        "url": f"{self.base}/{row['id']}/index.html"})
        return out

    def detail(self, run_id: str) -> dict:
        run = self.runs / run_id
        out = {"step": 0, "max_steps": 0}
        try:
            cursor = json.loads((run / "build_state.json").read_text())
            out = {"step": cursor.get("step") or 0, "max_steps": cursor.get("max_steps") or 0}
        except (OSError, ValueError):
            pass
        if run_id not in self._designs and (run / "design.md").exists():
            words, picks = design_summary(run / "design.md")
            self._designs[run_id] = " · ".join([f"{words:,} words", *picks[:5]])
        turns = self._turns.setdefault(run_id, _Turns())
        turns.read(run / "turns.jsonl")
        return {**out, "design_note": self._designs.get(run_id, ""), "feed": list(turns.feed),
                "thought": turns.thought}

    def queues(self) -> dict:
        with connection.platform_db() as c:
            rows = c.execute("SELECT queue, status, COUNT(*) FROM jobs "
                             "WHERE status IN ('pending', 'claimed') GROUP BY queue, status").fetchall()
        out = {}
        for queue, status, n in rows:
            running, waiting = out.get(queue, (0, 0))
            out[queue] = (running + n, waiting) if status == "claimed" else (running, waiting + n)
        return out

    def summon(self, request: str, started) -> bool:
        run_id = None
        for line in self._run(["-m", "maestro.codegen.run", request]):
            if line.startswith("run:"):
                run_id = line.split(":", 1)[1].strip()
                started(run_id)
        return run_id is not None

    def change(self, run_id: str, note: str) -> None:
        for _ in self._run(["-m", "maestro.codegen.run", "--change", run_id, note], run_id):
            pass

    def stop(self, run_id: str) -> None:
        for _ in self._run(["-c", "import sys; from maestro.codegen import build_chain; "
                                  "build_chain.stop(sys.argv[1])", run_id], run_id):
            pass

    def _run(self, argv: list, run_id: str = "-"):
        proc = subprocess.Popen([PYTHON, "-u", *argv], cwd=ROOT / "src", stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        self.procs.append(proc)
        for line in proc.stdout:
            if line.startswith("run:"):
                run_id = line.split(":", 1)[1].strip()
            with self._log_lock, open(self.logs / "build.log", "a") as log:
                log.write(f"[{run_id}] {line}")
            yield line
        proc.wait()

    def close(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
