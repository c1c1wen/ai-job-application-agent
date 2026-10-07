"""Read-only, privacy-conscious helpers for the portfolio demo."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DemoCase:
    title: str
    company: str
    match: str
    ats: str
    autonomy: str
    outcome: str
    explanation: str
    data_label: str


@dataclass(frozen=True)
class DemoOpportunity:
    """A current, persisted evaluation safe to present in the portfolio view."""

    job_id: int
    title: str
    company: str
    location: str | None
    source: str
    evidence_quality: str
    score: int
    recommendation: str
    package_prepared: bool
    evaluation: dict
    narrative: dict | None
    strongest_evidence: tuple[DemoEvidence, ...]
    requirement_mapping: tuple[RequirementEvidence, ...]
    tailoring_actions: tuple[str, ...]
    application_angle: str
    why_it_fits: str


@dataclass(frozen=True)
class DemoEvidence:
    """A concise, presentation-only view of one approved candidate fact."""

    fact_id: int
    fact_type: str
    value: str
    why: str
    supports: tuple[str, ...]


@dataclass(frozen=True)
class RequirementEvidence:
    requirement: str
    evidence: tuple[str, ...]
    result: str = "Matched"


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _role_terms(title: str) -> set[str]:
    ignored = {"and", "the", "for", "with", "role", "position"}
    return {
        term for term in re.findall(r"[a-z0-9]+", title.casefold())
        if len(term) > 2 and term not in ignored
    }


def _supports_for_fact(fact, requirements: tuple[str, ...], role_terms: set[str]) -> tuple[tuple[str, ...], int]:
    # Technical fact evidence can be a shared CV section containing several
    # neighbouring tools. Rank the fact's own value in that case, otherwise a
    # generic source line could incorrectly make (for example) VBA look like
    # the strongest Power BI capability.
    support_text = fact.value if fact.fact_type in {"tool", "programming_language", "skill"} else f"{fact.value} {fact.evidence}"
    blob = support_text.casefold()
    compact_blob = _normalise(blob)
    supported = tuple(
        requirement
        for requirement in requirements
        if _normalise(requirement) and _normalise(requirement) in compact_blob
    )
    role_matches = sum(term in blob for term in role_terms)
    return supported, (len(supported) * 100) + (role_matches * 15)


def _evidence_copy(fact_type: str, supports: tuple[str, ...]) -> str:
    if fact_type == "experience":
        return "Relevant professional or internship experience for this role family."
    if fact_type == "project":
        return "A directly relevant project aligned with the role's analytical focus."
    if fact_type in {"tool", "programming_language", "skill"}:
        return "An explicit approved capability that matches the role's stated needs."
    if fact_type == "education":
        return "Education that supports the analytical foundation for the role."
    return "Approved candidate evidence relevant to this opportunity."


def _rank_demo_evidence(repo, evaluation: dict, title: str) -> tuple[DemoEvidence, ...]:
    """Select at most three relevant facts without changing the stored evaluation."""
    requirements = tuple(str(item) for item in evaluation.get("matched_requirements", ()))
    role_terms = _role_terms(title)
    category_order = ("experience", "project", "tool", "programming_language", "skill", "education")
    category_rank = {category: len(category_order) - index for index, category in enumerate(category_order)}
    candidates: list[tuple[str, int, DemoEvidence]] = []
    for fact in repo.facts():
        if not fact.approved or not fact.active or fact.fact_type not in category_rank:
            continue
        supports, relevance = _supports_for_fact(fact, requirements, role_terms)
        if not supports and not relevance:
            continue
        candidates.append(
            (
                fact.fact_type,
                relevance,
                DemoEvidence(
                    fact_id=fact.id or 0,
                    fact_type=fact.fact_type,
                    value=fact.value,
                    why=_evidence_copy(fact.fact_type, supports),
                    supports=supports or ("Role relevance",),
                ),
            )
        )

    selected: list[DemoEvidence] = []
    # Prefer one high-relevance item from each evidence category, so the brief
    # does not become a repeated list of technical skills or degrees.
    for category in category_order:
        matching = [item for item in candidates if item[0] == category]
        if matching:
            selected.append(max(matching, key=lambda item: (item[1], item[2].value.casefold()))[2])
        if len(selected) == 3:
            return tuple(selected)
    remaining = [item for item in candidates if item[2] not in selected]
    remaining.sort(key=lambda item: (item[1], category_rank[item[0]], item[2].value.casefold()), reverse=True)
    selected.extend(item[2] for item in remaining[: 3 - len(selected)])
    return tuple(selected[:3])


def _tailoring_actions(evidence: tuple[DemoEvidence, ...], requirements: tuple[str, ...]) -> tuple[str, ...]:
    actions: list[str] = []
    types = {item.fact_type for item in evidence}
    if "experience" in types:
        actions.append("Lead with the directly relevant internship or work experience before broader education.")
    if "project" in types:
        actions.append("Use the most relevant project to demonstrate practical analytical work, not just tool names.")
    if requirements:
        actions.append(
            "Make the connection to " + ", ".join(requirements[:3]) + " explicit in the CV and cover letter."
        )
    if "education" not in types:
        actions.append("Keep education as concise supporting context rather than the lead claim.")
    return tuple(dict.fromkeys(actions))[:3]


def _application_angle(requirements: tuple[str, ...], evidence: tuple[DemoEvidence, ...]) -> str:
    requirement_text = ", ".join(requirements[:3]) or "the stated role requirements"
    if any(item.fact_type in {"experience", "project"} for item in evidence):
        return (
            "Position the candidate around the directly relevant experience and project evidence, "
            f"then connect it clearly to {requirement_text}."
        )
    return f"Position the candidate around approved analytical evidence that aligns with {requirement_text}."


def _requirement_evidence_map(repo, requirements: tuple[str, ...]) -> tuple[RequirementEvidence, ...]:
    """Map only approved active fact values to requirements already scored as matched."""
    priority = {"experience": 0, "project": 1, "tool": 2, "programming_language": 3, "skill": 4}
    facts = [fact for fact in repo.facts() if fact.approved and fact.active]
    mappings: list[RequirementEvidence] = []
    for requirement in requirements:
        matches = []
        for fact in facts:
            supports, _ = _supports_for_fact(fact, (requirement,), set())
            if supports:
                matches.append(fact)
        matches.sort(
            key=lambda fact: (
                fact.fact_type not in {"experience", "project"},
                _normalise(fact.value) != _normalise(requirement),
                priority.get(fact.fact_type, 9),
                fact.value.casefold(),
            )
        )
        if matches:
            mappings.append(
                RequirementEvidence(
                    requirement=requirement,
                    evidence=tuple(fact.value for fact in matches[:2]),
                )
            )
    return tuple(mappings)


def select_demo_opportunity(repo) -> DemoOpportunity | None:
    """Choose the strongest directly verified opportunity with current evidence.

    This is deliberately read-only. Stale briefs are excluded rather than
    re-evaluated, so a portfolio view can never imply that superseded candidate
    facts or an old canonical CV are current.
    """
    candidates: list[DemoOpportunity] = []
    for row in repo.review_queue():
        if row["resolution_status"] != "DIRECT_VERIFIED":
            continue
        status = repo.opportunity_evaluation_status(row["job_id"])
        stored = status["stored"]
        if not stored or status["stale"]:
            continue
        evaluation = json.loads(stored["evaluation_json"])
        narrative = json.loads(stored["narrative_json"]) if stored["narrative_json"] else None
        evidence = _rank_demo_evidence(repo, evaluation, row["title"])
        requirements = tuple(str(item) for item in evaluation.get("matched_requirements", ()))
        deterministic_summary = (
            f"This direct-verified role has a {evaluation['deterministic_score']}% deterministic match "
            f"based on approved evidence aligned with {', '.join(requirements[:3]) or 'the stated requirements'}."
        )
        candidates.append(
            DemoOpportunity(
                job_id=row["job_id"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                source=row["application_source"] or row["source"],
                evidence_quality=row["evidence_quality"],
                score=int(row["score"]),
                recommendation=row["recommendation"],
                package_prepared=(
                    row["state"] in {"MATERIALS_READY", "APPROVED", "READY_TO_APPLY", "APPLYING", "HUMAN_VERIFICATION_REQUIRED"}
                    and not repo.application_cv_is_outdated(row["application_id"])
                ),
                evaluation=evaluation,
                narrative=narrative,
                strongest_evidence=evidence,
                requirement_mapping=_requirement_evidence_map(repo, requirements),
                tailoring_actions=_tailoring_actions(evidence, requirements),
                application_angle=_application_angle(requirements, evidence),
                why_it_fits=(narrative["fit_summary"]["text"] if narrative else deterministic_summary),
            )
        )
    return max(candidates, key=lambda item: item.score) if candidates else None


def demo_cases(repo) -> tuple[DemoCase, ...]:
    """Build a demo that works offline and never reads candidate content."""
    coact_applied = repo.portfolio_case_state("CoAct", "Data Analyst") == "APPLIED"
    moreton_bay = repo.portfolio_case_score("City of Moreton Bay", "ePMO Systems and Reporting Analyst")
    rejected = repo.portfolio_filtered_case()
    filtered_title = rejected["title"] if rejected else "Senior analytics leadership opportunity"
    filtered_company = rejected["company"] if rejected else "Stored public vacancy"

    return (
        DemoCase(
            "Data Analyst",
            "CoAct",
            "Possible match",
            "Employment Hero",
            "AUTO",
            "APPLIED — verified" if coact_applied else "Recorded autonomous scenario",
            "Direct employer vacancy verified; grounded materials and safe browser execution completed before success was independently confirmed.",
            "HISTORICAL LIVE RECORD" if coact_applied else "DEMO SCENARIO",
        ),
        DemoCase(
            "ePMO Systems and Reporting Analyst",
            "City of Moreton Bay",
            f"{moreton_bay}% Good Match" if moreton_bay is not None else "70% Good Match",
            "PageUp",
            "ASSISTED",
            "Human handoff",
            "The direct vacancy and grounded materials were prepared, then PageUp's stateful workflow crossed the reliability threshold for assisted completion.",
            "HISTORICAL LIVE RECORD" if moreton_bay is not None else "DEMO SCENARIO",
        ),
        DemoCase(
            filtered_title,
            filtered_company,
            "Ineligible",
            "Public job source",
            "NOT STARTED",
            "Filtered before AI or execution",
            "Deterministic seniority and experience gates rejected an incompatible role, avoiding unnecessary model calls and low-quality applications.",
            "HISTORICAL FILTER" if rejected else "DEMO SCENARIO",
        ),
    )
