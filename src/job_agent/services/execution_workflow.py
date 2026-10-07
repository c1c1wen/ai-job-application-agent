"""Coordinate inspect/fill stages while retaining pre-submit human control."""

from __future__ import annotations

import json
from pathlib import Path

from job_agent.domain.models import ApplicationState, ExecutionState
from job_agent.services.application_execution import (
    EmploymentHeroApplicationAdapter,
    FormField,
    FormInspection,
    LeverApplicationAdapter,
    LiveFormValidation,
    PageUpApplicationAdapter,
    detect_platform,
    plan_form_fill,
    serialise_fields,
    serialise_plans,
)
from job_agent.services.cover_letter_docx import CoverLetterExportError
from job_agent.services.execution_documents import prepare_execution_documents


def adapter_for(application_url: str):
    platform = detect_platform(application_url)
    if platform == "LEVER":
        return LeverApplicationAdapter()
    if platform == "EMPLOYMENT_HERO":
        return EmploymentHeroApplicationAdapter()
    if platform == "PAGEUP":
        return PageUpApplicationAdapter()
    raise ValueError("This public application platform is not supported for guided execution yet.")


def unanswered_questions(unresolved_fields: list[dict], answers: dict[str, object]) -> list[dict]:
    """Keep saved answers out of the user-input list without discarding history."""
    # Live validation may report a non-field blocker such as a missing file
    # attachment. Preserve it for the UI rather than assuming it is a form
    # question with a serialised field key.
    return [
        field
        for field in unresolved_fields
        if not isinstance(field, dict) or field.get("key") not in answers
    ]


def can_continue_waiting_session(
    session, unresolved_fields: list[dict], answers: dict[str, object]
) -> bool:
    """A saved final answer must not strand an existing resumable session."""
    return session["state"] == ExecutionState.WAITING_FOR_USER and not unanswered_questions(
        unresolved_fields, answers
    )


def _validate_live(
    adapter, application_url: str, plans, answers, expected_cv_filename: str
) -> LiveFormValidation:
    """Keep fakes compatible while requiring real adapters to inspect the DOM."""
    validator = getattr(adapter, "validate_live", None)
    if validator:
        return validator(application_url, plans, answers, expected_cv_filename)
    unresolved = [
        plan.field.label
        for plan in plans
        if plan.action == "USER_INPUT_REQUIRED" and plan.field.key not in answers
    ]
    return LiveFormValidation(application_url, True, True, not unresolved, tuple(unresolved))


def inspect_application(repo, session, profile, facts) -> FormInspection:
    adapter = adapter_for(session["application_url"])
    inspection = adapter.inspect(session["application_url"])
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        inspection.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    previous_context = repo.browser_context(session["id"])
    lost_active_pageup_form = bool(
        previous_context
        and previous_context["platform"] == "PAGEUP"
        and previous_context["pageup_stage"] in {"PAGEUP_APPLICATION_FORM", "PAGEUP_SECTION_PROGRESS", "PAGEUP_FINAL_REVIEW"}
        and inspection.pageup_stage in {"PAGEUP_INIT", "PAGEUP_PRIVACY_READY", "PAGEUP_PREFILL", "PAGEUP_CV_CONFIRMED"}
    )
    repo.record_browser_context(session["id"], inspection)
    if inspection.context_mismatch or lost_active_pageup_form:
        repo.update_execution(
            session["id"],
            ExecutionState.BROWSER_CONTEXT_MISMATCH,
            "The expected PageUp form is not in the active agent browser context",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            [
                "BROWSER_CONTEXT_MISMATCH: The PageUp page you are viewing is not controlled by the active agent browser session. Open the application through the agent-owned headed browser, then recover it from the official URL."
            ],
        )
        return inspection
    if inspection.authentication_required:
        repo.set_ats_authentication(session["platform"], "LOGIN_REQUIRED")
        repo.update_execution(
            session["id"],
            ExecutionState.WAITING_FOR_USER,
            "ATS login is required",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            list(inspection.warnings)
            + [
                "LOGIN_REQUIRED: Sign in through the local persistent browser profile, then use Resume all ready applications."
            ],
        )
    elif inspection.human_verification:
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            "Human verification detected",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            list(inspection.warnings)
            + ["Verification must be completed by the user; automation stopped."],
        )
    else:
        repo.set_ats_authentication(session["platform"], "AUTHENTICATED", verified=True)
        repo.update_execution(
            session["id"],
            ExecutionState.FILLING_FORM,
            "Visible form inspected",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            list(inspection.warnings),
        )
    return inspection


