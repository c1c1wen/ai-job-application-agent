from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from job_agent.domain.models import CandidateFact


@dataclass(frozen=True)
class ExperienceMatch:
    points: int
    evidence: tuple[str, ...]
    reason: str


def _months(value: str) -> int | None:
    matches = re.findall(r"\b([A-Z][a-z]+)\s+(20\d{2})\b", value)
    if len(matches) < 2:
        return None
    try:
        month_names = {
            **{name.casefold(): number for number, name in enumerate(calendar.month_name) if name},
            **{name.casefold(): number for number, name in enumerate(calendar.month_abbr) if name},
        }

        def month_number(name: str) -> int:
            if name.casefold() not in month_names:
                raise ValueError(f"Unsupported month name: {name}")
            return month_names[name.casefold()]

        start = date(int(matches[0][1]), month_number(matches[0][0]), 1)
        end = date(int(matches[1][1]), month_number(matches[1][0]), 1)
    except ValueError:
        return None
    return max(0, (end.year - start.year) * 12 + end.month - start.month)


def _minimum_months(requirements: tuple[str, ...]) -> int | None:
    numbers = []
    for requirement in requirements:
        match = re.search(r"\b(\d+)(?:\s*-\s*(\d+))?\+?\s+years?", requirement.casefold())
        if match:
            numbers.append(int(match.group(1)) * 12)
    return min(numbers) if numbers else None


def match_experience(
    requirements: tuple[str, ...], facts: list[CandidateFact], weight: int
) -> ExperienceMatch:
    active = [fact for fact in facts if fact.active and fact.approved]
    experience = [fact for fact in active if fact.fact_type == "experience"]
    projects = [fact for fact in active if fact.fact_type == "project"]
    required_months = _minimum_months(requirements)
    if not requirements:
        return ExperienceMatch(0, (), "Experience: no explicit requirement found.")
    if required_months is None:
        hits = [fact.value for fact in experience if any(term in fact.value.casefold() for term in ("data", "analytic", "business intelligence", "reporting", "ai"))]
        return ExperienceMatch(round(weight * len(hits) / len(requirements)), tuple(hits), f"Experience: {len(hits)}/{len(requirements)} relevant approved employment facts matched.")

    internships = [fact for fact in experience if "intern" in fact.value.casefold()]
    professional = [fact for fact in experience if fact not in internships]
    professional_months = sum(_months(fact.value) or 0 for fact in professional)
    internship_months = sum(_months(fact.value) or 0 for fact in internships)
    relevant_internships = [fact for fact in internships if "data" in fact.value.casefold() or "analytic" in fact.value.casefold()]
    relevant_projects = [fact for fact in projects if any(term in fact.value.casefold() for term in ("data", "ai", "machine learning", "statistical"))]
    evidence = tuple(fact.value for fact in relevant_internships + relevant_projects)
    if professional_months >= required_months:
        return ExperienceMatch(weight, tuple(fact.value for fact in professional), f"Experience: documented professional duration meets the {required_months // 12}-year minimum.")
    if not evidence:
        return ExperienceMatch(0, (), "Experience: no approved dated professional, internship, or relevant project evidence supports the explicit duration.")
    internship_ratio = min(internship_months / required_months, 0.5)
    project_support = 0.15 if relevant_projects else 0
    fraction = min(0.65, internship_ratio + project_support)
    points = round(weight * fraction)
    return ExperienceMatch(points, evidence, f"Experience: partial credit from documented internship/project evidence; no full-duration professional claim inferred for the {required_months // 12}-year minimum.")
