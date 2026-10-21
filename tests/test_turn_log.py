"""The TURN LOG: a build's conversation lives in its run dir, one copy of each message.

Every build turn is an llm job whose request is the WHOLE transcript so far, so a jobs row per turn
stored the same conversation once per turn (measured 2026-07-31: 951 MB of jobs.payload, 26 MB for
one 117-turn build). The driver now appends each landed turn to runs/<id>/turns.jsonl and clears
the row — append first, so there is never a moment with neither copy.
"""

import json

import pytest

import maestro.state
from auth import store
from db import connection, games, jobs, workers
from maestro.codegen import build_chain, build_state, build_steps, turn_log
from maestro.codegen.run import create_run
from maestro.state import RunState


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(connection, "auth_path", lambda: tmp_path / "auth.db")
    monkeypatch.setattr(connection, "platform_path", lambda: tmp_path / "platform.db")
    monkeypatch.setattr(maestro.state, "resolve_base_path", lambda input_path=None: tmp_path)
    # A window small enough that a couple of file writes crosses it, so a test can drive a real
    # compaction instead of describing one.
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 2900)
    user = store.create_user("alice", "pw-pass1234", role="admin", email="alice@example.com")
    run_id = create_run(user.id)
    RunState(run_id).write_spec({"request": "make a game", "title": "Moon Miner"})
    games.charge_game(run_id, 1, 1_000_000)
    workers.worker_created("w1", None, "llm", None, 0.99)
    return run_id


@pytest.fixture
def headers():
    def _issue(handle="admin"):
        u = store.create_user(handle, "pw-pass1234", role="admin", email=f"{handle}@example.com")
        return {"Authorization": f"Bearer {store.issue_token(u.id)}"}
    return _issue


def _reply(programs=None, content="", prompt_tokens=10):
    message = {"role": "assistant", "content": content}
    if programs:
        message["tool_calls"] = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "python", "arguments": json.dumps({"code": code})}}
            for i, code in enumerate(programs)]
    return {"choices": [{"message": message}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 20}}


def _pending():
    """The turn waiting on the queue, carrying the body that was enqueued for it."""
    job = jobs.claim_job("llm", "w1", 60)
    assert job is not None, "the build enqueued no turn"
    return job


def _land(run_id, build_id, reply):
    """One turn, exactly as a worker completes it: claim, complete, drive the next."""
    job = _pending()
    jobs.complete_job(job["id"], "w1", reply, None, 2.0)
    build_chain.on_completion(run_id, build_id, reply, None, job["id"], 2.0)
    return job


def _write(name, size=40):
    body = "x" * size
    return f'write_file(path="{name}", content="{body}")'


def _log(run_id):
    return [json.loads(line) for line in
            turn_log.path(RunState(run_id).run_dir).read_text(encoding="utf-8").splitlines()]


def test_a_landed_turn_is_appended_and_its_row_keeps_only_measurements(run):
    build_id = build_chain.kickoff(run)
    job = _land(run, build_id, _reply([_write("index.html")]))

    kinds = [r["kind"] for r in _log(run)]
    assert kinds == ["meta", "turn"]
    record = _log(run)[1]
    assert record["job_id"] == job["id"]
    assert record["added"][0] == {"role": "user", "content": "make a game"}
    assert all(build_steps.is_seed(m) for m in record["added"][1:6])
    assert record["response"]["tool_calls"][0]["function"]["name"] == "python"
    assert (record["exec_seconds"], record["error"]) == (2.0, None)

    row = jobs.get_job(job["id"])
    assert "body" not in row["payload"] and row["payload"]["n_messages"] == 6


def test_every_request_the_build_sent_replays_from_the_log(run):
    """The whole point of storing only what each turn ADDED: turn k is the system prompt, the tool
    schemas and every added slice up to k. Including across a compaction, which drops rounds from
    the live transcript that the log still has to account for."""
    build_id = build_chain.kickoff(run)
    landed = [_land(run, build_id, _reply([_write("index.html", 6000)])),
              _land(run, build_id, _reply([_write("game.js", 300)], prompt_tokens=3000)),
              _land(run, build_id, _reply([_write("style.css")]))]

    assert [r["kind"] for r in _log(run)] == ["meta", "turn", "turn", "compact", "turn"]
    for job in landed:
        archived = turn_log.read_turn(RunState(run).run_dir, job["id"])
        assert job["payload"]["body"]["messages"] == (
            [{"role": "system", "content": archived["meta"]["system"]}] + archived["messages"])
        assert job["payload"]["body"]["tools"] == archived["meta"]["tools"]


def test_a_compaction_records_the_rounds_it_dropped_and_its_note(run, monkeypatch):
    # A window too small to hold even two rounds of stubs, so the only way to make room is to drop.
    monkeypatch.setattr(build_steps, "_n_ctx", lambda: 300)
    build_id = build_chain.kickoff(run)
    _land(run, build_id, _reply([_write("index.html", 3000)]))
    _land(run, build_id, _reply([_write("game.js", 3000)], prompt_tokens=3000))

    event = [r for r in _log(run) if r["kind"] == "compact"][-1]
    assert event["dropped"] >= 1
    assert "index.html" in event["note"]


def test_the_meta_record_is_written_once_per_build(run):
    build_id = build_chain.kickoff(run)
    for _ in range(3):
        _land(run, build_id, _reply([_write("index.html")]))

    assert [r["kind"] for r in _log(run)].count("meta") == 1
    assert build_state.load(RunState(run).run_dir).meta_logged is True


def test_a_fix_appends_its_own_build_and_keeps_the_first(run):
    """A fix re-enters the same turn machine on the same run dir. Its transcript starts over, so it
    gets its own meta — and truncating the file would throw away the build being fixed."""
    build_id = build_chain.kickoff(run)
    _land(run, build_id, _reply([_write("index.html")]))
    fix_id = build_chain.kickoff(run, kind="fix", note="the ship never moves")
    fix_job = _land(run, fix_id, _reply([_write("game.js")]))

    metas = [r for r in _log(run) if r["kind"] == "meta"]
    assert [m["build_id"] for m in metas] == [build_id, fix_id]
    archived = turn_log.read_turn(RunState(run).run_dir, fix_job["id"])
    assert len(archived["messages"]) == 6
    assert "the ship never moves" in archived["messages"][0]["content"]
    assert all(build_steps.is_seed(m) for m in archived["messages"][1:])


def test_the_cli_build_carries_the_default_step_cap(run):
    """run_build names no cap; the cursor must still hold a number the step check can compare."""
    import inspect

    from maestro.codegen import run as cli
    cap = inspect.signature(cli.run_build).parameters["max_steps"].default
    build_chain.kickoff(run, kind="build", max_steps=cap)
    cursor = build_state.load(RunState(run).run_dir)
    assert cursor.max_steps == build_state.DEFAULT_MAX_STEPS
