from __future__ import annotations

from job_agent.domain.models import ApplicationState, ExecutionState


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


ALLOWED_EXECUTION_TRANSITIONS: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.READY_TO_APPLY: {ExecutionState.OPENING_APPLICATION, ExecutionState.BROWSER_CONTEXT_MISMATCH},
    ExecutionState.OPENING_APPLICATION: {ExecutionState.FILLING_FORM, ExecutionState.HUMAN_VERIFICATION_REQUIRED, ExecutionState.BROWSER_CONTEXT_MISMATCH, ExecutionState.FAILED},
    ExecutionState.FILLING_FORM: {ExecutionState.WAITING_FOR_USER, ExecutionState.HUMAN_VERIFICATION_REQUIRED, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.BROWSER_CONTEXT_MISMATCH, ExecutionState.FAILED},
    ExecutionState.WAITING_FOR_USER: {ExecutionState.RESUMING, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.BROWSER_CONTEXT_MISMATCH, ExecutionState.FAILED},
    ExecutionState.RESUMING: {ExecutionState.WAITING_FOR_USER, ExecutionState.HUMAN_VERIFICATION_REQUIRED, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.BROWSER_CONTEXT_MISMATCH, ExecutionState.FAILED},
    ExecutionState.HUMAN_VERIFICATION_REQUIRED: {ExecutionState.POST_VERIFICATION_REVALIDATION, ExecutionState.RESUMING, ExecutionState.WAITING_FOR_USER, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.FAILED},
    ExecutionState.POST_VERIFICATION_REVALIDATION: {ExecutionState.HUMAN_VERIFICATION_REQUIRED, ExecutionState.WAITING_FOR_USER, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.FAILED},
    ExecutionState.READY_FOR_FINAL_REVIEW: {ExecutionState.SUBMITTING, ExecutionState.RESUMING, ExecutionState.BROWSER_CONTEXT_MISMATCH},
    ExecutionState.SUBMITTING: {ExecutionState.APPLIED, ExecutionState.FAILED, ExecutionState.HUMAN_VERIFICATION_REQUIRED, ExecutionState.POST_VERIFICATION_REVALIDATION, ExecutionState.WAITING_FOR_USER},
    ExecutionState.APPLIED: set(),
    ExecutionState.BROWSER_CONTEXT_MISMATCH: {ExecutionState.OPENING_APPLICATION, ExecutionState.RESUMING, ExecutionState.WAITING_FOR_USER, ExecutionState.FAILED},
    ExecutionState.FAILED: {ExecutionState.POST_VERIFICATION_REVALIDATION, ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.HUMAN_VERIFICATION_REQUIRED},
}


def assert_execution_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in ALLOWED_EXECUTION_TRANSITIONS[current]:
        raise InvalidTransition(f"Cannot transition execution from {current} to {target}.")
