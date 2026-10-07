import json
from pathlib import Path

import pytest

from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    JobInput,
    JobRequirements,
    ScoreConfig,
)
from job_agent.domain.scoring import score_job
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.application_execution import (
    FormField,
    FormInspection,
    LeverApplicationAdapter,
    LiveFormValidation,
    PageUpApplicationAdapter,
    SubmissionResult,
    _human_verification_present,
    _preserve_non_editable_text_control,
    _verification_status,
    answer_category,
    detect_platform,
    plan_form_fill,
    reusable_answer_allowed,
    validate_user_answer,
)


def test_non_editable_server_populated_text_field_is_preserved() -> None:
    """PageUp may make an already-set account email read-only."""

    class ReadOnlyEmail:
        def is_editable(self):
            return False

    assert _preserve_non_editable_text_control(ReadOnlyEmail()) is True
from job_agent.services.execution_documents import prepare_execution_documents
from job_agent.services.execution_workflow import (
    adapter_for,
    can_continue_waiting_session,
    resume_execution,
    revalidate_after_human_verification,
    submit_execution,
    unanswered_questions,
)


def test_pageup_public_application_uses_visible_form_adapter() -> None:
    url = "https://secure.dc2.pageuppeople.com/apply/586/cw/applicationForm/initApplication.asp?lJobID=501490"

    assert detect_platform(url) == "PAGEUP"
    assert adapter_for(url).platform == "PAGEUP"


def test_pageup_continue_with_application_is_an_intermediate_navigation_action() -> None:
    """The wording includes 'application' but is not final submission."""

    class Body:
        def inner_text(self):
            return "Upload resume"

    class Control:
        def __init__(self):
            self.clicked = 0

        def is_visible(self):
            return True

        def is_disabled(self):
            return False

        def evaluate(self, _script):
            return "Continue with application"

        def click(self):
            self.clicked += 1

    class Controls:
        def __init__(self, control):
            self.control = control

        def all(self):
            return [self.control]

        def evaluate_all(self, _script):
            return {"active": False, "containerPresent": False}

    class Form:
        def __init__(self):
            self.control = Control()

        def locator(self, selector):
            if selector == "body":
                return Body()
            return Controls(self.control)

        def wait_for_timeout(self, _milliseconds):
            pass

    form = Form()
    PageUpApplicationAdapter._advance_completed_intermediate_page(form)

    assert form.control.clicked == 1


def test_pageup_follows_the_active_public_application_tab(monkeypatch) -> None:
    class Tab:
        def __init__(self, url: str):
            self.url = url

        def is_closed(self):
            return False

    prefill = Tab("https://secure.dc2.pageuppeople.com/apply/586/cw/applicationForm/preFill.asp")
    application = Tab("https://secure.dc2.pageuppeople.com/apply/586/cw/applicationForm/application.asp")
    prefill.context = type("Context", (), {"pages": [prefill, application]})()

    class Worker:
        def call(self, _url, operation):
            return operation(prefill)

    monkeypatch.setattr("job_agent.services.application_execution._BROWSER_WORKER", Worker())

    assert PageUpApplicationAdapter()._on_page("https://secure.dc2.pageuppeople.com/apply/586", lambda page: page) is application


def test_pageup_prefill_upload_is_confirmed_after_native_input_rerenders() -> None:
    class Control:
        def __init__(self, label: str):
            self.label = label

        def is_visible(self):
            return True

        def evaluate(self, _script):
            return self.label

    class Controls:
        def all(self):
            return [Control("Delete")]

    class Form:
        def locator(self, _selector):
            return Controls()

    assert PageUpApplicationAdapter._cv_confirmation_present(
        Form(), "CV_Alex_Candidate.pdf (95 kb)", "CV_Alex_Candidate.pdf"
    )


def test_pageup_detects_real_form_in_same_origin_frame() -> None:
    class Locator:
        def __init__(self, count=0):
            self._count = count

        def count(self):
            return self._count

    class Frame:
        def locator(self, _selector):
            return Locator(5)

    class Page:
        def __init__(self):
            self.url = "https://secure.dc2.pageuppeople.com/apply/586/cw/applicationForm/application.asp"
            self.frames = [Frame()]

    assert PageUpApplicationAdapter._pageup_stage(Page(), Frame(), "Personal details Save and continue") == "PAGEUP_APPLICATION_FORM"


