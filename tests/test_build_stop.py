"""Stopping a run by hand.

A run with no way to end it is a run that stays live forever — the frontend shows it as building
and the reaper keeps re-driving it — and a job it left pending keeps the autoscaler renting pods
for it. Stop ends everything the run has in flight at whatever stage it is in, and keeps whatever
the model wrote.
"""

import pytest

from db import errors, games, jobs
from maestro.codegen import build_chain, build_state
from maestro.codegen.build_state import BuildCursor
from maestro.state import RunState


@pytest.fixture
def calls(monkeypatch):
    seen = {"status": None, "attempt": None, "jobs": [], "staged": False}
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(build_chain.games, "set_status",
                        lambda run_id, s: seen.__setitem__("status", s))
    monkeypatch.setattr(build_chain.games, "build_finished",
                        lambda bid, s, steps=None: seen.__setitem__("attempt", s))
    monkeypatch.setattr(build_chain.jobs, "abandon_game_jobs",
                        lambda rid, err: seen["jobs"].append(rid) or 1)
    monkeypatch.setattr(build_chain.games, "finish_open_builds", lambda rid, s: [])
    monkeypatch.setattr(build_chain, "stage_for_play",
                        lambda *a, **k: seen.__setitem__("staged", True))
    return seen


@pytest.fixture
def run(tmp_path):
    build_state.save(tmp_path, BuildCursor(build_id="b1", step=36))
    return str(tmp_path)


def _playable(tmp_path):
    d = tmp_path / "game"
    d.mkdir(exist_ok=True)
    (d / "index.html").write_text("<html></html>", encoding="utf-8")


def test_stop_ends_the_cursor(run, tmp_path, calls):
    assert build_chain.stop(run) is True
    cursor = build_state.load(tmp_path)
    assert cursor.phase == "done" and cursor.ok is False


def test_a_stopped_build_keeps_a_playable_game(run, tmp_path, calls):
    """Stopping is not discarding — an index.html that exists is still a game."""
    _playable(tmp_path)
    build_chain.stop(run)
    assert build_state.load(tmp_path).ok is True
    assert calls["status"] == "built" and calls["staged"] is True


def test_a_stopped_build_with_nothing_written_failed(run, tmp_path, calls):
    build_chain.stop(run)
    assert calls["status"] == "failed" and calls["staged"] is False


def test_the_attempt_is_recorded_as_stopped_not_succeeded(run, tmp_path, calls):
    """The game is playable; the build still did not finish. The builds row says which."""
    _playable(tmp_path)
    build_chain.stop(run)
    assert calls["attempt"] == "stopped"


def test_stop_fails_every_job_the_run_owns(run, tmp_path, calls):
    build_chain.stop(run)
    assert calls["jobs"] == [run]


def test_a_late_completion_cannot_restart_a_stopped_build(run, tmp_path, calls, monkeypatch):
    """The worker running the last turn still reports in. It must not drive another one."""
    def boom(*a, **k):
        raise AssertionError("stepped after stop")
    build_chain.stop(run)
    monkeypatch.setattr(build_chain.build_steps, "step", boom)
    build_chain.advance(run)
    assert build_state.load(tmp_path).step == 36


def test_stop_reports_when_there_is_nothing_in_flight(tmp_path, calls, monkeypatch):
    """The API answers 409 off this."""
    monkeypatch.setattr(build_chain.jobs, "abandon_game_jobs", lambda rid, err: 0)
    assert build_chain.stop(str(tmp_path)) is False
    build_state.save(tmp_path, BuildCursor(build_id="b1", phase="done"))
    assert build_chain.stop(str(tmp_path)) is False


# Against the real store: what stop does to the queue and the run's rows at each stage.

@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(build_chain, "_emit", lambda *a, **k: None)
    monkeypatch.setattr("maestro.codegen.run._emit", lambda *a, **k: None)


def _game(tmp_runs, run_id="g1", ask="a game about frogs"):
    games.create_game(run_id, "u1")
    games.charge_game(run_id, 1, 1_000_000)
    RunState(run_id).write_spec({"ask": ask, "title": "frogs"})
    return run_id