def fill_supported_fields(repo, session, profile, facts, artifacts_dir: Path) -> list[str]:
    adapter = adapter_for(session["application_url"])
    fields = tuple(FormField(**field) for field in json.loads(session["detected_fields_json"]))
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        fields, profile, facts, repo.application_profile(), job["location"] if job else None
    )
    candidate_cv = repo.application_cv_document(session["application_id"])
    if candidate_cv is None:
        raise ValueError("Choose the canonical CV for this application before uploading documents.")
    documents = prepare_execution_documents(
        repo.approved_artifacts(session["application_id"]), candidate_cv, artifacts_dir
    )
    uploaded = adapter.fill(
        session["application_url"], plans, [document.path for document in documents]
    )
    for index, filename in enumerate(uploaded):
        document = documents[index]
        repo.record_execution_upload(
            session["id"],
            document.artifact_id,
            document.artifact_version,
            document.source_document_id,
            filename,
            f"file-upload-{index + 1}",
        )
    unresolved = serialise_plans(plans, "USER_INPUT_REQUIRED")
    state = ExecutionState.WAITING_FOR_USER if unresolved else ExecutionState.READY_FOR_FINAL_REVIEW
    step = (
        "Paused for unresolved questions"
        if unresolved
        else "Ready for final user review; no submit action was taken"
    )
    repo.update_execution(
        session["id"],
        state,
        step,
        serialise_fields(fields),
        serialise_plans(plans, "AUTOFILL"),
        unresolved,
    )
    return uploaded


def refresh_execution_questions(repo, session, profile, facts) -> FormInspection:
    """Reinspect a persisted session after browser loss without applying answers."""
    repo.update_execution(
        session["id"],
        ExecutionState.RESUMING,
        "Reopening public application form to refresh questions",
        json.loads(session["detected_fields_json"]),
        json.loads(session["autofilled_fields_json"]),
        json.loads(session["unresolved_fields_json"]),
        json.loads(session["warnings_json"]),
    )
    inspection = adapter_for(session["application_url"]).inspect(session["application_url"])
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        inspection.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    unresolved = serialise_plans(plans, "USER_INPUT_REQUIRED")
    state = (
        ExecutionState.HUMAN_VERIFICATION_REQUIRED
        if inspection.human_verification
        else ExecutionState.WAITING_FOR_USER
    )
    warning = list(inspection.warnings)
    if inspection.human_verification:
        warning.append("Verification must be completed by the user; automation stopped.")
    repo.update_execution(
        session["id"],
        state,
        "Questions refreshed from visible form",
        serialise_fields(inspection.fields),
        serialise_plans(plans, "AUTOFILL"),
        unresolved,
        warning,
    )
    return inspection


