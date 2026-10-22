import pytest

from db import errors, games, jobs
from maestro.codegen import build_chain, build_state, build_steps, design
from maestro.codegen.build_state import BuildCursor
from maestro.state import RunState


@pytest.fixture
def events(monkeypatch):
    seen = []
    monkeypatch.setattr(build_chain, "_emit", lambda kind, run_id, **kw: seen.append((kind, kw)))
    monkeypatch.setattr("maestro.codegen.run._emit", lambda *a, **k: None)
    return seen


@pytest.fixture
def staged(monkeypatch):
    out = {"staged": [], "snapshots": []}
    monkeypatch.setattr(build_chain, "stage_for_play",
                        lambda run_dir, slug: out["staged"].append(slug))
    monkeypatch.setattr(build_chain.snapshots, "take",
                        lambda run_dir, label: out["snapshots"].append(label))
    return out


@pytest.fixture
def threads(monkeypatch):
    started = []

    class _Thread:
        def __init__(self, target=None, args=(), daemon=None):
            self.call = (target, args)

        def start(self):
            started.append(self.call)

    monkeypatch.setattr(build_chain.threading, "Thread", _Thread)
    return started


def _game(run_id="g1", *, stage="building", status="running", step=3, playable=False):
    games.create_game(run_id, "u1")
    games.charge_game(run_id, 10, 10_000_000)
    games.move(run_id, status, stage)
    RunState(run_id).write_spec({"ask": "a game about frogs", "title": "frogs",
                                 "request": "a frog game"})
    bid = games.create_build(run_id, kind="build")
    games.build_started(bid)
    build_state.save(BuildCursor(build_id=bid, step=step))
    if playable:
        d = RunState(run_id).run_dir / "game"
        d.mkdir(exist_ok=True)
        (d / "index.html").write_text("<html>a frog game</html>", encoding="utf-8")
    return bid


def _turn(run_id, bid):
    return jobs.enqueue_job("llm", {}, game_id=run_id, build_id=bid,
                            metadata={"stage": "build", "run_id": run_id, "build_id": bid})


def _art(run_id, bid):
    return jobs.enqueue_job("comfy", {}, game_id=run_id, build_id=bid, batch_id="batch1",
                            metadata={"run_id": run_id, "asset_id": "a0"})


def test_pause_parks_the_game_cancels_its_queued_turn_and_refunds_the_step(tmp_runs, events,
                                                                          staged):
    bid = _game()
    turn, art = _turn("g1", bid), _art("g1", bid)
    assert build_chain.pause("g1") is True
    assert {k: games.game("g1")[k] for k in ("stage", "status")} == {"stage": "building",
                                                                     "status": "paused"}
    assert [(jobs.get_job(j)["status"], jobs.get_job(j)["error"]) for j in (turn, art)] == [
        ("failed", "paused by hand")] * 2
    assert build_state.load("g1").step == 2
    assert [k for k, _ in events] == ["build_paused"]
    assert all(kw.get("build_id") == bid for _, kw in events)


def test_a_turn_claimed_before_the_pause_keeps_its_step_and_lands(tmp_runs, events, staged,
                                                                  monkeypatch):
    bid = _game()
    turn = _turn("g1", bid)
    jobs.claim_job("llm", "w1", 600)

    def applies(spec, run_dir, tools, cursor, result, error=None):
        cursor.history.append({"role": "tool", "content": "ok"})
        return build_steps.Infer([], [], 100, report="wrote index.html")
    monkeypatch.setattr(build_chain.build_steps, "step", applies)
    monkeypatch.setattr(build_chain, "build_tools", lambda rs, build_id: None)

    build_chain.pause("g1")
    build_chain.on_completion("g1", bid, {"choices": []}, None, turn, 4.0)

    cursor = build_state.load("g1")
    assert cursor.step == 3 and cursor.history[-1]["content"] == "ok"
    assert jobs.queue_has_work("llm") is False
    assert [k for k, _ in events] == ["build_paused", "build_step"]


def test_a_paused_build_takes_no_step_however_often_it_is_re_driven(tmp_runs, events, staged,
                                                                    monkeypatch):
    _game()
    build_chain.pause("g1")
    events.clear()

    def boom(*a, **k):
        raise AssertionError("stepped while paused")
    monkeypatch.setattr(build_chain.build_steps, "step", boom)
    for _ in range(5):
        build_chain.advance("g1")
    assert events == [] and build_state.load("g1").step == 3


@pytest.mark.parametrize("playable, staged_as", [(True, ["g1"]), (False, [])])
def test_pause_keeps_a_playable_game_playable(tmp_runs, events, staged, playable, staged_as):
    _game(playable=playable)
    build_chain.pause("g1")
    assert staged["staged"] == staged_as
    assert staged["snapshots"] == (["paused"] if playable else [])


def test_pause_during_design_cancels_the_design_and_drops_its_late_result(tmp_runs, events, staged):
    games.create_game("g1", "u1")
    games.charge_game("g1", 10, 10_000_000)
    RunState("g1").write_spec({"ask": "a game about frogs", "title": "frogs"})
    bid = games.create_build("g1", kind="design")
    games.build_started(bid)
    claimed = jobs.enqueue_job("decide", {}, game_id="g1", build_id=bid,
                               metadata={"stage": "design", "run_id": "g1", "step": "decide"})
    jobs.claim_job("decide", "w1", 600)
    pending = jobs.enqueue_job("comfy", {}, game_id="g1", build_id=bid)
    before = games.compute_remaining("g1")

    assert build_chain.pause("g1") is True
    assert jobs.get_job(pending)["status"] == "failed"
    assert games.compute_remaining("g1") > before
    assert games.builds_for("g1")[0]["status"] == "stopped"

    design.on_complete("g1", bid, {"state": "genre: platformer"}, None, "decide")
    assert jobs.get_job(claimed)["status"] == "claimed"
    assert "request" not in RunState("g1").read_spec()
    assert [b["kind"] for b in games.builds_for("g1")] == ["design"]
    assert {k: games.game("g1")[k] for k in ("stage", "status")} == {"stage": "designing",
                                                                     "status": "paused"}


