from __future__ import annotations

import re

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown, ScoreConfig


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _matched(requirement: str, facts: list[CandidateFact]) -> bool:
    need = _norm(requirement)
    return any(need in _norm(f.value) or _norm(f.value) in need for f in facts if f.active and f.approved)


def _component(requirements: tuple[str, ...], facts: list[CandidateFact], weight: int, name: str) -> tuple[int, str]:
    if not requirements:
        return 0, f"{name}: no explicit requirement found."
    hits = [r for r in requirements if _matched(r, facts)]
    return round(weight * len(hits) / len(requirements)), f"{name}: {len(hits)}/{len(requirements)} matched ({', '.join(hits) or 'none'})."


def score_job(requirements: JobRequirements, title: str, facts: list[CandidateFact], location: str | None, config: ScoreConfig) -> ScoreBreakdown:
    active = [f for f in facts if f.active and f.approved]
    skills = [f for f in active if f.fact_type == "skill"]
    experience = [f for f in active if f.fact_type == "experience"]
    education = [f for f in active if f.fact_type == "education"]
    work_rights = [f for f in active if f.fact_type == "work_right"]
    targets = [f for f in active if f.fact_type == "target_role"]
    locations = [f for f in active if f.fact_type == "preferred_location"]
    gates = [f"Work-right requirement not supported: {r}." for r in requirements.work_right_requirements if not _matched(r, work_rights)]
    gates += [f"Education requirement not supported: {r}." for r in requirements.education_requirements if not _matched(r, education)]
    required_locations = requirements.required_locations or ((location,) if location else ())
    if required_locations and locations and not any(_matched(r, locations) for r in required_locations):
        gates.append(f"Location incompatible with preference: {', '.join(required_locations)}.")
    skill, skill_reason = _component(requirements.required_skills, skills, config.skills_weight, "Skills")
    exp, exp_reason = _component(requirements.experience_requirements, experience, config.experience_weight, "Experience")
    edu, edu_reason = _component(requirements.education_requirements, education, config.education_weight, "Education")
    role = config.role_weight if any(_norm(f.value) in _norm(title) or _norm(title) in _norm(f.value) for f in targets) else 0
    loc = config.location_weight if required_locations and any(_matched(r, locations) for r in required_locations) else 0
    pref = config.preferences_weight if role else 0
    return ScoreBreakdown(skill, role, exp, edu, loc, pref, not gates, tuple(gates), (skill_reason, exp_reason, edu_reason, f"Role: {'matched' if role else 'not matched'}.", f"Location: {'matched' if loc else 'not matched'}."))
