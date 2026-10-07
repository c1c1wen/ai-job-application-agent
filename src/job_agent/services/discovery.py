"""Broad public discovery with a strict direct-source promotion boundary.

Discovery sources are leads.  They are intentionally separate from the ATS
destination used for an application: only a currently fetched employer ATS
posting or a resolver-confirmed public employer vacancy can enter review.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from job_agent.domain.models import CandidateFact, JobInput
from job_agent.domain.scoring import score_job
from job_agent.services.employer_resolution import VacancyResolution, resolve_public_job
from job_agent.services.job_search import (
    analyze_job_description,
    assess_search_eligibility,
    broad_discovery_queries,
    preliminary_eligibility,
)

DIRECT_ATS_SOURCES = {"LEVER", "GREENHOUSE", "EMPLOYMENT_HERO"}


def _safe_source_error(exc: Exception) -> str:
    """Keep source diagnostics useful without retaining query-string secrets."""
    message = str(exc)
    message = re.sub(
        r"(?i)((?:app_id|app_key|api[_-]?key|token|secret|authorization)=)[^&\s'\"]+",
        r"\1[redacted]",
        message,
    )
    return f"{type(exc).__name__}: {message[:160]}"


@dataclass(frozen=True)
class DiscoveryRun:
    run_id: int
    funnel: dict[str, int]
    sources: dict[str, dict]


def _direct_source_resolution(job: JobInput) -> VacancyResolution:
    """A successful public ATS feed response is live direct-vacancy evidence."""
    return VacancyResolution(
        "DIRECT_VERIFIED",
        1.0,
        job.source,
        job.url,
        {"reason": "Fetched from the employer's public ATS feed in this discovery run."},
    )


def run_broad_discovery(
    repo,
    search: dict,
    providers: list[tuple[str, Callable[[], list[JobInput]]]],
    *,
    resolve_limit: int = 100,
) -> DiscoveryRun:
    """Discover nationally, cheaply screen, then resolve only plausible leads.

    ``providers`` is injected so provider mechanics remain independent of the
    funnel.  Failures are recorded per source and never abort other sources.
    """
    source_metrics: dict[str, dict] = defaultdict(dict)
    discovered: list[JobInput] = []
    for source, fetch in providers:
        try:
            jobs = fetch()
        except Exception as exc:  # noqa: BLE001 - one source must not abort the run
            source_metrics[source] = {"raw": 0, "error": _safe_source_error(exc)}
            continue
        source_metrics[source] = {"raw": len(jobs)}
        discovered.extend(jobs)

    unique: dict[tuple[str, str], JobInput] = {}
    for job in discovered:
        key = (job.source, job.external_id or job.url.rstrip("/"))
        unique[key] = job
    candidates: list[tuple[int, JobInput]] = []
    funnel = Counter(raw_jobs_discovered=len(unique))
    direct_identities: set[tuple[str, str, str]] = set()
    observations: dict[int, dict] = {}
    for job in unique.values():
        identity = (
            " ".join(job.company.casefold().split()),
            " ".join(job.title.casefold().split()),
            " ".join((job.location or "").casefold().split()),
        )
        direct = job.source in DIRECT_ATS_SOURCES
        if direct:
            direct_identities.add(identity)
            funnel["direct_discovered"] += 1
        else:
            funnel["aggregator_leads"] += 1
        job_id, created = repo.add_job(job)
        funnel["new_jobs" if created else "previously_seen"] += 1
        metric = source_metrics[job.source]
        freshness_key = "new" if created else "previously_seen"
        metric[freshness_key] = metric.get(freshness_key, 0) + 1
        observations[job_id] = {
            "job_id": job_id,
            "freshness": "NEW" if created else "SEEN_BEFORE",
            "source_kind": "DIRECT_DISCOVERED" if direct else "AGGREGATOR_LEAD",
            "preliminary_status": None,
            "resolution_status": "DIRECT_VERIFIED" if direct else None,
        }
        # Preserve the aggregator record in SQLite, but do not spend resolver
        # or scoring work on a same-company/title/location lead when a direct
        # public ATS vacancy was already discovered in this run.
        if not direct and identity in direct_identities:
            funnel["duplicates"] += 1
            continue
        # Rediscovery is valuable for history, but an existing application is
        # never a fresh candidate and must not be re-prepared or re-executed.
        if repo.has_application(job_id):
            funnel["existing_application"] += 1
            continue
        preliminary = preliminary_eligibility(job, search)
        observations[job_id]["preliminary_status"] = preliminary.status
        if preliminary.status == "LIKELY_ELIGIBLE":
            funnel["role_relevant"] += 1
            funnel["location_plausible"] += 1
            funnel["seniority_plausible"] += 1
            metric["likely_eligible"] = metric.get("likely_eligible", 0) + 1
            candidates.append((job_id, job))
        elif preliminary.status == "POSSIBLE":
            funnel["role_relevant"] += 1
            # Country-level Australian labels are location-plausible leads,
            # not final location matches.  Resolution decides the latter.
            funnel["location_plausible"] += 1
            funnel["seniority_plausible"] += 1
            metric["possible"] = metric.get("possible", 0) + 1
            candidates.append((job_id, job))

    # Current public ATS feeds are already direct and live.  Index leads must
    # still pass the conservative resolver; no aggregator URL becomes an
    # execution destination.
    resolved: list[tuple[int, JobInput]] = []
    for job_id, job in candidates[:resolve_limit]:
        if job.source in DIRECT_ATS_SOURCES:
            resolution = _direct_source_resolution(job)
            repo.record_application_resolution(job_id, resolution)
        else:
            resolution = resolve_public_job(repo, job_id)
        metric = source_metrics[job.source]
        if resolution.status == "DIRECT_VERIFIED":
            observations[job_id]["resolution_status"] = "DIRECT_VERIFIED"
            funnel["direct_vacancy_resolved"] += 1
            funnel["live_direct_vacancy"] += 1
            metric["direct_verified"] = metric.get("direct_verified", 0) + 1
            metric["live"] = metric.get("live", 0) + 1
            row = repo.job_for_search_evaluation(job_id)
            resolved.append(
                (
                    job_id,
                    JobInput(
                        row["title"], row["company"], row["url"], row["description"], row["source"],
                        row["location"], row["source_key"], row["external_id"],
                    ),
                )
            )
        elif resolution.status == "CLOSED":
            observations[job_id]["resolution_status"] = "DIRECT_CLOSED"
            funnel["closed_stale"] += 1
        elif resolution.status == "NOT_FOUND":
            observations[job_id]["resolution_status"] = "DIRECT_NOT_FOUND"
            funnel["direct_not_found"] += 1

    config_hash = repr(sorted(search.items()))
    facts = repo.facts() + _preference_facts(search)
    config = repo.get_score_config()
    for job_id, job in resolved:
        strict = assess_search_eligibility(job, search)
        repo.record_search_eligibility(job_id, strict.passed, strict.reasons, config_hash)
        if not strict.passed:
            continue
        funnel["eligible"] += 1
        metric = source_metrics[job.source]
        metric["eligible"] = metric.get("eligible", 0) + 1
        analysis = analyze_job_description(job.description, job.location, job.title)
        score = score_job(analysis.requirements, job.title, facts, job.location, config)
        repo.screen_and_assess(job_id, score, analysis.as_dict())
        if score.total >= config.possible_match_threshold:
            funnel["good_or_possible"] += 1
            metric["good_or_possible"] = metric.get("good_or_possible", 0) + 1
            application_id = repo.create_application(job_id)
            if application_id:
                funnel["ready_for_application"] += 1

    # Ensure every named stage is represented, including zeroes.
    all_stages = (
        "raw_jobs_discovered", "new_jobs", "previously_seen", "duplicates",
        "direct_discovered", "aggregator_leads", "direct_not_found", "closed_stale",
        "role_relevant", "location_plausible",
        "seniority_plausible", "direct_vacancy_resolved", "live_direct_vacancy",
        "eligible", "good_or_possible", "ready_for_application",
        "existing_application",
    )
    compact = {stage: funnel[stage] for stage in all_stages}
    run_id = repo.record_discovery_run(
        {"queries": broad_discovery_queries(search), "search": search}, compact, source_metrics
    )
    repo.record_discovery_observations(run_id, list(observations.values()))
    return DiscoveryRun(run_id, compact, dict(source_metrics))


def _preference_facts(search: dict) -> list[CandidateFact]:
    values: list[CandidateFact] = []
    for role in search.get("roles", "").split(","):
        if role.strip():
            values.append(CandidateFact("target_role", role.strip(), "Job search setting"))
    for location in search.get("locations", "").split(","):
        if location.strip():
            values.append(CandidateFact("preferred_location", location.strip(), "Job search setting"))
    return values
