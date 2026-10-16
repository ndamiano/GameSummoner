from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from maestro.codegen import error_gate

_BOOT_MS = 10_000

_START = """src => {
  const F = Object.getPrototypeOf(async function () {}).constructor;
  window.__play_done = undefined;
  window.__press_q = [];
  window.__press = (key, ms) => new Promise(res => window.__press_q.push({key, ms, res}));
  new F(src)().then(v => { window.__play_done = {value: v === undefined ? null : v}; },
                   e => { window.__play_done = {error: String(e && e.stack || e)}; });
}"""
_TURN = "window.__play_done !== undefined || window.__press_q.length > 0"


def run(root: Path, js: str, seconds: float) -> dict:
    from playwright.sync_api import sync_playwright
    deadline = time.monotonic() + seconds
    errors, blocked, console = [], [], []
    with error_gate._serve(root) as base_url, sync_playwright() as pw:
        browser = error_gate._browser(pw)
        try:
            page = error_gate._open(browser, base_url, errors, blocked, settle=False)
            if page is None:
                return {"ok": False, "error": "the page did not load: " + errors[0]["message"]}
            page.on("console", lambda m: console.append(m.text))
            try:
                page.wait_for_function("window.__game", timeout=_BOOT_MS)
            except Exception:
                return {"ok": False, "error": "window.__game never appeared within 10 s of load "
                        "— install it in the page before play() can drive the game",
                        "errors": errors, "console": console[-50:]}
            page.evaluate(_START, js)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return _late(seconds, errors, console)
                try:
                    page.wait_for_function(_TURN, timeout=left * 1000)
                except Exception:
                    return _late(seconds, errors, console)
                done = page.evaluate("window.__play_done")
                if done is not None:
                    break
                req = page.evaluate("window.__press_q[0]")
                page.keyboard.down(req["key"])
                page.wait_for_timeout(float(req["ms"]))
                page.keyboard.up(req["key"])
                page.evaluate("window.__press_q.shift().res(true)")
        finally:
            browser.close()
    if "error" in done:
        return {"ok": False, "error": "the script threw: " + done["error"],
                "errors": errors, "console": console[-50:]}
    out = {"ok": True, "result": done["value"], "errors": errors, "console": console[-50:]}
    if blocked:
        out["blocked"] = sorted(set(blocked))[:10]
    return out


def _late(seconds, errors, console) -> dict:
    return {"ok": False, "error": f"the script was still running after {int(seconds)} s",
            "errors": errors, "console": console[-50:]}


def main() -> int:
    req = json.load(sys.stdin)
    try:
        out = run(Path(req["root"]), req["js"], float(req["seconds"]))
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
