"""Staged builds: each stage is checked when it ends, a broken one runs again from the snapshot
before it, the chain goes on from the best attempt, and the gates run only after the last stage."""

import json
import subprocess
import sys

import pytest

from db import games
from maestro.codegen import build_chain, build_state, snapshots, stages
from maestro.codegen.build_state import BuildCursor
from maestro.codegen.staging import game_dir, spec_path
from maestro.state import RunState

DESIGN = ("# BUILD SPEC\n\n# 2. CONTRACTS\n\nmodules: main.js\n\n## 2.1 Module layout\n\none file\n\n"
          "# 3. VISUAL SPEC\n\nteal sky\n\n# 4. GAMEPLAY SPEC\n\njump 3 tiles\n\n# 8. DEBUG API\n\n"
          "window.__game\n\n# 9. TESTS\n\nplay it\n")


def _plan(count=3):
    return {"stages": [{"name": f"part {i}", "stage": f"add part {i}", "done": f"part {i} shows",
                        "reads": ["# 4. GAMEPLAY SPEC"]} for i in range(1, count + 1)],
            "budgets": stages.budgets(count), "attempts": {}, "before": {}}


@pytest.fixture
def chain(tmp_runs, monkeypatch):
    games.create_game("g1", "u1")
    games.move("g1", "running", "checking")
    rs = RunState("g1")
    rs.write_spec({"ask": "frogs", "title": "frogs", "request": "a frog game"})
    spec_path(rs.run_dir).parent.mkdir(parents=True, exist_ok=True)
    spec_path(rs.run_dir).write_text(DESIGN, encoding="utf-8")
    stages._save(rs.run_dir, _plan())
    out = {"checks": [], "kicked": [], "gates": []}
    monkeypatch.setattr(build_chain.error_gate, "boot_check", lambda d: out["checks"].pop(0))
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: out["kicked"].append(kw) or "next")
    monkeypatch.setattr(build_chain.error_gate, "after_build",
                        lambda r, b: out["gates"].append(b) or False)
    monkeypatch.setattr(build_chain.play_gate, "after_build", lambda r, b: False)
    monkeypatch.setattr("maestro.codegen.archive.archive", lambda run_id: None)
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    return rs, out


def _game(rs, body):
    d = game_dir(rs.run_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.html").write_text("<html></html>", encoding="utf-8")
    (d / "main.js").write_text(body, encoding="utf-8")


def _ended(rs, n, attempt, body):
    if attempt == 0:
        stages.begin(rs.run_dir, n, 0)
    _game(rs, body)
    snapshots.take(rs.run_dir, "built")
    bid = games.create_build("g1", kind="build")
    cursor = BuildCursor(build_id=bid, stage=n, attempt=attempt, ok=True)
    build_state.save(cursor)
    return cursor


def _main_js(rs):
    return (game_dir(rs.run_dir) / "main.js").read_text()


def test_a_broken_stage_runs_again_from_where_it_started_and_the_best_attempt_carries_on(chain):
    rs, out = chain
    _ended(rs, 1, 0, "stage one")
    out["checks"] = [{"boots": True, "errors": []}]
    build_chain._post_finalize("g1", build_state.load("g1").build_id)
    assert [(k["stage"], k.get("attempt", 0)) for k in out["kicked"]] == [(2, 0)]
    assert "already plays through stage 1" in out["kicked"][-1]["request"]

    tries = [("two, three errors", {"boots": True, "errors": ["a", "b", "c"]}),
             ("two, one error", {"boots": True, "errors": ["x is not defined"]}),
             ("two, no boot", {"boots": False, "errors": []})]
    for attempt, (body, check) in enumerate(tries):
        cursor = _ended(rs, 2, attempt, body)
        out["checks"] = [check]
        build_chain._post_finalize("g1", cursor.build_id)
        if attempt < stages.RETRIES:
            assert _main_js(rs) == "stage one"
            assert (out["kicked"][-1]["stage"], out["kicked"][-1]["attempt"]) == (2, attempt + 1)
            assert out["kicked"][-1]["after"] == cursor.build_id

    assert _main_js(rs) == "two, one error"
    assert out["kicked"][-1]["stage"] == 3 and out["gates"] == []
    assert out["kicked"][-1]["request"].startswith(
        "The game in this folder does not load right now. The page throws: x is not defined.")
    attempts = stages.load(rs.run_dir)["attempts"]["2"]
    assert [(a["attempt"], a["boots"], len(a["errors"])) for a in attempts] == \
        [(0, True, 3), (1, True, 1), (2, False, 0)]


def test_the_last_stage_hands_the_game_to_the_gates(chain):
    rs, out = chain
    cursor = _ended(rs, 3, 0, "all three")
    out["checks"] = [{"boots": True, "errors": []}]

    build_chain._post_finalize("g1", cursor.build_id)

    assert out["kicked"] == [] and out["gates"] == [cursor.build_id]
    assert build_state.load("g1").ok is True
    assert games.game("g1")["status"] == "succeeded"


def test_a_stage_that_stalls_is_still_checked_but_running_out_of_compute_fails(tmp_runs, monkeypatch):
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain.threading, "Thread",
                        lambda target=None, args=(), daemon=None: type("T", (), {"start": lambda s: None})())
    for error, want in ((None, "checking"), ("compute exhausted: none left", "building")):
        rid = f"g{bool(error)}"
        games.create_game(rid, "u1")
        games.move(rid, "running", "building")
        RunState(rid).write_spec({"ask": "frogs", "request": "x"})
        bid = games.create_build(rid, kind="build")
        cursor = BuildCursor(build_id=bid, stage=2)
        build_state.save(cursor)
        build_chain._finalize(rid, RunState(rid), cursor, ok=False, error=error)
        g = games.game(rid)
        assert (g["stage"], g["status"]) == ((want, "running") if not error else ("building", "failed"))


