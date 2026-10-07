"""Deterministic application-history duplicate checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit


def normalise(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


COMPANY_GROUPS = {
    "dreamworld": "ardent leisure group",
    "ardent leisure group": "ardent leisure group",
}


def canonical_company(value: str | None) -> str:
    company = normalise(value)
    return COMPANY_GROUPS.get(company, company)


def canonical_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    path = parsed.path.rstrip("/")
    if path.casefold().endswith("/apply"):
        path = path[:-6]
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, "", ""))


@dataclass(frozen=True)
class DuplicateDecision:
    status: str
    reason: str
    history_id: int | None = None


def detect_duplicate(candidate: dict, history: list[dict]) -> DuplicateDecision:
    """Use identifiers/URLs first; never use title similarity across employers."""
    candidate_url = canonical_url(candidate.get("url"))
    candidate_id = str(candidate.get("external_id") or "").strip()
    company, title, location = (canonical_company(candidate.get("company")), normalise(candidate.get("title")), normalise(candidate.get("location")))
    for item in history:
        history_id = item.get("id")
        if candidate_id and candidate_id == str(item.get("job_external_id") or "").strip():
            return DuplicateDecision("ALREADY_APPLIED" if item.get("method") == "MANUAL" else "DUPLICATE_CONFIRMED", "Exact job/requisition ID match", history_id)
        if candidate_url and candidate_url == canonical_url(item.get("application_url") or item.get("url")):
            return DuplicateDecision("ALREADY_APPLIED" if item.get("method") == "MANUAL" else "DUPLICATE_CONFIRMED", "Exact canonical application URL match", history_id)
    for item in history:
        if company != canonical_company(item.get("company")):
            continue
        same_title = title == normalise(item.get("role_title") or item.get("title"))
        same_location = bool(location) and location == normalise(item.get("location"))
        if same_title and same_location:
            return DuplicateDecision("ALREADY_APPLIED" if item.get("method") == "MANUAL" else "DUPLICATE_CONFIRMED", "Same company, normalized role title, and location", item.get("id"))
        similarity = SequenceMatcher(None, title, normalise(item.get("role_title") or item.get("title"))).ratio()
        if similarity >= 0.85 and (same_location or not location or not normalise(item.get("location"))):
            return DuplicateDecision("DUPLICATE_POSSIBLE", "Same company with highly similar role title; review required", item.get("id"))
    return DuplicateDecision("CLEAR", "No duplicate evidence")
