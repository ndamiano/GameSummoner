import argparse
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.request
from pathlib import Path

from summon_local import LocalBackend
from summon_view import Game, Screen

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


def _ensure_account() -> None:
    code = ("from auth import store; import secrets; "
            "store.list_users() or store.create_user('local', secrets.token_urlsafe(24), role='admin')")
    subprocess.run([PYTHON, "-c", code], cwd=ROOT / "src", check=True)


class Session:
    def __init__(self, screen: Screen, backend: LocalBackend):
        self.screen = screen
        self.backend = backend

    def act(self, action: tuple) -> None:
        if action[0] == "summon":
            game = Game(action[1])
            self.screen.add(game)
            self._spawn(self._summon, game)
        else:
            self._spawn(self._change, action[1], action[2], action[0] == "pause_change")

    def _spawn(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def _summon(self, game: Game) -> None:
        def started(run_id: str) -> None:
            game.run_id = run_id
        if not self.backend.summon(game.request, started):
            game.failed = f"never started, see {LOGS / 'build.log'}"

    def _change(self, game: Game, note: str, pause: bool) -> None:
        if pause:
            self.backend.pause(game.run_id)
        self.backend.change(game.run_id, note)

    def sync(self) -> None:
        self.screen.merge(self.backend.games())
        self.screen.queues = self.backend.queues()
        if self.screen.picked and self.screen.picked.run_id:
            self.screen.picked.apply(self.backend.detail(self.screen.picked.run_id))


def _interactive(session: Session) -> None:
    screen = session.screen
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    sys.stdout.write("\x1b[?25l")
    polled = 0.0
    try:
        while True:
            if select.select([fd], [], [], 0.12)[0]:
                action = screen.key(os.read(fd, 1024).decode(errors="ignore"))
                if action:
                    session.act(action)
            if time.time() - polled > 1:
                polled = time.time()
                session.sync()
            screen.draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?25h\n")
        sys.stdout.flush()


def _plain(session: Session) -> None:
    while True:
        session.sync()
        session.screen.draw()
        time.sleep(1)


def summon(request: str, port: int) -> int:
    started = []
    session = None
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
        screen = Screen()
        if not screen.live and not request:
            sys.exit("[summon] give a request, or run it in a terminal to type one")
        LOGS.mkdir(parents=True, exist_ok=True)
        session = Session(screen, LocalBackend(port, LOGS))
        if request:
            session.act(("summon", request))
        (_interactive if screen.live else _plain)(session)
    except KeyboardInterrupt:
        pass
    finally:
        if session:
            session.backend.close()
        for proc in reversed(started):
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="GameSummoner")
    parser.add_argument("--port", type=int, default=8130)
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("summon", help="build games from prompts and serve them")
    p.add_argument("request", nargs="*")
    p.add_argument("--port", type=int, default=8130)
    args = parser.parse_args()
    return summon(" ".join(getattr(args, "request", [])), args.port)


if __name__ == "__main__":
    raise SystemExit(main())
