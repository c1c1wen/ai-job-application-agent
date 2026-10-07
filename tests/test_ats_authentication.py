from pathlib import Path

import pytest

from job_agent.domain.models import ApplicationState, JobInput
from job_agent.domain.workflow import InvalidTransition, assert_transition
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.application_execution import (
    authentication_status_from_text,
    detect_platform,
)
from job_agent.services.execution_workflow import resume_all_authenticated_sessions


def _waiting_login_session(repo: JobRepository, suffix: str) -> tuple[int, int]:
    job_id, _ = repo.add_job(
        JobInput(
            "Data Analyst",
            f"Example {suffix}",
            f"https://jobs.lever.co/example/{suffix}",
            "Python",
            "LEVER",
        )
    )
    app_id = repo.create_application(job_id)
    repo.transition(app_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(app_id, ApplicationState.APPROVED, "user")
    repo.transition(app_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(
        app_id, "LEVER", f"https://jobs.lever.co/example/{suffix}/apply"
    )
    repo.update_execution(session_id, "FILLING_FORM", "Form opened")
    repo.update_execution(
        session_id,
        "WAITING_FOR_USER",
        "Login required",
        warnings=[
            "LOGIN_REQUIRED: Sign in through the local persistent browser profile, then resume."
        ],
    )
    return app_id, session_id


def test_authenticated_metadata_is_non_secret_and_unblocks_all_login_waiters(tmp_path: Path):
    repo = JobRepository(connect(tmp_path / "agent.sqlite3"))
    migrate(repo.connection)
    _, first = _waiting_login_session(repo, "one")
    _, second = _waiting_login_session(repo, "two")
    repo.set_ats_authentication("LEVER", "AUTHENTICATED", verified=True)
    status = repo.ats_authentication("LEVER")
    assert status["authentication_status"] == "AUTHENTICATED"
    assert "cookie" not in status and "token" not in status
    assert {row["id"] for row in repo.authentication_ready_sessions()} == {first, second}


def test_expired_or_unknown_session_is_not_resumed(tmp_path: Path):
    repo = JobRepository(connect(tmp_path / "agent.sqlite3"))
    migrate(repo.connection)
    _waiting_login_session(repo, "one")
    repo.set_ats_authentication("LEVER", "SESSION_EXPIRED")
    assert repo.authentication_ready_sessions() == []
    assert authentication_status_from_text("Sign in or sign up to apply") == "LOGIN_REQUIRED"


def test_resume_all_continues_other_sessions_when_one_pauses(tmp_path: Path, monkeypatch):
    repo = JobRepository(connect(tmp_path / "agent.sqlite3"))
    migrate(repo.connection)
    _, first = _waiting_login_session(repo, "one")
    _, second = _waiting_login_session(repo, "two")
    repo.set_ats_authentication("LEVER", "AUTHENTICATED", verified=True)
    calls = []

    def fake_resume(_repo, session, *_args):
        calls.append(session["id"])
        if session["id"] == first:
            raise RuntimeError("CAPTCHA remains human-only")
        return []

    monkeypatch.setattr("job_agent.services.execution_workflow.resume_execution", fake_resume)
    result = resume_all_authenticated_sessions(repo, None, [], tmp_path / "uploads")
    assert set(calls) == {first, second}
    assert result == {"resumed": 1, "paused": 1}


def test_persistent_profile_directory_is_gitignored_and_applied_cannot_submit_again():
    ignored = Path(".gitignore").read_text()
    assert "data/browser_profile/" in ignored
    with pytest.raises(InvalidTransition):
        assert_transition(ApplicationState.APPLIED, ApplicationState.APPLYING)


def test_employment_hero_is_a_persistent_session_capable_ats():
    assert detect_platform("https://jobs.employmenthero.com/job/example/apply") == "EMPLOYMENT_HERO"
