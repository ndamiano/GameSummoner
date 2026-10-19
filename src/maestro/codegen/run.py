"""Run orchestrator + CLI.

  create_run(user_id)          → a fresh run dir
  open_ask(run_id, ask)        → the user's words, verbatim, as the run's ASK (and its title)
  propose_prompt(ask, id)      → open the ask and start the DESIGN that becomes the run's PROMPT;
                                 its landing starts the build (maestro.codegen.design; the web
                                 path, fire-and-forget)
  set_prompt(run_id, text)     → the human's edit of that prompt, before a rebuild
  run_build(run_id)            → kick the build off + BLOCK-poll the cursor to done (CLI only; the web
                                 path is fire-and-forget via build_chain.kickoff)
  python -m maestro.codegen.run "<request>"  → prompt → build → play path

What the run stores is what the build's one user message contains, byte for byte. The ask is
what the designer read; the prompt is what the build sends.

The build itself is a chain of llm jobs driven by build_chain's completion handler, so
`run_build`/`change_from_note` only START it and wait — the API server (where worker completions
land) must be up, same as every other queue stage.
"""

import logging
import sys
import time
import uuid
from typing import Optional

from auth import store
from billing.packages import MICROS_PER_CREDIT
from db import games, jobs
from maestro.codegen import build_chain, build_state, design
from maestro.codegen.staging import game_dir, is_staged
from maestro.state import RunState
from tools.build_events import _emit

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1.0


class BuildResult:
    """The CLI's view of a finished build (the web path is fire-and-forget and reads status/events
    instead)."""
    def __init__(self, ok: bool, steps: int, elapsed: float, summary: str = ""):
        self.ok = ok
        self.steps = steps
        self.elapsed = elapsed
        self.summary = summary


def create_run(user_id: str) -> str:
    run_id = uuid.uuid4().hex[:12]
    RunState(run_id)
    games.create_game(run_id, user_id)
    return run_id


_TITLE_CHARS = 60


def _title_of(request: str) -> str:
    """A label for the games list, cut from the prompt's first line. Mechanical: the human sees
    their text back with nothing to wait for."""
    line = (request.strip().splitlines() or [""])[0].strip()
    return line if len(line) <= _TITLE_CHARS else line[:_TITLE_CHARS].rsplit(" ", 1)[0] + "…"


def open_ask(run_id: str, ask: str) -> dict:
    """Store the user's words, VERBATIM, as the run's ask. No `request` yet: its absence is what
    says the design is still being written."""
    ask = ask.strip()
    if not ask:
        raise ValueError("the prompt is empty")
    spec = {"ask": ask, "title": _title_of(ask)}
    RunState(run_id).write_spec(spec)
    games.update_prompt_meta(run_id, spec["title"])
    return spec


def propose_prompt(ask: str, run_id: str) -> dict:
    """Open the ask and start its design — the run's prompt lands when the design does."""
    spec = open_ask(run_id, ask)
    design.enqueue(run_id, ask)
    return spec


def set_prompt(run_id: str, text: str, *, event: str = "prompt_updated") -> dict:
    """Write the run's prompt — the text the build will send as its user message. Only `request`
    moves: the ask and the title stay what the user wrote."""
    text = text.strip()
    if not text:
        raise ValueError("the prompt is empty")
    state = RunState(run_id)
    spec = state.read_spec() or {}
    spec["request"] = text
    state.write_spec(spec)
    _emit(event, run_id, title=spec.get("title", ""))
    return spec


def run_build(run_id: str, max_steps: int = build_state.DEFAULT_MAX_STEPS) -> BuildResult:
    """Stage 2 (CLI/blocking): kick the build off, then poll the durable cursor to completion. The
    build itself is fire-and-forget — build_chain enqueues each llm turn and the control-plane's
    completion handler drives the next — so this only WAITS."""
    build_chain.kickoff(run_id, kind="build", max_steps=max_steps)
    return _await_build(run_id)


def change_from_note(run_id: str, note: str,
                     max_steps: int = build_state.DEFAULT_MAX_STEPS) -> BuildResult:
    """Change a built game from a HUMAN playtest note ("let's change X"). The note becomes the
    build's request and the same turn machine reads its way in and makes the change."""
    build_chain.kickoff(run_id, kind="change", note=note, max_steps=max_steps)
    return _await_build(run_id)


