"""install.sh's model step: pick the llama-swap models, check ComfyUI against src/config/workflows,
confirm the plan. Prints the three entry names one per line; exits 3 when the plan is declined."""
import json
import os
import re
import select
import sys
import termios
import threading
import time
import tty
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(ROOT / "scripts"))

from summon_view import BRAND, SPIN

PROD = "gamesummoner.com runs Qwen3.8 Flash-Next (UD-Q4_K_XL) + JEV-27B-VL."
MODEL_FILE = re.compile(r"\.(safetensors|gguf|pth|pt|onnx|bin|ckpt)$")
SUGGESTED = {"llm": "gamesummoner-llm", "decide": "gamesummoner-decide", "comfy": "gamesummoner-comfy"}
ANSWERS = ("Yes, install", "No, quit without installing anything")
ESCAPES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1bO.")


def get(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def wanted() -> dict:
    try:
        d = json.loads((ROOT / "src" / "config" / "settings.json").read_text())
    except OSError:
        d = {}
    swap = d.get("local", {}).get("llama_swap", {})
    saved = {"llm": d.get("llm", {}).get("model"), "decide": swap.get("decide"), "comfy": swap.get("comfy")}
    return {k: os.environ.get(f"GS_{k.upper()}_MODEL") or saved[k] or SUGGESTED[k] for k in SUGGESTED}


def missing(info: dict) -> list[str]:
    gaps = set()
    for path in sorted((ROOT / "src" / "config" / "workflows").glob("*.json")):
        for node in json.loads(path.read_text()).values():
            if not isinstance(node, dict) or "class_type" not in node:
                continue
            kind = node["class_type"]
            if kind not in info:
                gaps.add(f"node {kind}")
                continue
            spec = {**info[kind]["input"].get("required", {}), **info[kind]["input"].get("optional", {})}
            for name, value in node["inputs"].items():
                if not (isinstance(value, str) and MODEL_FILE.search(value) and name in spec):
                    continue
                s = spec[name]
                options = s[0] if isinstance(s[0], list) else (s[1] if len(s) > 1 else {}).get("options", [])
                if value not in options:
                    gaps.add(f"model {value} ({kind})")
    return sorted(gaps)


def check_comfy(url: str, entry: str) -> list[str] | None:
    try:
        get(f"{url}/upstream/{entry}/system_stats", 300)
        return missing(get(f"{url}/upstream/{entry}/object_info", 300))
    except (OSError, ValueError):
        return None


class Setup:
    def __init__(self, url: str, models: list):
        self.url, self.models = url, models
        self.done: list = []
        self.question = None
        self.at = 0
        self.typed = ""
        self.note = ""
        self.busy = None
        self.plan: list = []
        self.color = "NO_COLOR" not in os.environ
        self.drawn = 0

    def _c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color else text

    def lines(self) -> list[str]:
        out = ["", f"  {self._c(BRAND + ';1', '◈ GameSummoner setup')}", f"  {self._c('2', PROD)}",
               f"  {self._c('2', 'Other models run; how good the games are depends on the model.')}", "",
               f"  {self._c('1', 'llama-swap')}  {self.url}  {self._c('32', '✓')}", ""]
        for label, value in self.done:
            out.append(f"  {self._c('32', '✓')} {self._c('1', f'{label:<9}')} {value}")
        if self.question:
            label, hint, mode = self.question
            out.append(f"  {self._c('1', label)} {self._c('2', '· ' + hint)}")
            if mode == "confirm":
                home = str(Path.home())
                for item in self.plan:
                    out.append(f"    {self._c('2', '·')} {item.replace(home, '~')}")
                out.append(f"  {self._c('1', 'Go ahead?')}")
                for n, answer in enumerate(ANSWERS):
                    mark = self._c(BRAND, "▶") if n == self.at else " "
                    out.append(f"  {mark} {answer}")
            elif mode == "pick":
                width = max(len(m) for m, _ in self.models)
                for n, (model, name) in enumerate(self.models):
                    mark = self._c(BRAND, "▶") if n == self.at else " "
                    out.append(f"  {mark} {model:<{width}}  {self._c('2', name)}")
            else:
                cursor = "" if self.busy else self._c("5", "▏")
                out.append(f"  {self._c(BRAND + ';1', 'comfy ›')} {self.typed}{cursor}")
            if self.note:
                out.append(f"  {self._c('31', '✗')} {self.note}")
        out.append("")
        if self.busy:
            spin = SPIN[int(time.time() * 10) % len(SPIN)]
            out.append(f"{self._c(BRAND, spin)} {self._c(BRAND + ';1', self.busy)}"
                       f" {self._c('2', '(the first start can take a minute)')}")
        elif self.question:
            keys = {"pick": "↑↓ pick · Enter choose", "type": "Enter check", "confirm": "↑↓ pick · Enter choose"}[
                self.question[2]]
            out.append(f"{self._c(BRAND, '◈')} {self._c('2', keys + ' · Ctrl-C quit')}")
        return out

    def draw(self) -> None:
        lines = self.lines()
        up = f"\x1b[{self.drawn}F" if self.drawn else ""
        sys.stderr.write(up + "".join(f"\x1b[2K{line}\n" for line in lines) + "\x1b[J")
        sys.stderr.flush()
        self.drawn = len(lines)

    def _keys(self) -> str:
        ready, _, _ = select.select([sys.stdin], [], [], 0.1)
        return os.read(sys.stdin.fileno(), 64).decode(errors="ignore") if ready else ""

    def pick(self, label: str, hint: str, start: str | None) -> str:
        ids = [m for m, _ in self.models]
        self.at = ids.index(start) if start in ids else 0
        self.question = (label, hint, "pick")
        while True:
            self.draw()
            data = self._keys()
            if "\x1b[A" in data or "\x1bOA" in data:
                self.at = max(0, self.at - 1)
            if "\x1b[B" in data or "\x1bOB" in data:
                self.at = min(len(ids) - 1, self.at + 1)
            if "\r" in data or "\n" in data:
                self.done.append((label, ids[self.at]))
                self.question = None
                return ids[self.at]

    def comfy(self, default: str) -> tuple[str, list[str]]:
        self.typed = default
        self.question = ("ComfyUI", "its entry name in llama-swap (unlisted entries are fine)", "type")
        while True:
            self.draw()
            for ch in ESCAPES.sub("", self._keys()):
                if ch in "\x7f\x08":
                    self.typed = self.typed[:-1]
                elif ch == "\x15":
                    self.typed = ""
                elif ch in "\r\n" and self.typed.strip():
                    entry = self.typed.strip()
                    found = self._check(entry)
                    if found is None:
                        self.note = f"'{entry}' did not answer as ComfyUI at {self.url}/upstream/{entry}/"
                        break
                    self.note, self.question = "", None
                    whole = self._c("2", "· every workflow node and model found") if not found else ""
                    self.done.append(("ComfyUI", f"{entry} {whole}".rstrip()))
                    return entry, found
                elif ch.isprintable():
                    self.typed += ch

    def confirm(self, plan: list[str]) -> bool:
        self.plan, self.at = plan, 0
        self.question = ("Install", "what happens next", "confirm")
        while True:
            self.draw()
            for key in re.findall(r"\x1b\[[AB]|\x1bO[AB]|[\r\n]", self._keys()):
                if key in "\r\n":
                    break
                self.at = 0 if key[-1] == "A" else 1
            else:
                continue
            self.question = None
            if self.at == 0:
                self.done.append(("Install", "yes"))
            return self.at == 0

    def _check(self, entry: str) -> list[str] | None:
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(found=check_comfy(self.url, entry)), daemon=True)
        worker.start()
        self.busy = f"Checking {entry}…"
        while worker.is_alive():
            self.draw()
            time.sleep(0.1)
        self.busy = None
        return result.get("found")