def test_pageup_stage_classifies_privacy_prefill_and_confirmation() -> None:
    class Locator:
        def __init__(self, count=0):
            self._count = count

        def count(self):
            return self._count

        def all(self):
            return []

    class Form:
        def locator(self, _selector):
            return Locator()

    class Page:
        def __init__(self, url):
            self.url = url

    form = Form()
    assert PageUpApplicationAdapter._pageup_stage(Page("https://x/pageuppeople.com/apply/initApplication.asp"), form, "Privacy Statement") == "PAGEUP_PRIVACY_READY"
    assert PageUpApplicationAdapter._pageup_stage(Page("https://x/pageuppeople.com/apply/preFill.asp"), form, "Upload resume") == "PAGEUP_PREFILL"


def test_browser_context_metadata_is_persisted_without_secrets(tmp_path) -> None:
    connection = connect(tmp_path / "agent.sqlite")
    migrate(connection)
    repo = JobRepository(connection)
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://example.test/job", "SQL"))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "test", "materials")
    repo.transition(application_id, ApplicationState.APPROVED, "test", "approved")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "test", "ready")
    session_id = repo.start_execution(application_id, "PAGEUP", "https://secure.pageuppeople.com/apply/test")
    inspection = FormInspection(
        "PAGEUP", (), page_url="https://secure.pageuppeople.com/apply/test/application.asp",
        browser_worker_id="worker-1", browser_context_id="context-1", browser_page_id="page-1",
        pageup_stage="PAGEUP_APPLICATION_FORM", cv_confirmed=True,
    )

    repo.record_browser_context(session_id, inspection)

    stored = repo.browser_context(session_id)
    assert dict(stored)["worker_instance_id"] == "worker-1"
    assert dict(stored)["pageup_stage"] == "PAGEUP_APPLICATION_FORM"
    assert dict(stored)["cv_confirmed"] == 1


def _attach_canonical_cv(repo: JobRepository, application_id: int, tmp_path: Path) -> None:
    source = tmp_path / "CV_Alex_Candidate.pdf"
    source.write_bytes(b"test canonical cv")
    canonical = repo.register_canonical_cv(str(source), source.name, "test-canonical-hash-" + str(application_id), 1, source.stat().st_size, "Test CV")
    copied = tmp_path / "application-cv.pdf"
    copied.write_bytes(source.read_bytes())
    repo.attach_application_cv(application_id, canonical["id"], canonical["source_document_id"], source.name, canonical["sha256"], str(copied))


def test_fill_plan_uses_only_explicit_values_and_pauses_sensitive_questions() -> None:
    profile = {"display_name": "Ada Lovelace", "email": "ada@example.com", "phone": "", "location": "Brisbane, QLD"}
    facts = [
        CandidateFact("work_right", "Australian permanent resident", "CV", id=1),
        CandidateFact("availability", "Available from 1 July", "CV", id=2),
    ]
    fields = (
        FormField("First name", "first_name", "text", True),
        FormField("Email address", "email", "email", True),
        FormField("Are you authorised to work?", "work_authorisation", "text", True),
        FormField("Salary expectations", "salary", "text", True),
        FormField("Years of experience", "years", "text", True),
    )

    plans = {plan.field.name: plan for plan in plan_form_fill(fields, profile, facts)}

    assert plans["first_name"].value == "Ada"
    assert plans["email"].value == "ada@example.com"
    assert plans["work_authorisation"].value == "Australian permanent resident"
    assert plans["salary"].action == "USER_INPUT_REQUIRED"
    assert plans["years"].action == "USER_INPUT_REQUIRED"


