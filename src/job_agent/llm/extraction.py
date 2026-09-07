from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from openai import OpenAI, OpenAIError


class ExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobRequirements:
    required_skills: list[str]
    preferred_skills: list[str]
    education_requirements: list[str]
    experience_requirements: list[str]
    work_right_requirements: list[str]
    concerns: list[str]


class OpenAIJobExtractor:
    """Extracts job facts only; it never decides suitability or writes candidate claims."""

    def __init__(self, api_key: str, model: str):
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def extract(self, title: str, description: str) -> JobRequirements:
        schema = {
            "type": "object",
            "properties": {
                "required_skills": {"type": "array", "items": {"type": "string"}},
                "preferred_skills": {"type": "array", "items": {"type": "string"}},
                "education_requirements": {"type": "array", "items": {"type": "string"}},
                "experience_requirements": {"type": "array", "items": {"type": "string"}},
                "work_right_requirements": {"type": "array", "items": {"type": "string"}},
                "concerns": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["required_skills", "preferred_skills", "education_requirements", "experience_requirements", "work_right_requirements", "concerns"],
            "additionalProperties": False,
        }
        try:
            response = self.client.responses.create(
                model=self.model,
                input=("Extract job requirements from the job description. Do not infer missing facts. "
                       "Return only requirements that are explicit or strongly stated.\n\n"
                       f"Title: {title}\nDescription:\n{description}"),
                text={"format": {"type": "json_schema", "name": "job_requirements", "strict": True, "schema": schema}},
            )
            return JobRequirements(**json.loads(response.output_text))
        except (OpenAIError, json.JSONDecodeError, TypeError) as exc:
            raise ExtractionError("No structured extraction result was available.") from exc

    @staticmethod
    def as_dict(result: JobRequirements) -> dict[str, list[str]]:
        return asdict(result)
