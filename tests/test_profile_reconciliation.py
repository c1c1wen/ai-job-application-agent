from pathlib import Path

from job_agent.domain.models import CandidateFact
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.profile_reconciliation import fact_core_identity, fact_identity


def _item(fact_type: str, value: str) -> dict:
    return {
        "fact": CandidateFact(fact_type, value, "CV"),
        "snippet": value,
        "confidence": "high",
    }


def _repo(tmp_path: Path) -> tuple[JobRepository, int]:
    db = connect(tmp_path / "profile.sqlite3")
    migrate(db)
    repo = JobRepository(db)
    return repo, repo.get_or_create_profile("Candidate")


def test_education_and_experience_identity_ignores_display_formatting() -> None:
    assert fact_identity(
        "education",
        "Master of Data Science | The University of Queensland | Brisbane, Australia | July 2024 - July 2026",
    ) == fact_identity(
        "education",
        "Master of Data Science, The University of Queensland, Brisbane, Australia (Jul 2024 - Jul 2026)",
    )
    assert fact_identity(
        "experience",
        "Data Management Intern | Example Analytics Co., Ltd. | Beijing, China | June 2023 - Dec 2023",
    ) == fact_identity(
        "experience",
        "Data Management Intern, Example Analytics Co., Ltd., Beijing, China (Jun 2023 - Dec 2023)",
    )


def test_reconciliation_preserves_same_identity_adds_new_and_retires_removed_cv_facts(tmp_path: Path) -> None:
    repo, profile_id = _repo(tmp_path)
    old_document = repo.add_document("CV_Alex_Candidate.pdf", "old.pdf", "application/pdf", "old")
    new_document = repo.add_document("CV_Alex_Candidate.pdf", "new.pdf", "application/pdf", "new")
    manual_id = repo.add_fact(profile_id, CandidateFact("skill", "Stakeholder reporting", "User entry"))
    old = [
        _item("education", "Master of Data Science | The University of Queensland | Brisbane, Australia | July 2024 - July 2026"),
        _item("experience", "Data Management Intern | Example Analytics Co., Ltd. | Beijing, China | June 2023 - Dec 2023"),
        _item("skill", "Old spreadsheet tool"),
    ]
    first = repo.reconcile_cv_facts(profile_id, old_document, old)
    assert first["added"] == 3
    new = [
        _item("education", "Master of Data Science, The University of Queensland, Brisbane, Australia (Jul 2024 - Jul 2026)"),
        _item("experience", "Data Management Intern, Example Analytics Co., Ltd., Beijing, China (Jun 2023 - Dec 2023)"),
        _item("experience", "Data Analyst Intern, Wisers, Chengdu, China (Mar 2024 - May 2024)"),
    ]
    result = repo.reconcile_cv_facts(profile_id, new_document, new)
    active = [fact for fact in repo.facts() if fact.active]
    assert result == {"added": 1, "updated": 2, "unchanged": 0, "deactivated": 1, "duplicates": 0}
    assert len([fact for fact in active if fact.fact_type == "education"]) == 1
    assert len([fact for fact in active if fact.fact_type == "experience"]) == 2
    assert any("Wisers" in fact.value for fact in active)
    assert repo.connection.execute("SELECT active FROM profile_facts WHERE id=?", (manual_id,)).fetchone()[0] == 1


def test_materially_changed_employment_versions_the_same_core_identity(tmp_path: Path) -> None:
    repo, profile_id = _repo(tmp_path)
    first_document = repo.add_document("CV_Alex_Candidate.pdf", "one.pdf", "application/pdf", "one")
    second_document = repo.add_document("CV_Alex_Candidate.pdf", "two.pdf", "application/pdf", "two")
    repo.reconcile_cv_facts(
        profile_id, first_document,
        [_item("experience", "Data Analyst Intern | Wisers | Chengdu, China | Mar 2024 - May 2024")],
    )
    fact_id = next(fact.id for fact in repo.facts() if fact.fact_type == "experience")
    repo.reconcile_cv_facts(
        profile_id, second_document,
        [_item("experience", "Data Analyst Intern | Wisers | Chengdu, China | Mar 2024 - June 2024")],
    )
    current = next(fact for fact in repo.facts() if fact.fact_type == "experience")
    versions = repo.connection.execute(
        "SELECT status FROM profile_fact_versions WHERE fact_id=? ORDER BY id", (fact_id,)
    ).fetchall()
    assert current.id == fact_id
    assert "June 2024" in current.value
    assert len(versions) == 2


def test_repeated_same_cv_reconciliation_is_idempotent_and_keeps_provenance(tmp_path: Path) -> None:
    repo, profile_id = _repo(tmp_path)
    document = repo.add_document("CV_Alex_Candidate.pdf", "same.pdf", "application/pdf", "same")
    extracted = [_item("education", "Bachelor of Science in Statistics | Beijing Normal University | Beijing, China | Sept 2019 - June 2023")]
    repo.reconcile_cv_facts(profile_id, document, extracted)
    repeated = repo.reconcile_cv_facts(profile_id, document, extracted)
    fact = next(fact for fact in repo.facts() if fact.fact_type == "education")
    sources = repo.connection.execute(
        "SELECT source_document_id FROM profile_fact_sources WHERE fact_id=?", (fact.id,)
    ).fetchall()
    assert repeated == {"added": 0, "updated": 0, "unchanged": 1, "deactivated": 0, "duplicates": 0}
    assert [row[0] for row in sources] == [document]
    assert fact_core_identity(fact.fact_type, fact.value).startswith("education|")


def test_award_reclassified_from_project_to_certification_keeps_one_identity(tmp_path: Path) -> None:
    repo, profile_id = _repo(tmp_path)
    old_document = repo.add_document("CV_Alex_Candidate.pdf", "old.pdf", "application/pdf", "First Prize")
    new_document = repo.add_document("CV_Alex_Candidate.pdf", "new.pdf", "application/pdf", "First Prize")
    repo.reconcile_cv_facts(
        profile_id, old_document,
        [_item("project", "First Prize (Merit) | Mathematical Contest in Modeling (MCM/ICM) | Feb 2021")],
    )
    repo.reconcile_cv_facts(
        profile_id, new_document,
        [_item("certification", "First Prize (Merit) — Mathematical Contest in Modeling (MCM/ICM) (Feb 2021)")],
    )
    active = [fact for fact in repo.facts() if fact.active]
    assert len(active) == 1
    assert active[0].fact_type == "certification"