def test_current_employer_never_uses_past_geovis_internship() -> None:
    facts = [CandidateFact("experience", "Data Management Intern | Example Analytics Co., Ltd. | Beijing | June 2023 - Dec 2023", "CV")]
    values = {
        "employment_status": {"value": "Not currently employed", "reusable": True, "category": "reusable"},
        "current_employer": {"value": None, "reusable": True, "category": "reusable"},
        "current_job_title": {"value": None, "reusable": True, "category": "reusable"},
    }
    optional_current = FormField("Current employer", "current_employer", "text", False)
    required_current = FormField("Current company", "current_company", "text", True)
    recent_employer = FormField("Most recent employer", "recent_employer", "text", True)
    current_title = FormField("Current job title", "current_title", "text", True)

    plans = plan_form_fill((optional_current, required_current, recent_employer, current_title), {}, facts, values)

    assert [(plan.action, plan.value) for plan in plans] == [
        ("LEAVE_BLANK", None),
        ("USER_INPUT_REQUIRED", None),
        ("AUTOFILL", "Example Analytics Co., Ltd."),
        ("USER_INPUT_REQUIRED", None),
    ]


def test_completed_study_and_internship_do_not_become_current_or_professional_tenure() -> None:
    fields = (
        FormField("Are you currently studying?", "current_study", "radio", True, ("Yes", "No")),
        FormField("Current university", "university", "text", False),
        FormField("Years of professional experience", "professional_years", "text", True),
    )
    facts = [
        CandidateFact("education", "Master of Data Science | UQ | July 2024 - July 2026", "CV"),
        CandidateFact("experience", "Data Management Intern | Example Analytics Co., Ltd. | June 2023 - Dec 2023", "CV"),
        CandidateFact("project", "Data project", "CV"),
    ]

    plans = plan_form_fill(fields, {}, facts, {})

    assert [plan.action for plan in plans] == ["USER_INPUT_REQUIRED", "LEAVE_BLANK", "USER_INPUT_REQUIRED"]


def test_dormant_captcha_nodes_do_not_keep_a_session_in_human_verification() -> None:
    class Locator:
        def __init__(self, evidence: dict):
            self.evidence = evidence

        def evaluate_all(self, _script):
            return self.evidence

    class Page:
        def __init__(self, evidence: dict):
            self.evidence = evidence

        def locator(self, _selector):
            return Locator(self.evidence)

    hidden = Page({"active": False, "containerPresent": True})
    zero_size = Page({"active": False, "containerPresent": True})
    preloaded_frame = Page({"active": False, "containerPresent": True})
    active = Page({"active": True, "containerPresent": True})

    assert _verification_status(hidden, "") == "hCaptcha container present but no active challenge."
    assert not _human_verification_present(hidden, "")
    assert not _human_verification_present(zero_size, "")
    assert not _human_verification_present(preloaded_frame, "")
    assert _human_verification_present(active, "")
    assert _human_verification_present(hidden, "Please complete the CAPTCHA to continue")


def test_adapter_never_caches_playwright_page_handles_across_ui_actions(monkeypatch) -> None:
    class Worker:
        def __init__(self):
            self.calls: list[str] = []

        def call(self, url, operation):
            self.calls.append(url)
            return operation(object())

    worker = Worker()
    monkeypatch.setattr("job_agent.services.application_execution._BROWSER_WORKER", worker)
    adapter = LeverApplicationAdapter()

    first = adapter._on_page("https://jobs.lever.co/example/apply", lambda page: page)
    second = adapter._on_page("https://jobs.lever.co/example/apply", lambda page: page)

    assert worker.calls == ["https://jobs.lever.co/example/apply", "https://jobs.lever.co/example/apply"]
    assert first is not second
    assert not hasattr(adapter, "page")


def test_execution_session_keeps_application_ready_to_apply_and_records_approved_uploads(tmp_path: Path) -> None:
    repo = JobRepository(connect(tmp_path / "execution.db"))
    migrate(repo.connection)
    profile_id = repo.get_or_create_profile("Ada")
    fact_id = repo.add_fact(profile_id, CandidateFact("skill", "Python", "CV"))
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/1", "Python"))
    score = score_job(JobRequirements(required_skills=("Python",)), "Data Analyst", repo.facts(), None, ScoreConfig())
    repo.screen_and_assess(job_id, score)
    application_id = repo.create_application(job_id)
    repo.add_artifact(application_id, "cover_letter", "First.\n\nSecond.\n\nThird.", [fact_id])
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    _attach_canonical_cv(repo, application_id, tmp_path)

    session_id = repo.start_execution(application_id, detect_platform("https://jobs.lever.co/example/1"), "https://jobs.lever.co/example/1")

    assert repo.latest_execution(application_id)["state"] == "OPENING_APPLICATION"
    assert repo.connection.execute("SELECT state FROM applications WHERE id=?", (application_id,)).fetchone()[0] == "READY_TO_APPLY"
    assert [artifact["kind"] for artifact in repo.approved_artifacts(application_id)] == ["cover_letter"]
    assert repo.execution_events(session_id)[0]["step"] == "Execution session started"


