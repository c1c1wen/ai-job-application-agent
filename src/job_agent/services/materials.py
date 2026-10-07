from __future__ import annotations

import re
from dataclasses import dataclass

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown, ScoreConfig


@dataclass(frozen=True)
class ApplicationRecommendation:
    label: str
    explanation: str


def application_recommendation(score: ScoreBreakdown, config: ScoreConfig) -> ApplicationRecommendation:
    if not score.eligibility_passed:
        return ApplicationRecommendation("SKIP", "Skip: a deterministic eligibility requirement is not supported by the documented profile.")
    if score.total >= config.good_match_threshold:
        gaps = f" Explicit gaps to review: {', '.join(score.missing_skills)}." if score.missing_skills else ""
        return ApplicationRecommendation("APPLY", f"Apply: the role passes eligibility and has a documented {score.total}% match.{gaps}")
    if score.total >= config.possible_match_threshold:
        return ApplicationRecommendation("CONSIDER", f"Consider: the role passes eligibility but has a {score.total}% documented match; review the stated gaps before preparing materials.")
    return ApplicationRecommendation("SKIP", f"Skip: the role is eligible but only has a {score.total}% documented match, so the evidence is currently too limited.")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _relevant_facts(facts: list[CandidateFact], requirements: JobRequirements) -> list[CandidateFact]:
    approved = [fact for fact in facts if fact.approved and fact.active]
    technical = set(requirements.required_skills + requirements.preferred_skills)
    if not technical:
        return approved
    selected = [fact for fact in approved if fact.fact_type in {"skill", "tool", "programming_language"} and any(_norm(fact.value) in _norm(requirement) or _norm(requirement) in _norm(fact.value) for requirement in technical)]
    selected += [fact for fact in approved if fact.fact_type in {"experience", "project", "education"}]
    return list({fact.id: fact for fact in selected if fact.id is not None}.values())


def grounded_materials(title: str, company: str, facts: list[CandidateFact], requirements: JobRequirements | None = None, score: ScoreBreakdown | None = None) -> dict[str, tuple[str, list[int]]]:
    """Create reviewable drafts only from approved facts and explicit job requirements."""
    requirements = requirements or JobRequirements()
    relevant = _relevant_facts(facts, requirements)
    ids = [fact.id for fact in relevant if fact.id is not None]
    skills = [fact.value for fact in relevant if fact.fact_type in {"skill", "tool", "programming_language"}]
    education = [fact.value for fact in relevant if fact.fact_type == "education"]
    experience = [fact.value for fact in relevant if fact.fact_type == "experience"]
    projects = [fact.value for fact in relevant if fact.fact_type == "project"]
    matched = list(score.matched_skills) if score else []
    missing = list(score.missing_skills) if score else []
    cv_suggestions = "\n".join(["Tailored CV suggestions (review before editing your CV):", *(f"- Move documented {fact.fact_type}: {fact.value} higher because it supports this role." for fact in relevant), *(f"- Emphasise the documented requirement match: {item}." for item in matched), *(f"- Do not claim {item}; it is an explicit job gap unless you add evidence separately." for item in missing), "- De-emphasise unrelated content only if it does not support the role's stated requirements."])
    letter = f"""Dear Hiring Team at {company},

I am applying for the {title} role. My approved profile documents relevant skills in {', '.join(skills) or 'areas that need manual confirmation'}.

{('My documented experience includes ' + '; '.join(experience) + '. ') if experience else ''}{('Relevant documented projects include ' + '; '.join(projects) + '. ') if projects else ''}{('My documented education includes ' + '; '.join(education) + '. ') if education else ''}I would welcome the opportunity to discuss how this documented background may support the role.

Kind regards,
[Candidate name]

Safety note: Review and personalise this draft. It contains no claims beyond approved profile facts."""
    answers = "\n\n".join(["Why are you interested in this role?\nThis role aligns with my documented skills in " + (", ".join(skills) or "relevant areas recorded in my profile") + ".", "Relevant experience\n" + ("My documented experience includes " + "; ".join(experience) + "." if experience else "No approved employment fact is available for this answer; complete it manually."), "Technical skills\n" + (", ".join(skills) if skills else "No approved technical fact is available for this answer; complete it manually."), "Work rights / availability\nAnswer manually unless an approved work-right or availability fact is present. Do not guess sensitive application answers."])
    ledger = "\n".join(f"- fact #{fact.id}: {fact.fact_type}: {fact.value}" for fact in relevant) or "No approved facts selected."
    return {"tailored_cv_suggestions": (cv_suggestions, ids), "cover_letter": (letter, ids), "application_answers": (answers, ids), "claim_ledger": (ledger, ids)}
