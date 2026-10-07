from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CandidateFactType(StrEnum):
    NAME = "name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    WORK_RIGHT = "work_right"
    EDUCATION = "education"
    EXPERIENCE = "experience"
    PROJECT = "project"
    SKILL = "skill"
    TOOL = "tool"
    PROGRAMMING_LANGUAGE = "programming_language"
    CERTIFICATION = "certification"
    AVAILABILITY = "availability"


CANDIDATE_FACT_TYPES = tuple(item.value for item in CandidateFactType)


class ApplicationState(StrEnum):
    DISCOVERED = "DISCOVERED"
    SCREENED = "SCREENED"
    REVIEW = "REVIEW"
    MATERIALS_READY = "MATERIALS_READY"
    APPROVED = "APPROVED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLYING = "APPLYING"  # V2 execution adapter only.
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    APPLIED = "APPLIED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"
    REJECTED = "REJECTED"
    CLOSED = "CLOSED"
    SKIPPED = "SKIPPED"


class ExecutionState(StrEnum):
    READY_TO_APPLY = "READY_TO_APPLY"
    OPENING_APPLICATION = "OPENING_APPLICATION"
    FILLING_FORM = "FILLING_FORM"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    RESUMING = "RESUMING"
    HUMAN_VERIFICATION_REQUIRED = "HUMAN_VERIFICATION_REQUIRED"
    POST_VERIFICATION_REVALIDATION = "POST_VERIFICATION_REVALIDATION"
    READY_FOR_FINAL_REVIEW = "READY_FOR_FINAL_REVIEW"
    SUBMITTING = "SUBMITTING"
    APPLIED = "APPLIED"
    BROWSER_CONTEXT_MISMATCH = "BROWSER_CONTEXT_MISMATCH"
    FAILED = "FAILED"


@dataclass(frozen=True)
class CandidateFact:
    fact_type: str
    value: str
    evidence: str
    approved: bool = True
    id: int | None = None
    active: bool = True


@dataclass(frozen=True)
class JobRequirements:
    required_skills: tuple[str, ...] = ()
    preferred_skills: tuple[str, ...] = ()
    education_requirements: tuple[str, ...] = ()
    experience_requirements: tuple[str, ...] = ()
    work_right_requirements: tuple[str, ...] = ()
    required_locations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScoreConfig:
    skills_weight: int = 35
    role_weight: int = 20
    experience_weight: int = 20
    education_weight: int = 10
    location_weight: int = 10
    preferences_weight: int = 5
    strong_match_threshold: int = 85
    good_match_threshold: int = 70
    possible_match_threshold: int = 55

    def __post_init__(self) -> None:
        if sum(self.weights) != 100:
            raise ValueError("Score weights must sum to 100.")
        if not 0 <= self.possible_match_threshold <= self.good_match_threshold <= self.strong_match_threshold <= 100:
            raise ValueError("Match thresholds must be ordered: possible, good, strong.")

    @property
    def weights(self) -> tuple[int, ...]:
        return (self.skills_weight, self.role_weight, self.experience_weight, self.education_weight, self.location_weight, self.preferences_weight)


@dataclass(frozen=True)
class ScoreBreakdown:
    skills: int
    role_relevance: int
    experience: int
    education: int
    location: int
    preferences: int
    eligibility_passed: bool
    hard_gate_reasons: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    matched_skills: tuple[str, ...] = ()
    missing_skills: tuple[str, ...] = ()
    matched_preferred_skills: tuple[str, ...] = ()
    missing_preferred_skills: tuple[str, ...] = ()
    experience_evidence: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return 0 if not self.eligibility_passed else sum((self.skills, self.role_relevance, self.experience, self.education, self.location, self.preferences))


@dataclass(frozen=True)
class JobInput:
    title: str
    company: str
    url: str
    description: str
    source: str = "MANUAL"
    location: str | None = None
    source_key: str | None = None
    external_id: str | None = None
    raw_payload: str | None = None
