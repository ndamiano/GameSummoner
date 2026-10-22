import json
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from maestro.codegen import play_gate


# ---------------------------------------------------------------- parsing turns

def test_parse_turn_key_action():
    turn = play_gate._parse_turn(json.dumps({
        "verdict": "met", "observed": "the panel opened",
        "action": {"key": "e"}, "expected": "the potion is consumed"}))
    assert turn["verdict"] == "met"
    assert play_gate._action_of(turn["action"]) == ("key", "e")


def test_parse_turn_fenced_and_bad_verdict_defaults_unclear():
    text = '```json\n{"verdict": "maybe", "action": {"key": "Tab"}, "expected": "inventory"}\n```'
    turn = play_gate._parse_turn(text)
    assert turn["verdict"] == "unclear"
    assert play_gate._action_of(turn["action"]) == ("key", "Tab")


def test_parse_turn_without_action_is_none():
    assert play_gate._parse_turn('{"verdict": "met", "observed": "x"}') is None
    assert play_gate._parse_turn("not json at all") is None


def test_action_click_bounds():
    assert play_gate._action_of({"click": {"x": 10, "y": 20}}) == ("click", (10, 20))
    assert play_gate._action_of({"click": {"x": 99999, "y": 20}}) is None


def test_action_key_name_mapping():
    assert play_gate._action_of({"key": "space"}) == ("key", "Space")
    assert play_gate._action_of({"key": "W"}) == ("key", "w")


def test_action_hold_clamps_seconds():
    kind, (name, seconds) = play_gate._action_of({"hold": {"key": "w", "seconds": 30}})
    assert kind == "hold" and name == "w" and seconds == 3.0


# ---------------------------------------------------------------- parsing the report

def test_parse_report_keeps_shaped_facts_only():
    turns = [{"verdict": "unmet"}]
    report = play_gate._parse_report(json.dumps({
        "broken": [{"action": "e", "expected": "potion used", "observed": "nothing"},
                   {"expected": "orphan without action"}, "not a dict"],
        "judgment": {"works": "movement", "impressions": "shallow"}}), turns)
    assert len(report["broken"]) == 1
    assert report["judgment"]["impressions"] == "shallow"
    assert report["turns"] is turns


def test_parse_report_off_shape_still_reports_turns():
    report = play_gate._parse_report("the model rambled", [{"verdict": "met"}])
    assert report["broken"] == [] and report["turns"]


# ---------------------------------------------------------------- transcript eliding

