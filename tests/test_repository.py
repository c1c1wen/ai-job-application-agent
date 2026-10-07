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


def test_rescreening_never_moves_a_queued_application_backwards(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.com/1", "Python"))
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", [], None, ScoreConfig())
    repo.screen_and_assess(job_id, score)
    application_id = repo.create_application(job_id)
    repo.screen_and_assess(job_id, score)
    repo.create_application(job_id)
    assert repo.review_queue()[0]["application_id"] == application_id
    assert connection.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "REVIEW"


def test_match_label_uses_good_and_possible_ranges(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.com/label", "Python"))
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", [CandidateFact("skill", "Python", "CV")], None, ScoreConfig())
    repo.screen_and_assess(job_id, score)
    assert connection.execute("SELECT recommendation FROM match_assessments WHERE job_id=?", (job_id,)).fetchone()[0] == "POSSIBLE MATCH"


def test_review_queue_suppresses_cross_source_duplicates_without_deleting_jobs(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    direct_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://careers.example/job", "Python", "LEVER", "Brisbane", "example", "direct"))
    index_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://index.example/job", "Python", "ADZUNA", "East Brisbane", "adzuna-au", "indexed"))
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", [CandidateFact("skill", "Python", "CV")], None, ScoreConfig())
    for job_id in (direct_id, index_id):
        repo.screen_and_assess(job_id, score)
        repo.create_application(job_id)
    queue = repo.review_queue()
    assert len(queue) == 1
    assert queue[0]["source"] == "LEVER"
    assert connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_rediscovery_duplicate_keeps_existing_execution_session_attached_to_queue(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    direct_id, _ = repo.add_job(
        JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/role", "Python SQL dashboards", "LEVER", "Brisbane", "example", "direct")
    )
    indexed_id, _ = repo.add_job(
        JobInput("Data Analyst", "Example", "https://index.example/role", "Python SQL", "ADZUNA", "East Brisbane", "adzuna-au", "indexed")
    )
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", [CandidateFact("skill", "Python", "CV")], None, ScoreConfig())
    for job_id in (direct_id, indexed_id):
        repo.screen_and_assess(job_id, score)
        repo.create_application(job_id)
    direct_application = connection.execute("SELECT id FROM applications WHERE job_id=?", (direct_id,)).fetchone()[0]
    repo.add_artifact(direct_application, "cover_letter", "One.\n\nTwo.\n\nThree.", [])
    repo.transition(direct_application, ApplicationState.MATERIALS_READY, "user")
    repo.transition(direct_application, ApplicationState.APPROVED, "user")
    repo.transition(direct_application, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(direct_application, "LEVER", "https://jobs.lever.co/example/role/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Detected visible fields")
    repo.update_execution(session_id, "WAITING_FOR_USER", "Paused for user input")

    queue = repo.review_queue()

    assert len(queue) == 1
    assert queue[0]["application_id"] == direct_application
    assert queue[0]["execution_session_id"] == session_id
    assert repo.latest_execution(direct_application)["state"] == "WAITING_FOR_USER"


def test_adzuna_reimport_preserves_the_public_detail_query_url(tmp_path: Path):
    connection = connect(tmp_path / "agent.sqlite3")
    migrate(connection)
    repo = JobRepository(connection)
    first = JobInput("Data Analyst", "Example", "https://www.adzuna.com.au/details/123", "Summary", "ADZUNA", "Brisbane", "adzuna-au", "123")
    updated = JobInput("Data Analyst", "Example", "https://www.adzuna.com.au/details/123?campaign=api", "Summary", "ADZUNA", "Brisbane", "adzuna-au", "123")
    job_id, _ = repo.add_job(first)
    assert repo.add_job(updated) == (job_id, False)
    assert connection.execute("SELECT url FROM jobs WHERE id=?", (job_id,)).fetchone()[0].endswith("?campaign=api")
