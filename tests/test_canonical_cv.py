from pathlib import Path

import pytest

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
from job_agent.services.canonical_cv import (
    application_cv_status,
    register_canonical_cv,
    use_current_canonical_cv,
)
from job_agent.services.execution_documents import prepare_execution_documents


def _repo_and_ready_application(tmp_path: Path) -> tuple[JobRepository, int]:
    repo = JobRepository(connect(tmp_path / "agent.db"))
    migrate(repo.connection)
    profile_id = repo.get_or_create_profile("Ada")
    fact_id = repo.add_fact(profile_id, CandidateFact("skill", "Python", "CV"))
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.test/job", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", repo.facts(), None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.add_artifact(application_id, "cover_letter", "One.\n\nTwo.\n\nThree.", [fact_id])
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    return repo, application_id


def _canonical_file(root: Path, content: bytes = b"canonical cv one") -> Path:
    path = root / "candidate_documents" / "canonical_cv.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def test_single_existing_candidate_pdf_remains_usable_without_renaming(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.canonical_cv.parse_cv", lambda _name, _bytes: "Legacy CV")
    repo, _application_id = _repo_and_ready_application(tmp_path)
    legacy = tmp_path / "candidate_documents" / "existing_local_cv.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"existing local cv")

    canonical = register_canonical_cv(repo, tmp_path)

    assert canonical["source_path"] == str(legacy)
    assert legacy.exists()


def test_canonical_cv_is_selected_instead_of_legacy_documents(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.canonical_cv.parse_cv", lambda _name, _bytes: "Canonical CV text")
    repo, application_id = _repo_and_ready_application(tmp_path)
    legacy = tmp_path / "legacy.pdf"; legacy.write_bytes(b"legacy")
    repo.add_document("candidate-cv-6.pdf", str(legacy), "application/pdf", "legacy text")
    _canonical_file(tmp_path)
    fact_count = repo.connection.execute("SELECT count(*) FROM profile_facts").fetchone()[0]

    canonical = register_canonical_cv(repo, tmp_path)
    attached = use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    documents = prepare_execution_documents([], repo.application_cv_document(application_id), tmp_path / "uploads")

    assert repo.latest_candidate_cv()["id"] == canonical["source_document_id"]
    assert attached["source_document_id"] == canonical["source_document_id"]
    assert documents[0].source_document_id == canonical["source_document_id"]
    assert "candidate-cv-6" not in documents[0].path.name
    assert repo.connection.execute("SELECT count(*) FROM profile_facts").fetchone()[0] == fact_count


def test_changed_canonical_cv_marks_unsubmitted_application_outdated_until_explicit_refresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.canonical_cv.parse_cv", lambda _name, content: content.decode())
    repo, application_id = _repo_and_ready_application(tmp_path)
    path = _canonical_file(tmp_path, b"version one")
    first = register_canonical_cv(repo, tmp_path)
    original = use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    path.write_bytes(b"version two")

    status = application_cv_status(repo, tmp_path, application_id)
    assert status.outdated
    assert status.canonical["id"] != first["id"]
    with pytest.raises(ValueError, match="explicitly"):
        use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=False)
    refreshed = use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)

    assert refreshed["sha256"] == status.canonical["sha256"]
    assert original["id"] != refreshed["id"]
    assert repo.application_cv_is_outdated(application_id, status.canonical) is False
    assert repo.connection.execute("SELECT count(*) FROM application_cv_versions WHERE application_id=?", (application_id,)).fetchone()[0] == 2


def test_submitted_application_preserves_its_historical_cv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.canonical_cv.parse_cv", lambda _name, _bytes: "CV")
    repo, application_id = _repo_and_ready_application(tmp_path)
    path = _canonical_file(tmp_path)
    use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    repo.transition(application_id, ApplicationState.APPLYING, "system")
    repo.transition(application_id, ApplicationState.APPLIED, "system")
    path.write_bytes(b"new canonical")

    with pytest.raises(ValueError, match="Submitted applications"):
        use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    assert repo.connection.execute("SELECT state FROM applications WHERE id=?", (application_id,)).fetchone()[0] == ApplicationState.APPLIED
    assert repo.connection.execute("SELECT count(*) FROM application_cv_versions WHERE application_id=?", (application_id,)).fetchone()[0] == 1


def test_refresh_preserves_existing_final_review_session_answers_and_no_submission(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("job_agent.services.canonical_cv.parse_cv", lambda _name, content: content.decode())
    repo, application_id = _repo_and_ready_application(tmp_path)
    path = _canonical_file(tmp_path, b"v1")
    use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Filled")
    repo.update_execution(session_id, "READY_FOR_FINAL_REVIEW", "Ready")
    repo.save_execution_answer(session_id, {"key": "salary", "name": "salary", "label": "Salary", "field_type": "text"}, "saved", "preference", False)
    repo.update_execution(session_id, "SUBMITTING", "Submission attempt was not verified")
    repo.update_execution(session_id, "FAILED", "Submission was not verified")
    path.write_bytes(b"v2")

    use_current_canonical_cv(repo, tmp_path, application_id, explicit_refresh=True)
    repo.record_execution_cv_refresh(session_id)

    assert repo.latest_execution(application_id)["id"] == session_id
    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert repo.answer_map(session_id) == {"salary": "saved"}
    application = repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
    assert application["state"] == ApplicationState.READY_TO_APPLY
    assert application["applied_at"] is None
