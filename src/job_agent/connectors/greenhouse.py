from __future__ import annotations

import re
from html import unescape

import httpx

from job_agent.domain.models import JobInput


class GreenhouseConnector:
    """Reads published jobs from a configured Greenhouse board; it never submits applications."""

    def fetch(self, board_token: str, company: str | None = None) -> list[JobInput]:
        response = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true", timeout=20)
        response.raise_for_status()
        jobs: list[JobInput] = []
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            return []
        for item in payload["jobs"]:
            if not isinstance(item, dict):
                continue
            description = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(item.get("content", "")))).strip()
            if not item.get("id") or not item.get("title") or not item.get("absolute_url"):
                continue
            jobs.append(JobInput(title=item["title"], company=item.get("company_name") or company or board_token, location=item.get("location", {}).get("name"), url=item["absolute_url"], description=description, source="GREENHOUSE", source_key=board_token, external_id=str(item["id"]), raw_payload=str(item)))
        return jobs
