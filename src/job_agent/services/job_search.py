from __future__ import annotations

import re
from dataclasses import dataclass

from job_agent.domain.models import CandidateFact, JobInput, JobRequirements

SENIOR_TITLE_WORDS = ("senior", "manager", "lead", "principal", "head", "director")
DISCOVERY_QUERIES = (
    "Data Analyst", "Junior Data Analyst", "Graduate Data Analyst",
    "Business Intelligence Analyst", "BI Analyst", "Reporting Analyst",
    "Insights Analyst", "Analytics Analyst", "Data and Reporting Analyst",
    "Graduate Analytics", "AI Analyst", "AI Operations", "AI Implementation",
    "Data Science Graduate", "Junior Data Scientist",
)
ANALYTICS_RESPONSIBILITY_TERMS = (
    "data analysis", "analytics", "reporting", "dashboard", "insight", "sql",
    "power bi", "python", "data quality", "data governance", "visualisation",
)
AUSTRALIAN_LOCATION_TERMS = (
    "australia", "australian", "brisbane", "queensland", "sydney",
    "new south wales", "melbourne", "victoria", "perth", "western australia",
    "adelaide", "south australia", "canberra", "tasmania", "darwin",
)
ROLE_FAMILIES = {
    "data analyst": ("data analyst", "analytics analyst", "insights analyst", "reporting analyst", "business intelligence", "bi analyst"),
    "analytics analyst": ("data analyst", "analytics analyst", "insights analyst", "reporting analyst", "business intelligence", "bi analyst"),
    "business intelligence": ("business intelligence", "bi analyst", "data analyst", "reporting analyst", "analytics analyst"),
    "ai": ("ai analyst", "ai operations", "ai implementation", "artificial intelligence", "data scientist"),
    "data scientist": ("data scientist", "machine learning", "artificial intelligence", "ai"),
}


@dataclass(frozen=True)
class SearchEligibility:
    passed: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreliminaryEligibility:
    """Cheap discovery-time classification before a direct vacancy is resolved.

    Index metadata is often incomplete, especially for Australian remote and
    nationwide adverts.  This class never promotes a role to the application
    queue; it only controls whether it is worth resolving safely.
    """

    status: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobDescriptionAnalysis:
    requirements: JobRequirements
    responsibilities: tuple[str, ...]
    required_years: tuple[str, ...]
    seniority: str | None
    work_mode: str | None

    def as_dict(self) -> dict:
        return {
            "required_skills": list(self.requirements.required_skills),
            "preferred_skills": list(self.requirements.preferred_skills),
            "education_requirements": list(self.requirements.education_requirements),
            "experience_requirements": list(self.requirements.experience_requirements),
            "work_right_requirements": list(self.requirements.work_right_requirements),
            "responsibilities": list(self.responsibilities),
            "required_years": list(self.required_years),
            "seniority": self.seniority,
            "work_mode": self.work_mode,
        }


def _values(search: dict, key: str) -> list[str]:
    return [value.strip() for value in search.get(key, "").split(",") if value.strip()]


def _text(job: JobInput) -> str:
    return f"{job.title} {job.location or ''} {job.description}".casefold()


def role_family_matches(text: str, roles: list[str]) -> bool:
    if not roles:
        return True
    text = text.casefold()
    for role in roles:
        normalized = role.casefold()
        terms = ROLE_FAMILIES.get(normalized)
        if terms is None:
            if any(word in normalized for word in ("data", "analytics", "insights", "reporting", "business intelligence", "bi ")):
                terms = ROLE_FAMILIES["data analyst"]
            elif any(word in normalized for word in ("ai", "artificial intelligence", "machine learning", "data science")):
                terms = ROLE_FAMILIES["ai"]
            else:
                terms = (normalized,)
        if any(term in text for term in terms):
            return True
    return False


def _role_matches(job: JobInput, roles: list[str]) -> bool:
    # Role compatibility is deliberately title-led. A sales role at an AI
    # company should not pass merely because its description says "AI".
    return role_family_matches(job.title, roles)


def _business_analyst_is_analytics_oriented(job: JobInput) -> bool:
    title = job.title.casefold()
    if "business analyst" not in title:
        return False
    text = _text(job)
    return sum(term in text for term in ANALYTICS_RESPONSIBILITY_TERMS) >= 2


def _is_australian_remote(job: JobInput) -> bool:
    """Accept an explicitly Australian remote role despite a city HQ label."""
    text = _text(job)
    return "remote" in text and any(term in text for term in AUSTRALIAN_LOCATION_TERMS)


