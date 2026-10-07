from __future__ import annotations

import html
from concurrent.futures import ThreadPoolExecutor

import httpx

from job_agent.domain.models import JobInput


class AdzunaConnector:
    """Uses Adzuna's documented search API when the user supplies API credentials."""

    def __init__(self, app_id: str, app_key: str):
        self.app_id = app_id
        self.app_key = app_key

    def search(self, roles: list[str], locations: list[str], *, pages_per_query: int = 1) -> list[JobInput]:
        """Search each role nationally; saved preferences filter the active queue.

        The endpoint is already scoped to Australia. Searching nationally avoids
        accidentally discarding Australian remote roles or roles whose location
        label uses a different Queensland synonym than the user's preferences.
        """
        jobs: list[JobInput] = []
        # Queries are intentionally national.  A result labelled only
        # "Australia" is a lead, not evidence that it is outside Queensland;
        # final location eligibility happens after direct-vacancy resolution.
        requests = [
            (role, page)
            for role in dict.fromkeys(roles or [""])
            for page in range(1, max(1, pages_per_query) + 1)
        ]

        def fetch(request: tuple[str, int]) -> object:
            role, page = request
            params = {"app_id": self.app_id, "app_key": self.app_key, "what": role, "results_per_page": 50}
            response = httpx.get(
                f"https://api.adzuna.com/v1/api/jobs/au/search/{page}", params=params, timeout=20
            )
            response.raise_for_status()
            return response.json()

        # A bounded pool keeps a broad role-first run responsive without making
        # an unbounded burst to the public API.  No request is retried here.
        with ThreadPoolExecutor(max_workers=min(6, len(requests))) as pool:
            payloads = list(pool.map(fetch, requests))
        for payload in payloads:
            for item in payload.get("results", []) if isinstance(payload, dict) else []:
                if not isinstance(item, dict) or not item.get("id") or not item.get("title") or not item.get("redirect_url"):
                    continue
                company = item.get("company") if isinstance(item.get("company"), dict) else {}
                location = item.get("location") if isinstance(item.get("location"), dict) else {}
                jobs.append(
                    JobInput(
                        title=item["title"],
                        company=company.get("display_name") or "Unknown employer",
                        url=item["redirect_url"],
                        description=html.unescape(item.get("description", "")),
                        source="ADZUNA",
                        location=location.get("display_name") or "Australia",
                        source_key="adzuna-au",
                        external_id=str(item["id"]),
                        raw_payload=str(item),
                    )
                )
        return jobs