def test_document_preparation_uses_only_approved_cover_letter_version(tmp_path: Path) -> None:
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"pdf")
    artifact = {"id": 7, "kind": "cover_letter", "content": "One.\n\nTwo.\n\nThree.", "version": 4}
    candidate_cv = {"id": 2, "stored_path": str(cv), "filename": "candidate.pdf"}

    documents = prepare_execution_documents([artifact], candidate_cv, tmp_path / "uploads")

    assert [(document.artifact_id, document.artifact_version, document.source_document_id) for document in documents] == [(None, None, 2), (7, 4, None)]
    assert all(document.path.is_file() for document in documents)


def test_preferences_can_be_reused_but_consent_can_never_be_reused() -> None:
    pronouns = FormField("What are your pronouns?", "pronouns", "radio", True, ("They/them",), "pronouns")
    consent = FormField("I consent to the privacy policy", "consent", "checkbox", True, (), "consent")

    assert answer_category(pronouns) == "preference"
    assert reusable_answer_allowed(pronouns)
    assert answer_category(consent) == "legal"
    assert not reusable_answer_allowed(consent)
    assert plan_form_fill((consent,), {}, [])[0].action == "USER_INPUT_REQUIRED"


def test_answer_validation_accepts_only_presented_options() -> None:
    field = {"field_type": "radio", "options": ["Yes", "No"]}

    validate_user_answer(field, "Yes")
    try:
        validate_user_answer(field, "Maybe")
    except ValueError as exc:
        assert "available options" in str(exc)
    else:
        raise AssertionError("Unsupported option was accepted")


def test_legal_consent_requires_an_explicit_selection() -> None:
    consent = {"field_type": "checkbox", "options": ["I agree"], "category": "legal"}

    try:
        validate_user_answer(consent, False)
    except ValueError as exc:
        assert "explicitly" in str(exc)
    else:
        raise AssertionError("Empty consent was accepted")


def test_saved_sensitive_answer_is_not_added_to_reusable_preferences(tmp_path: Path) -> None:
    repo = JobRepository(connect(tmp_path / "answers.db"))
    migrate(repo.connection)
    profile_id = repo.get_or_create_profile("Ada")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/2", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/2/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Inspected")
    repo.update_execution(session_id, "WAITING_FOR_USER", "Paused")
    field = {"key": "consent", "name": "consent", "label": "Privacy consent", "field_type": "checkbox"}

    repo.save_execution_answer(session_id, field, True, "legal", reusable=True)

    assert repo.execution_answers(session_id)[0]["reusable"] == 0
    assert repo.reusable_answer("LEVER", "consent") is None
    assert profile_id


def test_resume_reopens_and_pauses_for_newly_revealed_conditional_question(tmp_path: Path, monkeypatch) -> None:
    repo = JobRepository(connect(tmp_path / "resume.db"))
    migrate(repo.connection)
    repo.get_or_create_profile("Ada", email="ada@example.com")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/3", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/3/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Inspected")
    question = {"key": "work-mode", "name": "work_mode", "label": "Work mode", "field_type": "radio", "options": ["Remote", "Hybrid"], "category": "preference"}
    repo.update_execution(session_id, "WAITING_FOR_USER", "Paused", unresolved_fields=[question])
    repo.save_execution_answer(session_id, question, "Hybrid", "preference", reusable=True)
    _attach_canonical_cv(repo, application_id, tmp_path)

    class ReopenedAdapter:
        platform = "LEVER"

        def __init__(self):
            self.inspections = 0

        def inspect(self, _url):
            self.inspections += 1
            if self.inspections == 1:
                return FormInspection("LEVER", (FormField("Work mode", "work_mode", "radio", True, ("Remote", "Hybrid"), "work-mode"),))
            return FormInspection("LEVER", (FormField("Security clearance", "clearance", "text", True, (), "clearance"),))

        def fill(self, _url, _plans, _uploads, _answers):
            return []

    adapter = ReopenedAdapter()
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    resume_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads")

    session = repo.latest_execution(application_id)
    assert session["state"] == "WAITING_FOR_USER"
    assert repo.answer_map(session_id) == {"work-mode": "Hybrid"}
    assert "clearance" in session["unresolved_fields_json"]
    clearance = json.loads(session["unresolved_fields_json"])[0]
    repo.save_execution_answer(session_id, clearance, "Not applicable", "sensitive", reusable=False)

    resume_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads")

    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert adapter.inspections == 4


