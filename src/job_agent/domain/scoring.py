from __future__ import annotations

import re

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown, ScoreConfig
from job_agent.services.experience_matching import match_experience
from job_agent.services.job_search import location_matches, role_family_matches


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _matched(requirement: str, facts: list[CandidateFact]) -> bool:
    need = _norm(requirement)
    return any(
        need in _norm(f.value)
        or (len(_norm(f.value)) >= 3 and _norm(f.value) in need)
        for f in facts
        if f.active and f.approved
    )


def _component(requirements: tuple[str, ...], facts: list[CandidateFact], weight: int, name: str) -> tuple[int, str]:
    if not requirements:
        return 0, f"{name}: no explicit requirement found."
    hits = [r for r in requirements if _matched(r, facts)]
    return round(weight * len(hits) / len(requirements)), f"{name}: {len(hits)}/{len(requirements)} matched ({', '.join(hits) or 'none'})."


def score_job(requirements: JobRequirements, title: str, facts: list[CandidateFact], location: str | None, config: ScoreConfig) -> ScoreBreakdown:
    active = [f for f in facts if f.active and f.approved]
    # A CV can accurately classify Python as a language or a tool rather than a
    # generic skill.  All three are candidate-provided technical evidence.
    skills = [
        f
        for f in active
        if f.fact_type in {"skill", "tool", "programming_language"}
    ]
    education = [f for f in active if f.fact_type == "education"]
    work_rights = [f for f in active if f.fact_type == "work_right"]
    targets = [f for f in active if f.fact_type == "target_role"]
    locations = [f for f in active if f.fact_type == "preferred_location"]
    gates = [f"Work-right requirement not supported: {r}." for r in requirements.work_right_requirements if not _matched(r, work_rights)]
    gates += [f"Education requirement not supported: {r}." for r in requirements.education_requirements if not _matched(r, education)]
    required_locations = requirements.required_locations or ((location,) if location else ())
    location_values = [fact.value for fact in locations]
    if required_locations and location_values and not any(location_matches(required, location_values) for required in required_locations):
        gates.append(f"Location incompatible with preference: {', '.join(required_locations)}.")
    matched_skills = tuple(requirement for requirement in requirements.required_skills if _matched(requirement, skills))
    missing_skills = tuple(requirement for requirement in requirements.required_skills if requirement not in matched_skills)
    matched_preferred = tuple(requirement for requirement in requirements.preferred_skills if _matched(requirement, skills))
    missing_preferred = tuple(requirement for requirement in requirements.preferred_skills if requirement not in matched_preferred)
    skill, skill_reason = _component(requirements.required_skills, skills, config.skills_weight, "Skills")
    experience_match = match_experience(requirements.experience_requirements, active, config.experience_weight)
    exp, exp_reason = experience_match.points, experience_match.reason
    edu, edu_reason = _component(requirements.education_requirements, education, config.education_weight, "Education")
    role = config.role_weight if role_family_matches(title, [fact.value for fact in targets]) else 0
    loc = config.location_weight if required_locations and any(location_matches(required, location_values) for required in required_locations) else 0
    pref = config.preferences_weight if role else 0
    return ScoreBreakdown(skill, role, exp, edu, loc, pref, not gates, tuple(gates), (skill_reason, exp_reason, edu_reason, f"Role: {'matched' if role else 'not matched'}.", f"Location: {'matched' if loc else 'not matched'}."), matched_skills, missing_skills, matched_preferred, missing_preferred, experience_match.evidence)
