"""A private game's llm calls stay on our own hardware: the account sets the default, the create
call may override it, and every seat resolves against the game it serves."""

import pytest

from auth import store
from billing import ledger
from config import llm_models
from config.settings_manager import SEATS, settings_manager
from db import games
from maestro.codegen import design, error_gate
from tools.execution_context import run_scope


@pytest.fixture
def two_providers(llm_entries, monkeypatch):
    llm_entries["deepseek-4.1"] = {"model": "deepseek-flash", "n_ctx": 131072, "api": {
        "base_url": "u", "usd_per_mtok": {"input": 1, "cached_input": 1, "output": 1}}}
    monkeypatch.setitem(settings_manager._settings, "seats",
                        {seat: ["deepseek-4.1", "qwen-flash-next"] for seat in SEATS})


def test_the_account_default_and_the_create_choice_decide_who_a_game_may_call(
        app_client, tmp_runs, two_providers, monkeypatch):
    monkeypatch.setattr(design.jobs, "enqueue_job", lambda *a, **kw: "job1")
    user = store.create_user("alice", "pw-pass1234", email="alice@example.com")
    ledger.grant(user.id, 10, "admin_grant")
    headers = {"Authorization": f"Bearer {store.issue_token(user.id)}"}

    def new(**choice):
        return app_client.post("/api/games", headers=headers,
                               json={"prompt": "a maze game", **choice}).json()["run_id"]

    made = [("default", new())]
    assert app_client.get("/auth/me", headers=headers).json()["private"] is True
    assert app_client.post("/auth/privacy", headers=headers,
                           json={"private": False}).json() == {"ok": True, "private": False}
    assert app_client.get("/auth/me", headers=headers).json()["private"] is False
    made += [("quality default", new()), ("private here", new(private=True)),
             ("quality here", new(private=False))]

    asked = []
    monkeypatch.setattr(error_gate, "get_connector",
                        lambda name: asked.append(name) or (_ for _ in ()).throw(OSError()))
    for _, run_id in made:
        with run_scope(run_id, None):
            error_gate._targets(b"png")
    with run_scope(None, None):
        error_gate._targets(b"png")

    assert [(label, games.private(rid)) for label, rid in made] == [
        ("default", True), ("quality default", False), ("private here", True),
        ("quality here", False)]
    assert asked == ["qwen-flash-next", "deepseek-4.1", "qwen-flash-next", "deepseek-4.1",
                     "qwen-flash-next"]
    assert [llm_models.seat("build", p) for p in (True, False)] == ["qwen-flash-next",
                                                                     "deepseek-4.1"]


@pytest.mark.parametrize("private, want", [(True, "qwen-flash-next"), (False, "deepseek-4.1")])
def test_every_seat_asks_for_the_model_its_game_may_call(tmp_runs, two_providers, monkeypatch,
                                                          private, want):
    from llm_clients import connector
    from maestro.codegen import assets, build_chain, build_state, play_gate
    from maestro.codegen.build_state import BuildCursor
    from maestro.state import RunState
    from maestro.worldgen import llm as worldgen_llm
    from config.llm_models import UnknownModel

    asked = []

    def ask(name, **kw):
        asked.append(name)
        raise UnknownModel(name)
    for module in (design, build_chain, error_gate, play_gate, connector):
        monkeypatch.setattr(module, "get_connector", ask)
    monkeypatch.setattr(worldgen_llm, "_get_connector", ask)
    monkeypatch.setattr(build_chain, "_finalize", lambda *a, **k: None)

    games.create_game("g1", "u1", private=private)
    games.move("g1", "running", "building")
    RunState("g1").write_spec({"ask": "frogs", "title": "frogs", "request": "a frog game"})
    bid = games.create_build("g1", kind="build")
    games.build_started(bid)
    build_state.save(BuildCursor(build_id=bid))

    seats = []
    for seat, call in [
        ("design plan", lambda: design._enqueue_step("g1", bid, "frogs", "plan", "# spec")),
        ("design write", lambda: design._write("g1", "frogs", "state", "")),
        ("build", lambda: build_chain._advance_locked("g1", None, None, None)),
        ("error gate", lambda: error_gate._targets(b"png")),
        ("play gate", lambda: play_gate._ask("system", [], 100)),
        ("asset prompt", lambda: assets._merge_prompt("a frog", "make it red")),
        ("worldgen", lambda: worldgen_llm.LLMHarness().send_message_with_tools("hi")),
    ]:
        before = len(asked)
        with run_scope("g1", bid):
            try:
                call()
            except UnknownModel:
                pass
        seats.append((seat, asked[before:]))

    assert seats == [(seat, [want]) for seat, _ in seats]
