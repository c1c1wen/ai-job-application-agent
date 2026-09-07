from job_agent.domain.models import CandidateFact, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job


def test_hard_work_right_gate_blocks_score():
    result = score_job(JobRequirements(required_skills=("Python",), work_right_requirements=("Australian citizen",)), "Data Analyst", [CandidateFact("skill", "Python", "CV")], "Brisbane", ScoreConfig())
    assert not result.eligibility_passed and result.total == 0


def test_requirement_based_components():
    facts = [CandidateFact("skill", "Python", "CV"), CandidateFact("experience", "data analysis", "CV"), CandidateFact("education", "Master", "CV"), CandidateFact("target_role", "Data Analyst", "P"), CandidateFact("preferred_location", "Brisbane", "P")]
    result = score_job(JobRequirements(required_skills=("Python",), experience_requirements=("data analysis",), education_requirements=("Master",), required_locations=("Brisbane",)), "Data Analyst", facts, "Brisbane", ScoreConfig())
    assert result.total == 100