def resume_execution(repo, session, profile, facts, artifacts_dir: Path) -> list[str]:
    """Replay grounded values and explicit user answers; never submit the form."""
    repo.update_execution(
        session["id"],
        ExecutionState.RESUMING,
        "Resuming with explicit user answers",
        json.loads(session["detected_fields_json"]),
        json.loads(session["autofilled_fields_json"]),
        json.loads(session["unresolved_fields_json"]),
        json.loads(session["warnings_json"]),
    )
    adapter = adapter_for(session["application_url"])
    inspection = adapter.inspect(session["application_url"])
    repo.record_browser_context(session["id"], inspection)
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        inspection.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    if inspection.authentication_required:
        repo.set_ats_authentication(session["platform"], "LOGIN_REQUIRED")
        repo.update_execution(
            session["id"],
            ExecutionState.WAITING_FOR_USER,
            "ATS login is required while resuming",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            ["LOGIN_REQUIRED: Sign in through the local persistent browser profile, then resume."],
        )
        return []
    if inspection.human_verification:
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            "Human verification detected while resuming",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            ["Verification must be completed by the user; automation stopped."],
        )
        return []
    candidate_cv = repo.application_cv_document(session["application_id"])
    if candidate_cv is None:
        raise ValueError("Choose the canonical CV for this application before replaying documents.")
    documents = prepare_execution_documents(
        repo.approved_artifacts(session["application_id"]), candidate_cv, artifacts_dir
    )
    answers = repo.answer_map(session["id"])
    uploaded = adapter.fill(
        session["application_url"], plans, [document.path for document in documents], answers
    )
    repo.record_execution_answer_replay(session["id"], len(answers))
    for index, filename in enumerate(uploaded):
        document = documents[index]
        repo.record_execution_upload(
            session["id"],
            document.artifact_id,
            document.artifact_version,
            document.source_document_id,
            filename,
            f"file-upload-{index + 1}",
        )
    refreshed = adapter.inspect(session["application_url"])
    refreshed_plans = plan_form_fill(
        refreshed.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    unresolved = [
        entry
        for entry in serialise_plans(refreshed_plans, "USER_INPUT_REQUIRED")
        if entry["key"] not in answers
    ]
    live_validation = _validate_live(
        adapter,
        session["application_url"],
        refreshed_plans,
        answers,
        candidate_cv["filename"],
    )
    repo.record_execution_diagnostic(session["id"], "resume_live_validation", live_validation, len(answers))
    if refreshed.authentication_required:
        repo.set_ats_authentication(session["platform"], "SESSION_EXPIRED")
        state = ExecutionState.WAITING_FOR_USER
        step = "ATS session expired while applying saved answers"
        warnings = [
            "LOGIN_REQUIRED: Sign in through the local persistent browser profile, then resume."
        ]
    elif refreshed.human_verification:
        state = ExecutionState.HUMAN_VERIFICATION_REQUIRED
        step = "Human verification detected after applying user answers"
        warnings = ["Verification must be completed by the user; automation stopped."]
    elif unresolved:
        state = ExecutionState.WAITING_FOR_USER
        step = "Paused for unresolved or newly revealed questions"
        warnings = []
    elif not live_validation.ready_to_submit:
        # Planned values and historical upload events are insufficient. The
        # live browser must confirm the current CV and each required field
        # before this session can enter final review.
        unresolved = list(live_validation.missing_fields)
        state = ExecutionState.WAITING_FOR_USER
        step = "Live form validation requires another safe PageUp step"
        warnings = [
            "The visible form has not yet confirmed the required fields and CV attachment. Nothing was submitted."
        ]
    else:
        repo.set_ats_authentication(session["platform"], "AUTHENTICATED", verified=True)
        state = ExecutionState.READY_FOR_FINAL_REVIEW
        step = "All required answers applied; no submit action was taken"
        warnings = []
    repo.update_execution(
        session["id"],
        state,
        step,
        serialise_fields(refreshed.fields),
        serialise_plans(refreshed_plans, "AUTOFILL"),
        unresolved,
        warnings,
    )
    return uploaded


def resume_all_authenticated_sessions(repo, profile, facts, artifacts_root: Path) -> dict[str, int]:
    """Resume every login-blocked session whose ATS was freshly authenticated.

    This is deliberately pre-submit only. Each session independently pauses
    again for a question, CAPTCHA, expired login, or form change, so one bad
    application never stops the rest of the queue.
    """
    result = {"resumed": 0, "paused": 0}
    for session in repo.authentication_ready_sessions():
        try:
            resume_execution(repo, session, profile, facts, artifacts_root / str(session["id"]))
            result["resumed"] += 1
        except (RuntimeError, ValueError, CoverLetterExportError):
            result["paused"] += 1
    return result


def submit_execution(
    repo, session, profile, facts, artifacts_dir: Path, *, explicit_confirmation: bool
) -> None:
    """Perform the one permitted submit action only after a fresh user confirmation."""
    if not explicit_confirmation:
        raise ValueError("Explicit final submission confirmation is required.")
    repo.begin_final_submission(session["id"])
    adapter = adapter_for(session["application_url"])
    inspection = adapter.inspect(session["application_url"])
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        inspection.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    answers = repo.answer_map(session["id"])
    if inspection.human_verification:
        repo.transition(
            session["application_id"],
            ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            "system",
            "Human verification detected before final submission",
        )
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            "Human verification detected before final submission",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            serialise_plans(plans, "USER_INPUT_REQUIRED"),
            ["Verification must be completed by the user; submission stopped."],
        )
        return
    candidate_cv = repo.application_cv_document(session["application_id"])
    if candidate_cv is None:
        raise ValueError("Choose the canonical CV for this application before final execution.")
    documents = prepare_execution_documents(
        repo.approved_artifacts(session["application_id"]), candidate_cv, artifacts_dir
    )
    uploaded = adapter.fill(
        session["application_url"], plans, [document.path for document in documents], answers
    )
    repo.record_execution_answer_replay(session["id"], len(answers))
    for index, filename in enumerate(uploaded):
        document = documents[index]
        repo.record_execution_upload(
            session["id"],
            document.artifact_id,
            document.artifact_version,
            document.source_document_id,
            filename,
            f"file-upload-{index + 1}",
        )
    refreshed = adapter.inspect(session["application_url"])
    refreshed_plans = plan_form_fill(
        refreshed.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    unresolved = unanswered_questions(
        serialise_plans(refreshed_plans, "USER_INPUT_REQUIRED"), answers
    )
    if refreshed.human_verification:
        repo.transition(
            session["application_id"],
            ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            "system",
            "Human verification appeared before final submission",
        )
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            "Human verification detected before final submission",
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            unresolved,
            ["Verification must be completed by the user; submission stopped."],
        )
        return
    if unresolved:
        repo.update_execution(
            session["id"],
            ExecutionState.WAITING_FOR_USER,
            "New required input appeared before final submission",
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            unresolved,
            ["Submission stopped until the new input is supplied."],
        )
        return
    validation = _validate_live(
        adapter, session["application_url"], refreshed_plans, answers, candidate_cv["filename"]
    )
    repo.record_execution_diagnostic(session["id"], "pre_submit", validation, len(answers))
    if validation.human_verification:
        repo.transition(
            session["application_id"],
            ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            "system",
            "Human verification detected during pre-submit validation",
        )
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            "Human verification is pending; no submit action was taken",
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            [],
            ["Complete verification yourself, then revalidate the live form."],
        )
        return
    if not validation.ready_to_submit:
        repo.update_execution(
            session["id"],
            ExecutionState.WAITING_FOR_USER,
            "Live form validation blocked submission",
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            list(validation.missing_fields),
            [
                "Live form validation did not confirm a complete CV attachment and required fields. Nothing was submitted."
            ],
        )
        return
    result = adapter.submit(session["application_url"])
    if result.human_verification:
        repo.transition(
            session["application_id"],
            ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            "system",
            "Human verification appeared during final submission",
        )
        repo.update_execution(
            session["id"],
            ExecutionState.HUMAN_VERIFICATION_REQUIRED,
            result.message,
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            [],
            [result.message],
        )
    elif result.verification_error:
        repo.transition(
            session["application_id"],
            ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            "system",
            "Lever reported a post-verification error",
        )
        repo.update_execution(
            session["id"],
            ExecutionState.POST_VERIFICATION_REVALIDATION,
            result.message,
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            [],
            [
                result.message,
                "A fresh form revalidation and a new explicit confirmation are required. Nothing was submitted.",
            ],
        )
    elif result.verified:
        repo.mark_execution_submitted(session["id"], result.message)
    else:
        repo.update_execution(
            session["id"],
            ExecutionState.FAILED,
            result.message or "Submission could not be verified",
            serialise_fields(refreshed.fields),
            serialise_plans(refreshed_plans, "AUTOFILL"),
            [],
        )