def test_saved_final_answer_can_continue_existing_session_without_creating_duplicates(tmp_path: Path, monkeypatch) -> None:
    repo = JobRepository(connect(tmp_path / "continue.db"))
    migrate(repo.connection)
    repo.get_or_create_profile("Ada")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/continue", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/continue/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Inspected")
    salary = {
        "key": "salary",
        "name": "salary",
        "label": "What are your salary expectations?",
        "field_type": "text",
        "category": "sensitive",
    }
    repo.update_execution(session_id, "WAITING_FOR_USER", "Paused", unresolved_fields=[salary])
    repo.save_execution_answer(session_id, salary, "saved", "sensitive", reusable=False)
    _attach_canonical_cv(repo, application_id, tmp_path)

    class CompletedAdapter:
        platform = "LEVER"

        def __init__(self):
            self.answers = None

        def inspect(self, _url):
            return FormInspection("LEVER", (FormField(salary["label"], "salary", "text", True, (), "salary"),))

        def fill(self, _url, _plans, _uploads, answers):
            self.answers = answers
            return []

    adapter = CompletedAdapter()
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    session = repo.latest_execution(application_id)
    saved = repo.answer_map(session_id)
    assert unanswered_questions([salary], saved) == []
    assert can_continue_waiting_session(session, [salary], saved)

    resume_execution(repo, session, repo.profile(), repo.facts(), tmp_path / "uploads")

    assert repo.latest_execution(application_id)["id"] == session_id
    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert adapter.answers == saved
    assert repo.connection.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
    assert repo.connection.execute("SELECT count(*) FROM application_execution_sessions").fetchone()[0] == 1
    assert any(event["step"] == "Saved answers replayed" for event in repo.execution_events(session_id))
    assert not hasattr(adapter, "submit")


def _ready_submission_session(tmp_path: Path) -> tuple[JobRepository, int, int]:
    repo = JobRepository(connect(tmp_path / "submission.db"))
    migrate(repo.connection)
    repo.get_or_create_profile("Ada")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/submit", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    _attach_canonical_cv(repo, application_id, tmp_path)
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/submit/apply")
    repo.update_execution(session_id, "FILLING_FORM", "Inspected")
    repo.update_execution(session_id, "READY_FOR_FINAL_REVIEW", "Ready for final review")
    return repo, application_id, session_id


class _SubmissionAdapter:
    platform = "LEVER"

    def __init__(self, result: SubmissionResult, inspections: tuple[FormInspection, ...] | None = None):
        self.result = result
        self.inspections = list(inspections or (FormInspection("LEVER", ()), FormInspection("LEVER", ())))
        self.submit_calls = 0
        self.answers = None

    def inspect(self, _url):
        return self.inspections.pop(0)

    def fill(self, _url, _plans, _uploads, answers):
        self.answers = answers
        return []

    def submit(self, _url):
        self.submit_calls += 1
        return self.result


class _ValidatedSubmissionAdapter(_SubmissionAdapter):
    def __init__(self, validation: LiveFormValidation | tuple[LiveFormValidation, ...], result: SubmissionResult | None = None, inspections: tuple[FormInspection, ...] | None = None):
        super().__init__(result or SubmissionResult(verified=True), inspections)
        self.validations = list(validation) if isinstance(validation, tuple) else [validation]
        self.fill_calls = 0

    def fill(self, _url, _plans, uploads, answers):
        self.fill_calls += 1
        self.answers = answers
        return [path.name for path in uploads]

    def validate_live(self, _url, _plans, _answers, _filename):
        return self.validations.pop(0) if len(self.validations) > 1 else self.validations[0]


