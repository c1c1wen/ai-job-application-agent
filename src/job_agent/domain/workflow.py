from __future__ import annotations

from job_agent.domain.models import ApplicationState


class InvalidTransition(ValueError):
    pass


TERMINAL_STATES = {
    ApplicationState.APPLIED,
    ApplicationState.OFFER,
    ApplicationState.REJECTED,
    ApplicationState.CLOSED,
    ApplicationState.SKIPPED,
}

ALLOWED_TRANSITIONS: dict[ApplicationState, set[ApplicationState]] = {
    ApplicationState.DISCOVERED: {ApplicationState.SCREENED, ApplicationState.SKIPPED},
    ApplicationState.SCREENED: {ApplicationState.REVIEW, ApplicationState.SKIPPED},
    ApplicationState.REVIEW: {ApplicationState.MATERIALS_READY, ApplicationState.SKIPPED},
    ApplicationState.MATERIALS_READY: {ApplicationState.APPROVED, ApplicationState.REVIEW, ApplicationState.SKIPPED},
    ApplicationState.APPROVED: {ApplicationState.READY_TO_APPLY, ApplicationState.REVIEW, ApplicationState.CLOSED},
    ApplicationState.READY_TO_APPLY: {ApplicationState.APPLYING, ApplicationState.REVIEW, ApplicationState.CLOSED},
    ApplicationState.APPLYING: {ApplicationState.HUMAN_VERIFICATION_REQUIRED, ApplicationState.APPLIED, ApplicationState.REVIEW},
    ApplicationState.HUMAN_VERIFICATION_REQUIRED: {ApplicationState.APPLYING, ApplicationState.REVIEW, ApplicationState.CLOSED},
    ApplicationState.APPLIED: {ApplicationState.INTERVIEW, ApplicationState.OFFER, ApplicationState.REJECTED, ApplicationState.CLOSED},
    ApplicationState.INTERVIEW: {ApplicationState.OFFER, ApplicationState.REJECTED, ApplicationState.CLOSED},
    ApplicationState.OFFER: set(),
    ApplicationState.REJECTED: set(),
    ApplicationState.CLOSED: set(),
    ApplicationState.SKIPPED: set(),
}


def assert_transition(current: ApplicationState, target: ApplicationState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"Cannot transition from {current} to {target}.")


def is_human_checkpoint(state: ApplicationState) -> bool:
    return state in {ApplicationState.REVIEW, ApplicationState.APPROVED, ApplicationState.HUMAN_VERIFICATION_REQUIRED}