def test_the_pre_read_programs_print_the_stage_sections_and_every_file_whole(tmp_path):
    root = tmp_path / "game"
    (root / "design").mkdir(parents=True)
    (root / "design" / "design.md").write_text(DESIGN, encoding="utf-8")
    (root / "index.html").write_text("<script src=main.js></script>", encoding="utf-8")
    big = "".join(f"const jump{i} = {i};\n" for i in range(3000))
    (root / "main.js").write_text(big, encoding="utf-8")

    def ran(programs):
        return [subprocess.run([sys.executable, "-c", p], cwd=root, capture_output=True,
                               text=True, check=True).stdout for p in programs]

    shown = "".join(ran(stages.design_programs(DESIGN, ["# 4. GAMEPLAY SPEC", "# 2. CONTRACTS",
                                                        "# 7. UX"])))
    assert shown == "# 4. GAMEPLAY SPEC\n\njump 3 tiles\n\n" \
        "# 2. CONTRACTS\n\nmodules: main.js\n\n## 2.1 Module layout\n\none file\n\n"

    outputs = ran(stages.file_programs(root, ["index.html", "main.js"]))
    assert len(outputs) > 3 and all(len(o) < stages.ROOM for o in outputs)
    joined = "".join(outputs)
    assert joined.count("==================== main.js") == 1
    body = joined.split("==================== main.js\n", 1)[1]
    assert body.replace("\n", "") == big.replace("\n", "")


def test_a_stage_is_told_its_part_and_its_turns_left(tmp_runs):
    system = stages.system("Build the whole game by tier.\n\n# How you act\n\nCall python.")
    assert system.startswith("You are a game developer.") and "by tier" not in system
    assert system.endswith("# How you act\n\nCall python.")
    assert stages.turns_left(BuildCursor(build_id="b", stage=4, max_steps=25, turn=5)) == \
        "[20 turns left in stage 4]"
    assert stages.turns_left(BuildCursor(build_id="b", stage=4, max_steps=25, turn=23)) == \
        "[2 turns left in stage 4. Make sure the game loads and this stage plays.]"
    assert stages.budgets(9) == [20, 20, 20, 25, 25, 30, 30, 35, 35]
    assert stages.budgets(3) == [20, 25, 30]


@pytest.mark.parametrize("playable, fresh, want", [(False, False, 1), (True, False, 0), (True, True, 1)])
def test_a_planned_game_with_nothing_built_yet_or_built_afresh_starts_at_stage_one(
        tmp_runs, monkeypatch, playable, fresh, want):
    monkeypatch.setattr(build_chain, "advance", lambda *a, **k: None)
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    games.create_game("g1", "u1")
    rs = RunState("g1")
    rs.write_spec({"ask": "frogs", "request": "a frog game"})
    stages._save(rs.run_dir, _plan())
    if playable:
        _game(rs, "already built")
    bid = games.create_build("g1", kind="build")

    build_chain.start_build("g1", bid, kind="build", fresh=fresh)

    cursor = build_state.load("g1")
    assert cursor.stage == want
    assert (cursor.max_steps, "This is stage 1." in cursor.request) == \
        ((20, True) if want else (build_state.DEFAULT_MAX_STEPS, False))
