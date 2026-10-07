from job_agent.domain.models import CandidateFact, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job


def test_hard_work_right_gate_blocks_score():
    result = score_job(JobRequirements(required_skills=("Python",), work_right_requirements=("Australian citizen",)), "Data Analyst", [CandidateFact("skill", "Python", "CV")], "Brisbane", ScoreConfig())
    assert not result.eligibility_passed and result.total == 0


def test_requirement_based_components():
    facts = [CandidateFact("skill", "Python", "CV"), CandidateFact("experience", "data analysis", "CV"), CandidateFact("education", "Master", "CV"), CandidateFact("target_role", "Data Analyst", "P"), CandidateFact("preferred_location", "Brisbane", "P")]
    result = score_job(JobRequirements(required_skills=("Python",), experience_requirements=("data analysis",), education_requirements=("Master",), required_locations=("Brisbane",)), "Data Analyst", facts, "Brisbane", ScoreConfig())
    assert result.total == 100


def test_languages_and_tools_are_technical_evidence():
    result = score_job(
        JobRequirements(required_skills=("Python", "SQL")),
        "Data Analyst",
        [CandidateFact("programming_language", "Python", "CV"), CandidateFact("tool", "SQL", "CV")],
        None,
        ScoreConfig(),
    )
    assert result.skills == 35


def test_location_score_uses_the_same_brisbane_compatibility_as_the_hard_gate():
    result = score_job(
        JobRequirements(required_locations=("Brisbane, Queensland",)),
        "Data Analyst",
        [CandidateFact("preferred_location", "Brisbane", "Saved search setting")],
        "Brisbane, Queensland",
        ScoreConfig(),
    )
    assert result.eligibility_passed
    assert result.location == 10


def test_documented_internship_and_project_receive_bounded_partial_experience_credit():
    facts = [
        CandidateFact("experience", "Data Management Intern | Example | June 2023 - Dec 2023", "CV"),
        CandidateFact("project", "AI analytics project", "CV"),
    ]
    result = score_job(JobRequirements(experience_requirements=("1-2 years of experience",)), "Data Analyst", facts, None, ScoreConfig())
    assert result.experience == 13
    assert "Intern" in result.experience_evidence[0]
    assert "partial credit" in result.reasons[1]


def test_documented_full_professional_duration_receives_full_experience_credit():
    fact = CandidateFact("experience", "Data Analyst | Example | January 2022 - January 2024", "CV")
    result = score_job(JobRequirements(experience_requirements=("1 year of experience",)), "Data Analyst", [fact], None, ScoreConfig())
    assert result.experience == 20
