from __future__ import annotations

from pathlib import Path
from uuid import uuid4


def store_upload(folder: Path, original_filename: str, content: bytes) -> Path:
    """Stores user documents using a non-colliding name while retaining the original name in metadata."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4()}{Path(original_filename).suffix.casefold()}"
    path.write_bytes(content)
    return path