def test_final_submit_requires_ready_state_and_explicit_confirmation(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    adapter = _SubmissionAdapter(SubmissionResult(verified=True, message="Verified"))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    with pytest.raises(ValueError, match="Explicit"):
        submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=False)

    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert adapter.submit_calls == 0
    repo.update_execution(session_id, "SUBMITTING", "Test")
    with pytest.raises(ValueError, match="final review"):
        repo.begin_final_submission(session_id)


def test_final_submission_cannot_start_before_final_review(tmp_path: Path) -> None:
    repo = JobRepository(connect(tmp_path / "not-ready.db"))
    migrate(repo.connection)
    repo.get_or_create_profile("Ada")
    job_id, _ = repo.add_job(JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/not-ready", "Python"))
    repo.screen_and_assess(job_id, score_job(JobRequirements(), "Data Analyst", [], None, ScoreConfig()))
    application_id = repo.create_application(job_id)
    repo.transition(application_id, ApplicationState.MATERIALS_READY, "user")
    repo.transition(application_id, ApplicationState.APPROVED, "user")
    repo.transition(application_id, ApplicationState.READY_TO_APPLY, "system")
    session_id = repo.start_execution(application_id, "LEVER", "https://jobs.lever.co/example/not-ready/apply")

    with pytest.raises(ValueError, match="final review"):
        repo.begin_final_submission(session_id)
    assert repo.latest_execution(application_id)["state"] == "OPENING_APPLICATION"


def test_manual_history_duplicate_blocks_the_final_pre_submit_check(tmp_path: Path) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    repo.add_application_history("Example", "Data Analyst", application_url="https://jobs.lever.co/example/submit/apply")

    with pytest.raises(ValueError, match="ALREADY_APPLIED"):
        repo.begin_final_submission(session_id)

    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert repo.connection.execute("SELECT applied_at FROM applications WHERE id=?", (application_id,)).fetchone()["applied_at"] is None


def test_verified_final_submission_reuses_session_and_prevents_duplicate_submit(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    adapter = _SubmissionAdapter(SubmissionResult(verified=True, message="Verified platform confirmation"))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)

    assert repo.latest_execution(application_id)["id"] == session_id
    assert repo.latest_execution(application_id)["state"] == "APPLIED"
    application = repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
    assert (application["state"], application["applied_at"] is not None) == ("APPLIED", True)
    assert adapter.submit_calls == 1
    with pytest.raises(ValueError):
        submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)
    assert adapter.submit_calls == 1


def test_final_submission_requires_verified_success_and_pauses_for_human_or_conditional_fields(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, _ = _ready_submission_session(tmp_path)
    unverified = _SubmissionAdapter(SubmissionResult(message="No confirmation"))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: unverified)

    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)

    assert repo.latest_execution(application_id)["state"] == "FAILED"
    assert repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()["state"] == "APPLYING"
    assert unverified.submit_calls == 1
    with pytest.raises(ValueError, match="final review"):
        repo.begin_final_submission(repo.latest_execution(application_id)["id"])
    assert repo.latest_execution(application_id)["state"] == "FAILED"

    repo, application_id, _ = _ready_submission_session(tmp_path / "human")
    checkpoint = _SubmissionAdapter(SubmissionResult(verified=True), (FormInspection("LEVER", (), human_verification=True),))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: checkpoint)
    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "human-uploads", explicit_confirmation=True)
    assert repo.latest_execution(application_id)["state"] == "HUMAN_VERIFICATION_REQUIRED"
    assert checkpoint.submit_calls == 0

    repo, application_id, _ = _ready_submission_session(tmp_path / "conditional")
    conditional = FormField("Security clearance", "clearance", "text", True, (), "clearance")
    revealed = _SubmissionAdapter(SubmissionResult(verified=True), (FormInspection("LEVER", ()), FormInspection("LEVER", (conditional,))))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: revealed)
    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "conditional-uploads", explicit_confirmation=True)
    assert repo.latest_execution(application_id)["state"] == "WAITING_FOR_USER"
    assert revealed.submit_calls == 0


