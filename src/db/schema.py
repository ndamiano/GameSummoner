"""DDL for both control-plane databases."""

PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    mode            TEXT NOT NULL DEFAULT '',
    stage           TEXT NOT NULL DEFAULT 'designing',
    status          TEXT NOT NULL DEFAULT 'running',
    credits_spent   INTEGER NOT NULL DEFAULT 0,
    granted_micros  INTEGER NOT NULL DEFAULT 0,
    spent_micros    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_games_user ON games(user_id, created_at);

CREATE TABLE IF NOT EXISTS builds (
    id           TEXT PRIMARY KEY,
    game_id      TEXT NOT NULL REFERENCES games(id),
    kind         TEXT NOT NULL DEFAULT 'build',
    status       TEXT NOT NULL DEFAULT 'queued',
    steps        INTEGER,
    spent_micros INTEGER NOT NULL DEFAULT 0,
    queued_at    REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    maestro_rev  TEXT,
    cursor       TEXT
);
CREATE INDEX IF NOT EXISTS idx_builds_game ON builds(game_id, queued_at);

CREATE TABLE IF NOT EXISTS jobs (
    id               TEXT PRIMARY KEY,
    queue            TEXT NOT NULL,
    game_id          TEXT,
    build_id         TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    payload          TEXT,
    result           TEXT,
    error            TEXT,
    worker_id        TEXT,
    model            TEXT,
    gpu_type         TEXT,
    reserved_micros  INTEGER NOT NULL DEFAULT 0,
    exec_seconds     REAL,
    billed_micros    INTEGER,
    lease_expires_at REAL,
    metadata         TEXT,
    batch_id         TEXT,
    finalized_at     REAL,
    created_at       REAL NOT NULL,
    started_at       REAL,
    finished_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(queue, status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_build ON jobs(build_id);
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id, status);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    game_id    TEXT,
    user_id    TEXT,
    build_id   TEXT,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_game ON events(game_id, id);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id, created_at);

CREATE TABLE IF NOT EXISTS violations (
    id         INTEGER PRIMARY KEY,
    user_id    TEXT,
    game_id    TEXT,
    source     TEXT NOT NULL,
    category   TEXT NOT NULL,
    matched    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_violations_user ON violations(user_id, created_at);

CREATE TABLE IF NOT EXISTS workers (
    id            TEXT PRIMARY KEY,
    queue         TEXT,
    gpu_type      TEXT,
    source        TEXT,
    pod_id        TEXT,
    busy_seconds  REAL NOT NULL DEFAULT 0,
    usd_per_hour  REAL,
    started_at    REAL NOT NULL,
    registered_at REAL,
    last_seen_at  REAL,
    terminated_at REAL
);

CREATE TABLE IF NOT EXISTS pod_refusals (
    id         INTEGER PRIMARY KEY,
    queue      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    attempts   TEXT NOT NULL,
    error      TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pod_refusals_queue ON pod_refusals(queue, kind, created_at);

CREATE TABLE IF NOT EXISTS pod_request_days (
    queue           TEXT NOT NULL,
    day             TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    stock_refusals  INTEGER NOT NULL DEFAULT 0,
    other_refusals  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (queue, day)
);
"""
AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    handle        TEXT UNIQUE NOT NULL,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    credits       INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);
-- Case-insensitive: nobody remembers which case they signed up with, and two accounts
-- differing only in case would race for the same reset mail.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(lower(email));

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    used_at    REAL
);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    delta      INTEGER NOT NULL,
    reason     TEXT NOT NULL,
    run_id     TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS purchases (
    id             TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL REFERENCES users(id),
    package_id     TEXT NOT NULL,
    credits        INTEGER NOT NULL,
    usd_cents      INTEGER NOT NULL,
    provider_ref   TEXT,
    payment_intent TEXT,
    status         TEXT NOT NULL,
    created_at     REAL NOT NULL,
    completed_at   REAL
);
"""
