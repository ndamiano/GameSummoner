import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("summon_view", ROOT / "scripts" / "summon_view.py")
summon_view = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summon_view)


def _build(kind, status, queued, started=None, finished=None):
    return {"kind": kind, "status": status, "queued_at": queued, "started_at": started,
            "finished_at": finished}


def _row(id, status, created, request="frogs", builds=(), live=True, title=None):
    return {"id": id, "request": request, "status": status, "created_at": created, "live": live,
            "title": title, "builds": list(builds), "url": f"http://x/{id}/index.html"}


def test_the_list_holds_every_built_game_newest_first_and_keeps_a_typed_request_until_it_has_a_run():
    screen = summon_view.Screen()
    typed = summon_view.Game("a wizard cat")
    screen.add(typed)
    old_builds = [_build("design", "succeeded", 100, 100, 150), _build("build", "succeeded", 150, 150, 900),
                  _build("fix", "succeeded", 900, 900, 1200)]
    rows = [_row("new", "building", 300.0, builds=[_build("design", "running", 300, 300)]),
            _row("old", "built", 100.0, builds=old_builds, title="GLOOP RANGER")]
    screen.merge(rows)
    assert [g.run_id for g in screen.games] == [None, "new", "old"]
    assert screen.picked is typed
    typed.run_id = "cat"
    screen.merge([_row("cat", "building", typed.created, "a wizard cat")] + rows)
    assert [g.run_id for g in screen.games] == ["cat", "new", "old"] and screen.games[0] is typed
    old = screen.games[2]
    assert (old.active, old.stage, old.finished_at, old.name) == (False, "test", 1200, "GLOOP RANGER")
    screen.merge([_row("cat", "building", typed.created, live=False)] + rows)
    assert not typed.active and "stalled" in screen._brief(typed)


def test_the_input_summons_on_the_top_row_changes_a_game_and_asks_before_stopping_a_build():
    screen = summon_view.Screen()
    screen.merge([_row("live", "building", 200.0), _row("old", "built", 100.0)])
    live, old = screen.games
    assert screen.picked is None
    assert screen.key("a wizard catX\x7f") is None and screen.typed == "a wizard cat"
    assert screen.key("\x1b[B\x1b[B") is None and screen.picked is old and screen.typed == ""
    assert screen.key("slimes never stop\r") == ("change", old, "slimes never stop")
    assert screen.key("\x1b[A") is None and screen.picked is live
    assert screen.key("faster frogs\r") is None and screen.confirm == (live, "faster frogs")
    assert screen.key("n") is None and screen.confirm is None and screen.typed == "faster frogs"
    assert screen.key("\ry") == ("stop_change", live, "faster frogs") and screen.typed == ""
    assert screen.key("\x1b[A\x1b[A") is None and screen.picked is None
    assert screen.key("\r") == ("summon", "a wizard cat") and screen.typed == ""
    assert screen.key("   \r") is None


def test_every_screen_line_fits_the_terminal_and_the_status_line_counts_live_games(monkeypatch):
    monkeypatch.setattr(summon_view.shutil, "get_terminal_size", lambda *_: type("S", (), {"columns": 80})())
    screen = summon_view.Screen()
    long = "a roguelike about a wizard cat climbing a tower " * 3
    screen.merge([_row(str(i), "building" if i < 2 else "built", 100.0 - i, long,
                       [_build("design", "succeeded", 1, 1, 2),
                        _build("build", "running" if i < 2 else "succeeded", 2, 2, 3)]) for i in range(12)])
    screen.queues = {"llm": (2, 0), "comfy": (0, 3)}
    lines = screen.lines()
    assert all(summon_view.visible(line) <= 80 for line in lines)
    assert "12 games" in lines[1]
    assert sum("▶" in line for line in lines) == 1
    assert re.search(r"….*· 2 games", lines[-1])


def test_the_screen_keeps_one_height_so_the_input_line_never_moves():
    screen = summon_view.Screen()
    screen.merge([_row("a", "built", 2.0, builds=[_build("design", "succeeded", 1, 1, 2)]),
                  _row("b", "failed", 1.0)])
    screen.games[0].apply({"step": 3, "max_steps": 200, "design_note": "9 words",
                           "feed": [["wrote a.js", 1]] * 5, "thought": "hm"})
    heights = set()
    for _ in range(3):
        heights.add(len(screen.lines()))
        screen.key("\x1b[B")
    assert len(heights) == 1
