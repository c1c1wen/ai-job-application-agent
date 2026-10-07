from pathlib import Path

from job_agent.domain.models import CandidateFact, JobInput
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.discovery import run_broad_discovery


def _search():
    return {
        "roles": "Data Analyst, Analytics Analyst, Business Intelligence Analyst",
        "locations": "Brisbane, Queensland, Remote Australia",
        "work_mode": "", "employment_type": "", "include": "", "exclude": "",
        "level": "Graduate / entry-level",
    }


def test_discovery_funnel_keeps_aggregator_leads_out_of_review_until_directly_verified(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    profile_id = repo.get_or_create_profile()
    for fact in (
        CandidateFact("skill", "SQL", "CV"),
        CandidateFact("tool", "Power BI", "CV"),
        CandidateFact("target_role", "Data Analyst", "Preference"),
        CandidateFact("preferred_location", "Brisbane", "Preference"),
    ):
        repo.add_fact(profile_id, fact)
    direct = JobInput("Data Analyst", "Direct Co", "https://jobs.lever.co/direct/1", "Use SQL and Power BI", "LEVER", "Brisbane", "direct", "one")
    index = JobInput("Data Analyst", "Index Co", "https://www.adzuna.com.au/details/2", "Use SQL and Power BI", "ADZUNA", "Australia", "adzuna-au", "two")
    result = run_broad_discovery(repo, _search(), [("LEVER", lambda: [direct]), ("ADZUNA", lambda: [index])])
    assert result.funnel["raw_jobs_discovered"] == 2
    assert result.funnel["location_plausible"] == 2
    assert result.funnel["direct_vacancy_resolved"] == 1
    assert result.funnel["ready_for_application"] == 1
    assert result.sources["LEVER"]["new"] == 1
    assert result.sources["LEVER"]["eligible"] == 1
    assert result.sources["LEVER"]["good_or_possible"] == 1
    assert len(repo.review_queue()) == 1
    assert repo.latest_discovery_run()["id"] == result.run_id
    assert {row["source"] for row in repo.discovery_run_sources(result.run_id)} == {"ADZUNA", "LEVER"}


def test_a_failing_source_is_reported_without_blocking_other_sources(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    job = JobInput("Data Analyst", "Direct Co", "https://jobs.lever.co/direct/1", "SQL", "LEVER", "Brisbane", "direct", "one")
    result = run_broad_discovery(repo, _search(), [("BROKEN", lambda: (_ for _ in ()).throw(RuntimeError("offline"))), ("LEVER", lambda: [job])])
    assert result.sources["BROKEN"]["error"].startswith("RuntimeError")
    assert result.funnel["raw_jobs_discovered"] == 1


def test_rediscovered_job_with_existing_application_is_not_a_fresh_candidate(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    job = JobInput("Data Analyst", "Direct Co", "https://jobs.lever.co/direct/1", "SQL", "LEVER", "Brisbane", "direct", "one")
    job_id, _ = repo.add_job(job)
    repo.create_application(job_id)
    result = run_broad_discovery(repo, _search(), [("LEVER", lambda: [job])])
    assert result.funnel["existing_application"] == 1
    assert result.funnel["direct_vacancy_resolved"] == 0


def test_discovery_records_new_then_seen_freshness_without_resurfacing_an_application(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    job = JobInput("Data Analyst", "Direct Co", "https://jobs.lever.co/direct/1", "SQL", "LEVER", "Brisbane", "direct", "one")

    first = run_broad_discovery(repo, _search(), [("LEVER", lambda: [job])])
    second = run_broad_discovery(repo, _search(), [("LEVER", lambda: [job])])

    assert first.funnel["new_jobs"] == 1
    assert second.funnel["previously_seen"] == 1
    assert repo.discovery_inbox(first.run_id)[0]["freshness"] == "NEW"
    assert repo.discovery_inbox(second.run_id)[0]["freshness"] == "SEEN_BEFORE"


def test_source_diagnostics_redact_query_string_credentials(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)

    result = run_broad_discovery(
        repo,
        _search(),
        [("ADZUNA", lambda: (_ for _ in ()).throw(RuntimeError("https://api.example/?app_key=private-value")))],
    )

    assert "private-value" not in result.sources["ADZUNA"]["error"]
    assert "[redacted]" in result.sources["ADZUNA"]["error"]
