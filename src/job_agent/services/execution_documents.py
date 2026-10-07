"""Materialise only user-approved documents for an execution session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shutil import copy2

from job_agent.services.cover_letter_docx import export_cover_letter_docx


@dataclass(frozen=True)
class ExecutionDocument:
    path: Path
    filename: str
    artifact_id: int | None
    artifact_version: int | None
    source_document_id: int | None


def prepare_execution_documents(approved_artifacts, candidate_cv, output_dir: Path) -> list[ExecutionDocument]:
    """Return an auditable upload set, never using an unapproved artifact version."""
    output_dir.mkdir(parents=True, exist_ok=True)
    documents: list[ExecutionDocument] = []
    if candidate_cv and Path(candidate_cv["stored_path"]).is_file():
        source = Path(candidate_cv["stored_path"])
        # Preserve the canonical filename in the actual browser file input.
        # The application-CV table retains the immutable per-application path.
        copied = output_dir / Path(candidate_cv["filename"]).name
        copy2(source, copied)
        documents.append(ExecutionDocument(copied, candidate_cv["filename"], None, None, candidate_cv["id"]))
    cover = next((artifact for artifact in approved_artifacts if artifact["kind"] == "cover_letter"), None)
    if cover:
        filename = f"approved-cover-letter-v{cover['version']}.docx"
        path = output_dir / filename
        path.write_bytes(export_cover_letter_docx(cover["content"]))
        documents.append(ExecutionDocument(path, filename, cover["id"], cover["version"], None))
    return documents