def _designing(tmp_runs):
    run_id = _game(tmp_runs)
    bid = games.create_build(run_id, kind="design")
    games.build_started(bid)
    job = jobs.enqueue_job("llm", {"messages": []}, game_id=run_id, build_id=bid,
                            metadata={"stage": "design", "run_id": run_id})
    return run_id, bid, job


def test_stop_during_design_cancels_the_design_and_the_run_ends_failed(tmp_runs, quiet):
    run_id, bid, job = _designing(tmp_runs)
    assert build_chain.stop(run_id) is True
    assert jobs.get_job(job)["status"] == "failed"
    assert jobs.get_job(job)["error"] == "the run was stopped by hand"
    (build,) = games.builds_for(run_id)
    assert build["status"] == "stopped"
    assert games.game(run_id)["status"] == "failed"
    assert build_chain.stop(run_id) is False


def test_a_stopped_design_leaves_the_ask_as_the_prompt(tmp_runs, quiet):
    """The page reads a missing `request` as "still designing"; a stopped design is not."""
    run_id, _, _ = _designing(tmp_runs)
    build_chain.stop(run_id)
    assert RunState(run_id).read_spec()["request"] == "a game about frogs"


def test_stop_cancels_a_claimed_design_job_and_its_late_result_is_dropped(tmp_runs, quiet):
    """The worker holding the job may be the pod that hung; when it reports in after all, the
    result must not land the design and start a build."""
    run_id, bid, job = _designing(tmp_runs)
    jobs.claim_job("llm", "w1", 600)
    assert build_chain.stop(run_id) is True
    assert jobs.get_job(job)["status"] == "failed"
    assert jobs.complete_job(job, "w1", {"choices": []}, None, 12.0) is None
    assert games.game(run_id)["spent_micros"] == 0


def test_stop_releases_the_reservation(tmp_runs, quiet):
    run_id, _, _ = _designing(tmp_runs)
    before = games.compute_remaining(run_id)
    build_chain.stop(run_id)
    assert games.compute_remaining(run_id) > before


def _art_jobs(run_id, bid):
    return [jobs.enqueue_job(q, {}, game_id=run_id, build_id=bid, batch_id="batch1",
                              metadata={"run_id": run_id, "asset_id": f"a{i}",
                                        "then": {"enqueue": "mesh", "finalize": "assets"}})
            for i, q in enumerate(("image", "mesh"))]


def test_stop_cancels_pending_and_claimed_art_on_every_queue(tmp_runs, quiet, monkeypatch):
    run_id = _game(tmp_runs)
    bid = games.create_build(run_id, kind="art_build")
    games.build_started(bid)
    art = _art_jobs(run_id, bid)
    jobs.claim_job("image", "w1", 600)
    assert build_chain.stop(run_id) is True
    assert [jobs.get_job(j)["status"] for j in art] == ["failed"] * 2
    assert games.builds_for(run_id)[0]["status"] == "stopped"


def test_a_cancelled_art_jobs_late_completion_enqueues_nothing(tmp_runs, quiet):
    """An image landing normally chains a mesh; a cancelled one must not."""
    run_id = _game(tmp_runs)
    bid = games.create_build(run_id, kind="art_build")
    games.build_started(bid)
    image = _art_jobs(run_id, bid)[0]
    jobs.claim_job("image", "w1", 600)
    build_chain.stop(run_id)
    outcome = jobs.complete_job(image, "w1", {"images": [{"file": "x.png"}]}, None, 5.0,
                                 continuation={"queue": "mesh", "payload": {}})
    assert outcome is None
    assert all(j["status"] == "failed" for j in jobs.batch_jobs("batch1"))


def test_a_thread_still_working_for_a_stopped_build_cannot_enqueue(tmp_runs, quiet):
    """A world's later legs run on their own thread with the build's id; after stop their jobs
    are refused at the enqueue, so nothing new appears on a queue for the run."""
    run_id = _game(tmp_runs)
    bid = games.create_build(run_id, kind="build")
    games.build_started(bid)
    jobs.enqueue_job("mesh", {}, game_id=run_id, build_id=bid)
    build_chain.stop(run_id)
    with pytest.raises(errors.BuildEnded):
        jobs.enqueue_job("image", {}, game_id=run_id, build_id=bid)
    from tools.execution_context import run_scope
    from workqueue.client import run_job
    with run_scope(run_id, bid):
        assert run_job("image", {})["status"] == "failed"


