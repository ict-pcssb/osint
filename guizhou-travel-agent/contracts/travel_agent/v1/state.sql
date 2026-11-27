PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    owner_id TEXT PRIMARY KEY NOT NULL,
    username TEXT NOT NULL UNIQUE,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT NOT NULL REFERENCES accounts (owner_id),
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    owner_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts (owner_id),
    fields_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consents (
    owner_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts (owner_id),
    authorized INTEGER NOT NULL CHECK (authorized IN (0,1)),
    enabled INTEGER NOT NULL CHECK (enabled IN (0,1)),
    fields_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trips (
    trip_id TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT REFERENCES accounts (owner_id)
);

CREATE TABLE IF NOT EXISTS trip_versions (
    trip_id TEXT NOT NULL REFERENCES trips (trip_id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision)
);

CREATE TABLE IF NOT EXISTS save_keys (
    owner_id TEXT NOT NULL REFERENCES accounts (owner_id),
    request_key TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (owner_id, request_key),
    FOREIGN KEY (trip_id, revision) REFERENCES trip_versions (trip_id, revision)
);


CREATE TABLE IF NOT EXISTS recheck_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    owner_id TEXT NOT NULL REFERENCES accounts (owner_id),
    trip_id TEXT NOT NULL,
    base_revision INTEGER NOT NULL,
    fact_key TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running','done')),
    created_at TEXT NOT NULL,
    reusable_until TEXT NOT NULL,
    decision_key TEXT,
    declined INTEGER NOT NULL DEFAULT 0 CHECK (declined IN (0,1)),
    result_json TEXT,
    FOREIGN KEY (trip_id, base_revision) REFERENCES trip_versions (trip_id, revision)
);
CREATE INDEX IF NOT EXISTS recheck_run_scope
ON recheck_runs (owner_id, trip_id, base_revision, fact_key, reusable_until);
