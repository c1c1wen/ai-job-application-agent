from __future__ import annotations

import re
from html import unescape

import httpx

from job_agent.domain.models import JobInput

COUNTRY_NAMES = {"AU": "Australia", "FR": "France", "GB": "United Kingdom", "US": "United States"}


def _plain(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(value or "")))).strip()


def _full_description(item: dict) -> str:
    """Keep all publicly supplied Lever job sections, not just the overview."""
    sections = [
        _plain(item.get("descriptionPlain") or item.get("description")),
        _plain(item.get("openingPlain") or item.get("opening")),
        _plain(item.get("descriptionBodyPlain") or item.get("descriptionBody")),
    ]
    for section in item.get("lists", []):
        if isinstance(section, dict):
            heading = _plain(section.get("text"))
            content = _plain(section.get("content"))
            if heading or content:
                sections.append(f"{heading}\n{content}".strip())
    sections.append(_plain(item.get("additionalPlain") or item.get("additional")))
    workplace = _plain(item.get("workplaceType"))
    if workplace:
        sections.insert(0, f"Work mode: {workplace}")
    return "\n\n".join(section for section in sections if section)


class LeverConnector:
    """Reads published jobs from Lever's public postings endpoint only."""

    def fetch(self, site: str, company: str | None = None) -> list[JobInput]:
        response = httpx.get(
            f"https://api.lever.co/v0/postings/{site}?mode=json", timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return []

        jobs: list[JobInput] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            external_id = item.get("id")
            title = item.get("text")
            url = item.get("hostedUrl")
            if not external_id or not title or not url:
                continue
            description = _full_description(item)
            categories = item.get("categories")
            location = categories.get("location") if isinstance(categories, dict) else None
            if not location:
                country = item.get("country")
                if isinstance(country, dict):
                    country = country.get("name") or country.get("code")
                location = COUNTRY_NAMES.get(str(country).upper(), country) if country else None
            jobs.append(
                JobInput(
                    title=title,
                    company=company or site,
                    location=location,
                    url=url,
                    description=description,
                    source="LEVER",
                    source_key=site,
                    external_id=str(external_id),
                    raw_payload=str(item),
                )
            )
        return jobs
