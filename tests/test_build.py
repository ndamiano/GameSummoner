"""The build path: file-path safety, the turn machine, transcript compaction, staging."""
import json

import pytest

from maestro.codegen import build_steps
from maestro.codegen.build_state import BuildCursor
from maestro.codegen.staging import stage_for_play
from maestro.codegen.tools import build_tools
from maestro.state import RunState


@pytest.fixture
def tools(tmp_path):
    (tmp_path / "game").mkdir(exist_ok=True)
    return build_tools(RunState(tmp_path), "b1")


def _cursor(**kw):
    return BuildCursor(build_id="b1", **kw)


def _reply(content="", code=None, usage=None, programs=None, reasoning=None):
    msg = {"role": "assistant", "content": content}
    if reasoning:
        msg["reasoning_content"] = reasoning
    bodies = programs if programs is not None else ([code] if code is not None else [])
    if bodies:
        msg["tool_calls"] = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "python", "arguments": json.dumps({"code": b})}}
            for i, b in enumerate(bodies)]
    return {"choices": [{"message": msg}], "usage": usage or {}}


def _program(code, i, tag="c"):
    return {"id": f"{tag}{i}", "type": "function",
            "function": {"name": "python", "arguments": json.dumps({"code": code})}}


def _history():
    h = [{"role": "user", "content": "make a game"}]
    for i in range(6):
        h.append({"role": "assistant", "content": f"step {i}",
                  "tool_calls": [_program(f'write_file(path="f{i}.js", content="x")', i)]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 400})
    return h


def test_rounds_never_split_a_tool_result():
    groups = build_steps.rounds(_history())
    assert all(g[0]["role"] != "tool" for g in groups)
    assert sum(len(g) for g in groups) == len(_history()) - 1


def test_compact_drops_oldest_and_regrounds(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "game.js").write_text("x")
    cursor = _cursor(history=_history())
    assert build_steps.compact(tmp_path, cursor, keep_chars=1200) > 0
    assert cursor.history[0]["content"] == "make a game"
    assert "game.js" in cursor.history[1]["content"]
    open_ids = set()
    for m in cursor.history:
        if m.get("tool_calls"):
            open_ids |= {c["id"] for c in m["tool_calls"]}
        if m["role"] == "tool":
            assert m["tool_call_id"] in open_ids


def test_a_second_compaction_replaces_the_first_note(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "game.js").write_text("x")
    cursor = _cursor(history=_history())
    build_steps.compact(tmp_path, cursor, keep_chars=1200)
    (tmp_path / "game" / "later.js").write_text("y")
    cursor.history += _history()[1:]
    build_steps.compact(tmp_path, cursor, keep_chars=1200)
    notes = [m for m in cursor.history if build_steps.is_note(m)]
    assert len(notes) == 1 and "later.js" in notes[0]["content"]
    assert cursor.history[1] is notes[0]


def test_compact_noop_when_it_fits(tmp_path):
    (tmp_path / "game").mkdir()
    cursor = _cursor(history=_history())
    before = list(cursor.history)
    assert build_steps.compact(tmp_path, cursor, keep_chars=10_000_000) == 0
    assert cursor.history == before


def test_the_first_turn_sends_the_request_then_the_seeded_opening_run_for_real(tmp_path, tools):
    (tmp_path / "game" / "design").mkdir()
    (tmp_path / "game" / "design" / "design.md").write_text("# 0. SCOPE\nA card game.\n")
    cursor = _cursor()
    spec = {"request": "a card game", "design": {"look": "inky woodcut", "audio": "lute",
                                                 "mechanics": ["draw five cards a turn"]}}
    out = build_steps.step(spec, tmp_path, tools, cursor, {})
    assert isinstance(out, build_steps.Infer)
    sent = out.messages[1:]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "tool", "user"]
    assert sent[0]["content"] == "a card game"
    assert "design/design.md" in sent[2]["content"] and "A card game." in sent[2]["content"]
    assert "no module 'subprocess'" in sent[4]["content"]
    assert sent[5]["content"] == "Good, continue, and please do not use the subprocess module."
    assert cursor.turn == 0
    assert [t["function"]["name"] for t in out.schemas] == ["python"]


def test_fix_note_replaces_the_request(tmp_path, tools):
    cursor = _cursor(request="the player cannot move")
    out = build_steps.step({"request": "a card game"}, tmp_path, tools, cursor, {})
    assert out.messages[1]["content"] == "the player cannot move"
    assert out.messages[-1]["content"] == build_steps._nudge_after_seed()


def test_the_seeded_opening_stays_right_after_the_request_through_compaction(tmp_path, tools):
    (tmp_path / "game" / "game.js").write_text("x")
    cursor = _cursor()
    build_steps.step({"request": "make a game"}, tmp_path, tools, cursor, {})
    seeded = cursor.history[1:6]
    cursor.history += _history()[1:]
    assert build_steps.compact(tmp_path, cursor, keep_chars=1200) > 0
    assert cursor.history[0]["content"] == "make a game"
    assert cursor.history[1:6] == seeded
    assert build_steps.is_note(cursor.history[6])


def test_done_ends_the_build(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code='done(summary="shipped")'))
    assert isinstance(out, build_steps.Done)
    assert cursor.finished is True and "shipped" in out.report


