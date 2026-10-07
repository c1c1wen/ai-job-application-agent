"""Read publicly published Employment Hero careers pages without authentication."""

from __future__ import annotations

import re
from html import unescape

import httpx

from job_agent.domain.models import JobInput


def _plain(value: object) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(value or "")))).strip()


class EmploymentHeroConnector:
    """Public employer-board connector, deliberately scoped to verified boards.

    Employment Hero's general ATS API requires an organisation-owned token.
    This connector instead reads the unauthenticated careers-page endpoint
    published by an employer's own public Employment Hero board.
    """

    endpoint = "https://services.employmenthero.com/ats/api/v1/career_page/organisations/{board}/jobs"
    public_nonce_endpoint = "https://employmenthero.com/wp-admin/admin-ajax.php?action=eh_get_nonce"
    public_search_endpoint = "https://employmenthero.com/wp-json/eh-api/v1/jobs"

    @staticmethod
    def _normalise_item(item: dict, company: str | None, source_key: str) -> JobInput | None:
        external_id = item.get("id") or item.get("job_id")
        title = item.get("title")
        # The public feeds provide a stable friendly ID rather than a full URL.
        friendly_id = item.get("friendly_id")
        url = item.get("jobUrl") or item.get("job_url")
        if not url and friendly_id:
            url = f"https://employmenthero.com/jobs/position/{friendly_id}/"
        if not external_id or not title or not url:
            return None
        work_mode = _plain(item.get("workplace_type") or item.get("remote_setting"))
        description = _plain(item.get("description"))
        if work_mode:
            description = f"Work mode: {work_mode}\n\n{description}".strip()
        return JobInput(
            title=str(title),
            company=str(item.get("organisation_name") or company or source_key),
            location=_plain(item.get("vendor_location_name")) or None,
            url=str(url),
            description=description,
            source="EMPLOYMENT_HERO",
            source_key=source_key,
            external_id=str(external_id),
            raw_payload=str(item),
        )

    def fetch(self, board: str, company: str | None = None) -> list[JobInput]:
        page = 1
        jobs: list[JobInput] = []
        while True:
            response = httpx.get(
                self.endpoint.format(board=board),
                params={"page_index": page, "item_per_page": 100},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                return jobs
            for item in items:
                if not isinstance(item, dict):
                    continue
                normalised = self._normalise_item(item, company, board)
                if normalised:
                    jobs.append(normalised)
            total_pages = (
                (data.get("totalPages") or data.get("total_pages"))
                if isinstance(data, dict)
                else 1
            )
            if not isinstance(total_pages, int) or page >= total_pages:
                return jobs
            page += 1

    def search(self, queries: list[str], *, pages_per_query: int = 1) -> list[JobInput]:
        """Search Employment Hero's public Australian jobseeker catalogue.

        The nonce is fetched from the same unauthenticated public page flow as
        the normal jobseeker UI. It is held only in this request scope and is
        never persisted as an account credential.
        """
        if not queries:
            return []
        jobs: list[JobInput] = []
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            nonce_response = client.get(self.public_nonce_endpoint)
            nonce_response.raise_for_status()
            nonce = nonce_response.json().get("nonce")
            if not isinstance(nonce, str) or not nonce:
                raise httpx.HTTPError("Employment Hero public search nonce was unavailable.")
            headers = {"X-WP-Nonce": nonce}
            for query in queries:
                for page in range(1, pages_per_query + 1):
                    response = client.get(
                        self.public_search_endpoint,
                        headers=headers,
                        params={
                            "country_code": "AU",
                            "query": query,
                            "item_per_page": 14,
                            "page_index": page,
                            "date_posted": 30,
                            "sort": "relevance",
                            "require_all_query_words": "true",
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    data = payload.get("data") if isinstance(payload, dict) else None
                    items = data.get("items") if isinstance(data, dict) else None
                    if not isinstance(items, list):
                        break
                    for item in items:
                        if isinstance(item, dict):
                            normalised = self._normalise_item(item, None, "employmenthero-au-search")
                            if normalised:
                                jobs.append(normalised)
                    total_pages = data.get("total_pages") if isinstance(data, dict) else 1
                    if not isinstance(total_pages, int) or page >= total_pages:
                        break
        return jobs
