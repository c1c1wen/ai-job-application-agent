from pathlib import Path

from job_agent.domain.models import CandidateFact, JobInput, JobRequirements, ScoreConfig
from job_agent.domain.scoring import score_job
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.job_search import (
    analyze_job_description,
    assess_search_eligibility,
    broad_discovery_queries,
    extract_requirements,
    preliminary_eligibility,
    suggestion_from_profile,
)


def _search(**overrides):
    search = {"roles": "Data Analyst", "locations": "Brisbane, Queensland, Remote Australia", "work_mode": "", "employment_type": "", "include": "", "exclude": "", "level": "Graduate / entry-level"}
    search.update(overrides)
    return search


def test_location_and_seniority_are_hard_eligibility_filters():
    uk = JobInput("Data Analyst", "Co", "https://example.com/uk", "Analytics", location="London, United Kingdom")
    senior = JobInput("Senior Data Analyst", "Co", "https://example.com/senior", "Analytics", location="Brisbane")
    remote = JobInput("Insights Analyst", "Co", "https://example.com/remote", "Analytics", location="Remote - Australia")
    assert not assess_search_eligibility(uk, _search()).passed
    assert not assess_search_eligibility(senior, _search()).passed
    assert assess_search_eligibility(remote, _search()).passed


def test_australian_remote_role_is_eligible_when_its_hq_is_outside_brisbane():
    job = JobInput(
        "Data & Insights Analyst - Remote",
        "Co",
        "https://example.com/remote-sydney",
        "Work mode: remote. Australia-wide remote role.",
        location="Sydney, New South Wales",
    )
    assert assess_search_eligibility(job, _search()).passed


def test_role_families_allow_related_analyst_work():
    job = JobInput("Business Intelligence Analyst", "Co", "https://example.com/bi", "Build reporting dashboards", location="Brisbane")
    assert assess_search_eligibility(job, _search()).passed


def test_generic_ai_description_does_not_make_sales_role_compatible():
    job = JobInput("Account Executive", "Co", "https://example.com/sales", "Sell our AI platform", location="Brisbane")
    assert not assess_search_eligibility(job, _search(roles="AI Analyst")).passed


def test_substantial_experience_is_ineligible_for_graduate_search():
    job = JobInput("Data Analyst", "Co", "https://example.com/experienced", "At least 3 years of professional experience required", location="Brisbane")
    assert not assess_search_eligibility(job, _search()).passed


def test_unknown_location_is_not_active_when_locations_are_selected():
    job = JobInput("Data Analyst", "Co", "https://example.com/unknown", "Analytics")
    assert not assess_search_eligibility(job, _search()).passed


def test_country_only_index_location_is_resolved_before_strict_location_filtering():
    job = JobInput("Data Analyst", "Co", "https://example.com/unknown", "SQL and reporting", location="Australia")
    assert preliminary_eligibility(job, _search()).status == "POSSIBLE"
    assert not assess_search_eligibility(job, _search()).passed


def test_analytics_oriented_business_analyst_is_a_resolution_candidate_but_process_only_is_not():
    relevant = JobInput("Business Analyst", "Co", "https://example.com/ba", "Build Power BI dashboards and SQL reporting analytics", location="Brisbane")
    process_only = JobInput("Business Analyst", "Co", "https://example.com/process", "ERP implementation and stakeholder workshops", location="Brisbane")
    assert preliminary_eligibility(relevant, _search()).status == "LIKELY_ELIGIBLE"
    assert preliminary_eligibility(process_only, _search()).status == "LIKELY_INELIGIBLE"


def test_broad_role_queries_extend_saved_preferences_without_losing_them():
    queries = broad_discovery_queries(_search(roles="Data Analyst, AI Analyst"))
    assert queries[:2] == ["Data Analyst", "AI Analyst"]
    assert "Graduate Analytics" in queries
    assert "Data and Reporting Analyst" in queries
    assert "Graduate Business Analyst" not in queries


def test_generic_australia_preference_does_not_override_specific_brisbane_preference():
    search = _search(locations="Brisbane, Queensland, Australia")
    canberra = JobInput("Data Analyst", "Co", "https://example.com/canberra", "Analytics", location="Canberra Region, Australian Capital Territory")
    generic = JobInput("Data Analyst", "Co", "https://example.com/generic", "Analytics", location="Australia")
    remote = JobInput("Data Analyst", "Co", "https://example.com/remote", "Analytics", location="Remote, Australia")
    assert not assess_search_eligibility(canberra, search).passed
    assert not assess_search_eligibility(generic, search).passed
    assert assess_search_eligibility(remote, search).passed


def test_requirement_extraction_does_not_treat_every_letter_r_as_r_language():
    assert extract_requirements("Reporting and analysis", "Brisbane").required_skills == ()
    assert extract_requirements("Use R and SQL", "Brisbane").required_skills == ("sql", "r")


def test_full_jd_analysis_separates_required_and_preferred_skills():
    analysis = analyze_job_description("What We Require Strong SQL and Python. Nice to haves Tableau and dbt.", "Brisbane", "Data Analyst")
    assert analysis.requirements.required_skills == ("python", "sql")
    assert analysis.requirements.preferred_skills == ("tableau", "dbt")


def test_profile_suggestions_only_follow_stored_facts():
    suggested = suggestion_from_profile([CandidateFact("skill", "Data visualisation", "CV"), CandidateFact("skill", "Machine learning", "CV"), CandidateFact("experience", "Graduate project", "CV")], "Brisbane, Australia")
    assert "Data Analyst" in suggested["roles"]
    assert "AI Analyst" in suggested["roles"]
    assert suggested["locations"] == "Brisbane, Queensland, Remote Australia"
    assert suggested["level"] == "Graduate / entry-level"


def test_ineligible_jobs_remain_in_history_but_leave_active_queue(tmp_path: Path):
    db = connect(tmp_path / "agent.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Co", "https://example.com/1", "Python", location="London"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], "London", ScoreConfig()))
    repo.create_application(job_id)
    repo.record_search_eligibility(job_id, False, ("Location is outside the selected preferences.",), "settings-v1")
    assert repo.review_queue() == []
    assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
