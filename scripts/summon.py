
import argparse
import functools
import http.server
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = str(ROOT / "venv" / "bin" / "python")
CONTROL_PLANE = "http://localhost:8000/healthz"
LOGS = Path(os.environ.get("GAMESUMMONER_LOGS", str(Path.home() / ".gamesummoner" / "logs")))
AUTO_ARGV = [PYTHON, "scripts/local_gpu.py", "auto", "--idle-exit", "600"]


def _up(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except OSError:
        return False


def _auto_running() -> bool:
    return subprocess.run(["pgrep", "-fx", r"(\S*/)?python[0-9.]* scripts/local_gpu.py auto( .*)?"],
                          capture_output=True).returncode == 0


def _spawn(name: str, argv: list) -> subprocess.Popen:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.log", "a")
    print(f"[summon] starting {name} — log {LOGS / f'{name}.log'}", flush=True)
    return subprocess.Popen(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)


_BUSY = """
import sys
from db import connection
with connection.platform_db() as c:
    print(c.execute(
        "SELECT (SELECT COUNT(*) FROM jobs WHERE game_id = ? AND status IN ('pending', 'claimed'))"
        " + (SELECT COUNT(*) FROM builds WHERE game_id = ? AND status IN ('queued', 'running'))",
        (sys.argv[1], sys.argv[1])).fetchone()[0])
"""


def _busy(run_id: str) -> bool:
    out = subprocess.run([PYTHON, "-c", _BUSY, run_id], cwd=ROOT / "src", capture_output=True,
                         text=True, check=True)
    return int(out.stdout.strip()) > 0


def _settle(run_id: str) -> None:
    quiet = 0
    told = False
    while quiet < 12:
        if _busy(run_id):
            quiet = 0
            if not told:
                print("[summon] play-testing the game and fixing what it finds...", flush=True)
                told = True
        else:
            quiet += 1
        time.sleep(5)


def _ensure_account() -> None:
    code = ("from auth import store; import secrets; "
            "store.list_users() or store.create_user('local', secrets.token_urlsafe(24))")
    subprocess.run([PYTHON, "-c", code], cwd=ROOT / "src", check=True)


def _build(request: str) -> tuple:
    proc = subprocess.Popen([PYTHON, "-u", "-m", "maestro.codegen.run", request], cwd=ROOT / "src",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    game, ok, run_id = None, False, None
    with open(LOGS / "build.log", "a") as log:
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if re.match(r"step \d+/\d+$", line.strip()):
                print(f"\r{line.strip()}", end="", flush=True)
            elif line.startswith(("run:", "design", "building", "ok=", "summary:", "game:")):
                print(("\n" if line.startswith("ok=") else "") + line.rstrip()[:160], flush=True)
            if line.startswith("run:"):
                run_id = line.split(":", 1)[1].strip()
            if line.startswith("game:"):
                game = Path(line.split(":", 1)[1].strip())
            if line.startswith("ok=True"):
                ok = True
    proc.wait()
    return ok, game, run_id


def _serve(game: Path, port: int) -> None:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(game))
    handler.log_message = lambda *a: None
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), handler) as httpd:
        print(f"\n[summon] play: http://localhost:{httpd.server_address[1]}/index.html"
              "  (Ctrl-C to stop)", flush=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        while True:
            time.sleep(3600)


def summon(request: str, port: int) -> int:
    started = []
    try:
        if not _up(CONTROL_PLANE):
            started.append(_spawn("control-plane", [PYTHON, "run.py"]))
            while not _up(CONTROL_PLANE):
                if started[-1].poll() is not None:
                    sys.exit(f"[summon] the control plane exited — see {LOGS / 'control-plane.log'}")
                time.sleep(1)
        if not _auto_running():
            started.append(_spawn("local-gpu", AUTO_ARGV))
        _ensure_account()
        ok, game, run_id = _build(request)
        if run_id:
            _settle(run_id)
        if game is None:
            print(f"[summon] the build produced no game — see {LOGS / 'build.log'}")
            return 1
        if not ok:
            print("[summon] the build did not finish cleanly; serving what it made")
        _serve(game, port)
    except KeyboardInterrupt:
        print()
    finally:
        for proc in reversed(started):
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="GameSummoner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("summon", help="build a game from a prompt and serve it")
    p.add_argument("request", nargs="+")
    p.add_argument("--port", type=int, default=8130)
    args = parser.parse_args()
    return summon(" ".join(args.request), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
