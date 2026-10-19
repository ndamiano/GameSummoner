"""The games API against the platform db: list/detail read db rows, ownership gates cross-user
access, and the build endpoint charges once — credits deducted, compute granted, re-enqueue free."""

import json

import pytest

from auth import store
from billing import ledger
from billing.estimates import QUEUE_MICRO_ESTIMATES
from billing.packages import MICROS_PER_CREDIT
from db import connection, events, games, jobs, workers
from maestro.codegen import build_chain, design
from maestro.codegen.run import create_run
from maestro.codegen.staging import game_dir
from maestro.state import RunState


@pytest.fixture
def client(app_client, tmp_runs):
    return app_client


def _burn(run_id, micros):
    with connection.platform_db() as conn:
        conn.execute("UPDATE games SET spent_micros = spent_micros + ? WHERE id = ?",
                     (micros, run_id))


def _user(handle="alice", credits=10):
    u = store.create_user(handle, "pw-pass1234", email=f"{handle}@example.com")
    if credits:
        ledger.grant(u.id, credits, "admin_grant")
    return u, {"Authorization": f"Bearer {store.issue_token(u.id)}"}


def _make_game(user_id, spec):
    run_id = create_run(user_id)
    RunState(run_id).write_spec(spec)
    games.update_prompt_meta(run_id, spec.get("title", ""))
    return run_id


def test_list_and_detail_come_from_the_db(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})

    (row,) = client.get("/api/games", headers=headers).json()
    assert (row["run_id"], row["title"]) == (run_id, "Moon Miner")

    detail = client.get(f"/api/games/{run_id}", headers=headers).json()
    assert (detail["title"], detail["prompt"]) == ("Moon Miner", "make a moon miner")
    assert detail["credits_spent"] == 0
    assert detail["budget_pct_remaining"] is None   # uncharged → no bar


def test_new_game_creates_the_run_and_starts_its_design(client, monkeypatch):
    """Create opens the ask and enqueues the DESIGN — the build starts from the design's own
    completion, so at this point the run is charged but nothing is authored yet."""
    user, headers = _user()
    started, enqueued = [], []
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: started.append(rid) or "bid")
    monkeypatch.setattr(design.jobs, "enqueue_job",
                        lambda q, p, **kw: enqueued.append(kw.get("metadata")) or "job1")

    r = client.post("/api/games", headers=headers,
                    json={"prompt": "an open world RPG with card combat"})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    assert r.json()["status"] == "designing" and started == []
    spec = RunState(run_id).read_spec()
    assert spec["ask"] == "an open world RPG with card combat" and "request" not in spec
    assert enqueued == [{"stage": "design", "run_id": run_id, "step": "decide"}]
    assert games.owner_of(run_id) == user.id
    assert ledger.balance(user.id) == 9
    assert games.game(run_id)["granted_micros"] == MICROS_PER_CREDIT


def test_a_game_still_designing_reports_no_prompt_and_refuses_to_build(client, monkeypatch):
    """`prompt: null` is the page's only signal for the designing state, and a build sent before
    the design lands has nothing to send."""
    _, headers = _user()
    monkeypatch.setattr(design.jobs, "enqueue_job", lambda *a, **kw: "job1")

    run_id = client.post("/api/games", headers=headers,
                         json={"prompt": "a maze game"}).json()["run_id"]

    detail = client.get(f"/api/games/{run_id}", headers=headers).json()
    assert detail["prompt"] is None and detail["ask"] == "a maze game"
    assert client.post(f"/api/games/{run_id}/build", headers=headers,
                       json={}).status_code == 409


def test_new_game_with_no_credits_is_402_and_creates_nothing(client, monkeypatch):
    """The balance is checked BEFORE the run exists, so a broke user leaves no orphan run."""
    user, headers = _user(credits=0)
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: "bid")

    r = client.post("/api/games", headers=headers, json={"prompt": "a maze game"})
    assert r.status_code == 402
    assert client.get("/api/games", headers=headers).json() == []


def test_new_game_with_an_empty_prompt_is_400(client):
    _, headers = _user()
    assert client.post("/api/games", headers=headers,
                       json={"prompt": "   "}).status_code == 400
    assert client.get("/api/games", headers=headers).json() == []


def test_build_stores_the_edited_prompt_it_was_given(client, monkeypatch):
    """The build call is the only writer of the edited design."""
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "a maze game", "title": "Maze"})
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: "bid")

    r = client.post(f"/api/games/{run_id}/build", headers=headers,
                    json={"prompt": "a maze game with a boss fight"})
    assert r.status_code == 200
    assert RunState(run_id).read_spec()["request"] == "a maze game with a boss fight"
    assert client.get(f"/api/games/{run_id}", headers=headers).json()["prompt"] == \
        "a maze game with a boss fight"


def test_build_without_a_prompt_keeps_what_is_on_disk(client, monkeypatch):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "a maze game", "title": "Maze"})
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: "bid")

    assert client.post(f"/api/games/{run_id}/build", headers=headers).status_code == 200
    assert RunState(run_id).read_spec()["request"] == "a maze game"


