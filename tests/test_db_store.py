"""Platform db — games ownership/lifecycle, the charge contract, builds, and the event log."""

import json
from pathlib import Path

import pytest

from db import connection, events, games, jobs, workers


def test_create_and_owner():
    games.create_game("g1", "u1")
    assert games.owner_of("g1") == "u1"
    assert games.owner_of("ghost") is None
    row = games.game("g1")
    assert row["status"] == "draft"
    assert row["credits_spent"] == 0


def test_list_is_per_user():
    games.create_game("g1", "u1")
    games.create_game("g2", "u2")
    assert [g["id"] for g in games.list_games("u1")] == ["g1"]


def test_prompt_meta_mirrors_the_title_and_marks_the_run_buildable():
    games.create_game("g1", "u1")
    assert games.game("g1")["status"] == "draft"
    games.update_prompt_meta("g1", "Moon Miner")
    row = games.game("g1")
    assert (row["title"], row["status"]) == ("Moon Miner", "ready")


def test_charge_grants_compute_and_is_durable():
    games.create_game("g1", "u1")
    assert not games.is_charged("g1")
    games.charge_game("g1", 1, 14_400)
    assert games.is_charged("g1")
    row = games.game("g1")
    assert row["credits_spent"] == 1
    assert row["granted_micros"] == 14_400
    # An extension stacks; it never resets.
    games.charge_game("g1", 1, 14_400)
    row = games.game("g1")
    assert (row["credits_spent"], row["granted_micros"]) == (2, 28_800)


def test_build_lifecycle():
    games.create_game("g1", "u1")
    bid = games.create_build("g1", kind="build")
    games.build_started(bid)
    games.build_finished(bid, "succeeded", steps=7)
    (b,) = games.builds_for("g1")
    assert (b["id"], b["kind"], b["status"], b["steps"]) == (bid, "build", "succeeded", 7)
    assert b["queued_at"] <= b["started_at"] <= b["finished_at"]


def test_a_job_on_a_game_must_name_its_build():
    """Cost and history join on the build, so a game's job with no build is a bug at the enqueue,
    not a row to reconcile later. Platform jobs (no game) stay unattributed."""
    games.create_game("g1", "u1")
    games.charge_game("g1", 1, 1_000_000)
    with pytest.raises(ValueError):
        jobs.enqueue_job("llm", {}, game_id="g1")
    assert jobs.enqueue_job("llm", {}, game_id="g1", build_id="b1")
    assert jobs.enqueue_job("llm", {})


def test_a_game_accumulates_builds():
    games.create_game("g1", "u1")
    games.create_build("g1", kind="build")
    games.create_build("g1", kind="fix")
    games.create_build("g1", kind="assets")
    assert [b["kind"] for b in games.builds_for("g1")] == ["build", "fix", "assets"]


def test_events_append_and_replay_after_id():
    games.create_game("g1", "u1")
    events.record_event("g1", "spec_proposed", {"title": "Moon Miner"})
    events.record_event("g1", "build_step", {"step": 1})
    events.record_event("g2", "build_step", {"step": 9})

    rows = events.events_for("g1")
    assert [e["kind"] for e in rows] == ["spec_proposed", "build_step"]
    assert rows[0]["payload"] == {"title": "Moon Miner"}

    later = events.events_for("g1", after_id=rows[0]["id"])
    assert [e["kind"] for e in later] == ["build_step"]


def test_an_event_names_the_build_it_came_from():
    """A game holds its build and every fix after it, so the log is only readable per build."""
    games.create_game("g1", "u1")
    b1 = games.create_build("g1")
    b2 = games.create_build("g1", kind="fix")
    events.record_event("g1", "build_step", {"step": 1}, b1)
    events.record_event("g1", "build_step", {"step": 1}, b2)
    events.record_event("g1", "prompt_proposed", {"title": "Moon Miner"})

    rows = events.events_for("g1")
    assert [e["build_id"] for e in rows] == [b1, b2, None]


def test_event_payload_survives_non_json_values():
    games.create_game("g1", "u1")
    events.record_event("g1", "weird", {"path": Path("/tmp/x")})
    (e,) = events.events_for("g1")
    assert e["payload"]["path"] == "/tmp/x"


def _build_with_art():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    bid = games.create_build("g1", kind="build")
    turn = jobs.enqueue_job("llm", {}, game_id="g1", build_id=bid,
                             metadata={"stage": "build", "run_id": "g1", "build_id": bid})
    art = jobs.enqueue_job("image", {}, game_id="g1", build_id=bid, batch_id="b",
                            metadata={"run_id": "g1", "asset_id": "goblin"})
    return bid, turn, art


def test_cancel_pending_build_turn_spares_the_art():
    """Asset renders ride the same build_id as the turn that asked for them, so a pause that failed
    every job under the build would throw away art the model already paid for."""
    bid, turn, art = _build_with_art()
    assert jobs.cancel_pending_build_turn(bid, "paused by hand") == 1
    assert jobs.get_job(turn)["status"] == "failed"
    assert jobs.get_job(art)["status"] == "pending"


def test_a_claimed_turn_is_left_to_its_worker():
    bid, turn, _ = _build_with_art()
    jobs.claim_job("llm", "w1", 60)
    assert jobs.cancel_pending_build_turn(bid, "paused by hand") == 0
    assert jobs.get_job(turn)["status"] == "claimed"


def test_queue_has_work_sees_pending_and_lapsed_leases():
    jobs.enqueue_job("llm", {"k": 1})
    assert jobs.queue_has_work("llm") is True
    assert jobs.queue_has_work("image") is False

    job = jobs.claim_job("llm", "w1", lease_seconds=60)
    assert jobs.queue_has_work("llm") is False

    with connection.platform_db() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at = 0 WHERE id = ?", (job["id"],))
    assert jobs.queue_has_work("llm") is True


