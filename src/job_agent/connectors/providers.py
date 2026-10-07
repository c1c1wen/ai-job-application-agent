from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

import httpx

from job_agent.connectors.adzuna import AdzunaConnector
from job_agent.connectors.employment_hero import EmploymentHeroConnector
from job_agent.connectors.greenhouse import GreenhouseConnector
from job_agent.connectors.lever import LeverConnector
from job_agent.domain.models import JobInput


class JobSourceProvider(ABC):
    @abstractmethod
    def discover_jobs(self, *args: object, **kwargs: object) -> list[JobInput]: ...


class GreenhouseProvider(JobSourceProvider):
    def __init__(self, boards: dict[str, str] | list[str]):
        self.boards = boards if isinstance(boards, dict) else {board: board for board in boards}

    def discover_jobs(self) -> list[JobInput]:
        connector=GreenhouseConnector(); jobs=[]
        for token, company in self.boards.items(): jobs.extend(connector.fetch(token, company))
        return jobs


class LeverProvider(JobSourceProvider):
    def __init__(self, sites: dict[str, str]):
        self.sites = sites

    def discover_jobs(self) -> list[JobInput]:
        connector = LeverConnector()
        jobs = []
        if not self.sites:
            return jobs
        def fetch(board: tuple[str, str]) -> list[JobInput]:
            site, company = board
            try:
                return connector.fetch(site, company)
            except httpx.HTTPError:
                # One public board timing out must not hide all other boards.
                # Provider-level diagnostics remain available through the
                # discovery funnel; no board is retried automatically here.
                return []
        with ThreadPoolExecutor(max_workers=min(6, len(self.sites))) as pool:
            for postings in pool.map(fetch, self.sites.items()):
                jobs.extend(postings)
        return jobs


class EmploymentHeroProvider(JobSourceProvider):
    """Fetch only audited public Employment Hero employer boards."""

    def __init__(self, boards: dict[str, str]):
        self.boards = boards

    def discover_jobs(self, queries: list[str] | None = None) -> list[JobInput]:
        connector = EmploymentHeroConnector()
        jobs: list[JobInput] = []
        if queries:
            jobs.extend(connector.search(queries))
        for board, company in self.boards.items():
            try:
                jobs.extend(connector.fetch(board, company))
            except httpx.HTTPError:
                continue
        return jobs


class AdzunaProvider(JobSourceProvider):
    def __init__(self, app_id: str, app_key: str):
        self.connector = AdzunaConnector(app_id, app_key)

    def discover_jobs(self, roles: list[str], locations: list[str], *, pages_per_query: int = 1) -> list[JobInput]:
        return self.connector.search(roles, locations, pages_per_query=pages_per_query)
