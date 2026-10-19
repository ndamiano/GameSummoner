"""The `GameSummoner` terminal view: every game this box has built, the picked game's progress,
the queues, and a pinned summon/change input. Plain event lines when stdout is not a terminal."""
import os
import re
import shutil
import sys
import time
from datetime import datetime

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
VERBS = ["Conjuring", "Invoking", "Scrying", "Weaving runes", "Binding sprites",
         "Stirring the cauldron", "Channelling mana", "Chanting", "Drawing the circle"]
STAGES = {"design": "design", "build": "build", "change": "build", "fix": "test"}
LABELS = {"design": "design", "build": "build", "test": "play-test"}
ENDED = {"building": ("33", "■ stalled"), "built": ("32", "✓ built"), "ready": ("32", "✓ ready"),
         "stopped": ("33", "■ stopped"), "failed": ("31", "✗ failed"), "held": ("33", "■ held"),
         "draft": ("2", "· draft")}
QUEUES = ["llm", "decide", "comfy"]
BRAND = ("38;2;242;112;63" if os.environ.get("COLORTERM") in ("truecolor", "24bit")
         else "38;5;209")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LIST_ROWS = 8
DETAIL_ROWS = 12


def clock(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m{s:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def visible(text: str) -> int:
    return len(ANSI.sub("", text))


def fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[:max(0, width - 1)] + "…"


class Game:
    def __init__(self, request: str, run_id: str | None = None, created: float | None = None):
        self.request = request
        self.run_id = run_id
        self.created = created or time.time()
        self.status = "building"
        self.live = True
        self.failed = None
        self.stages: dict[str, dict] = {}
        self.url = self.title = None
        self.design_note = ""
        self.step = self.max_steps = 0
        self.feed = []
        self.thought = None

    @property
    def name(self) -> str:
        return self.title or f"“{self.request}”"

    @property
    def active(self) -> bool:
        return self.failed is None and self.status == "building" and self.live

    @property
    def stage(self) -> str:
        running = [s for s, b in self.stages.items() if b["status"] in ("running", "queued")]
        if running:
            return running[-1]
        return list(self.stages)[-1] if self.stages else "design"

    @property
    def finished_at(self) -> float | None:
        ends = [b["finished_at"] for b in self.stages.values() if b.get("finished_at")]
        return max(ends) if ends else None

    def sync(self, row: dict) -> None:
        self.run_id = row["id"]
        self.request = row["request"] or self.request
        self.status, self.live, self.created = row["status"], row["live"], row["created_at"]
        self.title = row["title"] or self.title
        self.url = row["url"]
        stages = {}
        for b in sorted(row["builds"], key=lambda b: b["queued_at"]):
            if b["kind"] in STAGES:
                stages.pop(STAGES[b["kind"]], None)
                stages[STAGES[b["kind"]]] = b
        self.stages = stages

    def apply(self, detail: dict) -> None:
        self.step, self.max_steps = detail["step"], detail["max_steps"]
        self.design_note = detail["design_note"]
        self.feed, self.thought = detail["feed"], detail["thought"]


class Screen:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.live = out.isatty() and os.environ.get("TERM") != "dumb"
        self.color = self.live and "NO_COLOR" not in os.environ
        self.games: list[Game] = []
        self.picked: Game | None = None
        self.drafts: dict = {}
        self.confirm: tuple | None = None
        self.queues: dict[str, tuple[int, int]] = {}
        self.session: list[Game] = []
        self._drawn = 0
        self._said: dict[int, tuple] = {}

    def _c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def add(self, game: Game) -> None:
        self.session.append(game)
        self.games.insert(0, game)
        self.picked = game

    def merge(self, rows: list[dict]) -> None:
        known = {g.run_id: g for g in self.games if g.run_id}
        listed = []
        for row in rows:
            game = known.get(row["id"]) or Game(row["request"], row["id"], row["created_at"])
            game.sync(row)
            listed.append(game)
        ids = {r["id"] for r in rows}
        waiting = [g for g in self.games if g.run_id is None or g.run_id not in ids]
        self.games = sorted(waiting + listed, key=lambda g: g.created, reverse=True)
        if self.picked is not None and self.picked not in self.games:
            self.picked = None

    @property
    def typed(self) -> str:
        return self.drafts.get(self.picked, "")

    @typed.setter
    def typed(self, text: str) -> None:
        self.drafts[self.picked] = text

    def key(self, data: str) -> tuple | None:
        action = None
        i = 0
        while i < len(data):
            ch = data[i]
            if self.confirm:
                game, note = self.confirm
                if ch in "yY":
                    action, self.typed = ("stop_change", game, note), ""
                self.confirm = None
                i += 3 if data.startswith("\x1b[", i) else 1
                continue
            arrow = data[i:i + 3]
            if arrow in ("\x1b[A", "\x1bOA", "\x1b[B", "\x1bOB"):
                self._move(-1 if arrow[2] == "A" else 1)
                i += 3
                continue
            if ch == "\x1b":
                i += 3 if data.startswith("\x1b[", i) else 1
                continue
            if ch in "\r\n":
                action = self._send() or action
            elif ch in "\x7f\x08":
                self.typed = self.typed[:-1]
            elif ch == "\x15":
                self.typed = ""
            elif ch.isprintable():
                self.typed += ch
            i += 1
        return action

    def _send(self) -> tuple | None:
        text = self.typed.strip()
        game = self.picked
        if not text or (game is not None and game.run_id is None):
            return None
        if game is None:
            self.typed = ""
            return ("summon", text)
        if game.status == "building":
            self.confirm = (game, text)
            return None
        self.typed = ""
        return ("change", game, text)

    def _move(self, by: int) -> None:
        entries = [None] + self.games
        at = entries.index(self.picked) if self.picked in entries else 0
        self.picked = entries[max(0, min(len(entries) - 1, at + by))]

    def _spin(self) -> str:
        return self._c(BRAND, SPIN[int(time.time() * 8) % len(SPIN)])

    def _bar(self, game: Game, cells: int) -> str:
        if not game.max_steps:
            return ""
        filled = min(cells, round(cells * game.step / game.max_steps))
        return self._c(BRAND, "▰" * filled) + self._c("2", "▱" * (cells - filled))

    def _brief(self, game: Game) -> str:
        if game.failed:
            return self._c("31", f"✗ {game.failed}")
        if not game.active:
            code, word = ENDED.get(game.status, ("2", game.status))
            when = datetime.fromtimestamp(game.finished_at or game.created).strftime("%b %d %H:%M")
            return f"{self._c(code, word)} {self._c('2', when)}"
        stage = game.stage
        since = (game.stages.get(stage) or {}).get("started_at") or game.created
        took = self._c("2", clock(time.time() - since))
        if stage == "build" and game.max_steps:
            return f"build {self._bar(game, 12)} {game.step}/{game.max_steps} {took}"
        return f"{LABELS[stage]} {self._spin()} {took}"

    def _row(self, game: Game, stage: str, detail: str) -> str:
        label = self._c("1", f"{LABELS[stage]:<9}")
        b = game.stages.get(stage)
        if b is None:
            return f"  {self._c('2', '·')} {self._c('2', LABELS[stage])}"
        start = b.get("started_at") or b["queued_at"]
        if b["status"] in ("running", "queued"):
            return f"  {self._spin()} {label} {self._c('2', f'{clock(time.time() - start):>6}')}  {detail}"
        mark = {"succeeded": self._c("32", "✓"), "failed": self._c("31", "✗")}.get(b["status"], self._c("33", "■"))
        took = clock((b.get("finished_at") or start) - start)
        return f"  {mark} {label} {self._c('2', f'{took:>6}')}  {detail}"

    def _detail(self, game: Game, width: int) -> list[str]:
        head = f"  {self._c(BRAND + ';1', fit(game.name, width - 4))}"
        if game.title:
            head += f"  {self._c('36', fit('“' + game.request + '”', max(8, width - len(game.title) - 8)))}"
        lines = [head]
        if game.failed:
            lines.append(f"  {self._c('31', '✗')} {game.failed}")
            return lines
        lines.append(self._row(game, "design", self._c("2", fit(game.design_note, width - 24))))
        bar = (f"{self._bar(game, 20)} step {game.step}/{game.max_steps}"
               if game.active and game.max_steps else "")
        lines.append(self._row(game, "build", bar))
        lines.append(self._row(game, "test", ""))
        for act, n in game.feed:
            lines.append(f"    {self._c('2', '▸')} {fit(act, width - 12)}"
                         + (self._c("2", f" ×{n}") if n > 1 else ""))
        if game.thought and game.active:
            lines.append(f"    {self._c('3;2', fit('“' + game.thought + '”', width - 6))}")
        if game.url:
            lines.append(f"  {self._c('1', 'play')}  {self._c('4;36', game.url)}"
                         + (self._c("2", "  live, refresh to watch it grow") if game.active else ""))
        return lines

    def _queue_lines(self) -> list[str]:
        out = [self._c("1", "queues")]
        for q in QUEUES:
            running, waiting = self.queues.get(q, (0, 0))
            parts = []
            if running:
                parts.append(self._c("32", f"● {running} running"))
            if waiting:
                parts.append(self._c("33", f"◌ {waiting} waiting"))
            out.append(f"{q:<7} " + (" ".join(parts) or self._c("2", "–")))
        return out

    def _status(self) -> str:
        active = [g for g in self.games if g.active]
        if active:
            since = min(g.created for g in active)
            verb = VERBS[int(time.time() // 4) % len(VERBS)]
            what = "" if len(active) == 1 else f" · {len(active)} games"
            return (f"{self._c(BRAND, '◈')} {self._c(BRAND + ';1', verb + '…')}"
                    f" {self._c('2', '(' + clock(time.time() - since) + what + ')')}")
        done = [g.finished_at for g in self.session if g.finished_at]
        if done:
            n = len(self.session)
            return (f"{self._c(BRAND, '◈')} {self._c('1', 'Summoned' + ('' if n == 1 else f' {n} games'))}"
                    f" {self._c('2', '· done ' + datetime.fromtimestamp(max(done)).strftime('%-I:%M %p'))}")
        return f"{self._c(BRAND, '◈')} {self._c('2', 'Waiting for a request')}"

    def _window(self) -> list:
        entries = [None] + self.games
        if len(entries) <= LIST_ROWS:
            return entries
        at = entries.index(self.picked) if self.picked in entries else 0
        first = max(0, min(at - LIST_ROWS // 2, len(entries) - LIST_ROWS))
        return entries[first:first + LIST_ROWS]

    def _input(self) -> str:
        cursor = self._c("5", "▏")
        if self.confirm:
            return (f"  {self._c('33;1', 'stop the build and apply this change?')} "
                    f"{self._c('1', 'y')}{self._c('2', '/n')}")
        if self.picked is None:
            return f"  {self._c(BRAND + ';1', 'summon ›')} {self.typed}{cursor}"
        if self.picked.run_id is None:
            return f"  {self._c('2', 'change › (waiting for the run to start)')}"
        return f"  {self._c('36;1', 'change ›')} {self.typed}{cursor}"

    def _intro(self) -> list[str]:
        return [f"  {self._c(BRAND + ';1', 'Summon a new game')}",
                f"  {self._c('2', 'Describe any game. It is designed, built and play-tested,')}",
                f"  {self._c('2', 'and shows up in the list above.')}"]

    def lines(self) -> list[str]:
        width = shutil.get_terminal_size((100, 24)).columns
        side = width >= 100
        left = width - 36 if side else width
        shown = self._window()
        count = f"{len(self.games)} games" if len(self.games) + 1 > len(shown) else ""
        top = ["", f"  {self._c(BRAND + ';1', '◈ GameSummoner')}  {self._c('2', count)}"]
        for g in shown:
            mark = self._c(BRAND, "▶") if g is self.picked else " "
            if g is None:
                top.append(f"  {mark} {self._c(BRAND, '◈ summon a new game')}")
            else:
                top.append(f"  {mark} {fit(g.name, 26):<26}  {self._brief(g)}")
        top += [""] * (LIST_ROWS + 2 - len(top))
        queues = self._queue_lines()
        if side:
            top += [""] * max(0, len(queues) + 1 - len(top))
            top = [t + " " * max(1, left - visible(t)) + (queues[i - 1] if 0 < i <= len(queues) else "")
                   for i, t in enumerate(top)]
        else:
            top += [""] + [f"  {q}" for q in queues]
        out = top + [self._c("2", "  " + "─" * max(0, min(left, 72) - 2))]
        detail = self._intro() if self.picked is None else self._detail(self.picked, left)
        out += (detail + [""] * DETAIL_ROWS)[:DETAIL_ROWS]
        hint = self._c("2", "   ↑↓ pick · Enter send · Ctrl-C quit")
        out += ["", self._input(), "", self._status() + hint]
        return [line if visible(line) <= width else fit(ANSI.sub("", line), width) for line in out]

    def draw(self) -> None:
        if not self.live:
            self._plain()
            return
        lines = self.lines()
        up = f"\x1b[{self._drawn}F" if self._drawn else ""
        self.out.write(up + "".join(f"\x1b[2K{line}\n" for line in lines) + "\x1b[J")
        self.out.flush()
        self._drawn = len(lines)

    def _plain(self) -> None:
        for i, g in enumerate(self.session):
            seen = (g.run_id, g.stage, g.status, g.step // 10, g.failed)
            if self._said.get(i) == seen:
                continue
            self._said[i] = seen
            tag = f"[{g.run_id or g.request[:24]}]"
            if g.failed:
                line = f"{tag} {g.failed}"
            elif not g.active:
                line = f"{tag} {g.status}  {g.title or ''}  {g.url or ''}"
            elif g.stage == "build" and g.step:
                line = f"{tag} step {g.step}/{g.max_steps}"
            else:
                line = f"{tag} {LABELS[g.stage]}  {g.design_note}  {g.url or ''}"
            print(line.rstrip(), file=self.out, flush=True)
