from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS candidate_profiles (
    id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, email TEXT, phone TEXT, location TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY, profile_id INTEGER NOT NULL REFERENCES candidate_profiles(id),
    fact_type TEXT NOT NULL, value TEXT NOT NULL, evidence TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1, last_synced_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES job_sources(id), external_id TEXT,
    title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, url TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'DISCOVERED',
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_snapshots (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), raw_payload TEXT,
    normalized_json TEXT NOT NULL, captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS screenings (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), eligibility_passed INTEGER NOT NULL,
    hard_rule_results_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS match_assessments (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), score INTEGER NOT NULL,
    recommendation TEXT NOT NULL, score_json TEXT NOT NULL, extraction_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id), state TEXT NOT NULL,
    approved_at TEXT, applied_at TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, approved_artifact_ids_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS user_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS application_artifacts (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id), kind TEXT NOT NULL,
    content TEXT NOT NULL, source_fact_ids_json TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id), from_state TEXT,
    to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY, entity_type TEXT NOT NULL, entity_id INTEGER, event_type TEXT NOT NULL,
    actor TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_events_application ON application_events(application_id, created_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)")
    # Versioned, transactional forward migrations. Existing V1 databases gain fact lifecycle support.
    if 2 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profile_facts)")}
        if "active" not in columns:
            connection.execute("ALTER TABLE profile_facts ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
    if 3 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(applications)")}
        if "approved_artifact_ids_json" not in columns:
            connection.execute("ALTER TABLE applications ADD COLUMN approved_artifact_ids_json TEXT NOT NULL DEFAULT '[]'")
        connection.execute("CREATE TABLE IF NOT EXISTS user_settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
    connection.commit()
