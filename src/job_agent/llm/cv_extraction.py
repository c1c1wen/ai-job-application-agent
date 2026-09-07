from __future__ import annotations

import json
from dataclasses import dataclass

from openai import OpenAI


@dataclass(frozen=True)
class ExtractedFact:
    fact_type: str
    value: str
    evidence: str
    snippet: str
    confidence: str


def validate_facts(payload: dict) -> list[ExtractedFact]:
    allowed = {"name","email","phone","location","work_right","education","experience","project","skill","tool","programming_language","certification","availability"}
    facts = payload.get("facts")
    if not isinstance(facts, list): raise TypeError("Extraction payload must contain a facts list.")
    result=[]
    for item in facts:
        if not isinstance(item,dict) or item.get("fact_type") not in allowed or not all(isinstance(item.get(k),str) and item[k].strip() for k in ("fact_type","value","evidence","snippet","confidence")): raise ValueError("Invalid extracted fact.")
        result.append(ExtractedFact(**item))
    return result


class CVExtractor:
    def __init__(self, api_key: str, model: str): self.client=OpenAI(api_key=api_key); self.model=model
    def extract(self, text: str) -> list[ExtractedFact]:
        schema={"type":"object","properties":{"facts":{"type":"array","items":{"type":"object","properties":{"fact_type":{"type":"string"},"value":{"type":"string"},"evidence":{"type":"string"},"snippet":{"type":"string"},"confidence":{"type":"string","enum":["high","medium","low"]}},"required":["fact_type","value","evidence","snippet","confidence"],"additionalProperties":False}}},"required":["facts"],"additionalProperties":False}
        response=self.client.responses.create(model=self.model,input="Extract only explicit CV facts. Never infer years, achievements, qualifications, skills, visa status or metrics. Return uncertain claims only when the CV states them.\n\n"+text,text={"format":{"type":"json_schema","name":"cv_facts","strict":True,"schema":schema}})
        return validate_facts(json.loads(response.output_text))