def _await_build(run_id: str) -> BuildResult:
    """Block until the build's cursor reports done, then summarize it. CLI-only."""
    state = RunState(run_id)
    shown = 0
    while True:
        cursor = build_state.load(state.run_dir)
        if cursor is not None and cursor.phase == "done":
            break
        if cursor is not None and cursor.step != shown:
            shown = cursor.step
            print(f"step {shown}/{cursor.max_steps}", flush=True)
        time.sleep(_POLL_INTERVAL)
    elapsed = time.time() - cursor.t0
    logger.info("build %s: ok=%s steps=%d elapsed=%.1fs", run_id, cursor.ok, cursor.step, elapsed)
    return BuildResult(bool(cursor.ok), cursor.step, elapsed, cursor.summary)


def _new_run(request: str) -> Optional[str]:
    """A fresh run whose prompt is the DESIGN of the request, written synchronously. None when
    there is no account to own it."""
    users = store.list_users()
    if not users:
        print("no accounts yet — create one first: python -m auth.cli create <handle>")
        return None
    run_id = create_run(users[0].id)
    # The CLI is the employee path — no credit charge, but the compute budget still gates every
    # enqueue, so grant the same budget a charged build would get or step 1 is refused.
    games.charge_game(run_id, 0, MICROS_PER_CREDIT)
    open_ask(run_id, request)
    print(f"run: {run_id}\nask: {request!r}\ndesigning...")
    prompt = design.generate(run_id, request)
    print(f"design: {len(prompt.split())} words" if prompt != request.strip()
          else "design: none — the ask is the prompt")
    return run_id


def _cli_new(request: str) -> int:
    """The run + its prompt on disk, STOPPING before the build so the text can be edited first."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    run_id = _new_run(request)
    if run_id is None:
        return 1
    print(f"\nprompt file: {RunState(run_id).spec_path.resolve()}")
    print(f"edit `request` in it, then: python -m maestro.codegen.run --build {run_id}")
    return 0


def _cli_build(run_id: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    state = RunState(run_id)
    if state.read_spec() is None:
        print(f"no prompt for run {run_id!r}")
        return 1
    print("building...\n")
    return _report(run_id, state, run_build(run_id))


def _cli(request: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    run_id = _new_run(request)
    if run_id is None:
        return 1
    print()
    return _cli_build(run_id)


def _report(run_id: str, state: RunState, result: BuildResult) -> int:
    mins, secs = divmod(int(result.elapsed), 60)
    print(f"\nok={result.ok}  steps={result.steps}  elapsed={mins}m{secs:02d}s")
    if result.summary:
        print(f"summary: {result.summary}")
    print(f"game: {game_dir(state.run_dir).resolve()}")
    if result.ok and is_staged(run_id):
        print(f"play: runtime/games/{run_id}/index.html")
    return 0 if result.ok else 1


def _cli_change(run_id: str, note: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    print(f"changing {run_id} from note: {note!r}\n")
    return _report(run_id, RunState(run_id), change_from_note(run_id, note))


def _await_batch(batch_id: str, run_id: str) -> list:
    """Poll a batch to completion and report what landed. CLI-only — a CLI has no socket to report on.

    The chain is advanced by the control plane's /worker/complete, so this needs the API server up."""
    from maestro.codegen.assets import asset_path, ext_for
    seen = 0
    while True:
        rows = jobs.batch_jobs(batch_id)
        if len(rows) > seen:
            seen = len(rows)
            print(f"  {seen} job(s) queued, "
                  f"{sum(1 for j in rows if j['status'] in ('done', 'failed'))} done")
        if rows and all(j["status"] in ("done", "failed") for j in rows):
            break
        time.sleep(2.0)
    out = []
    for j in rows:
        aid = j["metadata"].get("asset_id")
        if aid and asset_path(run_id, aid, ext_for(j["metadata"].get("kind"))).exists():
            out.append(aid)
    return sorted(set(out))


def _cli_assets(run_id: str) -> int:
    from maestro.codegen.assets import add_assets
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    print(f"rendering the assets {run_id} asked for\n")
    build_id = games.create_build(run_id, kind="assets")
    games.build_started(build_id)
    out = add_assets(run_id, build_id)
    if not out["ok"]:
        print(out.get("error") or "nothing to render")
        return 1
    got = _await_batch(out["batch_id"], run_id)
    print(f"\nplanned={out['planned']}  rendered={len(got)} {got}")
    if not got:
        print("  (nothing rendered — is an image worker up? the manifest still stands; re-run to fill)")
    return 0