def revalidate_after_human_verification(
    repo, session, profile, facts, artifacts_dir: Path
) -> LiveFormValidation:
    """Safely restore and validate a visible form after a user-owned checkpoint."""
    current_state = ExecutionState(session["state"])
    if current_state in {
        ExecutionState.HUMAN_VERIFICATION_REQUIRED,
        ExecutionState.FAILED,
        ExecutionState.READY_FOR_FINAL_REVIEW,
    }:
        repo.update_execution(
            session["id"],
            (
                ExecutionState.RESUMING
                if current_state == ExecutionState.READY_FOR_FINAL_REVIEW
                else ExecutionState.POST_VERIFICATION_REVALIDATION
            ),
            "Live form revalidation started; no submit action was taken",
            json.loads(session["detected_fields_json"]),
            json.loads(session["autofilled_fields_json"]),
            json.loads(session["unresolved_fields_json"]),
            json.loads(session["warnings_json"]),
        )
    adapter = adapter_for(session["application_url"])
    inspection = adapter.inspect(session["application_url"])
    job = repo.execution_job(session["id"])
    plans = plan_form_fill(
        inspection.fields,
        profile,
        facts,
        repo.application_profile(),
        job["location"] if job else None,
    )
    answers = repo.answer_map(session["id"])
    if inspection.human_verification:
        validation = LiveFormValidation(
            session["application_url"], True, False, False, human_verification=True
        )
        repo.record_execution_diagnostic(session["id"], "post_verification_checkpoint", validation)
        if session["state"] != ExecutionState.HUMAN_VERIFICATION_REQUIRED:
            application = repo.connection.execute(
                "SELECT state FROM applications WHERE id=?", (session["application_id"],)
            ).fetchone()
            if application and application["state"] == ApplicationState.APPLYING:
                repo.transition(
                    session["application_id"],
                    ApplicationState.HUMAN_VERIFICATION_REQUIRED,
                    "system",
                    "Human verification detected during recovery",
                )
            repo.update_execution(
                session["id"],
                ExecutionState.HUMAN_VERIFICATION_REQUIRED,
                "Human verification is pending; no submit action was taken",
                serialise_fields(inspection.fields),
                serialise_plans(plans, "AUTOFILL"),
                serialise_plans(plans, "USER_INPUT_REQUIRED"),
                list(inspection.warnings)
                + ["Complete verification yourself before revalidating again."],
            )
        return validation
    candidate_cv = repo.application_cv_document(session["application_id"])
    if candidate_cv is None:
        raise ValueError("Choose the canonical CV for this application before revalidating.")
    # A historical upload event is not evidence that the current browser DOM
    # still has the file after a third-party verification/reload.  Inspect it
    # first, then upload only if the live attachment is absent.
    before_replay = _validate_live(
        adapter, session["application_url"], plans, answers, candidate_cv["filename"]
    )
    repo.record_execution_diagnostic(
        session["id"], "post_verification_before_replay", before_replay, len(answers)
    )
    if before_replay.verification_error:
        # A completed challenge can leave Lever on an error/reconstructed form.
        # Reopen only after this explicit revalidation action, never as a submit
        # continuation, then build a new grounded replay plan from that DOM.
        reopen = getattr(adapter, "reopen_form", None)
        if not reopen:
            return before_replay
        inspection = reopen(session["application_url"])
        if inspection.human_verification:
            validation = LiveFormValidation(
                session["application_url"], True, False, False, human_verification=True
            )
            repo.record_execution_diagnostic(
                session["id"], "post_verification_reopened", validation
            )
            repo.update_execution(
                session["id"],
                ExecutionState.HUMAN_VERIFICATION_REQUIRED,
                "Human verification is pending after reopening the public form",
                serialise_fields(inspection.fields),
                serialise_plans(plans, "AUTOFILL"),
                serialise_plans(plans, "USER_INPUT_REQUIRED"),
                list(inspection.warnings),
            )
            return validation
        plans = plan_form_fill(
            inspection.fields,
            profile,
            facts,
            repo.application_profile(),
            job["location"] if job else None,
        )
        before_replay = _validate_live(
            adapter, session["application_url"], plans, answers, candidate_cv["filename"]
        )
        repo.record_execution_diagnostic(
            session["id"], "post_verification_reopened", before_replay, len(answers)
        )
    documents = prepare_execution_documents(
        repo.approved_artifacts(session["application_id"]), candidate_cv, artifacts_dir
    )
    upload_paths = (
        [document.path for document in documents] if not before_replay.cv_attached else []
    )
    uploaded = adapter.fill(session["application_url"], plans, upload_paths, answers)
    repo.record_execution_answer_replay(session["id"], len(answers))
    for index, filename in enumerate(uploaded):
        document = documents[index]
        repo.record_execution_upload(
            session["id"],
            document.artifact_id,
            document.artifact_version,
            document.source_document_id,
            filename,
            f"file-upload-{index + 1}",
        )
    validation = _validate_live(
        adapter, session["application_url"], plans, answers, candidate_cv["filename"]
    )
    repo.record_execution_diagnostic(
        session["id"], "post_verification_revalidated", validation, len(answers)
    )
    if validation.human_verification:
        # The active session is already paused at this human-owned checkpoint.
        return validation
    elif validation.ready_to_submit:
        application = repo.connection.execute(
            "SELECT state FROM applications WHERE id=?", (session["application_id"],)
        ).fetchone()
        if application and application["state"] == ApplicationState.HUMAN_VERIFICATION_REQUIRED:
            repo.transition(
                session["application_id"],
                ApplicationState.APPLYING,
                "system",
                "Human verification completed; form revalidated",
            )
        repo.update_execution(
            session["id"],
            ExecutionState.READY_FOR_FINAL_REVIEW,
            "Live form revalidated after human verification; a fresh confirmation is required",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            [],
            list(inspection.warnings),
        )
    else:
        repo.update_execution(
            session["id"],
            ExecutionState.WAITING_FOR_USER,
            "Live form requires attention after human verification",
            serialise_fields(inspection.fields),
            serialise_plans(plans, "AUTOFILL"),
            list(validation.missing_fields),
            list(inspection.warnings)
            + ["Nothing was submitted. Review the listed live-form issue before trying again."],
        )
    return validation
