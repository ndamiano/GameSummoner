import io
import tarfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools import s3
from maestro.codegen import archive


# ---------------------------------------------------------------- SigV4 against AWS's own vector

def test_sigv4_matches_the_published_aws_example():
    """The GET example from AWS's SigV4 documentation (examplebucket/test.txt, 2013-05-24) — the
    signature is printed in the docs, so a passing test means the whole chain (canonical request,
    string to sign, derived key) is byte-for-byte the spec's."""
    headers = s3.sign_headers(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        uri="/test.txt",
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        payload_hash=s3.EMPTY_SHA256,
        amz_date="20130524T000000Z",
        extra_headers={"range": "bytes=0-9"},
    )
    assert headers["Authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in headers["Authorization"]


def test_empty_payload_hash_is_the_sha256_of_nothing():
    assert s3.EMPTY_SHA256 == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


# ---------------------------------------------------------------- archive / evict / rehydrate

class FakeBucket:
    def __init__(self):
        self.objects = {}

    def put(self, key, data):
        self.objects[key] = data

    def get(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None


@pytest.fixture
def bucket(tmp_path, monkeypatch):
    fake = FakeBucket()
    monkeypatch.setattr(s3, "configured", lambda: True)
    monkeypatch.setattr(s3, "put", fake.put)
    monkeypatch.setattr(s3, "get", fake.get)
    monkeypatch.setattr(s3, "head", fake.head)

    runs = tmp_path / "runs"
    runtime_games = tmp_path / "runtime" / "games"
    runs.mkdir(parents=True)
    runtime_games.mkdir(parents=True)

    class FakeRS:
        def __init__(self, run_id):
            self.run_dir = runs / run_id
            self.run_dir.mkdir(parents=True, exist_ok=True)

    import maestro.state
    import maestro.codegen.staging as staging
    monkeypatch.setattr(maestro.state, "RunState", FakeRS)
    monkeypatch.setattr(staging, "RUNTIME_DIR", tmp_path / "runtime")
    return fake, runs, runtime_games


def _make_run(runs, run_id="r1"):
    game = runs / run_id / "game"
    game.mkdir(parents=True)
    (game / "index.html").write_text("<html>the game</html>")
    (game / "game.js").write_text("// authored")
    return runs / run_id


def test_archive_uploads_the_whole_run_dir(bucket):
    fake, runs, _ = bucket
    _make_run(runs)
    assert archive.archive("r1") is True
    with tarfile.open(fileobj=io.BytesIO(fake.objects["runs/r1.tar.gz"])) as tar:
        names = tar.getnames()
    assert "r1/game/index.html" in names


def test_archive_leaves_a_worlds_build_folder_behind(bucket):
    """The world a game plays was published into the game folder; the stages' working material is
    hundreds of megabytes nothing reads back."""
    fake, runs, _ = bucket
    run_dir = _make_run(runs)
    (run_dir / "world_build").mkdir()
    (run_dir / "world_build" / "concept.png").write_bytes(b"x" * 64)
    (run_dir / "game" / "world").mkdir()
    (run_dir / "game" / "world" / "world.json").write_text("{}")
    archive.archive("r1")
    with tarfile.open(fileobj=io.BytesIO(fake.objects["runs/r1.tar.gz"])) as tar:
        names = tar.getnames()
    assert "r1/game/world/world.json" in names
    assert not [n for n in names if "world_build" in n]


def test_evict_removes_local_copies_only_after_verifying_remote(bucket):
    fake, runs, runtime_games = bucket
    run_dir = _make_run(runs)
    (runtime_games / "r1").mkdir()
    (runtime_games / "r1" / "index.html").write_text("staged")
    archive.archive("r1")
    archive.evict("r1")
    assert not (run_dir / "game").exists()
    assert not (runtime_games / "r1").exists()


def test_evict_without_archive_archives_first(bucket):
    fake, runs, _ = bucket
    _make_run(runs)
    archive.evict("r1")
    assert "runs/r1.tar.gz" in fake.objects


def test_evict_refuses_when_upload_cannot_verify(bucket, monkeypatch):
    fake, runs, _ = bucket
    run_dir = _make_run(runs)
    monkeypatch.setattr(s3, "put", lambda k, d: None)     # upload silently loses the bytes
    with pytest.raises(RuntimeError, match="refusing to delete"):
        archive.evict("r1")
    assert (run_dir / "game").exists()


def test_evict_refuses_a_running_game_and_takes_a_paused_one(bucket):
    from db import games
    fake, runs, _ = bucket
    _make_run(runs)
    games.create_game("r1", "u1")
    with pytest.raises(RuntimeError, match="in flight"):
        archive.evict("r1")
    assert (runs / "r1" / "game").exists()
    games.move("r1", "paused", "building")
    archive.evict("r1")
    assert not (runs / "r1" / "game").exists()


def test_rehydrate_round_trips_and_restages(bucket):
    fake, runs, runtime_games = bucket
    run_dir = _make_run(runs)
    archive.archive("r1")
    archive.evict("r1")

    assert archive.rehydrate("r1") is True
    assert (run_dir / "game" / "index.html").read_text() == "<html>the game</html>"
    assert (runtime_games / "r1" / "index.html").exists()


def test_rehydrate_on_a_local_run_is_a_noop_true(bucket):
    fake, runs, _ = bucket
    _make_run(runs)
    assert archive.rehydrate("r1") is True


def test_rehydrate_with_no_archive_is_false(bucket):
    fake, runs, _ = bucket
    assert archive.rehydrate("ghost") is False


def test_archive_missing_backfills_only_the_absent(bucket, monkeypatch):
    fake, runs, _ = bucket
    _make_run(runs, "r1")
    _make_run(runs, "r2")
    (runs / "r3").mkdir()                          # no game folder — never archived
    archive.archive("r1")
    uploaded_before = dict(fake.objects)

    import tools.execution_context as ctx
    monkeypatch.setattr(ctx, "resolve_base_path", lambda: runs.parent)
    assert archive.archive_missing() == 1          # only r2
    assert "runs/r2.tar.gz" in fake.objects
    assert fake.objects["runs/r1.tar.gz"] == uploaded_before["runs/r1.tar.gz"]


def test_the_key_prefix_separates_boxes(monkeypatch):
    seen = {}
    monkeypatch.setattr(s3, "_cfg", lambda: {"endpoint": "s3.example.com", "region": "r",
                                             "bucket": "b", "access_key": "a", "secret_key": "s",
                                             "prefix": "dev/"})

    class Resp:
        status_code = 200
        headers = {"Content-Length": "1"}
        content = b"x"
        text = ""

    def fake_request(method, url, headers=None, data=None, timeout=None):
        seen["url"] = url
        return Resp()

    monkeypatch.setattr(s3.requests, "request", fake_request)
    s3.head("db/platform/latest.db.gz")
    assert "/b/dev/db/platform/latest.db.gz" in seen["url"]
