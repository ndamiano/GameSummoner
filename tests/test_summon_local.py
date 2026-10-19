import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("summon_local", ROOT / "scripts" / "summon_local.py")
summon_local = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summon_local)


def _turn(name, args, reasoning=""):
    return {"kind": "turn", "response": {"reasoning_content": reasoning, "tool_calls": [
        {"function": {"name": name, "arguments": json.dumps(args)}}]}}


def test_turn_events_name_each_tool_the_python_call_used_and_skip_reads():
    code = ("src = read_file('src/main.js')\n"
            "edit_file('src/main.js', old, new)\n"
            "write_file(path=\"src/capture.js\", content=x)\n"
            "generate_media(id='slime', subject='a slime')\n"
            "print(check_syntax())\nplay(seconds=5)\n")
    acts, thought = summon_local.turn_events(_turn("python", {"code": code}, "\n  Now wire capture.\nmore"))
    assert acts == ["edited src/main.js", "wrote src/capture.js", "conjuring art: slime",
                    "checked the code", "play-tested the game"]
    assert thought == "Now wire capture."
    assert summon_local.turn_events(_turn("done", {"summary": "ok"})) == (["declared it finished"], None)


def test_design_summary_counts_words_and_lists_the_picked_shape(tmp_path):
    design = tmp_path / "design.md"
    design.write_text("# 0. SCOPE\n\n| Requirement | Where |\n| --- | --- |\n"
                      "| Game prompt: “monsters” | §4 |\n| Dimension: 2D | §3 |\n"
                      "| Camera: top-down | §3 |\n")
    words, picks = summon_local.design_summary(design)
    assert words == len(design.read_text().split())
    assert picks == ["2D", "top-down"]


def test_detail_replays_the_turn_log_into_a_collapsed_feed_with_step_and_design(tmp_path):
    run = tmp_path / "r1"
    run.mkdir()
    turns = [_turn("python", {"code": "play()"}), _turn("python", {"code": "play()"}),
             _turn("write_file", {"path": "src/a.js", "content": ""}, "Fix the spawn.")]
    (run / "turns.jsonl").write_text("".join(json.dumps(t) + "\n" for t in turns))
    (run / "build_state.json").write_text(json.dumps({"step": 40, "max_steps": 200}))
    (run / "design.md").write_text("| Dimension: 2D | §3 |\n")
    backend = object.__new__(summon_local.LocalBackend)
    backend.runs, backend._turns, backend._designs = tmp_path, {}, {}
    assert backend.detail("r1") == {"step": 40, "max_steps": 200, "design_note": "6 words · 2D",
                                    "feed": [["play-tested the game", 2], ["wrote src/a.js", 1]],
                                    "thought": "Fix the spawn."}
    with open(run / "turns.jsonl", "a") as f:
        f.write(json.dumps(_turn("python", {"code": "play()"})) + "\n")
    assert backend.detail("r1")["feed"][-1] == ["play-tested the game", 1]


def test_game_title_reads_the_page_title_and_decodes_entities(tmp_path):
    (tmp_path / "index.html").write_text("<title>Claws &amp; Cantrips</title>")
    assert summon_local.game_title(tmp_path) == "Claws & Cantrips"
    assert summon_local.game_title(tmp_path / "missing") is None
