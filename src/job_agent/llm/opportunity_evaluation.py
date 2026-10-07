"""Optional grounded narrative for an existing deterministic opportunity brief."""

from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAIError

from job_agent.llm.client import create_openai_client
from job_agent.services.opportunity_evaluation import OpportunityEvaluation


class OpportunityNarrativeError(RuntimeError):
    pass


@dataclass(frozen=True)
class NarrativeClaim:
    text: str
    fact_ids: tuple[int, ...]


@dataclass(frozen=True)
class OpportunityNarrative:
    fit_summary: NarrativeClaim
    strongest_matches: tuple[NarrativeClaim, ...]
    important_gaps: tuple[str, ...]
    risk_or_stretch_factors: tuple[str, ...]
    recommendation_reasoning: str

    def as_dict(self) -> dict:
        return {
            "fit_summary": {
                "text": self.fit_summary.text,
                "fact_ids": list(self.fit_summary.fact_ids),
            },
            "strongest_matches": [
                {"text": item.text, "fact_ids": list(item.fact_ids)} for item in self.strongest_matches
            ],
            "important_gaps": list(self.important_gaps),
            "risk_or_stretch_factors": list(self.risk_or_stretch_factors),
            "recommendation_reasoning": self.recommendation_reasoning,
        }


def _schema(fact_ids: list[int]) -> dict:
    claim = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "fact_ids": {"type": "array", "items": {"type": "integer", "enum": fact_ids}, "minItems": 1},
        },
        "required": ["text", "fact_ids"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "fit_summary": claim,
            "strongest_matches": {"type": "array", "items": claim, "maxItems": 4},
            "important_gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "risk_or_stretch_factors": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            "recommendation_reasoning": {"type": "string"},
        },
        "required": [
            "fit_summary", "strongest_matches", "important_gaps", "risk_or_stretch_factors",
            "recommendation_reasoning",
        ],
        "additionalProperties": False,
    }


def validate_narrative(payload: dict, evaluation: OpportunityEvaluation) -> OpportunityNarrative:
    facts = {item.fact_id: item for item in evaluation.strongest_candidate_evidence}
    matches = payload.get("strongest_matches")
    if not isinstance(matches, list):
        raise OpportunityNarrativeError("Narrative did not contain grounded matches.")
    claims: list[NarrativeClaim] = []
    summary = payload.get("fit_summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("text"), str):
        raise OpportunityNarrativeError("Narrative summary was invalid.")
    summary_ids = tuple(summary.get("fact_ids") or ())
    if not summary_ids or not set(summary_ids).issubset(facts):
        raise OpportunityNarrativeError("Narrative summary included a candidate claim without approved evidence.")
    for item in matches:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise OpportunityNarrativeError("Narrative match was invalid.")
        ids = tuple(item.get("fact_ids") or ())
        if not ids or not set(ids).issubset(facts):
            raise OpportunityNarrativeError("Narrative included a candidate claim without approved evidence.")
        # A JD-only missing term cannot be presented as candidate evidence.
        text = item["text"].casefold()
        referenced_gap = next(
            (gap for gap in evaluation.missing_requirements if gap.casefold() in text), None
        )
        if referenced_gap and not any(
            referenced_gap.casefold() in facts[fact_id].value.casefold() for fact_id in ids
        ):
            raise OpportunityNarrativeError("Narrative turned a job-only requirement into candidate evidence.")
        claims.append(NarrativeClaim(item["text"].strip(), ids))
    gaps = tuple(str(item).strip() for item in payload.get("important_gaps", []) if str(item).strip())
    if not set(evaluation.missing_requirements).issubset(set(gaps)):
        raise OpportunityNarrativeError("Narrative omitted a deterministic requirement gap.")
    return OpportunityNarrative(
        NarrativeClaim(summary["text"].strip(), summary_ids), tuple(claims), gaps,
        tuple(str(item).strip() for item in payload["risk_or_stretch_factors"] if str(item).strip()),
        str(payload["recommendation_reasoning"]).strip(),
    )


class OpportunityNarrativeWriter:
    def __init__(self, api_key: str, model: str):
        self.client = create_openai_client(api_key)
        self.model = model

    def generate(self, evaluation: OpportunityEvaluation) -> OpportunityNarrative:
        evidence = [
            {"fact_id": item.fact_id, "type": item.fact_type, "value": item.value}
            for item in evaluation.strongest_candidate_evidence
        ]
        if not evidence:
            raise OpportunityNarrativeError("No grounded candidate evidence is available for an AI fit narrative.")
        packet = {
            "score": evaluation.deterministic_score,
            "recommendation": evaluation.deterministic_recommendation,
            "matched_requirements": evaluation.matched_requirements,
            "missing_requirements": evaluation.missing_requirements,
            "experience_evidence": evaluation.experience_evidence,
            "approved_candidate_evidence": evidence,
        }
        prompt = (
            "Write a concise decision brief for a job seeker. The deterministic score and recommendation "
            "are authoritative. Only describe a candidate capability in strongest_matches when its fact_ids "
            "cite supplied approved evidence. Describe missing requirements only as gaps, never as candidate "
            "experience. Do not infer years, skills, tools, qualifications, or employer facts. Cite approved "
            "evidence IDs for fit_summary and every strongest match. Keep recommendation_reasoning concise, "
            "limited to the score and stated gaps, and free of candidate-specific claims or internal terminology. Include every "
            "listed missing requirement in important_gaps.\n\n" + json.dumps(packet)
        )
        try:
            response = self.client.responses.create(
                model=self.model,
                input=prompt,
                max_output_tokens=900,
                reasoning={"effort": "low"},
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "opportunity_evaluation",
                        "strict": True,
                        "schema": _schema([item["fact_id"] for item in evidence]),
                    }
                },
            )
            return validate_narrative(json.loads(response.output_text), evaluation)
        except (OpenAIError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise OpportunityNarrativeError("Grounded fit narrative was unavailable.") from exc