def test_live_validation_blocks_missing_cv_or_required_fields_before_submit(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, _ = _ready_submission_session(tmp_path)
    missing_cv = _ValidatedSubmissionAdapter(LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, False, True, ("Resume/CV attachment",)))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: missing_cv)

    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)

    assert repo.latest_execution(application_id)["state"] == "WAITING_FOR_USER"
    assert missing_cv.submit_calls == 0
    diagnostic = repo.execution_diagnostics(repo.latest_execution(application_id)["id"])[0]
    assert diagnostic["cv_attached"] == 0


def test_verification_error_requires_post_verification_revalidation_and_fresh_confirmation(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    adapter = _SubmissionAdapter(SubmissionResult(verification_error=True, message="Lever reported a verification error"))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)

    assert repo.latest_execution(application_id)["state"] == "POST_VERIFICATION_REVALIDATION"
    assert repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()["state"] == ApplicationState.HUMAN_VERIFICATION_REQUIRED
    assert adapter.submit_calls == 1
    with pytest.raises(ValueError, match="final review"):
        repo.begin_final_submission(session_id)
    assert adapter.submit_calls == 1


def test_live_validation_classifies_lever_verification_error_separately() -> None:
    validation = LiveFormValidation(
        "https://jobs.lever.co/example/apply",
        True,
        True,
        True,
        visible_errors=("there was an error verifying your application",),
    )

    assert validation.verification_error
    assert not validation.ready_to_submit


def test_live_validation_blocks_missing_required_answer_before_submit(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, _ = _ready_submission_session(tmp_path)
    missing_field = _ValidatedSubmissionAdapter(LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, True, False, ("Salary expectations",)))
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: missing_field)

    submit_execution(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads", explicit_confirmation=True)

    assert repo.latest_execution(application_id)["state"] == "WAITING_FOR_USER"
    assert missing_field.submit_calls == 0
    assert "Salary expectations" in repo.execution_diagnostics(repo.latest_execution(application_id)["id"])[0]["missing_fields_json"]


def test_human_checkpoint_and_post_checkpoint_revalidation_never_auto_submit(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    repo.update_execution(session_id, "SUBMITTING", "Test checkpoint")
    repo.transition(application_id, ApplicationState.APPLYING, "system")
    repo.transition(application_id, ApplicationState.HUMAN_VERIFICATION_REQUIRED, "system")
    repo.update_execution(session_id, "HUMAN_VERIFICATION_REQUIRED", "Human checkpoint")
    adapter = _ValidatedSubmissionAdapter(
        (
            LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, False, False, ("Resume/CV attachment",)),
            LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, True, True),
        ),
        inspections=(FormInspection("LEVER", ()),),
    )
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    revalidate_after_human_verification(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads")

    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert repo.connection.execute("SELECT state,applied_at FROM applications WHERE id=?", (application_id,)).fetchone()["state"] == ApplicationState.APPLYING
    assert adapter.fill_calls == 1
    assert adapter.submit_calls == 0
    assert repo.execution_uploads(session_id)
    assert "POST_VERIFICATION_REVALIDATION" in [event["state"] for event in repo.execution_events(session_id)]


def test_stale_human_state_reconciles_from_live_dom_and_replays_without_submit(tmp_path: Path, monkeypatch) -> None:
    repo, application_id, session_id = _ready_submission_session(tmp_path)
    repo.update_execution(session_id, "SUBMITTING", "Test checkpoint")
    repo.transition(application_id, ApplicationState.APPLYING, "system")
    repo.transition(application_id, ApplicationState.HUMAN_VERIFICATION_REQUIRED, "system")
    repo.update_execution(session_id, "HUMAN_VERIFICATION_REQUIRED", "Historical verification event")
    field = {
        "key": "work-right",
        "name": "work_right",
        "label": "Authorised to work in Australia",
        "field_type": "radio",
    }
    repo.save_execution_answer(session_id, field, "Yes", "screening", reusable=False)
    adapter = _ValidatedSubmissionAdapter(
        (
            LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, False, False, ("Resume/CV attachment", "Authorised to work in Australia")),
            LiveFormValidation("https://jobs.lever.co/example/submit/apply", True, True, True),
        ),
        inspections=(FormInspection("LEVER", (FormField(field["label"], field["name"], "radio", True, ("Yes", "No"), field["key"]),)),),
    )
    monkeypatch.setattr("job_agent.services.execution_workflow.adapter_for", lambda _url: adapter)

    revalidate_after_human_verification(repo, repo.latest_execution(application_id), repo.profile(), repo.facts(), tmp_path / "uploads")

    assert repo.latest_execution(application_id)["id"] == session_id
    assert repo.latest_execution(application_id)["state"] == "READY_FOR_FINAL_REVIEW"
    assert adapter.answers == {"work-right": "Yes"}
    assert adapter.submit_calls == 0
    assert len(repo.execution_diagnostics(session_id)) >= 2


