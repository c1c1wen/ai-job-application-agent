"""Template-faithful DOCX export for candidate-facing cover letters."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from docx import Document


class CoverLetterExportError(ValueError):
    """Raised when the canonical template cannot safely accept a draft."""


def canonical_template_path() -> Path:
    return Path(__file__).resolve().parents[3] / "templates" / "cover_letter_template.docx"


def _letter_parts(letter: str) -> tuple[str | None, list[str], str | None, str | None]:
    paragraphs = [part.strip() for part in letter.replace("\r\n", "\n").split("\n\n") if part.strip()]
    # Candidate-entered letters often put the signature immediately after the
    # closing on the next line rather than after a blank paragraph.
    expanded: list[str] = []
    closings = {"kind regards", "yours sincerely", "yours faithfully", "sincerely"}
    for paragraph in paragraphs:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if lines and lines[0].casefold().rstrip(",") in closings:
            expanded.extend(lines)
        else:
            expanded.append(paragraph)
    paragraphs = expanded
    salutation = None
    if paragraphs and paragraphs[0].casefold().startswith(("dear ", "to whom")):
        salutation = paragraphs.pop(0)
    closing = next(
        (
            index
            for index, paragraph in enumerate(paragraphs)
            if paragraph.casefold().rstrip(",") in closings
        ),
        None,
    )
    closing_text = paragraphs[closing] if closing is not None else None
    signature = paragraphs[closing + 1] if closing is not None and len(paragraphs) > closing + 1 else None
    body = paragraphs[:closing] if closing is not None else paragraphs
    if not 3 <= len(body) <= 5:
        raise CoverLetterExportError("Cover letter must contain three to five body paragraphs for DOCX export.")
    return salutation, body, closing_text, signature


def _body_slots(document: Document) -> tuple[int, int, list]:
    paragraphs = document.paragraphs
    salutation = next(
        (index for index, paragraph in enumerate(paragraphs) if paragraph.text.strip().casefold().startswith(("dear ", "to whom"))),
        None,
    )
    if salutation is None:
        raise CoverLetterExportError("Canonical template is missing a salutation.")
    closing = next(
        (
            index
            for index, paragraph in enumerate(paragraphs[salutation + 1 :], salutation + 1)
            if paragraph.text.strip().casefold().rstrip(",")
            in {"kind regards", "yours sincerely", "yours faithfully", "sincerely"}
        ),
        None,
    )
    if closing is None or closing <= salutation + 1:
        raise CoverLetterExportError("Canonical template is missing a closing after its body.")
    return salutation, closing, paragraphs[salutation + 1 : closing]


def export_cover_letter_docx(letter: str, template_path: Path | None = None) -> bytes:
    """Fill only the template body, preserving its contact block and layout."""
    template = template_path or canonical_template_path()
    if not template.is_file():
        raise CoverLetterExportError("The canonical cover-letter template is unavailable.")

    document = Document(template)
    salutation_text, body, closing_text, signature_text = _letter_parts(letter)
    salutation, closing, slots = _body_slots(document)
    if len(body) > len(slots):
        raise CoverLetterExportError("Cover letter has more paragraphs than the canonical template supports.")

    for index, slot in enumerate(slots):
        slot.clear()
        if index < len(body):
            slot.add_run(body[index])
    if salutation_text:
        document.paragraphs[salutation].clear()
        document.paragraphs[salutation].add_run(salutation_text)
    if closing_text:
        document.paragraphs[closing].clear()
        document.paragraphs[closing].add_run(closing_text)
    if signature_text:
        signature = next(
            (paragraph for paragraph in document.paragraphs[closing + 1 :] if paragraph.text.strip()),
            None,
        )
        if signature is not None:
            signature.clear()
            signature.add_run(signature_text)

    output = BytesIO()
    document.save(output)
    return output.getvalue()