def test_a_finished_llm_row_keeps_measurements_and_never_the_words():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    job_id = jobs.enqueue_job("llm", {"body": {
        "model": "qwen", "max_tokens": 2048, "reasoning": "medium",
        "messages": [{"role": "system", "content": "You are a game designer."},
                     {"role": "user", "content": "Make a space game"}],
        "tools": [{"name": "draw_sprite"}]}}, game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, 0.99)
    jobs.claim_job("llm", "w1", 60)
    reply = {"choices": [{"message": {"role": "assistant", "content": "I'll create a space game",
                                      "tool_calls": [{"id": "c1", "type": "function", "function": {
                                          "name": "draw_sprite", "arguments": '{"prompt":"ship"}'}}]},
                          "finish_reason": "tool_calls"}],
             "usage": {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}}
    jobs.complete_job(job_id, "w1", reply, None, exec_seconds=5.0)

    job = jobs.get_job(job_id)
    assert job["payload"] == {"model": "qwen", "n_messages": 1, "prompt_chars": 41,
                              "reasoning": "medium", "max_tokens": 2048}
    assert job["result"] == reply    # whole until its consumer has read it

    jobs.elide_job_result(job_id)
    job = jobs.get_job(job_id)
    assert job["result"] == {"usage": reply["usage"], "finish_reason": "tool_calls",
                             "tool_names": ["draw_sprite"]}
    assert "ship" not in str(job) and "space game" not in str(job)


def test_a_responses_wire_turn_elides_to_the_same_fields():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    job_id = jobs.enqueue_job("llm", {"body": {
        "model": "m", "max_output_tokens": 900, "reasoning": {"effort": "low"},
        "instructions": "sys", "input": [{"type": "message", "role": "user", "content": "hi"}]}},
        game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "llm", None, 0.99)
    jobs.claim_job("llm", "w1", 60)
    jobs.complete_job(job_id, "w1", {"output": [{"type": "function_call", "name": "write",
                                                  "arguments": "{}"}], "status": "completed",
                                      "usage": {"input_tokens": 9}}, None, 1.0)
    jobs.elide_job_result(job_id)
    job = jobs.get_job(job_id)
    assert job["payload"] == {"model": "m", "n_messages": 1, "prompt_chars": 5, "reasoning": "low",
                              "max_tokens": 900}
    assert job["result"] == {"usage": {"input_tokens": 9}, "finish_reason": "completed",
                             "tool_names": ["write"]}


def test_an_art_row_keeps_its_prompt_and_render_record_and_drops_the_blobs():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    job_id = jobs.enqueue_job("image", {"kind": "comfy_image",
                                         "workflow": {"p": {"inputs": {"text": "a red dragon"}}},
                                         "uploads": [{"name": "init.png", "b64": "AAAA"}]},
                               game_id="g1", build_id="b1")
    workers.worker_created("w1", None, "image", None, 0.99)
    jobs.claim_job("image", "w1", 60)
    jobs.complete_job(job_id, "w1", {"model": "sdxl", "images": [
        {"file": "/blobs/x.png", "safety": {"scores": {"NSFW": 0.1}}, "png_b64": "BBBB"}]},
        None, 5.0)
    jobs.elide_job_result(job_id)
    job = jobs.get_job(job_id)
    assert job["payload"] == {"prompt": "a red dragon", "mode": "img2img"}
    assert job["result"] == {"model": "sdxl", "images": [
        {"file": "/blobs/x.png", "safety": {"scores": {"NSFW": 0.1}}}]}


def _enqueue_llm_turn(build_id="b1", stage="build"):
    body = {"model": "qwen", "messages": [{"role": "user", "content": "Make a space game " * 40}]}
    return jobs.enqueue_job("llm", {"body": body}, game_id="g1", build_id=build_id,
                             metadata={"stage": stage, "run_id": "g1", "build_id": build_id})


def _payload_of(job_id):
    return jobs.get_job(job_id)["payload"]


def test_every_failed_path_leaves_measurements_not_words():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    measured = {"model": "qwen", "n_messages": 1, "prompt_chars": 720, "reasoning": None,
                "max_tokens": None}

    abandoned = _enqueue_llm_turn()
    assert jobs.abandon_job(abandoned, "timed out") is True
    assert _payload_of(abandoned) == measured

    by_build = _enqueue_llm_turn(build_id="b2")
    assert jobs.abandon_build_jobs("b2", "stopped") == 1
    assert _payload_of(by_build) == measured

    cancelled = _enqueue_llm_turn(build_id="b3")
    assert jobs.cancel_pending_build_turn("b3", "paused") == 1
    assert _payload_of(cancelled) == measured

    stale = _enqueue_llm_turn(build_id="b4")
    assert [r["id"] for r in jobs.fail_stale_pending(-1.0)] == [stale]
    assert _payload_of(stale) == measured

    for job_id in (abandoned, by_build, cancelled, stale):
        row = jobs.get_job(job_id)
        assert row["status"] == "failed" and row["error"]
        assert "space game" not in str(row)


def test_a_failed_batch_render_drops_its_blob():
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 144_000)
    job_id = jobs.enqueue_job("mesh", {"kind": "trellis_mesh", "image_b64": "A" * 5000},
                               game_id="g1", build_id="b1", batch_id="batch1")
    assert jobs.abandon_pending_batch_jobs("g1", "budget") == 1
    assert _payload_of(job_id) == {"kind": "trellis_mesh"}