def preliminary_eligibility(job: JobInput, search: dict) -> PreliminaryEligibility:
    """Classify an index result without prematurely rejecting weak metadata."""
    title = job.title.casefold()
    text = _text(job)
    level = search.get("level", "").casefold()
    if any(word in title for word in SENIOR_TITLE_WORDS):
        return PreliminaryEligibility("LIKELY_INELIGIBLE", ("Title is clearly senior.",))
    if level in {"graduate / entry-level", "junior"} and re.search(
        r"\b(?:at least |minimum of |minimum )?[4-9]\+?\s+years?(?: of)? (?:professional |relevant |commercial |full-time )?experience\b",
        text,
    ):
        return PreliminaryEligibility("LIKELY_INELIGIBLE", ("Role explicitly requires 4+ years of experience.",))
    roles = _values(search, "roles")
    role_match = _role_matches(job, roles) or _business_analyst_is_analytics_oriented(job)
    if not role_match:
        return PreliminaryEligibility("LIKELY_INELIGIBLE", ("Title and visible summary are outside analytics role families.",))
    location = (job.location or "").strip().casefold()
    preferences = _values(search, "locations")
    if location in {"", "australia", "australian"}:
        return PreliminaryEligibility("POSSIBLE", ("Location is country-level metadata; verify direct vacancy before filtering.",))
    if location_matches(job.location, preferences) or _is_australian_remote(job):
        return PreliminaryEligibility("LIKELY_ELIGIBLE")
    return PreliminaryEligibility("LIKELY_INELIGIBLE", ("Index location is outside selected preferences.",))


def broad_discovery_queries(search: dict) -> list[str]:
    """Role-first public discovery terms, with saved preferences still included."""
    return list(dict.fromkeys([*_values(search, "roles"), *DISCOVERY_QUERIES]))


def location_matches(location: str | None, preferences: list[str]) -> bool:
    if not preferences:
        return True
    if not location:
        return False
    normalized = location.casefold()
    preference_values = [preference.casefold() for preference in preferences]
    remote_australia = "remote" in normalized and (
        "australia" in normalized or "australian" in normalized or "east coast" in normalized
    )
    specific_preferences = [
        value
        for value in preference_values
        if value not in {"australia", "australian"} and "remote" not in value
    ]
    # A generic country label is not evidence that a role is in the candidate's
    # selected city/state.  It may still admit an explicitly Australian remote
    # role, and a country-only search remains national when no local preference
    # is configured.
    if normalized.strip(" ,") in {"australia", "australian"} and specific_preferences:
        return False
    for preference in preferences:
        value = preference.casefold()
        if value in {"australia", "australian"}:
            if remote_australia or not specific_preferences:
                return "australia" in normalized or "australian" in normalized
            continue
        if value in normalized:
            return True
        if value == "queensland" and any(place in normalized for place in ("queensland", "brisbane", "gold coast")):
            return True
        if "remote" in value and remote_australia:
            return True
    return False


def _work_mode_matches(job: JobInput, requested: str) -> bool:
    if not requested:
        return True
    text = _text(job)
    requested = requested.casefold()
    explicit_modes = {mode for mode in ("remote", "hybrid", "on-site", "onsite") if mode in text}
    if not explicit_modes:
        return True
    return requested in explicit_modes or (requested == "on-site" and "onsite" in explicit_modes)


def _employment_matches(job: JobInput, requested: str) -> bool:
    if not requested:
        return True
    text = _text(job)
    requested = requested.casefold()
    explicit_types = {kind for kind in ("full-time", "full time", "part-time", "part time", "contract", "casual") if kind in text}
    if not explicit_types:
        return True
    return requested in text