def test_stop_with_a_cursor_ends_the_other_open_builds_too(tmp_runs, quiet, monkeypatch):
    run_id = _game(tmp_runs)
    bid = games.create_build(run_id, kind="build")
    games.build_started(bid)
    art = games.create_build(run_id, kind="art_build")
    games.build_started(art)
    build_state.save(RunState(run_id).run_dir, BuildCursor(build_id=bid, step=3))
    build_chain.stop(run_id)
    assert {b["id"]: b["status"] for b in games.builds_for(run_id)} == {
        bid: "stopped", art: "stopped"}
    assert games.game(run_id)["status"] == "failed"


def test_the_api_answers_409_with_nothing_in_flight(tmp_runs, app_client):
    from auth import store
    user = store.create_user("alice", "pw-pass1234", email="alice@example.com")
    hdr = {"Authorization": f"Bearer {store.issue_token(user.id)}"}
    games.create_game("g1", user.id)
    RunState("g1").write_spec({"ask": "x", "title": "x", "request": "x"})
    r = app_client.post("/api/games/g1/stop", headers=hdr)
    assert r.status_code == 409


def _user(handle, role="user"):
    from auth import store
    u = store.create_user(handle, "pw-pass1234", role=role, email=f"{handle}@example.com")
    return u, {"Authorization": f"Bearer {store.issue_token(u.id)}"}


def test_an_owner_cannot_stop_someone_elses_run(tmp_runs, app_client, quiet):
    owner, _ = _user("alice")
    _, stranger_hdr = _user("bob")
    games.create_game("g1", owner.id)
    RunState("g1").write_spec({"ask": "x", "title": "x", "request": "x"})
    assert app_client.post("/api/games/g1/stop", headers=stranger_hdr).status_code == 403


def test_an_admin_stops_a_run_owned_by_someone_else(tmp_runs, app_client, quiet):
    owner, _ = _user("alice")
    _, admin_hdr = _user("root", role="admin")
    games.create_game("g1", owner.id)
    games.charge_game("g1", 1, 1_000_000)
    RunState("g1").write_spec({"ask": "x", "title": "x"})
    bid = games.create_build("g1", kind="design")
    games.build_started(bid)
    job = jobs.enqueue_job("llm", {"messages": []}, game_id="g1", build_id=bid,
                            metadata={"stage": "design", "run_id": "g1"})

    r = app_client.post("/api/admin/games/g1/stop", headers=admin_hdr)
    assert r.status_code == 200 and r.json()["owner"] == owner.id
    assert jobs.get_job(job)["status"] == "failed"
    assert games.game("g1")["status"] == "failed"


def test_the_admin_stop_is_admin_only(tmp_runs, app_client, quiet):
    owner, owner_hdr = _user("alice")
    games.create_game("g1", owner.id)
    RunState("g1").write_spec({"ask": "x", "title": "x", "request": "x"})
    # Even over their OWN run: this surface is the operator's, and the owner has their own.
    assert app_client.post("/api/admin/games/g1/stop", headers=owner_hdr).status_code == 403


def test_the_admin_stop_404s_on_a_run_that_never_existed(tmp_runs, app_client, quiet):
    _, admin_hdr = _user("root", role="admin")
    assert app_client.post("/api/admin/games/nope/stop", headers=admin_hdr).status_code == 404


def test_the_admin_stop_409s_with_nothing_in_flight(tmp_runs, app_client, quiet):
    owner, _ = _user("alice")
    _, admin_hdr = _user("root", role="admin")
    games.create_game("g1", owner.id)
    RunState("g1").write_spec({"ask": "x", "title": "x", "request": "x"})
    assert app_client.post("/api/admin/games/g1/stop", headers=admin_hdr).status_code == 409
