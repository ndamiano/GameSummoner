"""Shared games: who can share, what a link opens, and what never goes public."""

import shutil

import pytest

import api.app
from auth import playgrants, store
from maestro import shares
from maestro.codegen.run import create_run
from maestro.codegen.staging import stage_for_play
from maestro.state import RunState

ASK = "a secret game about my coworker dave"


@pytest.fixture(autouse=True)
def clean_grants():
    playgrants.clear()
    yield
    playgrants.clear()


@pytest.fixture
def client(app_client, tmp_runs):
    return app_client


@pytest.fixture
def no_thumbs(monkeypatch):
    monkeypatch.setattr(shares, "_capture_thumb", lambda game: None)


@pytest.fixture
def cleanup():
    made = []
    yield made
    for run_id in made:
        shutil.rmtree(api.app._runtime / "games" / run_id, ignore_errors=True)
    for d in shares.SHARES_DIR.glob("*") if shares.SHARES_DIR.is_dir() else []:
        if (d / shares.META).is_file() and shares.meta(d.name).get("run_id") in made:
            shutil.rmtree(d, ignore_errors=True)


def _owner(handle):
    u = store.create_user(handle, "pw-pass1234", email=f"{handle}@example.com")
    return u, {"Authorization": f"Bearer {store.issue_token(u.id)}"}


def _built_game(user, cleanup, body="<head><title>Fox Run</title></head><body>v1</body>"):
    run_id = create_run(user.id)
    rs = RunState(run_id)
    rs.write_spec({"ask": ASK, "title": "Fox", "request": "Build this fox game."})
    gd = rs.run_dir / "game"
    gd.mkdir()
    (gd / "index.html").write_text(body, encoding="utf-8")
    stage_for_play(rs.run_dir, run_id)
    cleanup.append(run_id)
    return run_id


def _restage(run_id, body):
    rs = RunState(run_id)
    (rs.run_dir / "game" / "index.html").write_text(body, encoding="utf-8")
    stage_for_play(rs.run_dir, run_id)


def _play(client, share_id):
    r = client.post(f"/api/shares/{share_id}/play-session")
    assert r.status_code == 200
    r2 = client.get(r.json()["url"], follow_redirects=False)
    assert r2.headers["location"] == f"/play/shares/{share_id}/index.html"
    return r2.headers["set-cookie"].split("maestro_play=", 1)[1].split(";", 1)[0]


def test_a_shared_game_plays_for_anyone_and_never_shows_the_ask(client, cleanup, no_thumbs):
    owner, auth = _owner("alice")
    run_id = _built_game(owner, cleanup)

    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]
    assert sid != run_id and shares.SHARE_ID.match(sid)
    assert client.get(f"/api/games/{run_id}", headers=auth).json()["share_id"] == sid

    public = client.get(f"/api/shares/{sid}")
    assert public.json() == {"share_id": sid, "title": "Fox Run", "thumb_url": None}
    grant = _play(client, sid)
    page = client.get(f"/play/shares/{sid}/index.html", cookies={"maestro_play": grant})
    assert "v1" in page.text
    for text in (public.text, page.text, (shares.share_dir(sid) / shares.META).read_text()):
        assert "dave" not in text and "Build this fox game" not in text
    assert client.get(f"/play/games/{run_id}/index.html",
                      cookies={"maestro_play": grant}).status_code == 403


def test_a_share_is_frozen_until_shared_again_under_the_same_link(client, cleanup, no_thumbs):
    owner, auth = _owner("bob")
    run_id = _built_game(owner, cleanup)
    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]

    _restage(run_id, "<head><title>Fox Run II</title></head><body>v2</body>")
    grant = _play(client, sid)
    assert "v1" in client.get(f"/play/shares/{sid}/index.html", cookies={"maestro_play": grant}).text

    assert client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"] == sid
    assert client.get(f"/api/shares/{sid}").json()["title"] == "Fox Run II"
    grant = _play(client, sid)
    assert "v2" in client.get(f"/play/shares/{sid}/index.html", cookies={"maestro_play": grant}).text


def test_unsharing_or_a_hold_takes_the_link_down(client, cleanup, no_thumbs):
    owner, auth = _owner("carol")
    run_id = _built_game(owner, cleanup)
    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]

    assert client.delete(f"/api/games/{run_id}/share", headers=auth).json() == {"share_id": None}
    assert client.get(f"/api/shares/{sid}").status_code == 404
    assert client.post(f"/api/shares/{sid}/play-session").status_code == 404
    assert client.get(f"/api/games/{run_id}", headers=auth).json()["share_id"] is None

    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]
    from db import games
    games.set_status(run_id, "held")
    assert client.post(f"/api/games/{run_id}/share", headers=auth).status_code == 423
    shares.unshare(run_id)
    assert client.get(f"/api/shares/{sid}").status_code == 404


