import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))


def pytest_unconfigure(config):
    """Exit before interpreter finalization: greenlet (via playwright's sync API) segfaults in a
    thread's TLS destructor when threads unwind during shutdown — upstream race, reproduced on
    greenlet 3.2.4/3.5.4/3.5.5, full suite only. The suite's verdict is already decided here."""
    sys.stdout.flush()
    sys.stderr.flush()
    status = getattr(config, "_maestro_exitstatus", None)
    if status is not None:
        os._exit(status)


def pytest_sessionfinish(session, exitstatus):
    session.config._maestro_exitstatus = int(exitstatus)


@pytest.fixture
def anyio_backend():
    """asyncio only — the anyio plugin would otherwise run every async test twice, on trio too."""
    return "asyncio"


@pytest.fixture
def app_client():
    """No `with` — a unit test does not want the app's startup handlers (tool registration)."""
    from starlette.testclient import TestClient

    from api.app import app
    return TestClient(app)


@pytest.fixture
def tmp_runs(tmp_path, monkeypatch):
    """Run dirs under this test's tmp dir, for anything that reads or writes runs/<id>/."""
    import maestro.state

    monkeypatch.setattr(maestro.state, "resolve_base_path", lambda input_path=None: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """600K pbkdf2 rounds is 83 ms a hash and the suite hashes ~265 times — 22 seconds of it. The
    round count is a production cost parameter, and `test_password_hashing_stays_expensive_in_production`
    reads it from the source, so lowering it here cannot hide a weakened one."""
    from auth import store as auth_store

    monkeypatch.setattr(auth_store, "_PBKDF2_ROUNDS", 1_000)


@pytest.fixture(autouse=True)
def isolated_dbs(tmp_path, monkeypatch):
    """Both datastores under this test's tmp dir. Autouse, so a test that never thought about the
    db cannot reach the real one."""
    from db import connection

    monkeypatch.setattr(connection, "platform_path", lambda: tmp_path / "platform.db")
    monkeypatch.setattr(connection, "auth_path", lambda: tmp_path / "auth.db")
    return tmp_path


@pytest.fixture(autouse=True)
def llm_entries(monkeypatch):
    from config.settings_manager import SEATS, settings_manager

    entries = {"qwen-flash-next": {"model": "qwen3.8-flash-next-basalt", "n_ctx": 32768,
                                   "max_tokens": 50000, "reasoning": "medium", "slots": 1,
                                   "local_gpu": {}}}
    monkeypatch.setitem(settings_manager._settings, "llm", entries)
    monkeypatch.setitem(settings_manager._settings, "seats",
                        {seat: ["qwen-flash-next"] for seat in SEATS})
    return entries


@pytest.fixture(autouse=True)
def no_real_bucket(monkeypatch):
    """Autouse, so a test that never thought about s3 cannot reach the real bucket: the settings
    file on a dev box has live credentials, and `ensure_local` on any absent run dir goes to the
    network — which under a parallel run answers 503 SlowDown. The bucket tests patch this back on."""
    from tools import s3

    monkeypatch.setattr(s3, "configured", lambda: False)
