"""Keep the Streamlit Review Queue's repository calls aligned with its runtime class."""

from __future__ import annotations

import ast
from pathlib import Path

from job_agent.repositories.job_repository import JobRepository


def test_review_queue_repository_api_is_available_on_runtime_repository() -> None:
    """A missing method here would otherwise surface only while rendering Streamlit."""
    app = Path(__file__).resolve().parents[1] / "app" / "Home.py"
    tree = ast.parse(app.read_text())
    called_methods = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "repo"
    }

    missing = sorted(name for name in called_methods if not hasattr(JobRepository, name))

    assert not missing, f"Home.py calls repository APIs missing from JobRepository: {missing}"
    assert hasattr(JobRepository, "opportunity_evaluation_status")
    assert hasattr(JobRepository, "opportunity_evaluation_history")
