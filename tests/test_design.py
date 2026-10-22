"""The design stage: one decide job makes the decisions, one llm call writes the spec from them,
and the build starts on it."""

import json

import pytest

import maestro.codegen.run as run_mod
from maestro.codegen import build_chain, design, staging
from maestro.state import RunState

ASK = "Make me a platformer where you play as a frog."
SECTIONS = ["0. SCOPE", "1. CONVENTIONS", "2. CONTRACTS", "3. VISUAL SPEC", "4. GAMEPLAY SPEC",
            "5. CHARACTERS", "6. AUDIO", "7. UX", "8. DEBUG API", "9. TESTS", "10. BUILD ORDER",
            "11. DEFINITION OF DONE", "A. SANITY"]
SPEC = "# BUILD SPEC\n\nEvery number is final.\n\n" + "\n\n".join(f"# {h}\n\nbody of {h}" for h in SECTIONS)
HALF = SPEC[:SPEC.index("# 7. UX")]
REST = SPEC[SPEC.index("# 7. UX"):]
DECIDED = {
    "state": "\n".join([f"Game prompt: {ASK}", "dimension: 2d", "camera: side-on", "win/lose: reach the exit",
                        "mechanics: jump, bounce, coins, enemies, leaderboard",
                        "art style: pixel art: low-resolution sprites", "palette: saturated: bright, strong colours",
                        "screens: title, game over"]),
    "answers": {"dimension": "2d", "mechanics": "jump, bounce, coins, enemies, leaderboard"},
    "steps": [{"key": "dimension", "skipped": False, "probabilities": {"2d": 0.88, "3d": 0.12}},
              {"key": "first playable", "skipped": False,
               "probabilities": {"bounce": 0.61, "jump": 0.94, "coins": 0.45, "enemies": 0.3,
                                 "leaderboard": 0.08}}],
}
TIERED = "\n".join([f"Game prompt: {ASK}", "dimension: 2d", "camera: side-on", "win/lose: reach the exit",
                    "core mechanics: jump, bounce", "supporting mechanics: coins, enemies",
                    "extra mechanics: leaderboard", "art style: pixel art: low-resolution sprites",
                    "palette: saturated: bright, strong colours", "screens: title, game over"])


def _reply(content):
    return {"choices": [{"message": {"content": content}}]}


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(run_mod, "_emit", lambda et, rid, **p: captured.append((et, rid, p)))
    return captured


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    monkeypatch.setattr(run_mod.games, "update_prompt_meta", lambda *a, **k: None)
    monkeypatch.setattr(run_mod.games, "charge_game", lambda *a, **k: None)
    monkeypatch.setattr(design.games, "create_build", lambda *a, **k: "dbuild")
    monkeypatch.setattr(design.games, "build_started", lambda *a, **k: None)


@pytest.fixture
def finished(monkeypatch):
    out = []
    monkeypatch.setattr(design.games, "build_finished",
                        lambda bid, status, steps=None: out.append((bid, status)))
    return out


@pytest.fixture
def kicked(monkeypatch):
    started = []
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: started.append((rid, kw)) or "bid")
    return started


@pytest.fixture
def enqueued(monkeypatch):
    out = []

    def _enqueue(queue, payload, **kw):
        text = payload["body"]["messages"][-1]["content"] if queue == "llm" else payload
        out.append((queue, kw["metadata"]["step"], text, kw["metadata"], kw.get("build_id")))
        return f"job{len(out)}"
    monkeypatch.setattr(design.jobs, "enqueue_job", _enqueue)
    return out


@pytest.fixture
def connector(monkeypatch):
    class Fake:
        replies = []
        model_name = "m"
        reasoning = "medium"
        seen = []

        def generate_with_tools(self, msgs, tools, max_tokens=None, **kw):
            Fake.seen.append(msgs)
            return _reply(Fake.replies.pop(0))

        def build_llm_job(self, msgs, tools, max_tokens):
            Fake.seen.append(msgs)
            return {"body": {"messages": msgs}}, "m"

    Fake.seen = []
    Fake.replies = []
    monkeypatch.setattr(design, "get_connector", lambda: Fake())
    return Fake


