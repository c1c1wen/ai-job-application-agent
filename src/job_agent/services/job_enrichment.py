from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from job_agent.connectors.greenhouse import GreenhouseConnector
from job_agent.connectors.lever import LeverConnector
from job_agent.domain.models import JobInput


@dataclass(frozen=True)
class JobEnrichment:
    description: str | None
    original_job_url: str
    enrichment_url: str | None
    enrichment_source: str | None
    jd_status: str
    evidence_quality: str
    retrieval_status: str


class FullJobDescriptionEnricher:
    """Resolve Adzuna links only to known, public ATS feeds.

    It never authenticates, solves access challenges, or attempts to parse an
    arbitrary employer site.  Unknown destinations retain the index summary.
    """

    def __init__(self, client: httpx.Client | None = None, lever_fetcher=None, greenhouse_fetcher=None):
        self.client = client or httpx.Client(timeout=15)
        self.lever_fetcher = lever_fetcher or LeverConnector().fetch
        self.greenhouse_fetcher = greenhouse_fetcher or GreenhouseConnector().fetch

    def enrich(self, job: JobInput) -> JobEnrichment:
        if job.source != "ADZUNA":
            return JobEnrichment(job.description, job.url, job.url, job.source, "FULL", "HIGH", "DIRECT_PUBLIC_ATS")
        if (urlparse(job.url).hostname or "").casefold() == "www.adzuna.com.au":
            try:
                return self._from_adzuna_detail(job, job.url)
            except httpx.HTTPError:
                return self._partial(job, "PUBLIC_ATS_UNAVAILABLE", job.url)
        try:
            destination = self._resolve_redirect(job.url)
        except httpx.HTTPError:
            return self._partial(job, "REDIRECT_UNAVAILABLE")
        if not destination:
            return self._partial(job, "NO_PUBLIC_ATS_DESTINATION")
        host = (urlparse(destination).hostname or "").casefold()
        try:
            if host == "www.adzuna.com.au":
                return self._from_adzuna_detail(job, destination)
            if host == "jobs.lever.co":
                return self._from_lever(job, destination)
            if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
                return self._from_greenhouse(job, destination)
        except httpx.HTTPError:
            return self._partial(job, "PUBLIC_ATS_UNAVAILABLE", destination)
        return self._partial(job, "UNSUPPORTED_ORIGINAL_SOURCE", destination)

    def _from_adzuna_detail(self, job: JobInput, destination: str) -> JobEnrichment:
        response = self.client.get(destination)
        response.raise_for_status()
        description = self._job_posting_description(response.text)
        if not description or len(description) <= len(job.description):
            return self._partial(job, "ADZUNA_DETAIL_NOT_RICHER", destination)
        return JobEnrichment(
            description,
            job.url,
            destination,
            "ADZUNA_PUBLIC_DETAIL",
            "FULL",
            "MEDIUM",
            "ENRICHED_PUBLIC_DETAIL",
        )

    @staticmethod
    def _job_posting_description(page: str) -> str | None:
        scripts = re.findall(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            page,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for script in scripts:
            try:
                payload = json.loads(unescape(script))
            except json.JSONDecodeError:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    value = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(item.get("description", ""))))).strip()
                    if value:
                        return value
        return None

    def _resolve_redirect(self, source_url: str) -> str | None:
        current = source_url
        for _ in range(5):
            response = self.client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            return current
        return None

    def _from_lever(self, job: JobInput, destination: str) -> JobEnrichment:
        parts = [part for part in urlparse(destination).path.split("/") if part]
        if not parts:
            return self._partial(job, "INVALID_LEVER_DESTINATION", destination)
        postings = self.lever_fetcher(parts[0])
        matched = next((item for item in postings if item.url.rstrip("/") == destination.rstrip("/")), None)
        if not matched or len(matched.description) <= len(job.description):
            return self._partial(job, "LEVER_DESCRIPTION_NOT_RICHER", destination)
        return JobEnrichment(matched.description, job.url, matched.url, "LEVER", "FULL", "HIGH", "ENRICHED")

    def _from_greenhouse(self, job: JobInput, destination: str) -> JobEnrichment:
        parts = [part for part in urlparse(destination).path.split("/") if part]
        if len(parts) < 3 or parts[1] != "jobs":
            return self._partial(job, "INVALID_GREENHOUSE_DESTINATION", destination)
        postings = self.greenhouse_fetcher(parts[0])
        matched = next((item for item in postings if item.url.rstrip("/") == destination.rstrip("/")), None)
        if not matched or len(matched.description) <= len(job.description):
            return self._partial(job, "GREENHOUSE_DESCRIPTION_NOT_RICHER", destination)
        return JobEnrichment(matched.description, job.url, matched.url, "GREENHOUSE", "FULL", "HIGH", "ENRICHED")

    @staticmethod
    def _partial(job: JobInput, status: str, destination: str | None = None) -> JobEnrichment:
        return JobEnrichment(job.description, job.url, destination, None, "PARTIAL", "PARTIAL", status)
