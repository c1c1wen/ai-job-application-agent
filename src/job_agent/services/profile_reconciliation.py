"""Conservative canonical identities for CV-derived profile facts.

The rendered text of a CV fact is intentionally not its identity.  CV updates
often change separators, punctuation, or month abbreviations without changing
the underlying degree or employment record.
"""

import re

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_COUNTRIES = {
    "australia", "china", "new zealand", "united states", "united kingdom",
    "singapore", "canada", "india", "japan", "germany", "france",
}
_MONTH_YEAR = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(19\d{2}|20\d{2})\b", re.IGNORECASE
)


def normalize_text(value: str) -> str:
    """Normalise punctuation and whitespace without applying fuzzy matching."""
    value = re.sub(r"(?<=\w)'s\b", "", value.casefold())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def normalized_date_range(value: str) -> str:
    matches = _MONTH_YEAR.findall(value)
    if len(matches) < 2:
        return ""
    dates = [f"{year}-{_MONTHS[month.casefold()]:02d}" for month, year in matches[:2]]
    return "/".join(dates)


def _body_without_dates(value: str) -> str:
    body = _MONTH_YEAR.sub("", value)
    return re.sub(r"[()\[\]]", "", body).strip(" ,|-–—")


def _parts(value: str) -> list[str]:
    body = _body_without_dates(value)
    if "|" in body:
        return [part.strip(" ,") for part in body.split("|") if part.strip(" ,")]
    return [part.strip(" ,") for part in body.split(",") if part.strip(" ,")]


def _education_fields(value: str) -> tuple[str, str, str, str]:
    parts = _parts(value)
    degree = normalize_text(parts[0]) if parts else ""
    institution = normalize_text(parts[1]) if len(parts) > 1 else ""
    location = normalize_text(", ".join(parts[2:])) if len(parts) > 2 else ""
    return degree, institution, location, normalized_date_range(value)


def _experience_fields(value: str) -> tuple[str, str, str, str]:
    parts = _parts(value)
    role = normalize_text(parts[0]) if parts else ""
    remainder = parts[1:]
    location_part_index = None
    for index, part in enumerate(remainder):
        normalized_part = normalize_text(part)
        if normalized_part in _COUNTRIES and index >= 1:
            location_part_index = index - 1
            break
        if any(normalized_part.endswith(f" {country}") for country in _COUNTRIES):
            location_part_index = index
            break
    if location_part_index is not None:
        employer = normalize_text(", ".join(remainder[:location_part_index]))
        location = normalize_text(", ".join(remainder[location_part_index:]))
        return role, employer, location, normalized_date_range(value)
    country_index = next(
        (index for index in range(len(remainder) - 1, -1, -1) if normalize_text(remainder[index]) in _COUNTRIES),
        None,
    )
    if country_index is not None and country_index >= 1:
        employer = normalize_text(", ".join(remainder[: country_index - 1]))
        location = normalize_text(", ".join(remainder[country_index - 1 : country_index + 1]))
    else:
        employer = normalize_text(", ".join(remainder))
        location = ""
    return role, employer, location, normalized_date_range(value)


def fact_core_identity(fact_type: str, value: str) -> str:
    """Stable identity excluding dates, used for safe material updates."""
    if fact_type == "education":
        degree, institution, location, _ = _education_fields(value)
        return f"{fact_type}|{degree}|{institution}|{location}"
    if fact_type == "experience":
        role, employer, location, _ = _experience_fields(value)
        return f"{fact_type}|{role}|{employer}|{location}"
    if fact_type in {"tool", "project", "certification"}:
        primary = re.split(r"\s*(?:\||—|\(|\s-\s)\s*", value, maxsplit=1)[0]
        normalized_primary = normalize_text(primary)
        is_credential = any(
            marker in normalized_primary
            for marker in ("prize", "award", "certification", "simulation", "certificate")
        )
        category = "credential" if fact_type in {"project", "certification"} and is_credential else fact_type
        return f"{category}|{normalized_primary}"
    return f"{fact_type}|{normalize_text(value)}"


def fact_identity(fact_type: str, value: str) -> str:
    """Canonical identity including dates where the fact has structured dates."""
    core = fact_core_identity(fact_type, value)
    if fact_type in {"education", "experience"}:
        return "|".join((core, normalized_date_range(value)))
    return core


def fact_supported_by_text(fact_type: str, value: str, source_text: str) -> bool:
    """Conservatively retain a prior fact when the replacement CV still states it.

    Structured extraction can omit an otherwise explicit skill or project.  An
    omission alone must not revoke the fact; removal requires the newer CV text
    itself to stop supporting its stable identifying fields.
    """
    text = normalize_text(source_text)
    if fact_type == "education":
        degree, institution, _, _ = _education_fields(value)
        return bool(degree and institution and degree in text and institution in text)
    if fact_type == "experience":
        role, employer, _, _ = _experience_fields(value)
        return bool(role and employer and role in text and employer in text)
    if fact_type in {"tool", "project", "certification"}:
        value = re.split(r"\s*(?:\||—|\(|\s-\s)\s*", value, maxsplit=1)[0]
    normal = normalize_text(value)
    if len(normal) <= 2:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(normal)}(?![a-z0-9])", text))
    return normal in text
