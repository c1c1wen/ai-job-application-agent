from __future__ import annotations

from job_agent.domain.models import CandidateFact


def grounded_materials(title: str, company: str, facts: list[CandidateFact]) -> dict[str, tuple[str, list[int]]]:
    """Safe V1 fallback. A future LLM provider receives only these approved facts."""
    approved = [fact for fact in facts if fact.approved and fact.active]
    skills = [fact.value for fact in approved if fact.fact_type == "skill"]
    education = [fact.value for fact in approved if fact.fact_type == "education"]
    experience = [fact.value for fact in approved if fact.fact_type == "experience"]
    fact_lines = "\n".join(f"- {fact.fact_type}: {fact.value} (evidence: {fact.evidence})" for fact in approved)
    letter = f"""Dear Hiring Team at {company},

I am applying for the {title} role. My approved profile supports the following relevant information: {', '.join(skills) or 'no approved skills recorded'}.

{('My education includes ' + ', '.join(education) + '. ') if education else ''}{('My experience includes ' + ', '.join(experience) + '. ') if experience else ''}I would welcome the opportunity to discuss how this documented background may support your team.

Kind regards,
[Candidate name]

Draft safety note: This draft contains only approved profile facts. Review and personalise it before use."""
    ids = [fact.id for fact in approved if fact.id is not None]
    return {"cover_letter": (letter, ids), "claim_ledger": (fact_lines or "No approved profile facts available. Do not generate or submit materials.", ids)}
