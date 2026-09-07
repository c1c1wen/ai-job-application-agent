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


def test_artifact_versions_and_approval_snapshot(tmp_path):
    db=connect(tmp_path/"a.db"); migrate(db); repo=JobRepository(db); p=repo.get_or_create_profile("Ada"); fact=repo.add_fact(p,CandidateFact("skill","Python","CV")); job,_=repo.add_job(JobInput("Data Analyst","Co","https://x/1","Python")); repo.screen_and_assess(job,score_job(JobRequirements(required_skills=("Python",)),"Data Analyst",repo.facts(),None,ScoreConfig())); app=repo.create_application(job)
    v1=repo.add_artifact(app,"cover_letter","original",[fact]); v2=repo.add_artifact(app,"cover_letter","edited",[fact]); assert [r["content"] for r in repo.artifacts(app)]==["edited","original"]
    repo.transition(app,ApplicationState.MATERIALS_READY,"user"); repo.transition(app,ApplicationState.APPROVED,"user")
    approved=json.loads(repo.connection.execute("SELECT approved_artifact_ids_json FROM applications WHERE id=?",(app,)).fetchone()[0]); assert approved==[v2]
    assert v1 not in approved and any(v2 in json.loads(e["details_json"]).get("artifact_ids",[]) for e in repo.connection.execute("SELECT details_json FROM audit_events"))
