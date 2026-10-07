from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAIError

from job_agent.domain.models import CANDIDATE_FACT_TYPES
from job_agent.llm.client import create_openai_client

logger = logging.getLogger(__name__)
transport_logger = logging.getLogger("job_agent.openai_transport")
if not transport_logger.handlers:
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler("data/logs/openai_transport.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    transport_logger.addHandler(handler)
    transport_logger.setLevel(logging.INFO)
    transport_logger.propagate = False


class CVExtractionError(RuntimeError):
    """Safe wrapper that keeps transport details out of UI messages."""


@dataclass(frozen=True)
class ExtractedFact:
    fact_type: str
    value: str
    evidence: str
    snippet: str
    confidence: str


def validate_facts(payload: dict) -> list[ExtractedFact]:
    facts = payload.get("facts")
    if not isinstance(facts, list): raise TypeError("Extraction payload must contain a facts list.")
    result=[]
    for item in facts:
        if not isinstance(item,dict) or item.get("fact_type") not in CANDIDATE_FACT_TYPES or not all(isinstance(item.get(k),str) and item[k].strip() for k in ("fact_type","value","evidence","snippet","confidence")): raise ValueError("Invalid extracted fact.")
        result.append(ExtractedFact(**item))
    return result


def cv_fact_schema() -> dict:
    return {"type":"object","properties":{"facts":{"type":"array","items":{"type":"object","properties":{"fact_type":{"type":"string","enum":list(CANDIDATE_FACT_TYPES)},"value":{"type":"string"},"evidence":{"type":"string"},"snippet":{"type":"string"},"confidence":{"type":"string","enum":["high","medium","low"]}},"required":["fact_type","value","evidence","snippet","confidence"],"additionalProperties":False}}},"required":["facts"],"additionalProperties":False}


class CVExtractor:
    def __init__(self, api_key: str, model: str): self.client=create_openai_client(api_key); self.model=model
    def extract(self, text: str) -> list[ExtractedFact]:
        schema=cv_fact_schema()
        started = time.monotonic()
        try:
            response=self.client.responses.create(model=self.model,input="Extract only explicit CV facts. Never infer years, achievements, qualifications, skills, visa status or metrics. Return uncertain claims only when the CV states them.\n\n"+text,text={"format":{"type":"json_schema","name":"cv_facts","strict":True,"schema":schema}})
            try:
                return validate_facts(json.loads(response.output_text))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                transport_logger.info("stage=response_validation response_received=true exception=%s transport=none duration_seconds=%.3f",type(exc).__name__,time.monotonic()-started)
                raise CVExtractionError("Structured CV extraction response was invalid.") from exc
        except (OpenAIError, json.JSONDecodeError, TypeError, ValueError) as exc:
            cause = exc.__cause__ or exc.__context__
            transport = f"{type(cause).__name__}: {cause}"[:300] if cause else "none"
            transport_logger.info("stage=request_transport response_received=false exception=%s transport=%s duration_seconds=%.3f", type(exc).__name__, transport, time.monotonic() - started)
            logger.error("cv_extraction stage=api_or_schema exception=%s transport=%s", type(exc).__name__, transport)
            raise CVExtractionError("Structured CV extraction was unavailable.") from exc