def interactive(url: str, models: list, start: dict, comfy: str, plan: list) -> tuple[list, list, bool]:
    ui = Setup(url, models)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    sys.stderr.write("\x1b[?25l")
    try:
        tty.setcbreak(fd)
        llm = ui.pick("LLM", "designs and writes the game, needs tool calls", start["llm"])
        decide = ui.pick("Decision", "JEV-27B-VL through vLLM (docs/llama-swap.md)", start["decide"])
        entry, found = ui.comfy(comfy)
        go = not found and ui.confirm(plan)
        ui.draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stderr.write("\x1b[?25h")
        sys.stderr.flush()
    return [llm, decide, entry], [f"{m} in ComfyUI ('{entry}', docs/models.md)" for m in found], go


def plain(url: str, start: dict, comfy: str) -> tuple[list, list]:
    gaps = [f"the {label} in llama-swap (set GS_{key.upper()}_MODEL)"
            for key, label in (("llm", "LLM"), ("decide", "decision model")) if start[key] is None]
    print(f"checking ComfyUI through llama-swap ('{comfy}'); the first start can take a minute", file=sys.stderr)
    found = check_comfy(url, comfy)
    if found is None:
        gaps.append(f"ComfyUI as '{comfy}' in llama-swap (set GS_COMFY_MODEL)")
    else:
        gaps += [f"{m} in ComfyUI ('{comfy}', docs/models.md)" for m in found]
    return [start["llm"], start["decide"], comfy], gaps


def main() -> int:
    url = sys.argv[1]
    try:
        models = [(m["id"], m.get("name") or "") for m in get(f"{url}/v1/models", 5)["data"]]
    except (OSError, ValueError):
        print(f"missing: llama-swap at {url} (set LLAMA_SWAP_URL)", file=sys.stderr)
        return 1
    want = wanted()
    ids = [m for m, _ in models]
    start = {k: next((v for v in (want[k], SUGGESTED[k]) if v in ids), None) for k in ("llm", "decide")}
    try:
        if sys.stdin.isatty() and sys.stderr.isatty() and models:
            chosen, gaps, go = interactive(url, models, start, want["comfy"], sys.argv[2:])
        else:
            (chosen, gaps), go = plain(url, start, want["comfy"]), True
    except KeyboardInterrupt:
        return 130
    for gap in gaps:
        print(f"missing: {gap}", file=sys.stderr)
    if gaps:
        return 1
    if not go:
        return 3
    print("\n".join(chosen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
