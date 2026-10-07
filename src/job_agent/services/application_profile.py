"""Explicit, reusable application details kept separate from CV-derived facts."""

from __future__ import annotations

import re

REUSABLE_FIELDS = {
    "legal_name",
    "preferred_name",
    "linkedin_url",
    "current_location",
    "authorized_to_work_in_australia",
    "visa_type",
    "visa_expiry",
    "sponsorship_required_now",
    "sponsorship_required_future",
    "notice_period",
    "earliest_start_date",
    "preferred_pronouns",
    "preferred_work_mode",
    "employment_status",
    "current_employer",
    "current_job_title",
}
SENSITIVE_FIELDS = {
    "indigenous_status",
    "disability_adjustment",
    "gender",
    "ethnicity_race",
    "veteran_status",
    "criminal_history",
}
FORBIDDEN_REUSABLE_FIELDS = {"privacy_consent", "terms_acceptance", "legal_declaration"}


def application_field_category(field_key: str) -> str:
    if field_key in SENSITIVE_FIELDS:
        return "sensitive"
    if field_key in FORBIDDEN_REUSABLE_FIELDS:
        return "legal"
    return "reusable"


def can_store_reusable(field_key: str, reusable: bool) -> bool:
    if field_key in FORBIDDEN_REUSABLE_FIELDS:
        return False
    if field_key in SENSITIVE_FIELDS:
        return bool(reusable)
    return True


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def is_australian_job(job_location: str | None) -> bool:
    location = _normalise(job_location or "")
    return any(place in location for place in ("australia", "queensland", "brisbane", "qld", "sydney", "melbourne", "perth", "adelaide"))


def application_profile_match(field, values: dict[str, dict], job_location: str | None) -> tuple[str, str] | None:
    """Return only high-confidence semantic equivalents; uncertainty stays manual."""
    description = _normalise(f"{field.label} {field.name}")
    if any(term in description for term in ("consent", "privacy", "terms", "declaration")):
        return None

    def value(key: str) -> str | None:
        item = values.get(key)
        if item and item["reusable"] and item["value"] not in (None, ""):
            return str(item["value"])
        return None

    direct = (
        ("linkedin", "linkedin_url"),
        ("notice period", "notice_period"),
        ("legal name", "legal_name"),
        ("full name", "legal_name"),
        ("preferred name", "preferred_name"),
        ("pronoun", "preferred_pronouns"),
        ("work mode", "preferred_work_mode"),
        ("visa type", "visa_type"),
        ("visa expiry", "visa_expiry"),
        ("visa expiration", "visa_expiry"),
        ("start date", "earliest_start_date"),
        ("availability", "earliest_start_date"),
        ("current location", "current_location"),
        ("aboriginal", "indigenous_status"),
        ("torres strait", "indigenous_status"),
        ("disability", "disability_adjustment"),
        ("medical adjustment", "disability_adjustment"),
        ("gender", "gender"),
        ("ethnicity", "ethnicity_race"),
        ("race", "ethnicity_race"),
        ("veteran", "veteran_status"),
        ("criminal history", "criminal_history"),
    )
    for phrase, key in direct:
        if phrase in description and (stored := value(key)):
            return stored, key
    if (
        is_australian_job(job_location)
        and "work" in description
        and any(term in description for term in ("authorised", "authorized", "right to work"))
        and (stored := value("authorized_to_work_in_australia"))
    ):
        return stored, "authorized_to_work_in_australia"
    if "require sponsorship" in description:
        if "now" in description and "future" not in description and (stored := value("sponsorship_required_now")):
            return stored, "sponsorship_required_now"
        if "future" in description and "now" not in description and (stored := value("sponsorship_required_future")):
            return stored, "sponsorship_required_future"
    return None
