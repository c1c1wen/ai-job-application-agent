"""Canonical CV discovery, versioning, and per-application copies."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from shutil import copy2

from job_agent.config import get_settings
from job_agent.services.cv_parser import parse_cv


@dataclass(frozen=True)
class CanonicalCVStatus:
    canonical: object
    application_cv: object | None
    outdated: bool


def canonical_cv_path(root: Path) -> Path:
    configured = get_settings().canonical_cv_path
    return configured if configured.is_absolute() else root / configured


def _existing_canonical_source(repo, root: Path) -> Path | None:
    """Preserve an existing local canonical source without renaming it."""
    current = repo.current_canonical_cv()
    if current:
        registered = Path(current["source_path"])
        if registered.is_file():
            return registered

    candidates = sorted(
        (root / "candidate_documents").glob("*.pdf"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    return candidates[0] if len(candidates) == 1 else None


def register_canonical_cv(repo, root: Path):
    """Register the current source file once, without changing profile facts."""
    path = canonical_cv_path(root)
    if not path.is_file():
        path = _existing_canonical_source(repo, root) or path
    if not path.is_file():
        raise FileNotFoundError("Canonical CV is unavailable at the configured canonical CV path.")
    content = path.read_bytes()
    text = parse_cv(path.name, content)
    stat = path.stat()
    return repo.register_canonical_cv(
        str(path), path.name, hashlib.sha256(content).hexdigest(), stat.st_mtime_ns, stat.st_size, text
    )


def application_cv_status(repo, root: Path, application_id: int) -> CanonicalCVStatus:
    canonical = register_canonical_cv(repo, root)
    attachment = repo.application_cv(application_id)
    return CanonicalCVStatus(canonical, attachment, repo.application_cv_is_outdated(application_id, canonical))


def use_current_canonical_cv(repo, root: Path, application_id: int, *, explicit_refresh: bool) -> object:
    """Make an auditable local copy, never replacing a submitted application's CV."""
    status = application_cv_status(repo, root, application_id)
    if status.application_cv and not status.outdated:
        return status.application_cv
    if status.application_cv and status.outdated and not explicit_refresh:
        raise ValueError("A newer canonical CV is available. Update the application CV explicitly before continuing.")
    canonical = status.canonical
    source = Path(canonical["source_path"])
    target_dir = root / "data" / "artifacts" / "application_cvs" / str(application_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"cv-{canonical['id']}-{canonical['sha256'][:12]}{source.suffix.casefold()}"
    if not destination.exists():
        copy2(source, destination)
    repo.attach_application_cv(
        application_id,
        canonical["id"],
        canonical["source_document_id"],
        canonical["filename"],
        canonical["sha256"],
        str(destination),
    )
    return repo.application_cv(application_id)