def test_tool_call_lands_on_disk_and_continues(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor,
                           _reply(code='write_file(path="index.html", content="<h1>hi</h1>")'))
    assert isinstance(out, build_steps.Infer)
    assert (tmp_path / "game" / "index.html").read_text() == "<h1>hi</h1>"
    assert cursor.history[-1]["role"] == "tool"


def test_the_step_report_says_what_the_turn_did(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'write_file(path="index.html", content="<h1>hi</h1>")\n'
        'read_file(path="index.html")')))
    assert out.report == "wrote index.html, read index.html"

    out2 = build_steps.step({}, tmp_path, tools, cursor, _reply(code="list_files()"))
    assert out2.report == "listed files"


def test_the_report_folds_a_program_that_touched_many_files(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        "for i in range(6):\n"
        '    write_file(path=f"f{i}.js", content="x")')))
    assert out.report == "wrote 6 files"


def test_a_failed_tool_call_reports_its_reason(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor,
                           _reply(code='read_file(path="nope.js")'))
    assert out.report == "read nope.js — failed"


def test_the_opening_turn_says_the_prompt_went_out(tmp_path, tools):
    cursor = _cursor()
    out = build_steps.step({"request": "make a snake game"}, tmp_path, tools, cursor, {})
    assert out.report == "sent the prompt: make a snake game"


def test_the_opening_line_of_a_fix_carries_the_note(tmp_path, tools):
    cursor = _cursor(kind="fix", request="the snake never dies")
    out = build_steps.step({}, tmp_path, tools, cursor, {})
    assert out.report == "sent the fix note: the snake never dies"


def test_a_long_prompt_is_clipped_in_the_feed(tmp_path, tools):
    cursor = _cursor()
    out = build_steps.step({"request": "word " * 200}, tmp_path, tools, cursor, {})
    assert out.report.startswith("sent the prompt: word word")
    assert out.report.endswith("…") and len(out.report) < 340


def test_a_turn_with_no_tool_call_says_so(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(content="thinking out loud"))
    assert out.report == "no tool call — asked again"


def test_no_result_re_asks_instead_of_inventing_an_empty_turn(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(code='write_file(path="index.html", content="<h1>hi</h1>")'))
    before, turn = list(cursor.history), cursor.turn

    out = build_steps.step({}, tmp_path, tools, cursor, None)

    assert isinstance(out, build_steps.Infer)
    assert cursor.history == before and cursor.turn == turn
    assert cursor.no_call_streak == 0


def test_a_long_done_summary_is_cut_on_a_word_boundary(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(
        code='done(summary="' + "built the thing " * 60 + '")'))
    assert isinstance(out, build_steps.Done)
    assert cursor.summary.endswith("…")
    assert not cursor.summary.rstrip("…").endswith(" ")
    assert cursor.summary.rstrip("…").split()[-1] in ("built", "the", "thing")


