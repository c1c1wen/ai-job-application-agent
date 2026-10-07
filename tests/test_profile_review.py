from job_agent.domain.models import CandidateFact
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository


def test_batch_approval_and_pending_detail_prefill_data(tmp_path):
    db=connect(tmp_path/"profile.db"); migrate(db); repo=JobRepository(db); profile=repo.get_or_create_profile("Candidate")
    doc=repo.add_document("cv.pdf","data/artifacts/x.pdf","application/pdf","Ada ada@example.com")
    name=repo.add_pending_fact(profile,CandidateFact("name","Ada","Header",False),doc,"Ada","high")
    email=repo.add_pending_fact(profile,CandidateFact("email","ada@example.com","Header",False),doc,"ada@example.com","high")
    assert {row["fact_type"] for row in repo.fact_review_rows()} >= {"name","email"}
    repo.review_facts([name,email],"APPROVED")
    assert all(f.approved for f in repo.facts() if f.id in {name,email})


def test_explicit_cv_fact_is_trusted_regardless_of_confidence(tmp_path):
    db=connect(tmp_path/"trusted.db"); migrate(db); repo=JobRepository(db); profile=repo.get_or_create_profile("Ada"); doc=repo.add_document("cv.pdf","data/artifacts/x.pdf","application/pdf","Python")
    trusted=repo.add_cv_fact(profile,CandidateFact("skill","Python","Skills",False),doc,"Python","high")
    uncertain=repo.add_cv_fact(profile,CandidateFact("skill","R","Skills",False),doc,"R","low")
    statuses={row["id"]:row["status"] for row in repo.fact_review_rows()}
    assert statuses[trusted] == "APPROVED" and statuses[uncertain] == "APPROVED"


def test_stale_cv_pending_facts_are_promoted_when_evidence_exists(tmp_path):
    db=connect(tmp_path/"stale.db"); migrate(db); repo=JobRepository(db); profile=repo.get_or_create_profile("Ada"); doc=repo.add_document("cv.pdf","data/artifacts/x.pdf","application/pdf","Python")
    stale=repo.add_pending_fact(profile,CandidateFact("skill","Python","Skills",False),doc,"Python","high")
    assert repo.promote_stale_cv_facts() == 1
    assert {row["id"]: row["status"] for row in repo.fact_review_rows()}[stale] == "APPROVED"
