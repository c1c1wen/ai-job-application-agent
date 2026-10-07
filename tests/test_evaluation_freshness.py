from __future__ import annotations

import json
from pathlib import Path

import pytest

from job_agent.domain.models import CandidateFact, JobInput, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job
from job_agent.llm.opportunity_evaluation import OpportunityNarrativeError, validate_narrative
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.opportunity_evaluation import build_opportunity_evaluation


def _repo(tmp_path: Path) -> tuple[JobRepository, int, int]:
    repo = JobRepository(connect(tmp_path / "agent.sqlite3"))
    migrate(repo.connection)
    profile_id = repo.get_or_create_profile("Candidate", location="Brisbane")
    fact_id = repo.add_fact(profile_id, CandidateFact("skill", "Python", "Approved CV skill"))
    job_id, _ = repo.add_job(
        JobInput("Data Analyst", "Example", "https://jobs.example/evaluation", "Python")
    )
    repo.register_canonical_cv(
        "candidate_documents/canonical_cv.pdf", "canonical_cv.pdf", "cv-version-a", 1, 10, "CV A"
    )
    return repo, job_id, fact_id


def _brief(fact_id: int, label: str = "A") -> tuple[dict, dict]:
    evaluation = {"revision_label": label, "fact_ids": [fact_id]}
    return evaluation, {"emphasise": [{"fact_id": fact_id, "value": "Python"}]}


def test_evaluation_is_current_for_its_profile_and_canonical_cv_snapshot(tmp_path: Path) -> None:
    repo, job_id, fact_id = _repo(tmp_path)
    evaluation, tailoring = _brief(fact_id)

    assert repo.save_opportunity_evaluation(job_id, evaluation, tailoring) is True
    status = repo.opportunity_evaluation_status(job_id)

    assert status["stale"] is False
    assert status["stored"]["revision"] == 1


def test_canonical_cv_change_marks_evaluation_stale_without_overwriting_it(tmp_path: Path) -> None:
    repo, job_id, fact_id = _repo(tmp_path)
    evaluation, tailoring = _brief(fact_id, "A")
    repo.save_opportunity_evaluation(job_id, evaluation, tailoring)
    repo.register_canonical_cv(
        "candidate_documents/canonical_cv.pdf", "canonical_cv.pdf", "cv-version-b", 2, 11, "CV B"
    )

    assert repo.opportunity_evaluation_status(job_id)["stale"] is True
    assert repo.save_opportunity_evaluation(job_id, {"revision_label": "B"}, tailoring) is False
    assert json.loads(repo.opportunity_evaluation(job_id)["evaluation_json"])["revision_label"] == "A"


def test_approved_fact_change_marks_evaluation_stale(tmp_path: Path) -> None:
    repo, job_id, fact_id = _repo(tmp_path)
    evaluation, tailoring = _brief(fact_id)
    repo.save_opportunity_evaluation(job_id, evaluation, tailoring)
    profile_id = repo.profile()["id"]
    repo.add_fact(profile_id, CandidateFact("tool", "SQL", "New approved CV fact"))

    assert repo.opportunity_evaluation_status(job_id)["stale"] is True


def test_explicit_reevaluation_archives_old_revision_and_clears_old_narrative(tmp_path: Path) -> None:
    repo, job_id, fact_id = _repo(tmp_path)
    old, tailoring = _brief(fact_id, "A")
    old_narrative = {"fit_summary": {"text": "Old advice", "fact_ids": [fact_id]}}
    repo.save_opportunity_evaluation(job_id, old, tailoring, old_narrative)
    repo.register_canonical_cv(
        "candidate_documents/canonical_cv.pdf", "canonical_cv.pdf", "cv-version-b", 2, 11, "CV B"
    )
    new, new_tailoring = _brief(fact_id, "B")

    assert repo.save_opportunity_evaluation(job_id, new, new_tailoring, refresh_stale=True) is True
    current = repo.opportunity_evaluation(job_id)
    history = repo.opportunity_evaluation_history(job_id)

    assert current["revision"] == 2
    assert current["narrative_json"] is None
    assert json.loads(current["evaluation_json"])["revision_label"] == "B"
    assert len(history) == 1
    assert json.loads(history[0]["narrative_json"])["fit_summary"]["text"] == "Old advice"
    assert history[0]["archive_reason"] == "PROFILE_OR_CANONICAL_CV_CHANGED"
    # The UI must use this status to suppress the old narrative until explicit refresh.
    assert repo.opportunity_evaluation_status(job_id)["stale"] is False


def test_regenerated_narrative_cannot_reference_revoked_fact_ids(tmp_path: Path) -> None:
    repo, job_id, old_fact_id = _repo(tmp_path)
    repo.set_fact_active(old_fact_id, False)
    profile_id = repo.profile()["id"]
    current_fact_id = repo.add_fact(profile_id, CandidateFact("skill", "SQL", "Current approved fact"))
    facts = repo.facts()
    score = score_job(
        JobRequirements(required_skills=("SQL",)), "Data Analyst", facts, "Brisbane", ScoreConfig()
    )
    evaluation = build_opportunity_evaluation(
        job_id=job_id,
        title="Data Analyst",
        score=score,
        config=ScoreConfig(),
        requirements=JobRequirements(required_skills=("SQL",)),
        facts=facts,
        direct_source_status="DIRECT_VERIFIED",
        eligible=True,
        evidence_quality="HIGH",
    )
    assert old_fact_id not in {item.fact_id for item in evaluation.strongest_candidate_evidence}
    with pytest.raises(OpportunityNarrativeError, match="without approved evidence"):
        validate_narrative(
            {
                "fit_summary": {"text": "Old skill.", "fact_ids": [old_fact_id]},
                "strongest_matches": [],
                "important_gaps": [],
                "risk_or_stretch_factors": [],
                "recommendation_reasoning": "No claim.",
            },
            evaluation,
        )
    assert current_fact_id in {item.fact_id for item in evaluation.strongest_candidate_evidence}


def test_reevaluation_does_not_change_existing_application_state(tmp_path: Path) -> None:
    repo, job_id, fact_id = _repo(tmp_path)
    application_id = repo.create_application(job_id)
    before = repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
    old, tailoring = _brief(fact_id, "A")
    repo.save_opportunity_evaluation(job_id, old, tailoring)
    repo.register_canonical_cv(
        "candidate_documents/canonical_cv.pdf", "canonical_cv.pdf", "cv-version-b", 2, 11, "CV B"
    )
    current, current_tailoring = _brief(fact_id, "B")

    repo.save_opportunity_evaluation(job_id, current, current_tailoring, refresh_stale=True)
    after = repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()

    assert dict(after) == dict(before)
