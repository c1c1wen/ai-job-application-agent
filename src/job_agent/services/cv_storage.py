from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from job_agent.config import get_settings


def store_upload(folder: Path, original_filename: str, content: bytes) -> Path:
    """Stores user documents using a non-colliding name while retaining the original name in metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}{Path(original_filename).suffix.casefold()}"
    path.write_bytes(content)
    return path


def replace_canonical_pdf(root: Path, content: bytes) -> Path:
    """Atomically replace the user-selected canonical PDF without touching history."""
    configured = get_settings().canonical_cv_path
    destination = configured if configured.is_absolute() else root / configured
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.upload")
    temporary.write_bytes(content)
    temporary.replace(destination)
    return destination
