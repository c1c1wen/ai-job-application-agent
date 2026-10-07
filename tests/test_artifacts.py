import json

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
from job_agent.services.materials import application_recommendation, grounded_materials


def test_artifact_versions_and_approval_snapshot(tmp_path):
    db=connect(tmp_path/"a.db"); migrate(db); repo=JobRepository(db); p=repo.get_or_create_profile("Ada"); fact=repo.add_fact(p,CandidateFact("skill","Python","CV")); job,_=repo.add_job(JobInput("Data Analyst","Co","https://x/1","Python")); repo.screen_and_assess(job,score_job(JobRequirements(required_skills=("Python",)),"Data Analyst",repo.facts(),None,ScoreConfig())); app=repo.create_application(job)
    v1=repo.add_artifact(app,"cover_letter","original",[fact]); v2=repo.add_artifact(app,"cover_letter","edited",[fact]); assert [r["content"] for r in repo.artifacts(app)]==["edited","original"]
    repo.transition(app,ApplicationState.MATERIALS_READY,"user"); repo.transition(app,ApplicationState.APPROVED,"user")
    approved=json.loads(repo.connection.execute("SELECT approved_artifact_ids_json FROM applications WHERE id=?",(app,)).fetchone()[0]); assert approved==[v2]
    assert v1 not in approved and any(v2 in json.loads(e["details_json"]).get("artifact_ids",[]) for e in repo.connection.execute("SELECT details_json FROM audit_events"))


def test_replacing_an_approved_cover_letter_keeps_ready_application_and_other_snapshot_items(tmp_path):
    db=connect(tmp_path/"replace.db"); migrate(db); repo=JobRepository(db); p=repo.get_or_create_profile("Ada"); fact=repo.add_fact(p,CandidateFact("skill","Python","CV")); job,_=repo.add_job(JobInput("Data Analyst","Co","https://x/replace","Python")); repo.screen_and_assess(job,score_job(JobRequirements(required_skills=("Python",)),"Data Analyst",repo.facts(),None,ScoreConfig())); app=repo.create_application(job)
    original_cover=repo.add_artifact(app,"cover_letter","Original.\n\nSecond.\n\nThird.",[fact]); answer=repo.add_artifact(app,"application_answers","Answer",[fact])
    repo.transition(app,ApplicationState.MATERIALS_READY,"user"); repo.transition(app,ApplicationState.APPROVED,"user"); repo.transition(app,ApplicationState.READY_TO_APPLY,"system")

    revised_cover=repo.replace_approved_cover_letter(app,"Revised.\n\nSecond.\n\nThird.",[fact])

    application=repo.connection.execute("SELECT state,approved_artifact_ids_json FROM applications WHERE id=?",(app,)).fetchone()
    assert application["state"] == ApplicationState.READY_TO_APPLY
    assert json.loads(application["approved_artifact_ids_json"]) == [revised_cover,answer]
    assert [artifact["version"] for artifact in repo.artifacts(app) if artifact["kind"] == "cover_letter"] == [2,1]
    assert original_cover not in json.loads(application["approved_artifact_ids_json"])


def test_grounded_preparation_uses_only_relevant_approved_fact_ids():
    facts = [CandidateFact("skill", "Python", "CV", id=1), CandidateFact("skill", "Unrelated", "CV", id=2), CandidateFact("experience", "Data Intern", "CV", id=3), CandidateFact("target_role", "Data Analyst", "Settings", id=4)]
    requirements = JobRequirements(required_skills=("Python",))
    score = score_job(requirements, "Data Analyst", facts, "Brisbane", ScoreConfig())
    drafts = grounded_materials("Data Analyst", "Example", facts, requirements, score)
    assert drafts["cover_letter"][1] == [1, 3]
    assert "Unrelated" not in drafts["cover_letter"][0]
    assert application_recommendation(score, ScoreConfig()).label == "APPLY"