def test_a_truncated_reply_is_thrown_away_and_the_turn_sent_again(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    before = [dict(m) for m in cursor.history]
    outcome = build_steps.step({}, tmp_path, tools, cursor,
                               _reply(content="const x = ",
                                      usage={"completion_tokens": cursor.out_cap}))
    assert isinstance(outcome, build_steps.Infer)
    assert cursor.history == before, "the cut-off reply must leave no trace in the transcript"
    assert not any("cut off" in str(m.get("content", "")) for m in cursor.history)


def test_a_cut_off_turn_is_re_sent_with_the_window_compacted(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor(started=True, system="s", history=_history(), prompt_tokens=100_000,
                     chars_per_token=3.0, logged=13)
    small = build_steps._infer(tmp_path, cursor).max_tokens
    outcome = build_steps.step({}, tmp_path, tools, cursor,
                               _reply(content="const x = ",
                                      usage={"prompt_tokens": 100_000, "completion_tokens": small}))
    assert isinstance(outcome, build_steps.Infer)
    assert cursor.compacted == 1 and outcome.max_tokens > small * 2
    assert build_steps.is_note(cursor.history[1])


def test_the_build_gives_up_after_repeated_cut_offs(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    for _ in range(build_steps._NO_CALL_GIVE_UP):
        outcome = build_steps.step({}, tmp_path, tools, cursor,
                                   _reply(content="const x = ",
                                          usage={"completion_tokens": cursor.out_cap}))
    assert isinstance(outcome, build_steps.Done)
    assert "cut off" in outcome.report


def _raw_reply(raw_args):
    return {"choices": [{"message": {"role": "assistant", "content": "",
                                     "tool_calls": [{"id": "c0", "type": "function",
                                                     "function": {"name": "python",
                                                                  "arguments": raw_args}}]}}],
            "usage": {}}


def test_a_call_cut_off_mid_argument_says_so_and_writes_nothing(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _raw_reply('{"code":"write_file(path=\\"story.js\\", content=\\"const '))
    said = cursor.history[-1]["content"]
    assert "output token limit" in said and "nothing ran" in said
    assert "KeyError" not in said
    assert not (tmp_path / "game" / "story.js").exists()


def test_a_call_that_takes_no_arguments_is_not_read_as_cut_off(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _raw_reply(json.dumps({"code": "print(len(list_files()))"})))
    said = cursor.history[-1]["content"]
    assert "output token limit" not in said
    assert said.startswith("0")


def test_a_turn_the_server_refused_is_not_a_turn_that_said_nothing(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, {},
                           error="Status 500: Failed to parse tool call arguments as JSON")
    assert isinstance(out, build_steps.Infer)
    said = cursor.history[-1]["content"]
    assert "could not read your last reply" in said
    assert "Failed to parse tool call arguments" in said
    assert "smaller pieces" in said
    assert "no tool call" not in said


def test_a_refused_error_is_clipped_before_it_reaches_the_transcript(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, {}, error="Status 500: " + "x" * 50_000)
    assert len(cursor.history[-1]["content"]) < 1000


def test_a_build_the_server_keeps_refusing_gives_up(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = None
    for _ in range(build_steps._NO_CALL_GIVE_UP):
        out = build_steps.step({}, tmp_path, tools, cursor, {}, error="Status 500: nope")
    assert isinstance(out, build_steps.Done) and "could not read" in out.report


def test_a_landed_turn_after_a_refusal_clears_the_streak(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, {}, error="Status 500: nope")
    build_steps.step({}, tmp_path, tools, cursor, _reply(code="list_files()"))
    assert cursor.no_call_streak == 0


def test_the_cursor_cap_ends_the_build(tmp_path, tools):
    cursor = _cursor()
    cursor.max_steps = 500
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.turn = 499
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(content="thinking"))
    assert isinstance(out, build_steps.Done) and "500-turn cap" in out.report
    cursor.turn, cursor.max_steps = 499, 1000
    assert isinstance(build_steps.step({}, tmp_path, tools, cursor, _reply(content="more")),
                      build_steps.Infer)


def test_no_tool_call_is_nudged_not_failed(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(content="I will now write it."))
    assert isinstance(out, build_steps.Infer)
    assert "Keep going" in cursor.history[-1]["content"]


_QUOTED = "function f() {\n  el.innerHTML = '<div style=\"color:#888\">?</div>';\n}\n"


def test_a_read_reaches_the_program_as_the_file_itself(tmp_path, tools):
    (tmp_path / "game" / "game.js").write_text(_QUOTED, encoding="utf-8")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'src = read_file(path="game.js")\n'
        'line = src.splitlines()[1]\n'
        'print(edit_file(path="game.js", old_text=line, new_text="  el.innerHTML = \'x\';"))\n'
        'print("edited")')))
    assert out.report == "read game.js, edited game.js"
    assert "'x'" in (tmp_path / "game" / "game.js").read_text()


def test_only_what_the_program_prints_reaches_the_transcript(tmp_path, tools):
    (tmp_path / "game" / "big.js").write_text("y" * 50_000, encoding="utf-8")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'src = read_file(path="big.js")\n'
        'print("big.js is", len(src), "chars")')))
    said = cursor.history[-1]["content"]
    assert "big.js is 50000 chars" in said
    assert "y" * 200 not in said


def test_a_whole_file_reaches_the_program_however_long(tmp_path, tools):
    (tmp_path / "game" / "big.js").write_text("z" * 300_000, encoding="utf-8")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(code='print(len(read_file(path="big.js")))'))
    assert cursor.history[-1]["content"].startswith("300000")


def test_a_failed_call_returns_its_error_and_the_program_carries_on(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'print("first:", read_file(path="nope.js")[:6])\n'
        'write_file(path="after.js", content="// still ran")\n'
        'print("second write happened")')))
    said = cursor.history[-1]["content"]
    assert "first: ERROR" in said and "second write happened" in said
    assert (tmp_path / "game" / "after.js").exists()


def test_the_result_lists_the_calls_and_names_the_ones_that_failed(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'write_file(path="a.js", content="1")\n'
        'edit_file(path="a.js", old_text="nope", new_text="x")')))
    said = cursor.history[-1]["content"]
    assert "what the program called" in said
    assert "write_file a.js" in said
    assert "edit_file a.js  — FAILED" in said
    assert "1 of those FAILED" in said


def test_a_program_that_reaches_outside_the_session_is_refused_before_it_runs(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        'import subprocess\n'
        'write_file(path="never.js", content="x")')))
    said = cursor.history[-1]["content"]
    assert "no module 'subprocess'" in said and "nothing in the program ran" in said
    assert not (tmp_path / "game" / "never.js").exists()
    assert out.report.startswith("program refused:")


def test_only_the_first_program_of_a_reply_runs(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(programs=[
        'write_file(path="one.js", content="1")',
        'write_file(path="two.js", content="2")']))
    assert (tmp_path / "game" / "one.js").exists()
    assert not (tmp_path / "game" / "two.js").exists()
    assert "Only the first program ran" in cursor.history[-1]["content"]


def _fail(tmp_path, tools, cursor, code, times):
    for _ in range(times):
        build_steps.step({}, tmp_path, tools, cursor, _reply(code=code))
    return cursor.history[-1]["content"]