def _propose(run_id):
    return run_mod.propose_prompt(ASK, run_id)


def _decided(run_id):
    _propose(run_id)
    design.on_complete(run_id, "dbuild", DECIDED, None, "decide")


def test_the_ask_is_stored_verbatim_and_one_decide_job_carries_every_decision(
        tmp_runs, events, connector, enqueued):
    run_id = run_mod.create_run("u1")
    spec = _propose(run_id)

    assert spec["ask"] == ASK and "request" not in spec
    assert RunState(run_id).read_spec() == spec
    assert [(e[0], e[1]) for e in enqueued] == [("decide", "decide")]
    assert enqueued[0][3] == {"stage": "design", "run_id": run_id, "step": "decide"}
    job = enqueued[0][2]
    assert job["kind"] == "decide" and job["state"] == f"Game prompt: {ASK}"
    steps = {s["key"]: s for s in job["steps"]}
    assert steps["camera"]["options_by"]["key"] == "dimension"
    assert {"name": "jump", "text": "jump: the player leaps into the air and lands under gravity"} \
        in steps["mechanics"]["items"]
    assert len(steps["mechanics"]["items"]) == 157 and steps["mechanics"]["keep_over"] == 0.6
    assert steps["first playable"]["items_from"] == "mechanics" and not steps["first playable"]["record"]
    assert {"name": "title", "text": "title"} in steps["screens"]["items"]
    assert all("{item}" in s["question"] for s in job["steps"] if s["kind"] == "noul")
    assert [s["when"] for s in job["steps"] if s["key"] == "art style"] == [{"dimension": "2d"},
                                                                          {"dimension": "3d"}]
    assert events == []


def test_the_decisions_are_banded_by_first_playable_and_written_into_one_spec(
        tmp_runs, events, connector, enqueued, kicked, finished):
    run_id = run_mod.create_run("u1")
    _decided(run_id)

    assert [(e[0], e[1]) for e in enqueued] == [("decide", "decide"), ("llm", "write")]
    prompt = enqueued[-1][2]
    assert TIERED in prompt and "mechanics: jump, bounce, coins" not in prompt
    assert prompt.rstrip().endswith(ASK) and "{state}" not in prompt and "{request}" not in prompt
    assert enqueued[-1][4] == "dbuild"
    assert json.loads((RunState(run_id).run_dir / "design" / "decisions.json").read_text()) == DECIDED
    assert "request" not in RunState(run_id).read_spec() and kicked == []

    design.on_complete(run_id, "dbuild", _reply(SPEC), None, "write")

    spec = RunState(run_id).read_spec()
    assert spec["ask"] == ASK and ASK in spec["request"] and "design/design.md" in spec["request"]
    assert staging.spec_path(RunState(run_id).run_dir).read_text() == SPEC
    assert [e[0] for e in events] == ["prompt_proposed"]
    assert kicked == [(run_id, {"kind": "build", "designed": True})]
    assert finished == [("dbuild", "succeeded")]


def test_a_write_that_stops_early_is_asked_to_continue(tmp_runs, events, connector, enqueued,
                                                       kicked):
    run_id = run_mod.create_run("u1")
    _decided(run_id)

    design.on_complete(run_id, "dbuild", _reply(HALF), None, "write")

    assert enqueued[-1][1] == "write" and HALF in enqueued[-1][2] and TIERED in enqueued[-1][2]
    assert "Continue it from that point" in enqueued[-1][2]
    assert kicked == []

    design.on_complete(run_id, "dbuild", _reply(REST), None, "write")

    assert staging.spec_path(RunState(run_id).run_dir).read_text() == HALF + REST
    assert kicked == [(run_id, {"kind": "build", "designed": True})]


