import json
from pathlib import Path

import pytest

from job_agent.domain.models import CandidateFact, JobInput, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job
from job_agent.llm.opportunity_evaluation import (
    OpportunityNarrativeError,
    OpportunityNarrativeWriter,
    validate_narrative,
)
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.opportunity_evaluation import build_opportunity_evaluation


def _facts() -> list[CandidateFact]:
    return [
        CandidateFact("skill", "Python", "CV skills", id=1),
        CandidateFact("tool", "SQL", "CV skills", id=2),
        CandidateFact("experience", "Data Management Internship", "CV experience", id=3),
        CandidateFact("target_role", "Data Analyst", "Search preference", id=4),
        CandidateFact("preferred_location", "Brisbane", "Search preference", id=5),
        CandidateFact("skill", "C", "CV skills", id=6),
    ]


def _evaluation():
    facts = _facts()
    requirements = JobRequirements(
        required_skills=("Python", "SQL", "cohort analysis"), required_locations=("Brisbane",)
    )
    score = score_job(requirements, "Data Analyst", facts, "Brisbane", ScoreConfig())
    return build_opportunity_evaluation(
        job_id=7,
        title="Data Analyst",
        score=score,
        config=ScoreConfig(),
        requirements=requirements,
        facts=facts,
        direct_source_status="DIRECT_VERIFIED",
        eligible=True,
        evidence_quality="HIGH",
    )


def test_only_direct_verified_eligible_jobs_receive_an_evaluation_brief() -> None:
    evaluation = _evaluation()
    assert evaluation.deterministic_score == 58
    with pytest.raises(ValueError, match="directly verified"):
        build_opportunity_evaluation(
            job_id=7,
            title="Data Analyst",
            score=score_job(JobRequirements(), "Data Analyst", _facts(), "Brisbane", ScoreConfig()),
            config=ScoreConfig(),
            requirements=JobRequirements(),
            facts=_facts(),
            direct_source_status="NOT_FOUND",
            eligible=True,
            evidence_quality="PARTIAL",
        )


def test_tailoring_plan_uses_only_profile_evidence_and_keeps_gaps_visible() -> None:
    evaluation = _evaluation()
    emphasised = {item.value for item in evaluation.tailoring_plan.emphasise}
    assert {"Python", "SQL", "Data Management Internship"}.issubset(emphasised)
    assert "cohort analysis" in evaluation.tailoring_plan.do_not_claim
    assert "cohort analysis" not in " ".join(emphasised).casefold()


def test_jd_only_requirement_cannot_become_candidate_evidence_in_narrative() -> None:
    evaluation = _evaluation()
    payload = {
        "fit_summary": {"text": "A possible match based on Python.", "fact_ids": [1]},
        "strongest_matches": [{"text": "I performed cohort analysis.", "fact_ids": [1]}],
        "important_gaps": ["cohort analysis"],
        "risk_or_stretch_factors": [],
        "recommendation_reasoning": "Review the explicit gap.",
    }
    with pytest.raises(OpportunityNarrativeError, match="job-only"):
        validate_narrative(payload, evaluation)


def test_grounded_narrative_claims_keep_the_deterministic_score_unchanged() -> None:
    evaluation = _evaluation()
    payload = {
        "fit_summary": {
            "text": "The role aligns with the candidate's documented analytical tools.",
            "fact_ids": [1, 2],
        },
        "strongest_matches": [
            {"text": "Python and SQL are supported by the supplied CV skills.", "fact_ids": [1, 2]}
        ],
        "important_gaps": ["cohort analysis"],
        "risk_or_stretch_factors": ["Cohort analysis remains an explicit gap."],
        "recommendation_reasoning": "The deterministic Possible recommendation reflects the stated gap.",
    }
    narrative = validate_narrative(payload, evaluation)
    assert narrative.strongest_matches[0].fact_ids == (1, 2)
    assert evaluation.deterministic_score == 58


def test_llm_failure_leaves_a_usable_deterministic_evaluation() -> None:
    class BrokenClient:
        class responses:
            @staticmethod
            def create(**_kwargs):
                raise TypeError("offline")

    writer = OpportunityNarrativeWriter.__new__(OpportunityNarrativeWriter)
    writer.client = BrokenClient()
    writer.model = "test"
    evaluation = _evaluation()
    with pytest.raises(OpportunityNarrativeError):
        writer.generate(evaluation)
    assert evaluation.deterministic_score == 58
    assert evaluation.missing_requirements == ("cohort analysis",)


def test_evaluation_and_dashboard_metrics_are_persisted_not_generated_text(tmp_path: Path) -> None:
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    profile_id = repo.get_or_create_profile("Ada")
    for fact in _facts():
        repo.add_fact(profile_id, CandidateFact(fact.fact_type, fact.value, fact.evidence))
    job_id, _ = repo.add_job(
        JobInput("Data Analyst", "Example", "https://jobs.example/1", "Python SQL", "LEVER", "Brisbane")
    )
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", repo.facts(), "Brisbane", ScoreConfig())
    repo.screen_and_assess(job_id, score)
    application_id = repo.create_application(job_id)
    evaluation = _evaluation()
    repo.save_opportunity_evaluation(job_id, evaluation.as_dict(), evaluation.as_dict()["tailoring_plan"])
    repo.save_for_later(application_id)
    metrics = repo.portfolio_metrics()
    assert metrics["evaluated"] == 1
    assert metrics["saved"] == 1
    stored = repo.opportunity_evaluation(job_id)
    assert json.loads(stored["evaluation_json"])["deterministic_score"] == evaluation.deterministic_score
