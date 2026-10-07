"""Candidate-facing summaries that keep current state separate from audit history."""

from __future__ import annotations


def current_cv_summary(application_cv) -> str | None:
    if not application_cv:
        return None
    return f"{application_cv['canonical_filename']} · current canonical version"


def normalise_autofill_summary(items: list[dict]) -> list[str]:
    """Collapse noisy ATS labels while retaining the saved selected value."""
    summary: list[str] = []
    seen: set[str] = set()
    for item in items:
        field = str(item.get("field") or "Field").strip()
        source = str(item.get("source") or "Candidate Profile")
        if "pronoun" in field.casefold():
            if "pronouns" in seen:
                continue
            value = str(item.get("value") or "").strip()
            if value:
                summary.append(f"Pronouns: {value} ({source})")
            seen.add("pronouns")
            continue
        key = field.casefold()
        if key not in seen:
            summary.append(f"{field} ({source})")
            seen.add(key)
    return summary
