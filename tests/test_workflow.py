import pytest

from job_agent.domain.models import ApplicationState, ExecutionState
from job_agent.domain.workflow import (
    InvalidTransition,
    assert_execution_transition,
    assert_transition,
    is_human_checkpoint,
)


def test_approval_workflow_is_explicit():
    assert_transition(ApplicationState.MATERIALS_READY, ApplicationState.APPROVED)
    assert_transition(ApplicationState.APPROVED, ApplicationState.READY_TO_APPLY)


def test_captcha_is_a_resumable_human_checkpoint():
    assert is_human_checkpoint(ApplicationState.HUMAN_VERIFICATION_REQUIRED)
    assert_transition(ApplicationState.HUMAN_VERIFICATION_REQUIRED, ApplicationState.APPLYING)


def test_submission_cannot_skip_approval():
    with pytest.raises(InvalidTransition):
        assert_transition(ApplicationState.REVIEW, ApplicationState.APPLIED)


def test_final_review_can_be_revalidated_without_submitting():
    assert_execution_transition(ExecutionState.READY_FOR_FINAL_REVIEW, ExecutionState.RESUMING)
