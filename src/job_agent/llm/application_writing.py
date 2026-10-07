from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from openai import OpenAIError

from job_agent.domain.models import CandidateFact, JobRequirements, ScoreBreakdown
from job_agent.llm.client import create_openai_client


class ApplicationWritingError(RuntimeError):
    pass


INTERNAL_LANGUAGE = (
    "approved profile", "documented facts", "fact ids", "manual completion",
    "unless evidence is provided", "claim ledger", "safety note", "approved facts",
    "candidate evidence", "currently enrolled", "currently undertaking", "completing a master",
    "currently pursuing", "currently completing",
)


@dataclass(frozen=True)
class GeneratedApplication:
    artifacts: dict[str, str]
    fact_ids: list[int]
    claim_ledger: str


def _contains(value: str, text: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(value.casefold()) + r"(?!\w)", text.casefold()))


def _asserts_prior_candidate_experience(text: str, phrase: str) -> bool:
    """Return True only for language that attributes a JD term to the candidate.

    A role can legitimately be discussed as an interest or development area.
    This guard is deliberately narrower: it stops an extracted JD requirement
    from being rewritten as something the candidate has previously done.
    """
    escaped = re.escape(phrase.casefold())
    experience_markers = r"(?:i|my|during|have|has|having|performed|conducted|led|built|developed|analysed|analyzed|worked|experience)"
    value = text.casefold()
    return bool(
        re.search(rf"{experience_markers}.{{0,100}}{escaped}", value)
        or re.search(rf"{escaped}.{{0,100}}(?:experience|internship|work|project|stud(?:y|ies)|have|performed|conducted)", value)
    )


def _jd_only_terms(requirements: JobRequirements, job_description: str) -> tuple[str, ...]:
    """Keep only explicit, sufficiently specific requirements from a JD."""
    explicit = (*requirements.required_skills, *requirements.preferred_skills)
    detected = re.findall(r"\b(?:cohort|funnel)\s+analys(?:is|es)\b", job_description.casefold())
    return tuple(dict.fromkeys(term.strip().casefold() for term in (*explicit, *detected) if len(term.strip()) >= 5))


def select_evidence(facts: list[CandidateFact], requirements: JobRequirements, title: str, job_description: str = "") -> list[CandidateFact]:
    active = [fact for fact in facts if fact.approved and fact.active and fact.id is not None]
    terms = " ".join((title, job_description, *requirements.required_skills, *requirements.preferred_skills)).casefold()
    technical = [fact for fact in active if fact.fact_type in {"skill", "tool", "programming_language"} and len(fact.value.strip()) > 1 and _contains(fact.value, terms)]
    professional = [fact for fact in active if fact.fact_type == "experience" and any(term in fact.value.casefold() for term in ("data", "analytic", "report", "business intelligence"))]
    projects = [fact for fact in active if fact.fact_type == "project" and any(term in terms for term in ("ai", "machine learning", "data scientist")) and any(term in fact.value.casefold() for term in ("ai", "machine learning", "data"))]
    education = [fact for fact in active if fact.fact_type == "education" and any(term in terms for term in ("data", "analytic", "science"))]
    selected = technical + professional + projects + education
    return list({fact.id: fact for fact in selected}.values())


def _schema(fact_ids: list[int]) -> dict:
    claim = {"type": "object", "properties": {"artifact": {"type": "string", "enum": ["cover_letter", "application_answers", "tailored_cv_suggestions"]}, "claim": {"type": "string"}, "fact_ids": {"type": "array", "items": {"type": "integer", "enum": fact_ids}}}, "required": ["artifact", "claim", "fact_ids"], "additionalProperties": False}
    return {"type": "object", "properties": {"cover_letter": {"type": "string"}, "why_interested": {"type": "string"}, "relevant_experience": {"type": "string"}, "technical_skills": {"type": "string"}, "cv_suggestions": {"type": "array", "items": {"type": "string"}, "maxItems": 4}, "claims": {"type": "array", "items": claim, "maxItems": 8}}, "required": ["cover_letter", "why_interested", "relevant_experience", "technical_skills", "cv_suggestions", "claims"], "additionalProperties": False}


