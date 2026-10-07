"""Explainable opportunity evaluation built on the authoritative score.

This module deliberately does not calculate or modify a score.  It turns an
already-screened, direct vacancy into a compact decision brief and a grounded
tailoring plan for human review.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown, ScoreConfig
from job_agent.services.materials import application_recommendation


@dataclass(frozen=True)
class EvidenceItem:
    fact_id: int
    fact_type: str
    value: str
    source: str


@dataclass(frozen=True)
class TailoringPlan:
    emphasise: tuple[EvidenceItem, ...]
    de_emphasise: tuple[EvidenceItem, ...]
    do_not_claim: tuple[str, ...]
    application_angle: str


@dataclass(frozen=True)
class OpportunityEvaluation:
    job_id: int
    deterministic_score: int
    deterministic_recommendation: str
    direct_source_status: str
    eligibility_status: str
    matched_requirements: tuple[str, ...]
    missing_requirements: tuple[str, ...]
    strongest_candidate_evidence: tuple[EvidenceItem, ...]
    experience_evidence: tuple[str, ...]
    evidence_quality: str
    next_recommended_action: str
    tailoring_plan: TailoringPlan

    def as_dict(self) -> dict:
        return asdict(self)


def _normal(value: str) -> str:
    return " ".join(value.casefold().split())


def _matches_requirement(fact: CandidateFact, requirement: str) -> bool:
    value, need = _normal(fact.value), _normal(requirement)
    return bool(value and need and (need in value or (len(value) >= 3 and value in need)))


def _evidence_items(
    facts: list[CandidateFact], requirements: JobRequirements, score: ScoreBreakdown
) -> tuple[EvidenceItem, ...]:
    matched = (*score.matched_skills, *score.matched_preferred_skills)
    selected: list[CandidateFact] = []
    for fact in facts:
        if not (fact.active and fact.approved and fact.id is not None):
            continue
        if fact.fact_type in {"experience", "project", "education"} or any(
            _matches_requirement(fact, requirement) for requirement in matched
        ):
            selected.append(fact)
    # Preserve fact order while avoiding duplicated evidence from overlapping
    # requirement terms. A short brief is more useful than a CV dump.
    distinct = {fact.id: fact for fact in selected}
    return tuple(
        EvidenceItem(fact.id, fact.fact_type, fact.value, fact.evidence)
        for fact in list(distinct.values())[:6]
    )


def build_opportunity_evaluation(
    *,
    job_id: int,
    title: str,
    score: ScoreBreakdown,
    config: ScoreConfig,
    requirements: JobRequirements,
    facts: list[CandidateFact],
    direct_source_status: str,
    eligible: bool,
    evidence_quality: str,
) -> OpportunityEvaluation:
    """Build a brief only for a verified, eligible opportunity.

    The caller supplies the persisted score and eligibility result so there is
    no parallel scoring path that could disagree with the application queue.
    """
    if direct_source_status != "DIRECT_VERIFIED" or not eligible:
        raise ValueError("Only directly verified, eligible opportunities can receive an evaluation brief.")

    recommendation = application_recommendation(score, config)
    evidence = _evidence_items(facts, requirements, score)
    selected_ids = {item.fact_id for item in evidence}
    approved = [fact for fact in facts if fact.active and fact.approved and fact.id is not None]
    de_emphasise = tuple(
        EvidenceItem(fact.id, fact.fact_type, fact.value, fact.evidence)
        for fact in approved
        if fact.id not in selected_ids
        and fact.fact_type in {"skill", "tool", "programming_language", "project"}
    )[:4]
    gaps = tuple(dict.fromkeys((*score.missing_skills, *score.missing_preferred_skills)))
    matched = tuple(dict.fromkeys((*score.matched_skills, *score.matched_preferred_skills)))
    evidence_names = ", ".join(item.value for item in evidence[:2]) or "the strongest relevant profile evidence"
    requirements_text = ", ".join(matched[:3]) or "the role's stated analytical requirements"
    angle = (
        f"Position your application for {title} around {evidence_names}, "
        f"connecting it directly to {requirements_text}."
    )
    return OpportunityEvaluation(
        job_id=job_id,
        deterministic_score=score.total,
        deterministic_recommendation=recommendation.label,
        direct_source_status=direct_source_status,
        eligibility_status="ELIGIBLE",
        matched_requirements=matched,
        missing_requirements=gaps,
        strongest_candidate_evidence=evidence,
        experience_evidence=score.experience_evidence,
        evidence_quality=evidence_quality,
        next_recommended_action=(
            "Prepare a grounded application package"
            if recommendation.label in {"APPLY", "CONSIDER"}
            else "Save or skip after reviewing the stated gaps"
        ),
        tailoring_plan=TailoringPlan(evidence, de_emphasise, gaps, angle),
    )