def _cli_history(run_id: str) -> int:
    from maestro.codegen import snapshots
    rows = snapshots.list_snapshots(RunState(run_id).run_dir)
    if not rows:
        print(f"{run_id} has no snapshots yet")
        return 1
    for r in rows:
        print(f"  {r['id']}  {r['at']}  {r['label']}")
    return 0


def _cli_restore(run_id: str, ref: str) -> int:
    from maestro.codegen import snapshots
    from maestro.codegen.staging import stage_for_play
    rs = RunState(run_id)
    try:
        snapshots.restore(rs.run_dir, ref)
    except ValueError as e:
        print(e)
        return 1
    stage_for_play(rs.run_dir, run_id)
    print(f"{run_id} restored to {ref} and re-staged")
    return 0


_HELP = """maestro — write a prompt, build a game, render its art.

usage:
  python -m maestro.codegen.run "<request>"   design the request → the design IS the prompt → build → play
  python -m maestro.codegen.run --new "<request>"         design and stop (edit the prompt first)
  python -m maestro.codegen.run --build <run_id>          build the prompt on disk
  python -m maestro.codegen.run --change <run_id> "<note>"   change a built run from a play note
  python -m maestro.codegen.run --assets <run_id>         render the art the game declared
  python -m maestro.codegen.run --evict <run_id>          archive to the bucket, reclaim the disk
  python -m maestro.codegen.run --rehydrate <run_id>      pull an evicted run back and re-stage
  python -m maestro.codegen.run --archive-all             upload every run the bucket lacks
  python -m maestro.codegen.run --history <run_id>        list the run's snapshots
  python -m maestro.codegen.run --restore <run_id> <ref>  put the game back to one, and re-stage
  python -m maestro.codegen.run --help | -h              show this help
"""
if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--help", "-h"):
        print(_HELP)
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--new":
        if len(sys.argv) < 3:
            sys.exit('usage: python -m maestro.codegen.run --new "<request>"')
        sys.exit(_cli_new(" ".join(sys.argv[2:])))
    if len(sys.argv) >= 2 and sys.argv[1] == "--build":
        if len(sys.argv) < 3:
            sys.exit("usage: python -m maestro.codegen.run --build <run_id>")
        sys.exit(_cli_build(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--change":
        if len(sys.argv) < 4:
            sys.exit('usage: python -m maestro.codegen.run --change <run_id> "<what to change>"')
        sys.exit(_cli_change(sys.argv[2], " ".join(sys.argv[3:])))
    if len(sys.argv) >= 2 and sys.argv[1] == "--assets":
        if len(sys.argv) < 3:
            sys.exit('usage: python -m maestro.codegen.run --assets <run_id>')
        sys.exit(_cli_assets(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--evict":
        if len(sys.argv) < 3:
            sys.exit("usage: python -m maestro.codegen.run --evict <run_id>")
        from maestro.codegen import archive as _archive
        _archive.evict(sys.argv[2])
        print(f"evicted {sys.argv[2]} — rehydrates on play/change, or --rehydrate")
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--rehydrate":
        if len(sys.argv) < 3:
            sys.exit("usage: python -m maestro.codegen.run --rehydrate <run_id>")
        from maestro.codegen import archive as _archive
        sys.exit(0 if _archive.rehydrate(sys.argv[2]) else 1)
    if len(sys.argv) >= 2 and sys.argv[1] == "--archive-all":
        from maestro.codegen import archive as _archive
        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
        print(f"archived {_archive.archive_missing()} run(s) the bucket lacked")
        sys.exit(0)
    if len(sys.argv) >= 2 and sys.argv[1] == "--history":
        if len(sys.argv) < 3:
            sys.exit("usage: python -m maestro.codegen.run --history <run_id>")
        sys.exit(_cli_history(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--restore":
        if len(sys.argv) < 4:
            sys.exit("usage: python -m maestro.codegen.run --restore <run_id> <ref>")
        sys.exit(_cli_restore(sys.argv[2], sys.argv[3]))
    if len(sys.argv) < 2:
        sys.exit('usage: python -m maestro.codegen.run "<request>"   |   --change <run_id> "<note>"')
    sys.exit(_cli(" ".join(sys.argv[1:])))