@pytest.mark.parametrize("step, result, error", [
    ("decide", None, "decide image returned 502"),
    ("decide", {}, None),
    ("write", _reply(""), None),
])
def test_a_failed_step_is_asked_for_again_before_the_ask_is_the_prompt(
        tmp_runs, events, connector, enqueued, kicked, finished, step, result, error):
    run_id = run_mod.create_run("u1")
    _decided(run_id) if step == "write" else _propose(run_id)
    asked = len(enqueued)

    design.on_complete(run_id, "dbuild", result, error, step)

    assert [e[1] for e in enqueued[asked:]] == [step]
    assert enqueued[-1][2] == enqueued[asked - 1][2]
    assert RunState(run_id).read_spec().get("request") is None
    assert kicked == [] and finished == []

    design.on_complete(run_id, "dbuild", result, error, step)

    assert RunState(run_id).read_spec()["request"] == ASK
    assert [e[0] for e in events] == ["prompt_proposed"]
    assert [r for r, _ in kicked] == [run_id]
    assert finished == [("dbuild", "failed")]


def test_a_write_the_worker_failed_leaves_the_ask_as_the_prompt(tmp_runs, events, connector,
                                                                enqueued, kicked, finished):
    run_id = run_mod.create_run("u1")
    _decided(run_id)

    design.on_complete(run_id, "dbuild", None, "the worker died", "write")

    assert RunState(run_id).read_spec()["request"] == ASK
    assert finished == [("dbuild", "failed")]


def test_an_enqueue_that_is_refused_builds_from_the_ask_at_once(tmp_runs, events, connector,
                                                                kicked, monkeypatch):
    monkeypatch.setattr(design.jobs, "enqueue_job",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no compute")))
    run_id = run_mod.create_run("u1")

    _propose(run_id)

    assert RunState(run_id).read_spec()["request"] == ASK
    assert [e[0] for e in events] == ["prompt_proposed"]
    assert kicked == [(run_id, {"kind": "build", "designed": True})]


def test_a_step_that_lands_after_a_pause_is_dropped_and_resume_designs_from_the_start(
        tmp_runs, events, connector, enqueued, kicked, finished):
    run_id = run_mod.create_run("u1")
    _decided(run_id)
    design.on_complete(run_id, "dbuild", _reply(HALF), None, "write")
    asked = len(enqueued)
    design.games.move(run_id, "paused")

    design.on_complete(run_id, "dbuild", _reply(REST), None, "write")

    assert len(enqueued) == asked and kicked == []
    assert RunState(run_id).read_spec().get("request") is None
    assert finished == [("dbuild", "stopped")]

    design.enqueue(run_id, ASK)

    assert [e[1] for e in enqueued[asked:]] == ["decide"]
    assert not (RunState(run_id).run_dir / "design" / "spec.md").exists()
    row = design.games.game(run_id)
    assert (row["stage"], row["status"]) == ("designing", "running")


def test_the_human_edit_moves_only_the_request(tmp_runs, events, connector, enqueued, kicked):
    run_id = run_mod.create_run("u1")
    _decided(run_id)
    design.on_complete(run_id, "dbuild", _reply(SPEC), None, "write")
    title = RunState(run_id).read_spec()["title"]

    run_mod.set_prompt(run_id, "A user asked for a game. Read design/ and build it.")

    spec = RunState(run_id).read_spec()
    assert spec["ask"] == ASK and spec["title"] == title
    assert spec["request"].endswith("build it.")
    assert events[-1][0] == "prompt_updated"


def test_the_cli_decides_and_writes_before_it_builds(tmp_runs, events, connector, monkeypatch,
                                                     finished, capsys):
    import workqueue.client
    monkeypatch.setattr(run_mod.store, "list_users", lambda: [type("U", (), {"id": "u1"})()])
    decides = [{"status": "failed", "error": "no decide worker"}, {"status": "done", "result": DECIDED}]
    monkeypatch.setattr(workqueue.client, "run_job", lambda queue, payload, **kw: decides.pop(0))
    connector.replies = ["", HALF, REST]

    run_id = run_mod._new_run(ASK)

    spec = RunState(run_id).read_spec()
    assert ASK in spec["request"] and spec["ask"] == ASK
    assert staging.spec_path(RunState(run_id).run_dir).read_text() == HALF + REST
    assert len(connector.seen) == 3 and connector.seen[0] == connector.seen[1]
    assert TIERED in connector.seen[0][-1]["content"]
    assert finished == [("dbuild", "succeeded")]
    assert "designing" in capsys.readouterr().out