def test_application_profile_reuses_work_rights_only_for_clear_australian_question() -> None:
    values = {"authorized_to_work_in_australia": {"value": "Yes", "reusable": True, "category": "reusable"}}
    clear = FormField("Are you authorised to work in Australia?", "work_authorisation", "radio", True, ("Yes", "No"), "work")
    ambiguous = FormField("Are you eligible under applicable laws?", "eligibility", "radio", True, ("Yes", "No"), "eligibility")

    clear_plan = plan_form_fill((clear,), {}, [], values, "Brisbane, Queensland")[0]
    ambiguous_plan = plan_form_fill((ambiguous,), {}, [], values, "Brisbane, Queensland")[0]

    assert (clear_plan.action, clear_plan.value, clear_plan.source) == ("AUTOFILL", "Yes", "Application Profile")
    assert ambiguous_plan.action == "USER_INPUT_REQUIRED"


def test_application_profile_reuses_only_exact_sponsorship_and_notice_period_questions() -> None:
    values = {
        "sponsorship_required_now": {"value": "No", "reusable": True, "category": "reusable"},
        "notice_period": {"value": "Two weeks", "reusable": True, "category": "reusable"},
    }
    sponsorship = FormField("Will you require sponsorship now?", "sponsorship", "radio", True, ("Yes", "No"), "sponsor")
    notice = FormField("What is your notice period?", "notice", "text", True, (), "notice")
    combined = FormField("Will you now or in the future require sponsorship?", "sponsor_combined", "radio", True, ("Yes", "No"), "combined")

    plans = plan_form_fill((sponsorship, notice, combined), {}, [], values, "Brisbane")

    assert [(plan.action, plan.value) for plan in plans] == [("AUTOFILL", "No"), ("AUTOFILL", "Two weeks"), ("USER_INPUT_REQUIRED", None)]


def test_sensitive_application_profile_fields_are_never_inferred() -> None:
    field = FormField("What is your gender?", "gender", "text", True, (), "gender")

    plan = plan_form_fill((field,), {}, [], {}, "Brisbane")[0]

    assert plan.action == "USER_INPUT_REQUIRED"


def test_sensitive_application_profile_answer_requires_per_field_reuse_opt_in() -> None:
    field = FormField("What is your gender?", "gender", "text", True, (), "gender")
    not_reusable = {"gender": {"value": "Prefer not to say", "reusable": False, "category": "sensitive"}}
    reusable = {"gender": {"value": "Prefer not to say", "reusable": True, "category": "sensitive"}}

    paused = plan_form_fill((field,), {}, [], not_reusable, "Brisbane")[0]
    planned = plan_form_fill((field,), {}, [], reusable, "Brisbane")[0]

    assert paused.action == "USER_INPUT_REQUIRED"
    assert (planned.action, planned.value, planned.source) == (
        "AUTOFILL",
        "Prefer not to say",
        "Application Profile",
    )


def test_application_profile_never_stores_per_application_consent(tmp_path: Path) -> None:
    repo = JobRepository(connect(tmp_path / "application-profile.db"))
    migrate(repo.connection)

    with pytest.raises(ValueError, match="per-application decisions"):
        repo.save_application_profile_value("privacy_consent", True, "legal", True)


def test_unanswered_questions_preserves_live_validation_blockers() -> None:
    assert unanswered_questions(["Resume/CV attachment"], {}) == ["Resume/CV attachment"]