def test_only_the_owner_can_share_and_only_a_built_game(client, cleanup, no_thumbs):
    owner, auth = _owner("dana")
    _, other_auth = _owner("eve")
    run_id = _built_game(owner, cleanup)
    assert client.post(f"/api/games/{run_id}/share", headers=other_auth).status_code == 403
    assert client.post(f"/api/games/{run_id}/share").status_code == 401

    unbuilt = create_run(owner.id)
    RunState(unbuilt).write_spec({"ask": ASK, "request": "x"})
    assert client.post(f"/api/games/{unbuilt}/share", headers=auth).status_code == 409
    assert client.get("/api/shares/not-a-real-share-id").status_code == 404
    assert client.get("/api/shares/..%2F..%2Fgames").status_code == 404


def test_a_share_carries_a_screenshot_of_the_game_running(client, cleanup):
    owner, auth = _owner("finn")
    run_id = _built_game(owner, cleanup, body=(
        "<head><title>Red</title></head><body style='margin:0;background:#f00'>"
        "<canvas></canvas></body>"))
    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]

    assert client.get(f"/api/shares/{sid}").json()["thumb_url"] == f"/api/shares/{sid}/thumb"
    thumb = client.get(f"/api/shares/{sid}/thumb")
    assert thumb.headers["content-type"] == "image/png"
    assert thumb.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_shared_link_unfurls_as_that_game(client, cleanup, no_thumbs):
    owner, auth = _owner("gail")
    run_id = _built_game(owner, cleanup, body='<head><title>Fox "&" Hound</title></head><body></body>')
    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]
    (shares.share_dir(sid) / shares.THUMB).write_bytes(b"png")
    shell = ('<head>\n    <meta property="og:title" content="GameSummoner — describe a game, play it" />\n'
             '    <meta property="og:url" content="https://gamesummoner.com/" />\n'
             '    <meta property="og:description" content="Describe a game." />\n  </head>')

    card = api.app.share_card(shell, sid, "https://gamesummoner.com/")

    assert "describe a game, play it" not in card
    assert 'content="Fox &quot;&amp;&quot; Hound — made with GameSummoner"' in card
    assert f'og:url" content="https://gamesummoner.com/g/{sid}"' in card
    assert f'og:image" content="https://gamesummoner.com/api/shares/{sid}/thumb"' in card
    assert 'og:description" content="Describe a game."' in card


def test_share_views_and_plays_rank_the_hot_list_with_their_referrers(client, cleanup, no_thumbs):
    owner, auth = _owner("carol")
    hot = _built_game(owner, cleanup)
    cold = _built_game(owner, cleanup)
    hot_sid = client.post(f"/api/games/{hot}/share", headers=auth).json()["share_id"]
    cold_sid = client.post(f"/api/games/{cold}/share", headers=auth).json()["share_id"]

    for ref in ("https://www.reddit.com/r/gamedev/x?y=1", "https://www.reddit.com/", None):
        assert client.post(f"/api/shares/{hot_sid}/view", json={"referrer": ref}).status_code == 200
    _play(client, hot_sid)
    client.post(f"/api/shares/{cold_sid}/view", json={"referrer": "https://www.linkedin.com/feed"})
    client.get(f"/g/{cold_sid}")
    assert client.post("/api/shares/nosuchshare1/view", json={}).status_code == 404

    admin = store.create_user("root", "pw-pass1234", role="admin", email="root@example.com")
    body = client.get("/api/admin/analytics",
                      headers={"Authorization": f"Bearer {store.issue_token(admin.id)}"}).json()
    assert [(s["run_id"], s["share_id"], s["owner"], s["views"], s["plays"], s["referrers"])
            for s in body["shares"]] == [
        (hot, hot_sid, "carol", 3, 1, [["www.reddit.com", 2], ["direct", 1]]),
        (cold, cold_sid, "carol", 1, 0, [["www.linkedin.com", 1]]),
    ]
    assert (body["totals"]["share_views"], body["totals"]["share_plays"]) == (4, 1)

    client.delete(f"/api/games/{hot}/share", headers=auth)
    body = client.get("/api/admin/analytics",
                      headers={"Authorization": f"Bearer {store.issue_token(admin.id)}"}).json()
    assert body["shares"][0]["run_id"] == hot and body["shares"][0]["share_id"] is None


def test_a_title_with_entities_reads_decoded_on_the_share_and_the_game(client, cleanup, no_thumbs):
    owner, auth = _owner("dana")
    run_id = _built_game(owner, cleanup,
                         body="<head><title>Claws &amp; Cantrips: &quot;Red&quot; &lt;Dot&gt;</title></head>")
    sid = client.post(f"/api/games/{run_id}/share", headers=auth).json()["share_id"]
    want = 'Claws & Cantrips: "Red" <Dot>'
    assert client.get(f"/api/shares/{sid}").json()["title"] == want
    assert client.get(f"/api/games/{run_id}", headers=auth).json()["title"] == want
