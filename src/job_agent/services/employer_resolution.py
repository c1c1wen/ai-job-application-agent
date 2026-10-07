"""Resolve index-discovered jobs to an employer-controlled public vacancy.

The resolver is deliberately conservative.  A search result is only a lead;
execution may use a URL only after the identity checks below mark it verified.
It never authenticates, defeats access controls, or uses an index URL as an
application destination.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from html import unescape
from typing import Protocol
from urllib.parse import urlparse

import httpx

RESOLUTION_STATUSES = {"DIRECT_VERIFIED", "DIRECT_PROBABLE", "AMBIGUOUS", "NOT_FOUND", "CLOSED"}


def _normal(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _tokens(value: str | None) -> set[str]:
    return set(_normal(value).split())


def _similarity(left: str | None, right: str | None) -> float:
    first, second = _tokens(left), _tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def _location_similarity(left: str | None, right: str | None) -> float:
    """Country-level index metadata supports, but does not replace, a direct location."""
    score = _similarity(left, right)
    left_tokens, right_tokens = _tokens(left), _tokens(right)
    if "australia" in left_tokens and "australia" in right_tokens:
        return max(score, 0.5)
    return score


def provider_for_url(url: str | None) -> str | None:
    host = (urlparse(url or "").hostname or "").casefold()
    if "lever.co" in host:
        return "LEVER"
    if "greenhouse.io" in host:
        return "GREENHOUSE"
    if "pageuppeople.com" in host:
        return "PAGEUP"
    if "employmenthero.com" in host:
        return "EMPLOYMENT_HERO"
    if "myworkdayjobs.com" in host or "workday.com" in host:
        return "WORKDAY"
    if "smartrecruiters.com" in host:
        return "SMARTRECRUITERS"
    if "successfactors" in host:
        return "SUCCESSFACTORS"
    if "workable.com" in host:
        return "WORKABLE"
    if "ashbyhq.com" in host:
        return "ASHBY"
    if "jobvite.com" in host:
        return "JOBVITE"
    if "oraclecloud.com" in host:
        return "ORACLE"
    return None


@dataclass(frozen=True)
class DirectVacancy:
    url: str
    company: str
    title: str
    location: str | None = None
    description: str | None = None
    requisition_id: str | None = None
    employment_type: str | None = None
    closed: bool = False


@dataclass(frozen=True)
class VacancyResolution:
    status: str
    confidence: float
    application_source: str | None
    canonical_application_url: str | None
    evidence: dict[str, object]


@dataclass(frozen=True)
class PublicSearchHit:
    """A title/URL/snippet result returned by a permitted search provider."""

    url: str
    title: str
    snippet: str = ""


class PublicSearchClient(Protocol):
    def search(self, query: str) -> list[PublicSearchHit]: ...


class GoogleProgrammableSearchClient:
    """Official Google Programmable Search JSON API adapter.

    It is optional: the application does not fall back to scraping a search
    engine when this explicitly configured public-search integration is absent.
    """

    endpoint = "https://customsearch.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, search_engine_id: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self.client = client or httpx.Client(timeout=15, follow_redirects=True)

    def search(self, query: str) -> list[PublicSearchHit]:
        response = self.client.get(
            self.endpoint,
            params={"key": self.api_key, "cx": self.search_engine_id, "q": query, "num": 10},
        )
        response.raise_for_status()
        payload = response.json()
        return [
            PublicSearchHit(str(item["link"]), str(item.get("title") or ""), str(item.get("snippet") or ""))
            for item in payload.get("items", []) if isinstance(item, dict) and isinstance(item.get("link"), str)
        ] if isinstance(payload, dict) else []


def configured_search_resolution_finder(api_key: str | None, search_engine_id: str | None) -> SearchBasedResolutionFinder | None:
    if not api_key or not search_engine_id:
        return None
    return SearchBasedResolutionFinder(GoogleProgrammableSearchClient(api_key, search_engine_id))


def resolution_queries(job: dict, aliases: tuple[str, ...] = (), careers_domain: str | None = None, ats_domain: str | None = None) -> list[str]:
    """Bounded role-first query plan; no unbounded crawling or retries."""
    company = str(job.get("company") or "").strip()
    title = str(job.get("title") or "").strip()
    location = str(job.get("location") or "").strip()
    normalized_title = _normal(title)
    terms = [f'"{title}" "{company}"']
    terms.append(f'"{normalized_title}" "{company}"')
    if location:
        terms.append(f'"{title}" "{company}" "{location}"')
    for alias in aliases[:2]:
        terms.append(f'"{title}" "{alias}"')
    if careers_domain:
        terms.append(f'site:{careers_domain} "{title}"')
    if ats_domain:
        terms.append(f'site:{ats_domain} "{title}" "{company}"')
    # Distinctive requirements are supporting evidence only, never a source of
    # candidate claims.  Limit the phrase to avoid noisy, generic searches.
    description_words = [word for word in _tokens(job.get("description")) if len(word) >= 8]
    if description_words:
        terms.append(f'"{company}" "{title}" "{description_words[0]}"')
    return list(dict.fromkeys(terms))[:7]


class SearchBasedResolutionFinder:
    """Resolve a lead using a permitted search client and verified domains.

    Search results are only candidates.  A domain is first verified from the
    employer's public career page; then role results are restricted to that
    domain or an identified public ATS host.  This deliberately prevents a
    third-party index result from becoming an application destination.
    """

    def __init__(self, search: PublicSearchClient, client: httpx.Client | None = None):
        self.search = search
        self.client = client or httpx.Client(timeout=15, follow_redirects=True)
        self.last_careers_domain: str | None = None
        self.last_ats_provider: str | None = None
        self.attempts: list[str] = []

    def candidates_for(self, job: dict, aliases: tuple[str, ...] = ()) -> list[DirectVacancy]:
        company = str(job.get("company") or "")
        careers = self._discover_careers_domain(company, aliases)
        if not careers:
            return []
        self.last_careers_domain = careers
        candidates: list[DirectVacancy] = []
        for query in resolution_queries(job, aliases, careers):
            self.attempts.append(query)
            for hit in self.search.search(query)[:8]:
                host = (urlparse(hit.url).hostname or "").casefold()
                if not self._trusted_candidate_host(host, careers):
                    continue
                if _similarity(job.get("title"), hit.title) < 0.45:
                    continue
                vacancy = self._read_vacancy(hit.url, company, hit.title, hit.snippet)
                if vacancy:
                    candidates.append(vacancy)
        return _unique_vacancies(candidates)

    def _discover_careers_domain(self, company: str, aliases: tuple[str, ...]) -> str | None:
        names = (company, *aliases)
        for name in names:
            for hit in self.search.search(f'"{name}" careers')[:8]:
                host = (urlparse(hit.url).hostname or "").casefold()
                if not host:
                    continue
                # Verify the public page associates itself with the employer;
                # a search snippet alone is not enough.
                try:
                    response = self.client.get(hit.url)
                    response.raise_for_status()
                except httpx.HTTPError:
                    continue
                page = _normal(response.text[:200_000])
                if any(_normal(value) and _normal(value) in page for value in names):
                    return host
        return None

    def _trusted_candidate_host(self, host: str, careers_domain: str) -> bool:
        if host == careers_domain or host.endswith(f".{careers_domain}"):
            return True
        return provider_for_url(f"https://{host}") is not None

    def _read_vacancy(self, url: str, company: str, fallback_title: str, snippet: str) -> DirectVacancy | None:
        try:
            response = self.client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        vacancy = _jobposting_from_html(response.text, url, company, fallback_title, snippet)
        if vacancy:
            self.last_ats_provider = provider_for_url(url) or "EMPLOYER_CAREERS"
        return vacancy


def _unique_vacancies(candidates: list[DirectVacancy]) -> list[DirectVacancy]:
    seen: set[str] = set()
    return [candidate for candidate in candidates if not (candidate.url in seen or seen.add(candidate.url))]


def _jobposting_from_html(html_text: str, url: str, company: str, fallback_title: str, snippet: str) -> DirectVacancy | None:
    """Read only public JobPosting metadata; no interaction with form pages."""
    for script in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", html_text, re.IGNORECASE | re.DOTALL):
        try:
            parsed = json.loads(unescape(script))
        except json.JSONDecodeError:
            continue
        for item in parsed if isinstance(parsed, list) else [parsed]:
            job_type = item.get("@type") if isinstance(item, dict) else None
            if not isinstance(item, dict) or (
                job_type != "JobPosting" and "JobPosting" not in (job_type if isinstance(job_type, list) else [])
            ):
                continue
            organisation = item.get("hiringOrganization") or {}
            employer = organisation.get("name") if isinstance(organisation, dict) else company
            location = item.get("jobLocation") or {}
            if isinstance(location, dict):
                address = location.get("address") or {}
                location = ", ".join(str(address.get(key)) for key in ("addressLocality", "addressRegion", "addressCountry") if address.get(key))
            return DirectVacancy(url, str(employer or company), str(item.get("title") or fallback_title), str(location or "") or None, str(item.get("description") or snippet) or None, str(item.get("identifier") or "") or None, closed="job not found" in html_text.casefold())
    # A public careers page may not expose JSON-LD.  Keep it only as a
    # candidate: the resolver's company/title/location/JD checks still decide.
    text = re.sub(r"<[^>]+>", " ", unescape(html_text))
    if fallback_title and _normal(fallback_title) in _normal(text):
        return DirectVacancy(url, company, fallback_title, description=snippet or text[:4_000])
    return None


class EmployerAtsResolver:
    """Scores public direct-vacancy candidates supplied by permitted providers.

    Candidate discovery is intentionally injected.  This keeps provider/search
    policy outside matching, makes every source auditable, and lets a future
    official ATS provider be added without changing application workflow code.
    """

    def resolve(
        self, job: dict, candidates: list[DirectVacancy], aliases: tuple[str, ...] = ()
    ) -> VacancyResolution:
        if not candidates:
            return VacancyResolution(
                "NOT_FOUND", 0.0, None, None, {"reason": "No permitted direct vacancy was found."}
            )

        employer_names = {_normal(job.get("company")), *(_normal(value) for value in aliases)}
        scored: list[tuple[float, DirectVacancy, dict[str, object]]] = []
        for candidate in candidates:
            company_match = _normal(candidate.company) in employer_names
            title = _similarity(job.get("title"), candidate.title)
            location = _location_similarity(job.get("location"), candidate.location)
            description = _similarity(job.get("description"), candidate.description)
            requisition_match = bool(
                job.get("external_id")
                and candidate.requisition_id
                and str(job["external_id"]) == str(candidate.requisition_id)
            )
            # Company identity is a hard prerequisite: title-only matches are unsafe.
            if not company_match:
                continue
            confidence = min(
                1.0,
                0.45
                + 0.30 * title
                + 0.12 * location
                + 0.13 * description
                + (0.30 if requisition_match else 0.0),
            )
            evidence = {
                "employer_exact_or_alias": company_match,
                "title_similarity": round(title, 3),
                "location_similarity": round(location, 3),
                "description_similarity": round(description, 3),
                "requisition_id_match": requisition_match,
                "candidate_url": candidate.url,
            }
            scored.append((confidence, candidate, evidence))
        if not scored:
            return VacancyResolution(
                "NOT_FOUND",
                0.0,
                None,
                None,
                {"reason": "No employer-matched direct vacancy was found."},
            )
        scored.sort(key=lambda item: item[0], reverse=True)
        confidence, best, evidence = scored[0]
        if best.closed:
            return VacancyResolution(
                "CLOSED",
                confidence,
                provider_for_url(best.url),
                None,
                evidence | {"reason": "Direct employer vacancy is closed."},
            )
        # Requisition identity is conclusive.  Otherwise require company + strong
        # title, plus independent location or description corroboration.
        verified = evidence["requisition_id_match"] or (
            evidence["title_similarity"] >= 0.80
            and (
                evidence["location_similarity"] >= 0.45
                or evidence["description_similarity"] >= 0.35
            )
        )
        if verified:
            return VacancyResolution(
                "DIRECT_VERIFIED",
                confidence,
                provider_for_url(best.url) or "EMPLOYER_CAREERS",
                best.url,
                evidence | {
                    "direct_title": best.title,
                    "direct_location": best.location,
                    "direct_description_available": bool(best.description),
                },
            )
        if confidence >= 0.68:
            return VacancyResolution(
                "DIRECT_PROBABLE",
                confidence,
                provider_for_url(best.url) or "EMPLOYER_CAREERS",
                best.url,
                evidence,
            )
        return VacancyResolution(
            "AMBIGUOUS",
            confidence,
            provider_for_url(best.url) or "EMPLOYER_CAREERS",
            best.url,
            evidence,
        )


class PublicResolutionFinder:
    """Find only direct URLs exposed in the public index page's JobPosting data.

    This is intentionally not a general web scraper.  It follows no login
    flow and treats an unavailable or opaque page as ``NOT_FOUND``.
    """

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=15, follow_redirects=True)

    def candidates_for(self, job: dict) -> list[DirectVacancy]:
        if "adzuna" not in (urlparse(job.get("url", "")).hostname or "").casefold():
            return []
        try:
            response = self.client.get(job["url"])
            response.raise_for_status()
        except httpx.HTTPError:
            return []
        candidates: list[DirectVacancy] = []
        scripts = re.findall(
            r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
            response.text,
            re.IGNORECASE | re.DOTALL,
        )
        for script in scripts:
            try:
                parsed = json.loads(unescape(script))
            except json.JSONDecodeError:
                continue
            for item in parsed if isinstance(parsed, list) else [parsed]:
                if not isinstance(item, dict):
                    continue
                job_type = item.get("@type")
                if job_type != "JobPosting" and "JobPosting" not in (
                    job_type if isinstance(job_type, list) else []
                ):
                    continue
                direct_url = item.get("applicationUrl") or item.get("url")
                if (
                    not isinstance(direct_url, str)
                    or "adzuna" in (urlparse(direct_url).hostname or "").casefold()
                ):
                    continue
                organisation = item.get("hiringOrganization") or {}
                company = (
                    organisation.get("name")
                    if isinstance(organisation, dict)
                    else job.get("company", "")
                )
                location = item.get("jobLocation") or {}
                if isinstance(location, dict):
                    address = location.get("address") or {}
                    location = ", ".join(
                        str(address.get(key))
                        for key in ("addressLocality", "addressRegion", "addressCountry")
                        if address.get(key)
                    )
                candidates.append(
                    DirectVacancy(
                        direct_url,
                        str(company or ""),
                        str(item.get("title") or ""),
                        str(location or "") or None,
                        str(item.get("description") or "") or None,
                        str(item.get("identifier") or "") or None,
                    )
                )
        return candidates


def resolve_public_job(
    repo, job_id: int, finder: PublicResolutionFinder | None = None
) -> VacancyResolution:
    """Resolve and persist a public direct source for one aggregator record."""
    row = repo.connection.execute(
        "SELECT j.id,j.title,j.company,j.location,j.url,j.description,j.external_id,COALESCE(s.kind,'MANUAL') source FROM jobs j LEFT JOIN job_sources s ON s.id=j.source_id WHERE j.id=?",
        (job_id,),
    ).fetchone()
    if not row:
        raise ValueError("Job was not found.")
    job = dict(row)
    aliases: tuple[str, ...] = ()
    for entry in repo.employer_ats_registry():
        values = (entry["canonical_employer"], *json.loads(entry["aliases_json"]))
        if _normal(job["company"]) in {_normal(value) for value in values}:
            aliases = tuple(values)
            break
    active_finder = finder or PublicResolutionFinder()
    try:
        candidates = active_finder.candidates_for(job, aliases)
    except TypeError:  # backward-compatible simple public finder
        candidates = active_finder.candidates_for(job)
    resolution = EmployerAtsResolver().resolve(job, candidates, aliases)
    careers_domain = getattr(active_finder, "last_careers_domain", None)
    attempts = getattr(active_finder, "attempts", ())
    if careers_domain:
        repo.upsert_employer_ats(
            job["company"],
            list(aliases),
            careers_domain,
            getattr(active_finder, "last_ats_provider", None),
            None,
            {"resolution_queries": list(attempts), "careers_domain_verified": True},
        )
    if attempts:
        resolution = replace(
            resolution,
            evidence=resolution.evidence | {"resolution_queries": list(attempts), "careers_domain": careers_domain},
        )
    repo.record_application_resolution(job_id, resolution)
    if resolution.status == "DIRECT_VERIFIED" and resolution.canonical_application_url:
        host = urlparse(resolution.canonical_application_url).hostname or ""
        repo.upsert_employer_ats(
            job["company"],
            list(aliases),
            host,
            resolution.application_source,
            None,
            resolution.evidence,
        )
        # Public direct-vacancy metadata is stronger than an index label such
        # as simply "Australia".  Keep the source record, but use the verified
        # location/JD for final eligibility and scoring.
        direct = next(
            (candidate for candidate in candidates if candidate.url == resolution.canonical_application_url),
            None,
        )
        if direct:
            repo.connection.execute(
                "UPDATE jobs SET title=?,location=?,description=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (
                    direct.title or job["title"],
                    direct.location or job["location"],
                    direct.description or job["description"],
                    job_id,
                ),
            )
            repo.connection.commit()
    return resolution
