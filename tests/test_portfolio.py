import ast
import json
from pathlib import Path

from job_agent.domain.models import ApplicationState, CandidateFact, JobInput
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.portfolio import demo_cases, select_demo_opportunity


def test_portfolio_metrics_are_persisted_counts_only(tmp_path) -> None:
    connection = connect(tmp_path / "agent.sqlite")
    migrate(connection)
    repo = JobRepository(connection)
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.test/job", "SQL"))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "test", "prepared")

    metrics = repo.portfolio_metrics()

    assert metrics["discovered"] == 1
    assert metrics["prepared"] == 1
    assert metrics["applied"] == 0


def test_demo_cases_do_not_include_candidate_profile_content(tmp_path) -> None:
    connection = connect(tmp_path / "agent.sqlite")
    migrate(connection)
    cases = demo_cases(JobRepository(connection))

    assert len(cases) == 3
    assert all(
        case.autonomy in {"AUTO", "ASSISTED", "HUMAN_REQUIRED", "NOT STARTED"}
        for case in cases
    )


def test_demo_opportunity_uses_only_current_direct_verified_evaluation() -> None:
    evaluation = {
        "deterministic_score": 70,
        "tailoring_plan": {"emphasise": [], "do_not_claim": []},
    }

    class ReadOnlyRepo:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int | None]] = []

        def review_queue(self):
            self.calls.append(("review_queue", None))
            return [
                {
                    "job_id": 1,
                    "application_id": 1,
                    "title": "Stale analyst role",
                    "company": "Example One",
                    "location": "Brisbane",
                    "source": "GREENHOUSE",
                    "application_source": "GREENHOUSE",
                    "score": 99,
                    "recommendation": "STRONG MATCH",
                    "resolution_status": "DIRECT_VERIFIED",
                    "evidence_quality": "HIGH",
                    "state": "REVIEW",
                },
                {
                    "job_id": 2,
                    "application_id": 2,
                    "title": "Current analyst role",
                    "company": "Example Two",
                    "location": "Brisbane",
                    "source": "LEVER",
                    "application_source": "LEVER",
                    "score": 70,
                    "recommendation": "GOOD MATCH",
                    "resolution_status": "DIRECT_VERIFIED",
                    "evidence_quality": "HIGH",
                    "state": "REVIEW",
                },
            ]

        def opportunity_evaluation_status(self, job_id):
            self.calls.append(("opportunity_evaluation_status", job_id))
            return {
                "stale": job_id == 1,
                "stored": {
                    "evaluation_json": json.dumps(evaluation),
                    "narrative_json": None,
                },
            }

        def facts(self):
            self.calls.append(("facts", None))
            return [CandidateFact("tool", "SQL", "Approved candidate fact", id=7)]

        def application_cv_is_outdated(self, _application_id):
            self.calls.append(("application_cv_is_outdated", None))
            return False

    repo = ReadOnlyRepo()
    demo = select_demo_opportunity(repo)

    assert demo is not None
    assert demo.job_id == 2
    assert all(name in {"review_queue", "opportunity_evaluation_status", "facts", "application_cv_is_outdated"} for name, _ in repo.calls)


def test_demo_evidence_ranking_prefers_relevant_experience_project_and_own_tool_value() -> None:
    evaluation = {
        "deterministic_score": 70,
        "matched_requirements": ["sql", "excel", "power bi"],
        "tailoring_plan": {"emphasise": [], "do_not_claim": []},
    }

    class DemoRepo:
        def review_queue(self):
            return [{
                "job_id": 2, "application_id": 2, "title": "Business Intelligence Analyst", "company": "Example",
                "location": "Brisbane", "source": "EMPLOYMENT_HERO",
                "application_source": "EMPLOYMENT_HERO", "score": 70,
                "recommendation": "GOOD MATCH", "resolution_status": "DIRECT_VERIFIED",
                "evidence_quality": "HIGH", "state": "REVIEW",
            }]

        def opportunity_evaluation_status(self, _job_id):
            return {"stale": False, "stored": {"evaluation_json": json.dumps(evaluation), "narrative_json": None}}

        def facts(self):
            return [
                CandidateFact("experience", "Data Analyst Intern, Example", "Approved experience", id=1),
                CandidateFact("experience", "Data Management Intern, Example", "Approved experience", id=2),
                CandidateFact("project", "Sales Analytics Dashboard — Power BI", "Approved project", id=3),
                CandidateFact("project", "NLP Research Project", "Approved project", id=4),
                CandidateFact("tool", "VBA", "Power BI, Excel, VBA", id=5),
                CandidateFact("tool", "Power BI", "Power BI, Excel, VBA", id=6),
                CandidateFact("education", "Master of Data Science", "Approved education", id=7),
                CandidateFact("programming_language", "SQL", "Approved SQL fact", id=8),
                CandidateFact("tool", "Excel", "Approved Excel fact", id=9),
            ]

        def application_cv_is_outdated(self, _application_id):
            return False

    demo = select_demo_opportunity(DemoRepo())

    assert [item.value for item in demo.strongest_evidence] == [
        "Data Analyst Intern, Example",
        "Sales Analytics Dashboard — Power BI",
        "Power BI",
    ]
    assert all("NLP" not in item.value and "Master" not in item.value for item in demo.strongest_evidence)
    assert [(item.requirement, item.evidence) for item in demo.requirement_mapping] == [
        ("sql", ("SQL",)),
        ("excel", ("Excel",)),
        ("power bi", ("Sales Analytics Dashboard — Power BI", "Power BI")),
    ]


def test_demo_branch_has_no_execution_controls_or_repository_mutations() -> None:
    """The portfolio page is a presentation view, never an application control surface."""
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app" / "Home.py").read_text())
    demo_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "page"
        and any(isinstance(operator, ast.Eq) for operator in node.test.ops)
        and any(isinstance(comparator, ast.Constant) and comparator.value == "Demo" for comparator in node.test.comparators)
    )
    demo_body = ast.Module(body=demo_branch.body, type_ignores=[])
    calls = [node for node in ast.walk(demo_body) if isinstance(node, ast.Call)]
    streamlit_controls = {
        node.func.attr
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
    }
    repository_calls = {
        node.func.attr
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "repo"
    }

    assert "button" not in streamlit_controls
    assert "form_submit_button" not in streamlit_controls
    assert repository_calls == {"portfolio_metrics"}