def assess_search_eligibility(job: JobInput, search: dict) -> SearchEligibility:
    """Hard, explainable user-preference gate before deterministic ranking."""
    reasons: list[str] = []
    title = job.title.casefold()
    text = _text(job)
    roles = _values(search, "roles")
    locations = _values(search, "locations")
    included = [value.casefold() for value in _values(search, "include")]
    excluded = [value.casefold() for value in _values(search, "exclude")]
    level = search.get("level", "").casefold()

    if not (_role_matches(job, roles) or _business_analyst_is_analytics_oriented(job)):
        reasons.append("Role is outside the selected role families.")
    if not (location_matches(job.location, locations) or _is_australian_remote(job)):
        reasons.append("Location is outside the selected preferences.")
    if level in {"graduate / entry-level", "junior"} and any(word in title for word in SENIOR_TITLE_WORDS):
        reasons.append("Role is clearly senior for the selected experience level.")
    if level in {"graduate / entry-level", "junior"} and re.search(
        r"\b(?:at least |minimum of |minimum )?[3-9]\+?\s+years?(?: of)? (?:professional |relevant |commercial |full-time )?experience\b",
        text,
    ):
        reasons.append("Role requires substantial professional experience for the selected level.")
    if not _work_mode_matches(job, search.get("work_mode", "")):
        reasons.append("Work mode conflicts with the selected preference.")
    if not _employment_matches(job, search.get("employment_type", "")):
        reasons.append("Employment type conflicts with the selected preference.")
    if included and not any(keyword in text for keyword in included):
        reasons.append("Role does not include a selected keyword.")
    if any(keyword in text for keyword in excluded):
        reasons.append("Role includes an excluded keyword.")
    return SearchEligibility(not reasons, tuple(reasons))


def analyze_job_description(description: str, location: str | None, title: str = "") -> JobDescriptionAnalysis:
    """Deterministically extract explicit requirements from a normalized public JD."""
    text = description.casefold()
    preferred_start = re.search(r"\b(nice to haves?|preferred qualifications?|desirable skills?)\b", text)
    required_text = text[:preferred_start.start()] if preferred_start else text
    preferred_text = text[preferred_start.start():] if preferred_start else ""
    vocabulary = ("python", "sql", "excel", "power bi", "powerbi", "tableau", "r", "databricks", "bigquery", "snowflake", "dbt", "pyspark", "salesforce", "twilio segment", "google analytics")
    def has(term: str, source: str) -> bool:
        return bool(re.search(r"\br\b", source)) if term == "r" else term in source
    skills = tuple(
        skill
        for skill in vocabulary
        if has(skill, required_text)
    )
    preferred = tuple(skill for skill in vocabulary if has(skill, preferred_text) and skill not in skills)
    rights = tuple(
        value
        for value in ("australian citizen", "permanent resident", "unrestricted work rights")
        if value in text
    )
    education = tuple(
        value
        for value in ("bachelor", "master", "phd")
        if value in text and ("required" in text or "must" in text)
    )
    years = tuple(re.findall(r"\b(?:at least |minimum of |minimum )?(?:\d+\s*(?:-\s*\d+)?|\d+)\+?\s+years?(?: of)? (?:professional |relevant |commercial )?experience\b", text))
    responsibilities = tuple(
        sentence.strip(" -•")
        for sentence in re.split(r"[\n•]+", description)
        if any(marker in sentence.casefold() for marker in ("write sql", "build", "create", "run ", "support ", "improve "))
    )[:8]
    seniority = next((word for word in SENIOR_TITLE_WORDS if word in title.casefold()), None)
    work_mode_match = re.search(r"work mode:\s*(remote|hybrid|on-site|onsite)", text)
    requirements = JobRequirements(
        required_skills=skills,
        preferred_skills=preferred,
        education_requirements=education,
        experience_requirements=years,
        work_right_requirements=rights,
        required_locations=((location,) if location else ()),
    )
    return JobDescriptionAnalysis(requirements, responsibilities, years, seniority, work_mode_match.group(1) if work_mode_match else None)


def extract_requirements(description: str, location: str | None) -> JobRequirements:
    """Compatibility wrapper for callers that only need deterministic requirements."""
    return analyze_job_description(description, location).requirements


def suggestion_from_profile(facts: list[CandidateFact], location: str | None = None) -> dict[str, str]:
    """Suggest only search preferences supported by stored candidate information."""
    profile_text = " ".join(fact.value for fact in facts if fact.active and fact.approved).casefold()
    roles: list[str] = []
    if any(term in profile_text for term in ("data", "analytics", "business intelligence", "reporting", "visualisation")):
        roles.extend(("Data Analyst", "Analytics Analyst", "Business Intelligence Analyst"))
    if any(term in profile_text for term in ("ai", "artificial intelligence", "machine learning")):
        roles.append("AI Analyst")
    locations = []
    location_text = (location or "").casefold()
    if "brisbane" in location_text:
        locations.extend(("Brisbane", "Queensland", "Remote Australia"))
    elif location:
        locations.append(location)
    level = ""
    if re.search(r"\b(graduate|student|intern|junior)\b", profile_text):
        level = "Graduate / entry-level"
    return {"roles": ", ".join(dict.fromkeys(roles)), "locations": ", ".join(dict.fromkeys(locations)), "level": level}