def test_an_identical_failing_call_is_told_it_is_repeating(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    prog = 'edit_file(path="game.js", old_text="a", new_text="b")'
    assert "sent this exact program" not in _fail(tmp_path, tools, cursor, prog, 1)
    assert "sent this exact program 2 times" in _fail(tmp_path, tools, cursor, prog, 1)
    assert "sent this exact program 4 times" in _fail(tmp_path, tools, cursor, prog, 2)


def test_a_succeeding_call_between_retries_does_not_reset_the_count(tmp_path, tools):
    (tmp_path / "game" / "game.js").write_text("hello\n", encoding="utf-8")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    prog = 'edit_file(path="game.js", old_text="nope", new_text="b")'
    for _ in range(3):
        _fail(tmp_path, tools, cursor, 'read_file(path="game.js")', 1)
        content = _fail(tmp_path, tools, cursor, prog, 1)
    assert "sent this exact program 3 times" in content


def test_the_repeat_note_keeps_the_reason_the_call_failed(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    content = _fail(tmp_path, tools, cursor, 'print(read_file(path="nope.js"))', 2)
    assert "no such file" in content and "2 times" in content


def test_a_changed_argument_is_not_a_repeat(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    _fail(tmp_path, tools, cursor, 'read_file(path="nope.js")', 1)
    content = _fail(tmp_path, tools, cursor, 'read_file(path="other.js")', 1)
    assert "sent this exact program" not in content


def test_the_repeat_note_stays_out_of_the_build_feed(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    prog = 'read_file(path="nope.js")'
    build_steps.step({}, tmp_path, tools, cursor, _reply(code=prog))
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=prog))
    assert "sent this exact program" not in out.report


def test_generate_media_reaches_the_tool_and_its_path_reaches_the_transcript(tmp_path):
    seen = {}

    def _spy(**kw):
        seen.update(kw)
        return {"ok": True, "path": "assets/goblin.glb", "status": "rendering"}

    (tmp_path / "game").mkdir()
    cursor = _cursor()
    spy_tools = {"generate_media": _spy}
    build_steps.step({}, tmp_path, spy_tools, cursor, {})
    build_steps.step({}, tmp_path, spy_tools, cursor, _reply(code=(
        'print(generate_media(id="goblin", subject="a snarling goblin", kind="mesh"))')))
    assert seen == {"id": "goblin", "subject": "a snarling goblin", "kind": "mesh"}
    assert "assets/goblin.glb" in cursor.history[-1]["content"]


def test_seed_places_the_renderer_and_leaves_edits_alone(tmp_path, monkeypatch):
    from maestro.codegen import staging
    monkeypatch.setattr(staging, "RUNTIME_DIR", tmp_path / "runtime")
    vendor = tmp_path / "runtime" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "three.module.js").write_text("// three")
    staging.seed_vendor(tmp_path)
    assert (tmp_path / "game" / "three.module.js").read_text() == "// three"

    (tmp_path / "game" / "index.html").write_text("<h1>hi</h1>")
    staging.seed_vendor(tmp_path)
    assert (tmp_path / "game" / "index.html").read_text() == "<h1>hi</h1>"
    assert sorted(p.name for p in (tmp_path / "game").iterdir()) == ["docs", "index.html", "lib", "three.module.js"]


def test_seed_places_the_world_loader(tmp_path):
    from maestro.codegen import staging
    staging.seed_vendor(tmp_path)
    game = tmp_path / "game"
    for name in ("world.js", "GLTFLoader.js", "BufferGeometryUtils.js"):
        assert (game / name).is_file()
    assert "export async function loadWorld" in (game / "world.js").read_text()
    assert not (game / "world_render.html").exists()


def test_stage_copies_the_folder(tmp_path, monkeypatch):
    from maestro.codegen import staging
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(staging, "RUNTIME_DIR", runtime)
    gd = tmp_path / "game"
    (gd / "assets").mkdir(parents=True)
    (gd / "index.html").write_text("<h1>hi</h1>")
    (gd / "assets" / "card.svg").write_text("<svg/>")
    (gd / "_transcript.jsonl").write_text("noise")
    assert stage_for_play(tmp_path, "abc123") == "games/abc123/index.html"
    assert (runtime / "games" / "abc123" / "index.html").read_text() == "<h1>hi</h1>"
    assert (runtime / "games" / "abc123" / "assets" / "card.svg").exists()
    assert not (runtime / "games" / "abc123" / "_transcript.jsonl").exists()


def test_stage_replaces_a_previous_stage(tmp_path, monkeypatch):
    from maestro.codegen import staging
    runtime = tmp_path / "runtime"
    monkeypatch.setattr(staging, "RUNTIME_DIR", runtime)
    stale = runtime / "games" / "abc123"
    stale.mkdir(parents=True)
    (stale / "gone.js").write_text("old")
    gd = tmp_path / "game"
    gd.mkdir()
    (gd / "index.html").write_text("new")
    stage_for_play(tmp_path, "abc123")
    assert not (stale / "gone.js").exists()


def test_cursor_survives_a_reload(tmp_path):
    from maestro.codegen import build_state
    build_state.save(tmp_path, _cursor(phase="audit", turn=7, compacted=2))
    back = build_state.load(tmp_path)
    assert (back.phase, back.turn, back.compacted) == ("audit", 7, 2)


def test_seed_places_the_helper_library_and_leaves_edits_alone(tmp_path):
    from maestro.codegen import staging
    staging.seed_vendor(tmp_path)
    lib = tmp_path / "game" / "lib"
    vendored = sorted(p.name for p in (staging.RUNTIME_DIR / "vendor" / "lib").glob("*.js"))
    assert vendored and sorted(p.name for p in lib.iterdir()) == vendored
    edited = lib / vendored[0]
    edited.write_text("// edited")
    staging.seed_vendor(tmp_path)
    assert edited.read_text() == "// edited"


def _file_history(n=6):
    h = [{"role": "user", "content": "make a game"}]
    for i in range(n):
        h.append({"role": "assistant", "content": f"step {i}",
                  "tool_calls": [_program(f'write_file(path="f{i}.js", content={"y" * 1500!r})', i)]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": "wrote it"})
        h.append({"role": "assistant", "content": f"look {i}",
                  "tool_calls": [_program(f'print(read_file(path="f{i}.js")[:20])', i, "r")]})
        h.append({"role": "tool", "tool_call_id": f"r{i}", "content": "yyyyyyyyyyyyyyyyyyyy"})
    return h


def _bodies(history):
    return [i for i, m in enumerate(history) if "y" * 1500 in json.dumps(m)]


def test_compact_stubs_the_oldest_bodies_only_until_the_tail_fits(tmp_path):
    (tmp_path / "game").mkdir()
    cursor = _cursor(history=_file_history())
    kept = build_steps.drop_read_only_rounds(cursor.history)
    keep = sum(len(json.dumps(m)) for m in kept[1:]) - 2 * 1400
    assert build_steps.compact(tmp_path, cursor, keep_chars=keep) > 0
    assert "whole project" in cursor.history[1]["content"]
    assert "the 1500 chars written to f0.js" in json.dumps(cursor.history[2])
    assert "the 1500 chars written to f1.js" in json.dumps(cursor.history[4])
    assert _bodies(cursor.history) == [6, 8, 10, 12]


def test_a_stubbed_program_still_says_what_the_model_did(tmp_path):
    (tmp_path / "game").mkdir()
    code = ('for name in ["a", "b"]:\n'
            '    generate_media(id=name, kind="sprite", subject=name, style="ink")\n'
            f'write_file(path="game.js", content={"y" * 1500!r})\n'
            'print("done")')
    cursor = _cursor(history=[{"role": "user", "content": "go"},
                              {"role": "assistant", "content": "", "tool_calls": [_program(code, 0)]},
                              {"role": "tool", "tool_call_id": "c0", "content": "done"}])
    build_steps.compact(tmp_path, cursor, keep_chars=700)
    left = build_steps.parse_args(
        cursor.history[-2]["tool_calls"][0]["function"]["arguments"])["code"]
    assert "generate_media(id=name" in left and "for name in" in left
    assert "y" * 100 not in left
    assert "the 1500 chars written to game.js" in left
    import ast
    ast.parse(left)


def test_compact_keeps_only_the_newest_copy_of_each_file(tmp_path):
    (tmp_path / "game").mkdir()
    h = [{"role": "user", "content": "make a game"}]
    for k in range(3):
        h.append({"role": "assistant", "content": f"write {k}",
                  "tool_calls": [_program(f'write_file(path="f0.js", content={"y" * 1500!r})', k)]})
        h.append({"role": "tool", "tool_call_id": f"c{k}", "content": "ok"})
    cursor = _cursor(history=h)
    assert build_steps.compact(tmp_path, cursor, keep_chars=10_000_000) == 1
    assert _bodies(cursor.history) == [len(cursor.history) - 2]
    assert "read it again" not in json.dumps(cursor.history)


def test_an_edit_is_never_stubbed(tmp_path):
    (tmp_path / "game").mkdir()
    wrote = f'write_file(path="f0.js", content={"y" * 3000!r})'
    edited = f'edit_file(path="f0.js", old_text={"a" * 900!r}, new_text={"b" * 900!r})'
    h = [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "", "tool_calls": [_program(wrote, 0)]},
         {"role": "tool", "tool_call_id": "c0", "content": "ok"},
         {"role": "assistant", "content": "", "tool_calls": [_program(edited, 1)]},
         {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    cursor = _cursor(history=h)
    build_steps.compact(tmp_path, cursor, keep_chars=2500)
    assert "the 3000 chars written to f0.js" in json.dumps(cursor.history)
    assert "a" * 900 in json.dumps(cursor.history)


def test_a_body_built_at_runtime_is_left_alone(tmp_path):
    (tmp_path / "game").mkdir()
    built = ('rows = [f"const x{i} = {i};" for i in range(400)]\n'
             'write_file(path="data.js", content="\\n".join(rows))')
    literal = f'write_file(path="other.js", content={"y" * 3000!r})'
    h = [{"role": "user", "content": "go"},
         {"role": "assistant", "content": "", "tool_calls": [_program(literal, 0)]},
         {"role": "tool", "tool_call_id": "c0", "content": "ok"},
         {"role": "assistant", "content": "", "tool_calls": [_program(built, 1)]},
         {"role": "tool", "tool_call_id": "c1", "content": "ok"}]
    cursor = _cursor(history=h)
    build_steps.compact(tmp_path, cursor, keep_chars=2500)
    assert "the 3000 chars written to other.js" in json.dumps(cursor.history)
    left = build_steps.parse_args(
        cursor.history[-2]["tool_calls"][0]["function"]["arguments"])["code"]
    assert left == built


def _one_round(code):
    return [{"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_program(code, 0)]},
            {"role": "tool", "tool_call_id": "c0", "content": "ok"}]


def _left_after_compact(tmp_path, code):
    (tmp_path / "game").mkdir(exist_ok=True)
    cursor = _cursor(history=_one_round(code))
    build_steps.compact(tmp_path, cursor, keep_chars=700)
    return build_steps.parse_args(
        cursor.history[-2]["tool_calls"][0]["function"]["arguments"])["code"]


def test_a_body_bound_to_a_name_and_passed_by_position_is_stubbed(tmp_path):
    code = (f"util = {'u' * 1281!r}\n"
            f"data = {'d' * 7904!r}\n"
            "print(write_file('js/util.js', util))\n"
            "print(write_file('js/data.js', data))\n"
            "print('files written', flush=True)")
    left = _left_after_compact(tmp_path, code)
    assert "the 1281 chars written to js/util.js" in left
    assert "the 7904 chars written to js/data.js" in left
    assert "write_file('js/util.js', util)" in left and "u" * 100 not in left
    import ast
    ast.parse(left)


def test_a_body_added_from_a_bound_literal_is_stubbed(tmp_path):
    code = (f"chunk1 = {'c' * 7143!r}\n"
            "print(write_file('js/main.js', chunk1 + '\\n// __NEXT__\\n'))\n"
            "print(check_syntax(['js/main.js']))")
    left = _left_after_compact(tmp_path, code)
    assert "the 7143 chars written to js/main.js" in left
    assert "// __NEXT__" in left and "c" * 100 not in left


def test_a_name_that_refers_to_itself_is_left_whole():
    for code in (f"x = x + {'y' * 900!r}\nwrite_file('a.js', x)",
                 f"a = b + {'y' * 900!r}\nb = a + ''\nwrite_file('a.js', a)"):
        assert build_steps._stub_call(_program(code, 0), "on disk") is None


def test_a_body_read_back_and_replaced_is_left_whole():
    code = ("t = read_file('src/nav.js')\n"
            f"t = t.replace('old', {'n' * 900!r})\n"
            "write_file('src/nav.js', t)")
    assert build_steps._stub_call(_program(code, 0), "on disk") is None


def test_compact_is_a_noop_when_nothing_is_superseded_and_it_fits(tmp_path):
    (tmp_path / "game").mkdir()
    h = [{"role": "user", "content": "go"}]
    for i in range(2):
        h.append({"role": "assistant", "content": f"s{i}",
                  "tool_calls": [_program(f'write_file(path="f{i}.js", content="short")', i)]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    cursor = _cursor(history=h)
    assert build_steps.compact(tmp_path, cursor, keep_chars=10_000_000) == 0


def test_compact_drops_rounds_only_when_trimming_is_not_enough(tmp_path):
    (tmp_path / "game").mkdir()
    (tmp_path / "game" / "f0.js").write_text("x")
    cursor = _cursor(history=_file_history())
    assert build_steps.compact(tmp_path, cursor, keep_chars=1500) > 0
    assert "f0.js" in cursor.history[1]["content"]
    assert len(cursor.history) < len(_file_history())


def test_compaction_replays_the_trim_from_the_turn_log(tmp_path):
    (tmp_path / "game").mkdir()
    cursor = _cursor(history=_file_history())
    build_steps.compact(tmp_path, cursor, keep_chars=8000)
    from maestro.codegen import turn_log
    record = json.loads(turn_log.path(tmp_path).read_text().splitlines()[-1])
    assert record["kind"] == "compact" and record["trimmed"] > 0 and record["dropped"] == 0
    assert turn_log._compacted(_file_history(), record) == cursor.history


def test_a_turns_thinking_rides_the_transcript(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(code="print(1)", reasoning="plan it"))
    build_steps.step({}, tmp_path, tools, cursor, _reply(content="hm", reasoning="no call"))
    replies = [m for m in cursor.history if m["role"] == "assistant" and not build_steps.is_seed(m)]
    assert [m["reasoning_content"] for m in replies] == ["plan it", "no call"]


def test_compaction_drops_every_turns_thinking(tmp_path):
    (tmp_path / "game").mkdir()
    history = [{**m, "reasoning_content": "why"} if m["role"] == "assistant" else m
               for m in _history()]
    cursor = _cursor(history=list(history))
    assert build_steps.compact(tmp_path, cursor, keep_chars=10_000_000) > 0
    assert not any("reasoning_content" in m for m in cursor.history)
    from maestro.codegen import turn_log
    record = json.loads(turn_log.path(tmp_path).read_text().splitlines()[-1])
    assert turn_log._compacted(history, record) == cursor.history


def test_replies_cut_off_at_the_cap_count_toward_the_stall(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = None
    for _ in range(build_steps._NO_CALL_GIVE_UP):
        out = build_steps.step({}, tmp_path, tools, cursor,
                               _reply(content="const x = ",
                                      usage={"completion_tokens": cursor.out_cap}))
    assert isinstance(out, build_steps.Done) and "stalled" in out.report


def test_a_turn_re_sent_forever_ends_the_build(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = None
    for _ in range(build_steps._REDRIVE_GIVE_UP):
        out = build_steps.step({}, tmp_path, tools, cursor, None)
    assert isinstance(out, build_steps.Done) and "never ran" in out.report


def test_the_last_nudge_never_tells_a_fix_to_write_a_fresh_page(tmp_path, tools):
    (tmp_path / "game" / "index.html").write_text("<h1>done</h1>")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    for _ in range(3):
        build_steps.step({}, tmp_path, tools, cursor, _reply(content="hmm"))
    said = cursor.history[-1]["content"]
    assert "index.html" not in said and "edits it" in said
    assert (tmp_path / "game" / "index.html").read_text() == "<h1>done</h1>"


def test_a_misnamed_argument_is_named_back(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(code='print(read_file(file="lib/input.js"))'))
    said = cursor.history[-1]["content"]
    assert "KeyError" not in said
    assert "needs the argument 'path'" in said and "file" in said


def test_the_output_cap_is_the_ceiling_until_a_turn_overruns_it(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    assert cursor.out_cap == build_steps._OUT_CAP

    outcome = build_steps.step({}, tmp_path, tools, cursor,
                               _reply(content="const x = ",
                                      usage={"completion_tokens": cursor.out_cap}))
    assert isinstance(outcome, build_steps.Infer)
    assert cursor.out_cap > build_steps._OUT_CAP, "the retry gets the whole window"


def test_a_turn_that_merely_called_no_tool_does_not_earn_the_whole_window(tmp_path, tools,
                                                                          monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(content="thinking out loud, no call", usage={"completion_tokens": 40}))
    assert cursor.out_cap == build_steps._OUT_CAP


def test_the_cap_never_exceeds_what_the_window_has_left(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.logged = len(cursor.history)
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(code=f'write_file(path="a.js", content={"z" * 3000!r})',
                            usage={"prompt_tokens": 110_000}))
    assert 131_072 - 110_000 - 1200 < cursor.out_cap < 131_072 - 110_000 - 1000


def test_compaction_fires_when_the_window_has_less_than_the_room_left(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.history = _file_history()
    cursor.logged = len(cursor.history)
    cursor.prompt_tokens = 131_072 - build_steps._COMPACT_ROOM - 1
    build_steps._infer(tmp_path, cursor)
    assert cursor.compacted == 0
    cursor.prompt_tokens = 131_072 - build_steps._COMPACT_ROOM + 1
    build_steps._infer(tmp_path, cursor)
    assert cursor.compacted == 1


def test_the_server_count_sets_the_chars_per_token(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    sent = len(cursor.system) + sum(len(json.dumps(m)) for m in cursor.history)
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(code="print(1)", usage={"prompt_tokens": 1000}))
    assert cursor.chars_per_token == sent / 1000


def test_the_keep_target_is_a_third_of_the_window_in_tokens(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(build_steps, "compact",
                        lambda run_dir, cursor, keep_chars: seen.append(keep_chars) or 0)
    build_steps._compact(tmp_path, _cursor(chars_per_token=1.9), 131_072)
    build_steps._compact(tmp_path, _cursor(), 131_072)
    assert seen == [int(131_072 * build_steps._COMPACT_KEEP * 1.9),
                    int(131_072 * build_steps._COMPACT_KEEP * 3)]


def test_the_compaction_note_carries_the_code_map(tmp_path):
    (tmp_path / "game" / "systems").mkdir(parents=True)
    (tmp_path / "game" / "systems" / "combat.js").write_text(
        "export function resolveCombat(a, d) {\n  return 1;\n}\n")
    cursor = _cursor(history=_file_history(n=2))
    build_steps.compact(tmp_path, cursor, keep_chars=1000)
    note = cursor.history[1]["content"]
    assert "systems/combat.js (4 lines)" in note and "  1-3  resolveCombat(a, d)" in note
    assert "offset and lines" in note


def test_a_program_with_hundreds_of_calls_reports_a_readable_ledger(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        "for i in range(60):\n"
        '    write_file(path=f"f{i}.js", content="x")')))
    said = cursor.history[-1]["content"]
    assert "… 20 more …" in said
    assert said.count("write_file") == 40


def test_the_feed_line_folds_failures_too(tmp_path, tools):
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, _reply(code=(
        "for i in range(22):\n"
        '    edit_file(path=f"gone{i}.js", old_text="a", new_text="b")')))
    assert out.report == "edited 22 files — all failed"


def _fat_history(rounds=40, body=6000):
    h = [{"role": "user", "content": "make a game"}]
    for i in range(rounds):
        h.append({"role": "assistant", "content": f"step {i}",
                  "tool_calls": [_program(f'write_file(path="f{i}.js", content={"z" * body!r})', i)]})
        h.append({"role": "tool", "tool_call_id": f"c{i}", "content": "wrote f%d.js" % i})
    return h


def test_compaction_fires_on_the_round_that_jumps_the_window(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.history = _fat_history()
    cursor.logged = 1
    cursor.prompt_tokens = 100_000
    build_steps._infer(tmp_path, cursor)
    assert cursor.compacted == 1


def test_a_prompt_over_the_window_is_trimmed_and_re_sent(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.history = _fat_history()
    cursor.logged = 1
    out = build_steps.step({}, tmp_path, tools, cursor, None,
                           error='Status 400: {"error":{"code":"context_length_exceeded",'
                                 '"message":"prepared prompt has 131453 tokens, exceeding Engine '
                                 'max_context 131072"}}')
    assert cursor.compacted == 1
    assert isinstance(out, build_steps.Infer)
    assert not any("could not read your last reply" in str(m.get("content"))
                   for m in cursor.history)


def test_the_build_gives_up_when_the_prompt_cannot_be_trimmed(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    out = build_steps.step({}, tmp_path, tools, cursor, None,
                           error='Status 400: max_tokens must be positive')
    assert isinstance(out, build_steps.Done)
    assert "nothing is left to trim" in out.report


def test_the_cap_stays_positive_when_the_window_is_full(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.history = _fat_history()
    cursor.logged = 1
    cursor.prompt_tokens = 130_000
    build_steps._infer(tmp_path, cursor)
    assert cursor.out_cap > 0


def test_the_compaction_note_says_what_is_already_read_and_unchanged(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    (tmp_path / "game" / "main.js").write_text("const a = 1;\n", encoding="utf-8")
    tools["read_file"](path="main.js")
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.history = _file_history()
    build_steps.compact(tmp_path, cursor, keep_chars=1200)
    note = cursor.history[1]["content"]
    assert "already read these files and they have NOT changed" in note
    assert "main.js" in note


def test_dedup_alone_can_be_enough_and_the_older_bodies_stay(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    cursor = _cursor()
    body = "z" * 9000
    cursor.history = [{"role": "user", "content": "make a game"}]
    for i in range(4):
        cursor.history.append({"role": "assistant", "content": f"step {i}",
                               "tool_calls": [_program(
                                   f'write_file(path="a.js", content={body!r})', i)]})
        cursor.history.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
    keep = sum(len(json.dumps(m)) for m in cursor.history) // 2
    assert build_steps.compact(tmp_path, cursor, keep_chars=keep) > 0
    kept = json.dumps(cursor.history)
    assert kept.count(body) == 1
    assert "step 0" in kept


def test_a_transcript_over_the_window_never_asks_for_a_negative_reply(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    monkeypatch.setattr(build_steps, "compact", lambda *a, **k: False)
    cursor = _cursor()
    cursor.prompt_tokens = 200_000
    build_steps.step({}, tmp_path, tools, cursor, {})
    assert cursor.out_cap >= build_steps._MIN_OUT


def test_a_full_window_retry_is_positive_too(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 131_072)
    monkeypatch.setattr(build_steps, "compact", lambda *a, **k: False)
    cursor = _cursor()
    build_steps.step({}, tmp_path, tools, cursor, {})
    cursor.prompt_tokens = 200_000
    build_steps.step({}, tmp_path, tools, cursor,
                     _reply(content="const x = ", usage={"completion_tokens": cursor.out_cap}))
    assert cursor.out_cap >= build_steps._MIN_OUT


def test_staging_never_leaves_the_played_game_half_deleted(tmp_path, monkeypatch):
    from maestro.codegen import staging
    monkeypatch.setattr(staging, "RUNTIME_DIR", tmp_path / "runtime")
    gd = tmp_path / "game"
    (gd / "assets").mkdir(parents=True)
    (gd / "index.html").write_text("v1")
    for i in range(5):
        (gd / "assets" / f"a{i}.png").write_text("art")
    staging.stage_for_play(tmp_path, "abc123")
    staged = tmp_path / "runtime" / "games" / "abc123"

    real_copytree = staging.shutil.copytree

    def land_a_render_mid_copy(src, dst, *a, **kw):
        out = real_copytree(src, dst, *a, **kw)
        (staged / "assets" / "late.png").write_text("landed while we copied")
        return out

    monkeypatch.setattr(staging.shutil, "copytree", land_a_render_mid_copy)
    (gd / "index.html").write_text("v2")
    staging.stage_for_play(tmp_path, "abc123")

    assert (staged / "index.html").read_text() == "v2"
    assert sorted(p.name for p in (staged / "assets").iterdir()) == \
        [f"a{i}.png" for i in range(5)], "the new copy is whole, not a partial delete"


def test_staging_swaps_in_and_leaves_nothing_behind(tmp_path, monkeypatch):
    from maestro.codegen import staging
    monkeypatch.setattr(staging, "RUNTIME_DIR", tmp_path / "runtime")
    gd = tmp_path / "game"
    gd.mkdir()
    (gd / "index.html").write_text("one")
    staging.stage_for_play(tmp_path, "abc123")
    (gd / "index.html").write_text("two")
    staging.stage_for_play(tmp_path, "abc123")
    games = tmp_path / "runtime" / "games"
    assert (games / "abc123" / "index.html").read_text() == "two"
    assert [p.name for p in games.iterdir()] == ["abc123"], "no staging or stale folder is left"


def test_a_crashed_staging_does_not_block_the_next_one(tmp_path, monkeypatch):
    from maestro.codegen import staging
    monkeypatch.setattr(staging, "RUNTIME_DIR", tmp_path / "runtime")
    games = tmp_path / "runtime" / "games"
    (games / "_abc123.staging" / "junk").mkdir(parents=True)
    (games / "_abc123.stale").mkdir(parents=True)
    gd = tmp_path / "game"
    gd.mkdir()
    (gd / "index.html").write_text("fresh")
    staging.stage_for_play(tmp_path, "abc123")
    assert (games / "abc123" / "index.html").read_text() == "fresh"
    assert [p.name for p in games.iterdir()] == ["abc123"]