class GroundedApplicationWriter:
    def __init__(self, api_key: str, model: str):
        self.client = create_openai_client(api_key)
        self.model = model

    def generate(self, title: str, company: str, requirements: JobRequirements, score: ScoreBreakdown, facts: list[CandidateFact], job_description: str = "") -> GeneratedApplication:
        selected = select_evidence(facts, requirements, title, job_description)
        if not selected:
            raise ApplicationWritingError("No relevant approved facts are available for a grounded application draft.")
        fact_ids = [fact.id for fact in selected if fact.id is not None]
        packet = {"job": {"title": title, "company": company, "responsibilities": [line for line in job_description.split("\n") if line.strip()][:8], "required_skills": list(requirements.required_skills), "preferred_skills": list(requirements.preferred_skills), "experience_requirements": list(requirements.experience_requirements), "matched_requirements": list(score.matched_skills), "candidate_gaps": list(score.missing_skills)}, "candidate_evidence": [self._fact_packet(fact) for fact in selected]}
        prompt = ("Write polished Australian-market application materials for an early-career candidate. Use ONLY the candidate evidence supplied. You may select, rephrase and connect it naturally, but never invent dates, metrics, responsibilities, tools, qualifications, years of experience, company facts, or achievements. Respect each evidence item's employment_or_education_status exactly: never present completed study as current, or past work as current. Do not mention profiles, evidence, IDs, compliance, validation, gaps, safety, work rights, availability, or any internal process in candidate-facing prose. Omit unsupported or sensitive topics.\n\nThe canonical cover-letter template supplies the contact block, salutation, closing and signature. Generate only the body: 250-350 words in exactly five short paragraphs separated by blank lines. Use a natural, confident Australian professional tone. Start with a direct, role-specific introduction; then select the strongest relevant evidence, connect it to this role's stated responsibilities and skills, and close with concise interest in contributing. Do not list every skill, repeat the full CV chronology, use database/audit language, or make unsupported claims about the company. Make at most four actionable CV suggestions. Keep each application answer under 90 words. Emit at most eight factual candidate claims, each citing one or more evidence IDs. Do not emit unsupported claims.\n\nEvidence packet:\n" + json.dumps(packet))
        try:
            response = self.client.responses.create(model=self.model, input=prompt, max_output_tokens=2600, reasoning={"effort": "low"}, text={"format": {"type": "json_schema", "name": "grounded_application_materials", "strict": True, "schema": _schema(fact_ids)}})
            payload = json.loads(response.output_text)
        except (OpenAIError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ApplicationWritingError("Grounded application writing was unavailable.") from exc
        ledger = self._validate(payload, set(fact_ids), facts, requirements, job_description)
        if "UNSUPPORTED" in ledger or "NEEDS_USER_CONFIRMATION" in ledger:
            raise ApplicationWritingError("Generated draft contained ungrounded claims and was not saved.")
        answers = "\n\n".join(("Why are you interested in this role?\n" + payload["why_interested"], "Relevant experience\n" + payload["relevant_experience"], "Technical skills\n" + payload["technical_skills"]))
        suggestions = "\n".join("- " + suggestion for suggestion in payload["cv_suggestions"])
        return GeneratedApplication({"cover_letter": payload["cover_letter"], "application_answers": answers, "tailored_cv_suggestions": suggestions, "claim_ledger": ledger}, fact_ids, ledger)

    @staticmethod
    def _fact_packet(fact: CandidateFact) -> dict:
        packet = {"id": fact.id, "type": fact.fact_type, "value": fact.value, "employment_or_education_status": "past_or_not_applicable"}
        if fact.fact_type == "education":
            dates = re.findall(r"([A-Z][a-z]+)\s+(20\d{2})", fact.value)
            if len(dates) >= 2:
                months = {name.casefold(): number for number, name in enumerate(calendar.month_name) if name}
                end = date(int(dates[-1][1]), months[dates[-1][0].casefold()], 1)
                packet["employment_or_education_status"] = "completed" if end <= datetime.now(UTC).date() else "current"
                packet["completion_or_expected_date"] = end.isoformat()
        return packet

    @staticmethod
    def _validate(
        payload: dict,
        allowed_ids: set[int],
        candidate_facts: list[CandidateFact] | None = None,
        requirements: JobRequirements | None = None,
        job_description: str = "",
    ) -> str:
        lines = ["Claim validation"]
        candidate_text = "\n".join((payload["cover_letter"], payload["why_interested"], payload["relevant_experience"], payload["technical_skills"], *payload["cv_suggestions"])).casefold()
        if any(phrase in candidate_text for phrase in INTERNAL_LANGUAGE):
            raise ApplicationWritingError("Generated candidate-facing text contained internal safety language and was not saved.")
        evidence_text = "\n".join(
            f"{fact.value}\n{fact.evidence}" for fact in candidate_facts or [] if fact.approved and fact.active
        )
        if requirements:
            jd_only_experience = [
                term for term in _jd_only_terms(requirements, job_description)
                if _contains(term, candidate_text)
                and not _contains(term, evidence_text)
                and _asserts_prior_candidate_experience(candidate_text, term)
            ]
            if jd_only_experience:
                raise ApplicationWritingError(
                    "Generated candidate-facing text attributed JD-only requirements to the candidate and was not saved."
                )
        for claim in payload["claims"]:
            ids = set(claim["fact_ids"])
            status = "SUPPORTED" if ids and ids.issubset(allowed_ids) else "NEEDS_USER_CONFIRMATION" if not ids else "UNSUPPORTED"
            lines.append(f"- {status} · {claim['artifact']} · evidence {sorted(ids)} · {claim['claim']}")
        return "\n".join(lines)
