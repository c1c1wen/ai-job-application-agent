from job_agent.domain.models import ApplicationState, JobInput, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.execution_summary import current_cv_summary, normalise_autofill_summary


def test_current_cv_summary_ignores_duplicate_historical_upload_filenames() -> None:
    current = {"canonical_filename": "canonical_cv.pdf"}
    historical_uploads = ["candidate-cv-6.pdf", "candidate-cv-6.pdf", "candidate-cv-6.pdf"]

    assert current_cv_summary(current) == "canonical_cv.pdf · current canonical version"
    assert all(filename not in current_cv_summary(current) for filename in historical_uploads)


def test_pronoun_summary_shows_only_selected_value() -> None:
    items = [
        {"field": "Pronouns Custom Let the employer know what pronouns you use", "value": "She/her", "source": "Application Profile"},
        {"field": "Pronouns He/him She/her They/them Xe/xem", "value": "She/her", "source": "Application Profile"},
        {"field": "Email", "value": "hidden", "source": "Candidate Profile"},
    ]

    assert normalise_autofill_summary(items) == ["Pronouns: She/her (Application Profile)", "Email (Candidate Profile)"]


def test_display_summary_preserves_final_review_session_and_never_submits(tmp_path) -> None:
    repo = JobRepository(connect(tmp_path / "summary.db"))
    migrate(repo.connection)
    repo.get_or_create_profile("Ada")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.test/summary", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Filled")
    repo.update_execution(session_id, "READY_FOR_FINAL_REVIEW", "Ready")
    repo.record_execution_upload(session_id, None, None, 1, "candidate-cv-6.pdf", "file-upload-1")
    repo.record_execution_upload(session_id, None, None, 1, "candidate-cv-6.pdf", "file-upload-1")

    current_cv_summary({"canonical_filename": "canonical_cv.pdf"})
    normalise_autofill_summary([{"field": "Pronouns options", "value": "She/her", "source": "Application Profile"}])

    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()["state"] == ApplicationState.READY_TO_APPLY
    assert len(repo.execution_uploads(session_id)) == 2
    assert not [event for event in repo.events(application_id) if event["to_state"] == ApplicationState.APPLIED]