def test_building_an_empty_prompt_is_400_and_changes_nothing(client, monkeypatch):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "a maze game", "title": "Maze"})
    started = []
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: started.append(rid))

    assert client.post(f"/api/games/{run_id}/build", headers=headers,
                       json={"prompt": "  "}).status_code == 400
    assert RunState(run_id).read_spec()["request"] == "a maze game"
    assert started == []
    assert ledger.balance(user.id) == 10


def test_cross_user_access_is_403(client):
    user, _ = _user("alice")
    _, other_headers = _user("bob")
    run_id = _make_game(user.id, {"request": "make a mine", "title": "Mine"})
    assert client.get(f"/api/games/{run_id}", headers=other_headers).status_code == 403
    assert client.get("/api/games", headers=other_headers).json() == []


def test_build_charges_once_and_grants_seconds(client, monkeypatch):

    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})

    # Kick off without running a real build (no worker to drive completions in tests).
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: "bid")

    r = client.post(f"/api/games/{run_id}/build", headers=headers)
    assert r.status_code == 200
    assert ledger.balance(user.id) == 9
    row = games.game(run_id)
    assert row["credits_spent"] == 1
    assert row["granted_micros"] == MICROS_PER_CREDIT

    # Second enqueue: already charged — no second deduction, no second grant.
    client.post(f"/api/games/{run_id}/build", headers=headers)
    assert ledger.balance(user.id) == 9
    assert games.game(run_id)["granted_micros"] == MICROS_PER_CREDIT


def test_build_with_no_credits_is_402_and_uncharged(client, monkeypatch):
    user, headers = _user(credits=0)
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})

    r = client.post(f"/api/games/{run_id}/build", headers=headers)
    assert r.status_code == 402
    assert not games.is_charged(run_id)


def test_a_game_out_of_compute_is_402_on_every_gpu_endpoint(client, monkeypatch):
    """Credits buy a grant ONCE; the grant is what each later build/change/skin spends. A game that
    has burned it must be refused before it takes the GPU slot, not after."""
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    monkeypatch.setattr(build_chain, "kickoff", lambda rid, **kw: "bid")

    assert client.post(f"/api/games/{run_id}/build", headers=headers).status_code == 200
    _burn(run_id, MICROS_PER_CREDIT)

    for path, body in (("build", None), ("change", {"note": "a"}), ("assets", None), ("resume", None)):
        r = client.post(f"/api/games/{run_id}/{path}", headers=headers, json=body)
        assert r.status_code == 402, path
        assert r.json()["detail"]["reason"] == "compute_exhausted"

    # Still charged — a refusal is not a refund, and credits are not re-deducted on retry.
    assert ledger.balance(user.id) == 9
    assert games.game(run_id)["credits_spent"] == 1


def test_detail_reports_remaining_net_of_queued_work(client):
    """The budget bar nets out queued work immediately: spent_micros lags by the whole depth of the
    queue, so a bar drawn from spend alone would read full while a build's jobs are already spoken
    for. budget_pct_remaining is computed from compute_remaining, which reserves the estimate."""
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    games.charge_game(run_id, 1, 1_000_000)
    jobs.enqueue_job("comfy", {}, game_id=run_id, build_id="b1")

    detail = client.get(f"/api/games/{run_id}", headers=headers).json()
    assert detail["budget_pct_remaining"] == (1_000_000 - QUEUE_MICRO_ESTIMATES["comfy"]) / 1_000_000


