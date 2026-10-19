"""Where a game lives on disk (`<run_dir>/game/`: the model's files plus what seed_vendor put there)
and how it reaches the browser: copied to `runtime/games/<slug>/` minus design/ and docs/, which
are ours, not the game's."""

import html
import os
import re
import shutil
from pathlib import Path
from typing import Optional

RUNTIME_DIR = Path(__file__).resolve().parents[3] / "runtime"
GAME_DIR = "game"
ENTRY = "index.html"
SPEC_FILE = "design.md"
DESIGN_DIR = "design"
DOCS_DIR = "docs"
_PRIVATE = (DESIGN_DIR, DOCS_DIR)


def game_dir(run_dir) -> Path:
    return Path(run_dir) / GAME_DIR


def spec_path(run_dir) -> Path:
    return Path(run_dir) / SPEC_FILE


def seed_vendor(run_dir) -> None:
    """Copy the vendored renderer and the helper library into the game folder. A game may fetch
    nothing at runtime, so a 3D one can only import three.js if it is already a file beside it.
    An existing copy is left alone: a re-seed (a fix, a resumed build) must not overwrite what
    the model edited."""
    d = game_dir(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    for src in sorted((RUNTIME_DIR / "vendor").glob("*.js")):
        dst = d / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
    for sub, pattern in (("lib", "*.js"), (DOCS_DIR, "*.md")):
        (d / sub).mkdir(exist_ok=True)
        for src in sorted((RUNTIME_DIR / "vendor" / sub).glob(pattern)):
            dst = d / sub / src.name
            if not dst.exists():
                shutil.copy2(src, dst)
    spec = spec_path(run_dir)
    if spec.is_file() and not (d / DESIGN_DIR / SPEC_FILE).exists():
        (d / DESIGN_DIR).mkdir(exist_ok=True)
        shutil.copy2(spec, d / DESIGN_DIR / SPEC_FILE)


def is_vendored(path: Path, game_dir: Path) -> bool:
    """Seeded by the platform, not written by the model."""
    rel = path.relative_to(game_dir)
    if rel.parts and rel.parts[0] in _PRIVATE:
        return True
    return rel.suffix == ".js" and (RUNTIME_DIR / "vendor" / rel).is_file()


def has_authored_files(run_dir) -> bool:
    """Has a build written anything into the game folder? The vendored renderer is seeded there
    before the model runs, so its presence says nothing — this is what tells a run with a dead
    build's leftovers apart from one with an empty folder."""
    d = game_dir(run_dir)
    if not d.is_dir():
        return False
    return any(p.is_file() and not is_vendored(p, d) for p in d.rglob("*"))


def entry_path(run_dir) -> Path:
    return game_dir(run_dir) / ENTRY


def is_staged(slug: str) -> bool:
    return (RUNTIME_DIR / "games" / slug / ENTRY).exists()


_TITLE = re.compile(r"<title[^>]*>\s*(.*?)\s*</title>", re.IGNORECASE | re.DOTALL)


def staged_title(slug: str) -> Optional[str]:
    """The name the MODEL gave the game — its staged index.html's <title> — or None when there
    is no staging, no tag, or an empty one."""
    return title_of(RUNTIME_DIR / "games" / slug)


def title_of(game: Path) -> Optional[str]:
    entry = game / ENTRY
    if not entry.is_file():
        return None
    match = _TITLE.search(entry.read_text(encoding="utf-8", errors="replace")[:4096])
    if not match:
        return None
    return " ".join(html.unescape(match.group(1)).split())[:80] or None


def stage_for_play(run_dir, slug: str) -> str:
    """Publish the game folder, replacing any previous staging of the same run.

    The new copy is BUILT BESIDE the live one and swapped in by rename, because the live one is
    what someone is playing: deleting it first means every request during the copy 404s, and a
    render landing mid-delete fails the whole staging half-way through, leaving the played game
    with some of its art gone (measured 2026-09-10: `OSError: Directory not empty: 'assets'`, 31
    of 55 assets deleted from a staged game). A rename is atomic, so a player sees the old game
    or the new one and never the gap between them."""
    games = RUNTIME_DIR / "games"
    games.mkdir(parents=True, exist_ok=True)
    dst = games / slug
    fresh = games / f"_{slug}.staging"
    stale = games / f"_{slug}.stale"
    for leftover in (fresh, stale):                 # a previous crash owes us nothing
        shutil.rmtree(leftover, ignore_errors=True)
    src = game_dir(run_dir)
    shutil.copytree(src, fresh, ignore=lambda d, names: [n for n in names if n.startswith("_")
                                                          or (Path(d) == src and n in _PRIVATE)])
    if dst.exists():
        os.replace(dst, stale)
    os.replace(fresh, dst)
    shutil.rmtree(stale, ignore_errors=True)
    return f"games/{slug}/{ENTRY}"