def test_a_paused_game_admits_no_new_job_on_any_queue(tmp_runs, events, staged):
    bid = _game()
    build_chain.pause("g1")
    for queue in ("llm", "comfy", "decide"):
        with pytest.raises(errors.GamePaused):
            jobs.enqueue_job(queue, {}, game_id="g1", build_id=bid)


@pytest.mark.parametrize("status", ["paused", "failed", "succeeded", "held"])
def test_only_a_running_game_can_be_paused(tmp_runs, events, staged, status):
    _game(status=status)
    assert build_chain.pause("g1") is False
    assert games.game("g1")["status"] == status


def test_resume_re_sends_the_turn_the_pause_cancelled(tmp_runs, events, staged, monkeypatch):
    bid = _game()
    _turn("g1", bid)
    build_chain.pause("g1")
    sent = []
    monkeypatch.setattr(build_chain, "build_tools", lambda rs, build_id: None)
    monkeypatch.setattr(build_chain.build_steps, "step",
                        lambda spec, run_dir, tools, cursor, result, error=None: build_steps.Infer(
                            [], [], 100, report="re-sent" if result is None else "applied"))
    monkeypatch.setattr(build_chain, "_enqueue_turn",
                        lambda run_id, cursor, inf: sent.append(inf.report) or True)
    events.clear()

    assert build_chain.resume("g1") is True
    assert sent == ["re-sent"] and build_state.load("g1").step == 3
    assert games.game("g1")["status"] == "running"
    assert [k for k, _ in events] == ["build_resumed", "build_step"]


def test_a_pause_during_the_gates_resumes_by_running_them_again(tmp_runs, events, staged, threads):
    bid = _game(stage="checking")
    assert build_chain.pause("g1") is True
    assert staged["staged"] == [] and games.game("g1")["stage"] == "checking"
    assert build_chain.resume("g1") is True
    assert threads == [(build_chain._post_finalize, ("g1", bid))]


@pytest.mark.parametrize("status", ["running", "failed", "succeeded", "held"])
def test_only_a_paused_game_can_be_resumed(tmp_runs, events, staged, status):
    _game(status=status)
    assert build_chain.resume("g1") is False
    assert games.game("g1")["status"] == status


def test_the_reaper_re_drives_a_running_build_with_no_job_and_never_a_paused_one(tmp_runs, events,
                                                                                 staged):
    _game("g1")
    _game("g2")
    assert sorted(games.stuck_builds(0)) == ["g1", "g2"]
    build_chain.pause("g2")
    assert games.stuck_builds(0) == ["g1"]


def _user(handle, role="user"):
    from auth import store
    u = store.create_user(handle, "pw-pass1234", role=role, email=f"{handle}@example.com")
    return u, {"Authorization": f"Bearer {store.issue_token(u.id)}"}


def _owned(owner_id, *, status="running"):
    games.create_game("g1", owner_id)
    games.charge_game("g1", 1, 1_000_000)
    RunState("g1").write_spec({"ask": "x", "title": "x"})
    bid = games.create_build("g1", kind="design")
    games.build_started(bid)
    job = jobs.enqueue_job("llm", {"messages": []}, game_id="g1", build_id=bid,
                           metadata={"stage": "design", "run_id": "g1"})
    games.move("g1", status, "designing")
    return job


@pytest.mark.parametrize("who, status, code", [
    ("owner", "running", 200),
    ("owner", "succeeded", 409),
    ("stranger", "running", 403),
])
def test_the_pause_endpoint(tmp_runs, app_client, events, who, status, code):
    owner, owner_hdr = _user("alice")
    _, stranger_hdr = _user("bob")
    job = _owned(owner.id, status=status)
    r = app_client.post("/api/games/g1/pause",
                        headers=owner_hdr if who == "owner" else stranger_hdr)
    assert r.status_code == code
    assert (jobs.get_job(job)["status"] == "failed") is (code == 200)


@pytest.mark.parametrize("who, game, status, code", [
    ("admin", "g1", "running", 200),
    ("admin", "g1", "failed", 409),
    ("admin", "nope", "running", 404),
    ("owner", "g1", "running", 403),
])
def test_the_admin_pause_reaches_any_run_and_only_admins_reach_it(tmp_runs, app_client, events,
                                                                  who, game, status, code):
    owner, owner_hdr = _user("alice")
    _, admin_hdr = _user("root", role="admin")
    job = _owned(owner.id, status=status)
    r = app_client.post(f"/api/admin/games/{game}/pause",
                        headers=admin_hdr if who == "admin" else owner_hdr)
    assert r.status_code == code
    if code == 200:
        assert r.json()["owner"] == owner.id
        assert jobs.get_job(job)["status"] == "failed"
        assert games.game("g1")["status"] == "paused"


def test_a_held_game_cannot_be_resumed_or_rebuilt(tmp_runs, app_client, events):
    owner, hdr = _user("alice")
    _owned(owner.id, status="held")
    RunState("g1").write_spec({"ask": "x", "title": "x", "request": "x"})
    for path in ("resume", "build"):
        assert app_client.post(f"/api/games/g1/{path}", headers=hdr).status_code == 423
    assert games.game("g1")["status"] == "held"
