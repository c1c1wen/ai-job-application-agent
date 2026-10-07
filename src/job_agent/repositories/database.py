from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 24

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
CREATE TABLE IF NOT EXISTS source_documents (id INTEGER PRIMARY KEY, filename TEXT NOT NULL, stored_path TEXT NOT NULL, mime_type TEXT NOT NULL, text_content TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS profile_fact_versions (id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL REFERENCES profile_facts(id), value TEXT NOT NULL, evidence TEXT NOT NULL, status TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS application_artifacts (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id), kind TEXT NOT NULL,
    content TEXT NOT NULL, source_fact_ids_json TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, model_used TEXT NOT NULL DEFAULT 'deterministic'
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
CREATE TABLE IF NOT EXISTS job_search_eligibility (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), eligible INTEGER NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]', config_hash TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_enrichments (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), discovery_source TEXT NOT NULL,
    original_job_url TEXT NOT NULL, enrichment_url TEXT, enrichment_source TEXT,
    description TEXT, jd_status TEXT NOT NULL, evidence_quality TEXT NOT NULL,
    retrieval_status TEXT NOT NULL, retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_execution_sessions (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
    state TEXT NOT NULL, platform TEXT NOT NULL, application_url TEXT NOT NULL,
    detected_fields_json TEXT NOT NULL DEFAULT '[]', autofilled_fields_json TEXT NOT NULL DEFAULT '[]',
    unresolved_fields_json TEXT NOT NULL DEFAULT '[]', warnings_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_execution_uploads (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id),
    artifact_id INTEGER, artifact_version INTEGER, source_document_id INTEGER, filename TEXT NOT NULL, upload_field TEXT,
    uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_execution_events (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id),
    state TEXT NOT NULL, step TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_execution_answers (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id),
    field_key TEXT NOT NULL, field_name TEXT NOT NULL, label TEXT NOT NULL, field_type TEXT NOT NULL,
    value_json TEXT NOT NULL, category TEXT NOT NULL, reusable INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(session_id, field_key)
);
CREATE TABLE IF NOT EXISTS execution_reusable_answers (
    id INTEGER PRIMARY KEY, platform TEXT NOT NULL, field_key TEXT NOT NULL, label TEXT NOT NULL,
    field_type TEXT NOT NULL, value_json TEXT NOT NULL, category TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(platform, field_key)
);
CREATE TABLE IF NOT EXISTS application_profile_values (
    field_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, category TEXT NOT NULL,
    reusable INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS canonical_cv_versions (
    id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, filename TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE, modified_ns INTEGER NOT NULL, size_bytes INTEGER NOT NULL,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
    detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_cv_versions (
    id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
    canonical_cv_id INTEGER NOT NULL REFERENCES canonical_cv_versions(id),
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
    canonical_filename TEXT NOT NULL, sha256 TEXT NOT NULL, stored_path TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_execution_diagnostics (
    id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id),
    phase TEXT NOT NULL, page_url TEXT, cv_attached INTEGER, required_complete INTEGER,
    missing_fields_json TEXT NOT NULL DEFAULT '[]', visible_errors_json TEXT NOT NULL DEFAULT '[]',
    human_verification INTEGER NOT NULL DEFAULT 0, page_reloaded INTEGER,
    replayed_answer_count INTEGER NOT NULL DEFAULT 0, execution_state TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS application_history (
    id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id), company TEXT NOT NULL, role_title TEXT NOT NULL,
    application_url TEXT, job_external_id TEXT, location TEXT, source TEXT, date_applied TEXT,
    status TEXT NOT NULL, method TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS job_duplicate_checks (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), status TEXT NOT NULL, reason TEXT NOT NULL,
    matched_history_id INTEGER REFERENCES application_history(id), checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS employer_ats_registry (
    id INTEGER PRIMARY KEY, canonical_employer TEXT NOT NULL UNIQUE, aliases_json TEXT NOT NULL DEFAULT '[]',
    careers_domain TEXT NOT NULL, ats_provider TEXT, ats_identifier TEXT, verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    verification_evidence_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS job_application_resolutions (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), discovery_source TEXT NOT NULL, discovery_url TEXT NOT NULL,
    application_source TEXT, canonical_application_url TEXT, resolution_status TEXT NOT NULL,
    resolution_confidence REAL NOT NULL DEFAULT 0, resolution_evidence_json TEXT NOT NULL DEFAULT '{}',
    resolved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ats_authentication_status (
    ats TEXT PRIMARY KEY, authentication_status TEXT NOT NULL, last_verified_at TEXT,
    last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS opportunity_evaluations (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    evaluation_json TEXT NOT NULL,
    narrative_json TEXT,
    tailoring_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    narrative_generated_at TEXT,
    profile_snapshot_hash TEXT NOT NULL DEFAULT '',
    canonical_cv_id INTEGER,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS opportunity_evaluation_history (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    revision INTEGER NOT NULL,
    evaluation_json TEXT NOT NULL,
    narrative_json TEXT,
    tailoring_json TEXT NOT NULL,
    profile_snapshot_hash TEXT NOT NULL,
    canonical_cv_id INTEGER,
    evaluated_at TEXT NOT NULL,
    narrative_generated_at TEXT,
    archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archive_reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_fact_sources (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES profile_facts(id),
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
    identity_key TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'CV',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(fact_id, source_document_id)
);
CREATE INDEX IF NOT EXISTS idx_application_cv_active ON application_cv_versions(application_id, active, id);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_events_application ON application_events(application_id, created_at);
CREATE INDEX IF NOT EXISTS idx_execution_application ON application_execution_sessions(application_id, updated_at);
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
    if 4 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(profile_facts)")}
        for name, definition in (("status", "TEXT NOT NULL DEFAULT 'APPROVED'"), ("source_document_id", "INTEGER"), ("source_snippet", "TEXT"), ("confidence", "TEXT NOT NULL DEFAULT 'high'")):
            if name not in columns:
                connection.execute(f"ALTER TABLE profile_facts ADD COLUMN {name} {definition}")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")
    if 5 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS job_search_eligibility (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), eligible INTEGER NOT NULL, reasons_json TEXT NOT NULL DEFAULT '[]', config_hash TEXT NOT NULL, evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")
    if 6 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS job_enrichments (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), discovery_source TEXT NOT NULL, original_job_url TEXT NOT NULL, enrichment_url TEXT, enrichment_source TEXT, description TEXT, jd_status TEXT NOT NULL, evidence_quality TEXT NOT NULL, retrieval_status TEXT NOT NULL, retrieved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
    if 7 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(application_artifacts)")}
        if "model_used" not in columns:
            connection.execute("ALTER TABLE application_artifacts ADD COLUMN model_used TEXT NOT NULL DEFAULT 'deterministic'")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")
    if 8 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS application_execution_sessions (id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id), state TEXT NOT NULL, platform TEXT NOT NULL, application_url TEXT NOT NULL, detected_fields_json TEXT NOT NULL DEFAULT '[]', autofilled_fields_json TEXT NOT NULL DEFAULT '[]', unresolved_fields_json TEXT NOT NULL DEFAULT '[]', warnings_json TEXT NOT NULL DEFAULT '[]', error_message TEXT, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE TABLE IF NOT EXISTS application_execution_uploads (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id), artifact_id INTEGER, artifact_version INTEGER, filename TEXT NOT NULL, upload_field TEXT, uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE TABLE IF NOT EXISTS application_execution_events (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id), state TEXT NOT NULL, step TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_execution_application ON application_execution_sessions(application_id, updated_at)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")
    if 9 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(application_execution_uploads)")}
        if "source_document_id" not in columns:
            connection.execute("ALTER TABLE application_execution_uploads ADD COLUMN source_document_id INTEGER")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)")
    if 10 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS application_execution_answers (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id), field_key TEXT NOT NULL, field_name TEXT NOT NULL, label TEXT NOT NULL, field_type TEXT NOT NULL, value_json TEXT NOT NULL, category TEXT NOT NULL, reusable INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(session_id, field_key))")
        connection.execute("CREATE TABLE IF NOT EXISTS execution_reusable_answers (id INTEGER PRIMARY KEY, platform TEXT NOT NULL, field_key TEXT NOT NULL, label TEXT NOT NULL, field_type TEXT NOT NULL, value_json TEXT NOT NULL, category TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(platform, field_key))")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (10)")
    if 11 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS application_profile_values (field_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, category TEXT NOT NULL, reusable INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (11)")
    if 12 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS canonical_cv_versions (id INTEGER PRIMARY KEY, source_path TEXT NOT NULL, filename TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, modified_ns INTEGER NOT NULL, size_bytes INTEGER NOT NULL, source_document_id INTEGER NOT NULL REFERENCES source_documents(id), detected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE TABLE IF NOT EXISTS application_cv_versions (id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id), canonical_cv_id INTEGER NOT NULL REFERENCES canonical_cv_versions(id), source_document_id INTEGER NOT NULL REFERENCES source_documents(id), canonical_filename TEXT NOT NULL, sha256 TEXT NOT NULL, stored_path TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_application_cv_active ON application_cv_versions(application_id, active, id)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (12)")
    if 13 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS application_execution_diagnostics (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES application_execution_sessions(id), phase TEXT NOT NULL, page_url TEXT, cv_attached INTEGER, required_complete INTEGER, missing_fields_json TEXT NOT NULL DEFAULT '[]', visible_errors_json TEXT NOT NULL DEFAULT '[]', human_verification INTEGER NOT NULL DEFAULT 0, page_reloaded INTEGER, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (13)")
    if 14 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(application_execution_diagnostics)")}
        if "replayed_answer_count" not in columns:
            connection.execute("ALTER TABLE application_execution_diagnostics ADD COLUMN replayed_answer_count INTEGER NOT NULL DEFAULT 0")
        if "execution_state" not in columns:
            connection.execute("ALTER TABLE application_execution_diagnostics ADD COLUMN execution_state TEXT")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (14)")
    if 15 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS application_history (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id), company TEXT NOT NULL, role_title TEXT NOT NULL, application_url TEXT, job_external_id TEXT, location TEXT, source TEXT, date_applied TEXT, status TEXT NOT NULL, method TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE TABLE IF NOT EXISTS job_duplicate_checks (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), status TEXT NOT NULL, reason TEXT NOT NULL, matched_history_id INTEGER REFERENCES application_history(id), checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (15)")
    if 16 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS employer_ats_registry (id INTEGER PRIMARY KEY, canonical_employer TEXT NOT NULL UNIQUE, aliases_json TEXT NOT NULL DEFAULT '[]', careers_domain TEXT NOT NULL, ats_provider TEXT, ats_identifier TEXT, verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, verification_evidence_json TEXT NOT NULL DEFAULT '{}')")
        connection.execute("CREATE TABLE IF NOT EXISTS job_application_resolutions (job_id INTEGER PRIMARY KEY REFERENCES jobs(id), discovery_source TEXT NOT NULL, discovery_url TEXT NOT NULL, application_source TEXT, canonical_application_url TEXT, resolution_status TEXT NOT NULL, resolution_confidence REAL NOT NULL DEFAULT 0, resolution_evidence_json TEXT NOT NULL DEFAULT '{}', resolved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (16)")
    if 17 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS ats_authentication_status (ats TEXT PRIMARY KEY, authentication_status TEXT NOT NULL, last_verified_at TEXT, last_checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (17)")
    if 18 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS discovery_runs (id INTEGER PRIMARY KEY, query_json TEXT NOT NULL, funnel_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("CREATE TABLE IF NOT EXISTS discovery_run_sources (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES discovery_runs(id), source TEXT NOT NULL, raw_count INTEGER NOT NULL DEFAULT 0, new_count INTEGER NOT NULL DEFAULT 0, previously_seen_count INTEGER NOT NULL DEFAULT 0, likely_eligible_count INTEGER NOT NULL DEFAULT 0, possible_count INTEGER NOT NULL DEFAULT 0, eligible_count INTEGER NOT NULL DEFAULT 0, good_possible_count INTEGER NOT NULL DEFAULT 0, direct_verified_count INTEGER NOT NULL DEFAULT 0, live_count INTEGER NOT NULL DEFAULT 0, error_message TEXT)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (18)")
    if 19 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS execution_browser_contexts (session_id INTEGER PRIMARY KEY REFERENCES application_execution_sessions(id), worker_instance_id TEXT NOT NULL, context_id TEXT NOT NULL, page_id TEXT NOT NULL, current_url TEXT NOT NULL, platform TEXT NOT NULL, pageup_stage TEXT, cv_confirmed INTEGER NOT NULL DEFAULT 0, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (19)")
    if 20 not in applied:
        connection.execute("CREATE TABLE IF NOT EXISTS discovery_job_observations (run_id INTEGER NOT NULL REFERENCES discovery_runs(id), job_id INTEGER NOT NULL REFERENCES jobs(id), freshness TEXT NOT NULL, source_kind TEXT NOT NULL, preliminary_status TEXT, resolution_status TEXT, PRIMARY KEY(run_id,job_id))")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_discovery_observations_run ON discovery_job_observations(run_id, freshness)")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (20)")
    if 21 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(discovery_run_sources)")}
        for name in ("new_count", "previously_seen_count", "eligible_count", "good_possible_count"):
            if name not in columns:
                connection.execute(f"ALTER TABLE discovery_run_sources ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (21)")
    if 22 not in applied:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS opportunity_evaluations ("
            "job_id INTEGER PRIMARY KEY REFERENCES jobs(id), evaluation_json TEXT NOT NULL, "
            "narrative_json TEXT, tailoring_json TEXT NOT NULL, "
            "evaluated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, narrative_generated_at TEXT)"
        )
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (22)")
    if 23 not in applied:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS profile_fact_sources ("
            "id INTEGER PRIMARY KEY, fact_id INTEGER NOT NULL REFERENCES profile_facts(id), "
            "source_document_id INTEGER NOT NULL REFERENCES source_documents(id), "
            "identity_key TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'CV', "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "UNIQUE(fact_id, source_document_id))"
        )
        connection.execute(
            "INSERT OR IGNORE INTO profile_fact_sources(fact_id,source_document_id,identity_key,relation) "
            "SELECT f.id,f.source_document_id,'legacy:' || f.id,'CV' "
            "FROM profile_facts f JOIN source_documents d ON d.id=f.source_document_id "
            "WHERE f.source_document_id IS NOT NULL AND lower(d.filename) LIKE '%cv%.pdf'"
        )
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (23)")
    if 24 not in applied:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunity_evaluations)")}
        if "profile_snapshot_hash" not in columns:
            connection.execute(
                "ALTER TABLE opportunity_evaluations ADD COLUMN profile_snapshot_hash TEXT NOT NULL DEFAULT ''"
            )
        if "canonical_cv_id" not in columns:
            connection.execute("ALTER TABLE opportunity_evaluations ADD COLUMN canonical_cv_id INTEGER")
        if "revision" not in columns:
            connection.execute(
                "ALTER TABLE opportunity_evaluations ADD COLUMN revision INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS opportunity_evaluation_history ("
            "id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), revision INTEGER NOT NULL, "
            "evaluation_json TEXT NOT NULL, narrative_json TEXT, tailoring_json TEXT NOT NULL, "
            "profile_snapshot_hash TEXT NOT NULL, canonical_cv_id INTEGER, evaluated_at TEXT NOT NULL, "
            "narrative_generated_at TEXT, archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "archive_reason TEXT NOT NULL)"
        )
        connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (24)")
    connection.commit()
