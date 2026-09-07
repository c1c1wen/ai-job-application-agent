from __future__ import annotations

from io import BytesIO
from pathlib import Path


class CVParseError(ValueError): pass


def parse_cv(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.casefold()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)
        elif suffix == ".docx":
            from docx import Document
            text = "\n".join(p.text for p in Document(BytesIO(content)).paragraphs)
        else: raise CVParseError("Only PDF and DOCX files are supported.")
    except CVParseError: raise
    except Exception as exc: raise CVParseError("The CV could not be read.") from exc
    if not text.strip(): raise CVParseError("No readable text was found in the document.")
    return text.strip()
