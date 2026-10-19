"""Shared games: a built game frozen under a random id that anyone with the link can play."""

import json
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Dict, Optional

from maestro.codegen.staging import ENTRY, RUNTIME_DIR, title_of
from maestro.state import RunState

logger = logging.getLogger(__name__)

SHARES_DIR = RUNTIME_DIR / "shares"
META = "_share.json"
THUMB = "_thumb.png"
LINK = "share.json"
SHARE_ID = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
SETTLE_MS = 2500


def share_dir(share_id: str) -> Path:
    return SHARES_DIR / share_id


def exists(share_id: str) -> bool:
    return bool(SHARE_ID.match(share_id)) and (share_dir(share_id) / ENTRY).is_file()


def meta(share_id: str) -> Dict:
    try:
        return json.loads((share_dir(share_id) / META).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def thumb(share_id: str) -> Optional[Path]:
    path = share_dir(share_id) / THUMB
    return path if exists(share_id) and path.is_file() else None


def share_id_of(run_id: str) -> Optional[str]:
    try:
        sid = json.loads((RunState(run_id).run_dir / LINK).read_text(encoding="utf-8"))["id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return sid if exists(sid) else None


def share(run_id: str) -> str:
    src = RUNTIME_DIR / "games" / run_id
    if not (src / ENTRY).is_file():
        raise FileNotFoundError(f"{run_id} is not staged")
    run_dir = RunState(run_id).run_dir
    sid = share_id_of(run_id) or secrets.token_urlsafe(9)
    dst = share_dir(sid)
    tmp = SHARES_DIR / f".{sid}.{secrets.token_hex(4)}"
    shutil.copytree(src, tmp)
    (tmp / META).write_text(json.dumps({"run_id": run_id, "title": title_of(tmp) or ""}, indent=1),
                            encoding="utf-8")
    _capture_thumb(tmp)
    if dst.exists():
        shutil.rmtree(dst)
    tmp.rename(dst)
    (run_dir / LINK).write_text(json.dumps({"id": sid}), encoding="utf-8")
    return sid


def unshare(run_id: str) -> None:
    sid = share_id_of(run_id)
    if sid:
        shutil.rmtree(share_dir(sid), ignore_errors=True)
    (RunState(run_id).run_dir / LINK).unlink(missing_ok=True)


def _capture_thumb(game: Path) -> None:
    from playwright.sync_api import sync_playwright

    from maestro.codegen.error_gate import _browser, _open, _serve

    try:
        with _serve(game) as base_url, sync_playwright() as pw:
            browser = _browser(pw)
            try:
                page = _open(browser, base_url, [], [])
                if page is not None:
                    page.wait_for_timeout(SETTLE_MS)
                    (game / THUMB).write_bytes(page.screenshot())
            finally:
                browser.close()
    except Exception as e:
        logger.warning("share thumb for %s failed: %s", game.name, e)