def test_events_endpoint_replays_the_log(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    events.record_event(run_id, "prompt_proposed", {"title": "Moon Miner"})
    events.record_event(run_id, "build_step", {"step": 1})

    body = client.get(f"/api/games/{run_id}/events", headers=headers).json()
    assert [e["kind"] for e in body] == ["prompt_proposed", "build_step"]
    after = body[0]["id"]
    later = client.get(f"/api/games/{run_id}/events?after={after}", headers=headers).json()
    assert [e["kind"] for e in later] == ["build_step"]


def _write_assets(run_id, manifest, files=()):
    gd = game_dir(RunState(run_id).run_dir)
    (gd / "assets").mkdir(parents=True, exist_ok=True)
    (gd / "assets.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name, data in files:
        (gd / "assets" / name).write_bytes(data)


def test_assets_empty_before_the_game_declares_any(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    assert client.get(f"/api/games/{run_id}/assets", headers=headers).json() == []


def test_assets_report_per_asset_status(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    # hero.webp is on disk (rendered), slime is planned but not yet rendered → pending.
    _write_assets(run_id,
                  {"images": [{"id": "hero", "file": "assets/hero.webp", "prompt": "a hero"},
                              {"id": "slime", "file": "assets/slime.webp", "prompt": "a slime"}]},
                  files=[("hero.webp", b"WEBPDATA")])

    assets = client.get(f"/api/games/{run_id}/assets", headers=headers).json()
    by_id = {a["id"]: a for a in assets}
    assert by_id["hero"] == {"id": "hero", "kind": "sprite", "status": "ready",
                             "prompt": "a hero", "defect": None}
    assert by_id["slime"]["status"] == "pending"


def test_asset_blob_streams_and_guards(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    _write_assets(run_id, {"images": [{"id": "hero", "file": "assets/hero.webp",
                                      "prompt": "a hero"}]},
                  files=[("hero.webp", b"WEBPDATA")])

    ok = client.get(f"/api/games/{run_id}/assets/hero", headers=headers)
    assert ok.status_code == 200 and ok.content == b"WEBPDATA"
    assert ok.headers["content-type"] == "image/webp"

    assert client.get(f"/api/games/{run_id}/assets/nope", headers=headers).status_code == 404
    assert client.get(f"/api/games/{run_id}/assets/bad!id", headers=headers).status_code == 400

    _, other = _user("bob")
    assert client.get(f"/api/games/{run_id}/assets/hero", headers=other).status_code == 403
    assert client.get(f"/api/games/{run_id}/assets", headers=other).status_code == 403


def test_regenerate_enqueues_one_image_job_with_the_new_prompt(client, monkeypatch):
    """A single-asset regen puts ONE image job on the queue carrying the merged prompt (in the
    comfy workflow) + the asset_id/then in metadata — the batch's finalize saves and re-stages it.
    No whole-game re-render."""
    import maestro.codegen.assets as assets_mod

    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    _write_assets(run_id, {"images": [{"id": "hero", "file": "assets/hero.webp",
                                      "prompt": "a hero"}]})
    games.charge_game(run_id, 1, 10_000_000)
    # The merge is an LLM call; the note-vs-original contract is tested on _merge_prompt itself.
    monkeypatch.setattr(assets_mod, "_merge_prompt",
                        lambda original, note: "a brave knight, pixel art")

    r = client.post(f"/api/games/{run_id}/assets/hero/regenerate", headers=headers,
                    json={"prompt": "make him a knight"})
    assert r.status_code == 200
    assert r.json() == {"status": "regenerating", "run_id": run_id, "asset_id": "hero"}

    workers.worker_created("w1", None, "comfy", None, 0.99)
    job = jobs.claim_job("comfy", "w1", 60)
    assert job is not None and job["game_id"] == run_id and job["batch_id"]
    assert [b["kind"] for b in games.builds_for(run_id) if b["id"] == job["build_id"]] == ["regen"]
    # the merged prompt reaches the positive behind the item quality-tag prefix
    assert job["payload"]["workflow"]["p"]["inputs"]["text"].endswith("a brave knight, pixel art")
    meta = json.loads(job["metadata"])
    assert meta["asset_id"] == "hero"
    assert meta["then"] == {"operations": ["save_sprite"], "finalize": "art_build"}


def test_regenerate_merge_is_attributed_to_the_game(client, monkeypatch):
    """The prompt merge is a real GPU job, and the only enqueue here that goes through the blocking
    connector — which reads the owning game off the run scope. Outside that scope it lands with no
    game_id, so it is neither metered nor gated by the budget it is spending."""
    import maestro.codegen.assets as assets_mod
    from tools.execution_context import get_run_id

    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    _write_assets(run_id, {"images": [{"id": "hero", "file": "assets/hero.webp",
                                      "prompt": "a hero"}]})
    games.charge_game(run_id, 1, 10_000_000)
    seen = {}

    def _merge(original, note):
        seen["run_id"] = get_run_id()
        return "merged"

    monkeypatch.setattr(assets_mod, "_merge_prompt", _merge)

    r = client.post(f"/api/games/{run_id}/assets/hero/regenerate", headers=headers,
                    json={"prompt": "make him a knight"})
    assert r.status_code == 200
    assert seen["run_id"] == run_id


def test_regenerate_cross_user_is_403(client):
    user, _ = _user("alice")
    _, other = _user("bob")
    run_id = _make_game(user.id, {"request": "make a mine", "title": "Mine"})
    games.charge_game(run_id, 1, 10_000_000)
    r = client.post(f"/api/games/{run_id}/assets/hero/regenerate", headers=other,
                    json={"prompt": "x"})
    assert r.status_code == 403


def test_regenerate_bad_asset_id_is_400(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    games.charge_game(run_id, 1, 10_000_000)
    r = client.post(f"/api/games/{run_id}/assets/bad!id/regenerate", headers=headers,
                    json={"prompt": "x"})
    assert r.status_code == 400


def test_regenerate_out_of_compute_is_402(client):
    user, headers = _user()
    run_id = _make_game(user.id, {"request": "make a moon miner", "title": "Moon Miner"})
    games.charge_game(run_id, 1, MICROS_PER_CREDIT)
    _burn(run_id, MICROS_PER_CREDIT)
    r = client.post(f"/api/games/{run_id}/assets/hero/regenerate", headers=headers,
                    json={"prompt": "x"})
    assert r.status_code == 402
    assert r.json()["detail"]["reason"] == "compute_exhausted"
