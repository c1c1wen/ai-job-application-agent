import json

import pytest

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown
from job_agent.llm.application_writing import ApplicationWritingError, GroundedApplicationWriter


class Response:
    output_text = json.dumps({
        "cover_letter": "A polished letter.",
        "why_interested": "I am interested in this work.",
        "relevant_experience": "My internship is relevant.",
        "technical_skills": "I have used Python and SQL.",
        "cv_suggestions": ["Move Python higher."],
        "claims": [
            {"artifact": "cover_letter", "claim": "I have used Python and SQL.", "fact_ids": [1, 2]},
            {"artifact": "application_answers", "claim": "My internship is relevant.", "fact_ids": [3]},
        ],
    })


class Client:
    class responses:
        @staticmethod
        def create(**_kwargs): return Response()


def test_writer_uses_selected_evidence_and_records_supported_claims():
    facts = [CandidateFact("skill", "Python", "CV", id=1), CandidateFact("tool", "SQL", "CV", id=2), CandidateFact("experience", "Data Intern", "CV", id=3), CandidateFact("skill", "C", "CV", id=4)]
    writer = GroundedApplicationWriter.__new__(GroundedApplicationWriter)
    writer.client = Client(); writer.model = "test-model"
    score = ScoreBreakdown(35, 20, 0, 0, 0, 5, True, matched_skills=("python", "sql"))
    draft = writer.generate("Data Analyst", "Example", JobRequirements(required_skills=("python", "sql")), score, facts)
    assert draft.fact_ids == [1, 2, 3]
    assert "SUPPORTED" in draft.claim_ledger
    assert "C" not in draft.artifacts["cover_letter"]


def test_completed_education_is_not_marked_current():
    packet = GroundedApplicationWriter._fact_packet(CandidateFact("education", "Master of Data Science | UQ | July 2024 - July 2026", "CV", id=5))
    assert packet["employment_or_education_status"] == "completed"
    assert packet["completion_or_expected_date"] == "2026-07-01"


def test_internal_workflow_language_is_rejected_from_candidate_facing_text():
    payload = json.loads(Response.output_text)
    payload["cover_letter"] = "Please use manual completion unless evidence is provided."
    with pytest.raises(ApplicationWritingError, match="internal safety language"):
        GroundedApplicationWriter._validate(payload, {1, 2, 3})


def test_jd_only_cohort_analysis_cannot_become_candidate_experience_claim():
    payload = json.loads(Response.output_text)
    payload["cover_letter"] = "During my internship, I performed cohort analysis to support reporting."
    facts = [CandidateFact("experience", "Data Management Intern", "CV", id=3)]

    with pytest.raises(ApplicationWritingError, match="JD-only requirements"):
        GroundedApplicationWriter._validate(
            payload,
            {3},
            facts,
            JobRequirements(required_skills=("cohort analysis",)),
            "The role requires cohort analysis and funnel analysis.",
        )