def test_elided_keeps_last_window_of_screenshots():
    history = []
    for n in range(6):
        history.append({"role": "user", "content": [
            {"type": "text", "text": f"Turn {n}"},
            {"type": "image_url", "image_url": {"url": "data:..."}}]})
        history.append({"role": "assistant", "content": "{}"})
    out = play_gate._elided(history)
    stubs = [m for m in out if m["role"] == "user" and isinstance(m["content"], str)]
    pixels = [m for m in out if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(pixels) == play_gate.IMAGE_WINDOW
    assert len(stubs) == 6 - play_gate.IMAGE_WINDOW
    assert stubs[0]["content"].startswith("Turn 0")


def test_attach_errors_lands_on_the_turn_that_threw(tmp_path):
    turns = [{"verdict": "unmet", "action": {"key": "Enter"}, "expected": "the run starts",
              "observed": "stayed on stage select"},
             {"verdict": "met", "action": {"key": "w"}, "expected": "player moves",
              "observed": "player moved"}]
    errors = [{"message": "TypeError: floats is not iterable",
               "stack": "TypeError: floats is not iterable\n    at gen.js:42", "turn": 1}]
    play_gate._attach_errors(turns, errors, tmp_path)
    assert turns[0]["error"]["message"] == "TypeError: floats is not iterable"
    assert "gen.js:42" in turns[0]["error"]["address"]
    assert "error" not in turns[1]


def test_attach_errors_during_load_is_logged_not_attached(tmp_path, caplog):
    import logging
    turns = [{"verdict": "unclear", "action": {"key": "Enter"}, "expected": "the game starts",
              "observed": "title screen"}]
    errors = [{"message": "ReferenceError: THREE is not defined", "stack": "", "turn": "load"}]
    with caplog.at_level(logging.WARNING):
        play_gate._attach_errors(turns, errors, tmp_path)
    assert "error" not in turns[0]
    assert any("threw before the session started" in r.getMessage() for r in caplog.records)


def test_attach_errors_dedups_a_repeating_error_and_keeps_its_first_turn(tmp_path):
    turns = [{"verdict": "unmet", "action": {"key": "w"}, "expected": "player moves",
              "observed": "nothing"},
             {"verdict": "unmet", "action": {"key": "s"}, "expected": "player moves back",
              "observed": "nothing"}]
    errors = [{"message": "TypeError: boom", "stack": "", "turn": 1} for _ in range(50)]
    errors.append({"message": "TypeError: boom", "stack": "", "turn": 2})
    play_gate._attach_errors(turns, errors, tmp_path)
    assert turns[0]["error"]["message"] == "TypeError: boom"
    assert "error" not in turns[1]


def test_parse_report_folds_a_thrown_turn_into_broken_regardless_of_the_models_own_summary():
    turns = [{"verdict": "unclear", "action": {"key": "Enter"}, "expected": "the run starts",
              "observed": "stayed on stage select",
              "error": {"message": "TypeError: floats is not iterable", "address": "gen.js:42"}}]
    report = play_gate._parse_report(json.dumps({"broken": [], "judgment": {}}), turns)
    assert len(report["broken"]) == 1
    fact = report["broken"][0]
    assert fact["action"] == "Enter" and fact["expected"] == "the run starts"
    assert fact["error"]["message"] == "TypeError: floats is not iterable"


# ---------------------------------------------------------------- the note

def test_note_carries_the_fact():
    note = play_gate.note_for_fact({"action": "e", "expected": "potion consumed", "observed": ""})
    assert "Input pressed: e" in note
    assert "potion consumed" in note
    assert "(no visible change)" in note
    assert "keep the game playing the way it already does" in note


def test_note_without_error_has_no_thrown_line():
    note = play_gate.note_for_fact({"action": "e", "expected": "x", "observed": "y"})
    assert "What the page threw" not in note


def test_note_with_error_states_what_the_page_threw():
    fact = {"action": "Enter", "expected": "the run starts", "observed": "stayed on stage select",
            "error": {"message": "TypeError: floats is not iterable", "address": "gen.js:42"}}
    note = play_gate.note_for_fact(fact)
    assert "What the page threw: TypeError: floats is not iterable at gen.js:42" in note


def test_thrown_turn_flows_end_to_end_into_the_fix_note(tmp_path):
    turns = [{"verdict": "unmet", "action": {"key": "Enter"}, "expected": "the run starts",
              "observed": "stayed on stage select"}]
    errors = [{"message": "TypeError: floats is not iterable",
               "stack": "TypeError: floats is not iterable\n    at gen.js:42", "turn": 1}]
    play_gate._attach_errors(turns, errors, tmp_path)
    report = play_gate._parse_report(json.dumps({"broken": [], "judgment": {}}), turns)
    note = play_gate.note_for_fact(report["broken"][0])
    assert "Input pressed: Enter" in note
    assert "What the page threw: TypeError: floats is not iterable" in note
    assert "gen.js:42" in note


# ---------------------------------------------------------------- the loop

def _run(tmp_path, monkeypatch, reports, state=None):
    """Drive after_build with a stubbed session; returns (kicked notes, run_dir)."""
    run_dir = tmp_path / "runs" / "r1"
    (run_dir / "game").mkdir(parents=True)
    if state:
        (run_dir / play_gate.STATE_FILE).write_text(json.dumps(state))

    kicked = []

    class _RS:
        def __init__(self, run_id):
            self.run_dir = run_dir
        def read_spec(self):
            return {"request": "a game"}

    import maestro.state
    import maestro.codegen.build_chain as build_chain
    import maestro.codegen.staging as staging
    import tools.execution_context as ec

    monkeypatch.setattr(maestro.state, "RunState", _RS)
    monkeypatch.setattr(staging, "game_dir", lambda rd: rd / "game")
    monkeypatch.setattr(ec, "run_scope", lambda rid, bid: __import__("contextlib").nullcontext())
    def kickoff(run_id, *, kind, note, after):
        assert (kind, after) == ("fix", "b1")
        kicked.append(note)
        return "f1"

    monkeypatch.setattr(build_chain, "kickoff", kickoff)
    monkeypatch.setattr(play_gate, "play", lambda gdir, req: reports.pop(0))
    started = play_gate.after_build("r1", "b1")
    return started, kicked, run_dir


def test_broken_fact_kicks_one_fix(tmp_path, monkeypatch):
    report = {"broken": [{"action": "e", "expected": "use", "observed": "nothing"},
                         {"action": "q", "expected": "drink", "observed": "nothing"}],
              "judgment": {}, "turns": []}
    started, kicked, run_dir = _run(tmp_path, monkeypatch, [report])
    assert started and len(kicked) == 1
    assert "Input pressed: e" in kicked[0]
    assert json.loads((run_dir / play_gate.REPORT_FILE).read_text())["broken"]


def test_thrown_fact_preferred_over_plain_fact(tmp_path, monkeypatch):
    report = {"broken": [{"action": "q", "expected": "drink", "observed": "nothing"},
                         {"action": "Enter", "expected": "the run starts",
                          "observed": "stayed on stage select",
                          "error": {"message": "TypeError: boom", "address": "gen.js:9"}}],
              "judgment": {}, "turns": []}
    started, kicked, _ = _run(tmp_path, monkeypatch, [report])
    assert started and len(kicked) == 1
    assert "Input pressed: Enter" in kicked[0]
    assert "What the page threw: TypeError: boom at gen.js:9" in kicked[0]


def test_clean_report_starts_nothing_and_writes_report(tmp_path, monkeypatch):
    started, kicked, run_dir = _run(tmp_path, monkeypatch,
                                    [{"broken": [], "judgment": {"works": "all"}, "turns": [1]}])
    assert not started and not kicked
    assert (run_dir / play_gate.REPORT_FILE).exists()


def test_failed_session_stands_aside(tmp_path, monkeypatch):
    started, kicked, run_dir = _run(tmp_path, monkeypatch, [None])
    assert not started and not kicked
    assert not (run_dir / play_gate.REPORT_FILE).exists()


def test_same_fact_twice_stops(tmp_path, monkeypatch):
    report = {"broken": [{"action": "e", "expected": "use", "observed": "no"}],
              "judgment": {}, "turns": []}
    started, kicked, _ = _run(tmp_path, monkeypatch, [report],
                              state={"rounds": 1, "last_fact": "e|use"})
    assert not started and not kicked


def test_max_rounds_stops(tmp_path, monkeypatch):
    report = {"broken": [{"action": "w", "expected": "move", "observed": "no"}],
              "judgment": {}, "turns": []}
    started, kicked, _ = _run(tmp_path, monkeypatch, [report],
                              state={"rounds": play_gate.MAX_ROUNDS, "last_fact": None})
    assert not started and not kicked
