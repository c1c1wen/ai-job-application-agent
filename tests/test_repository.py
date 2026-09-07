from pathlib import Path

from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    JobInput,
    JobRequirements,
    ScoreConfig,
)
from job_agent.domain.scoring import score_job
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository


def test_deduplication_and_auditable_approval(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    profile_id = repo.get_or_create_profile("Ada")
    repo.add_fact(profile_id, CandidateFact("skill", "Python", "CV: skills section"))
    repo.add_fact(profile_id, CandidateFact("target_role", "Data Analyst", "Profile preference"))
    repo.add_fact(profile_id, CandidateFact("preferred_location", "Brisbane", "Profile preference"))
    job = JobInput("Data Analyst", "Example Co", "https://example.com/jobs/1", "Use Python to analyse data", "MANUAL", "Brisbane")
    job_id, created = repo.add_job(job)
    assert created and repo.add_job(job) == (job_id, False)
    repo.screen_and_assess(job_id, score_job(JobRequirements(required_skills=("Python",)), job.title, repo.facts(), job.location, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user", "Reviewed")
    repo.transition(application_id, ApplicationState.APPROVED, "user", "Approved")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system", "Deferred to V2")
    events = repo.events(application_id)
    assert [event["to_state"] for event in events] == ["REVIEW", "MATERIALS_READY", "APPROVED", "READY_TO_APPLY"]
