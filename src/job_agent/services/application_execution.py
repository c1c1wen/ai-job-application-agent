"""Safe, resumable pre-submit application execution planning and adapters.

This module never clicks a final submit control and never attempts to solve or
bypass verification, authentication, or anti-bot controls.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import Protocol

from greenlet import error as GreenletError

from job_agent.config import get_settings
from job_agent.domain.models import CandidateFact
from job_agent.services.application_profile import application_profile_match

SENSITIVE_PATTERNS = (
    "salary",
    "disability",
    "ethnicity",
    "race",
    "gender",
    "indigenous",
    "veteran",
    "criminal",
    "medical",
    "security clearance",
    "background check",
    "diversity",
    "sponsorship",
)
LEGAL_PATTERNS = ("declaration", "consent", "privacy", "terms")
PREFERENCE_PATTERNS = ("pronoun", "preferred name", "notice period", "work mode", "availability")
VERIFICATION_PATTERNS = (
    "human verification",
    "verify you are human",
    "complete the captcha",
    "complete captcha",
    "captcha challenge",
    "security check",
)
LOGIN_PATTERNS = (
    "sign in to apply",
    "log in to apply",
    "login to apply",
    "create an account",
    "sign in or sign up",
    "continue with email",
)


class _PlaywrightBrowserWorker:
    """Own all synchronous Playwright handles on one long-lived thread.

    Streamlit reruns execute on different threads.  Passing a sync Playwright
    page between them raises ``greenlet.error``.  The UI therefore sends only
    operations and plain data to this worker; Playwright, Browser, Context,
    Page, and Locator objects never leave it.
    """

    def __init__(self) -> None:
        self._commands: Queue = Queue()
        self._started = False
        self._lock = Lock()
        self._thread = Thread(target=self._run, name="job-agent-playwright-worker", daemon=True)
        self.instance_id = uuid.uuid4().hex
        self._context_id = ""
        self._page_ids: dict[int, str] = {}

    def call(self, application_url: str, operation: Callable[[object], object]) -> object:
        with self._lock:
            if not self._started:
                self._thread.start()
                self._started = True
        reply: Queue = Queue(maxsize=1)
        self._commands.put((application_url, operation, reply))
        success, result = reply.get()
        if success:
            return result
        raise result

    def _run(self) -> None:  # pragma: no cover - exercised by headed runtime
        playwright = context = None
        pages: dict[str, object] = {}
        while True:
            application_url, operation, reply = self._commands.get()
            try:
                if playwright is None:
                    from playwright.sync_api import sync_playwright

                    playwright = sync_playwright().start()
                    # A local Chromium user-data directory retains ordinary ATS
                    # sessions between worker operations.  It is never copied
                    # into SQLite, inspected, logged, or exposed to Streamlit.
                    profile_path = get_settings().browser_profile_path
                    profile_path.mkdir(parents=True, exist_ok=True)
                    context = playwright.chromium.launch_persistent_context(
                        str(profile_path), headless=False
                    )
                    self._context_id = uuid.uuid4().hex
                page = pages.get(application_url)
                if page is None or page.is_closed():
                    page = context.new_page()
                    page.goto(application_url, wait_until="commit")
                    page.locator("body").wait_for(state="visible")
                    pages[application_url] = page
                self._page_ids.setdefault(id(page), uuid.uuid4().hex)
                reply.put((True, operation(page)))
            except Exception as exc:  # noqa: BLE001 - worker must return browser errors to Streamlit
                # Never let a greenlet/thread-affinity failure escape into a
                # Streamlit rerun. Discard only transient browser handles; the
                # persisted application/session state remains untouched.
                if isinstance(exc, GreenletError) or "cannot switch to a different thread" in str(
                    exc
                ):
                    try:
                        if context:
                            context.close()
                        if playwright:
                            playwright.stop()
                    finally:
                        playwright = context = None
                        pages = {}
                        self._context_id = ""
                        self._page_ids = {}
                    reply.put(
                        (
                            False,
                            RuntimeError(
                                "The browser session was restarted safely. Revalidate the form before trying again."
                            ),
                        )
                    )
                else:
                    reply.put((False, exc))


_BROWSER_WORKER = _PlaywrightBrowserWorker()


def _browser_identity(page) -> tuple[str, str, str]:
    """Return non-secret ownership metadata for the worker-owned page only."""
    worker = _BROWSER_WORKER
    if not isinstance(worker, _PlaywrightBrowserWorker):
        return "", "", ""
    return worker.instance_id, worker._context_id, worker._page_ids.get(id(page), "")


@dataclass(frozen=True)
class FormField:
    label: str
    name: str
    field_type: str
    required: bool = False
    options: tuple[str, ...] = ()
    key: str = ""
    current_value: str | None = None
    policy_url: str | None = None


@dataclass(frozen=True)
class FieldPlan:
    field: FormField
    action: str
    value: str | None = None
    reason: str = ""
    source: str | None = None


@dataclass(frozen=True)
class FormInspection:
    platform: str
    fields: tuple[FormField, ...]
    human_verification: bool = False
    warnings: tuple[str, ...] = ()
    authentication_required: bool = False
    page_url: str = ""
    browser_worker_id: str = ""
    browser_context_id: str = ""
    browser_page_id: str = ""
    pageup_stage: str = ""
    cv_confirmed: bool = False
    context_mismatch: bool = False


@dataclass(frozen=True)
class SubmissionResult:
    verified: bool = False
    human_verification: bool = False
    verification_error: bool = False
    message: str = ""


@dataclass(frozen=True)
class LiveFormValidation:
    page_url: str
    page_url_matches: bool
    cv_attached: bool
    required_complete: bool
    missing_fields: tuple[str, ...] = ()
    visible_errors: tuple[str, ...] = ()
    human_verification: bool = False
    page_reloaded: bool | None = None

    @property
    def ready_to_submit(self) -> bool:
        return (
            self.page_url_matches
            and self.cv_attached
            and self.required_complete
            and not self.visible_errors
            and not self.human_verification
        )

    @property
    def verification_error(self) -> bool:
        return "there was an error verifying your application" in self.visible_errors


class ApplicationPlatformAdapter(Protocol):
    platform: str

    def inspect(self, application_url: str) -> FormInspection: ...
    def fill(
        self,
        application_url: str,
        plans: list[FieldPlan],
        uploads: list[Path],
        answers: dict[str, object] | None = None,
    ) -> list[str]: ...
    def submit(self, application_url: str) -> SubmissionResult: ...
    def validate_live(
        self,
        application_url: str,
        plans: list[FieldPlan],
        answers: dict[str, object],
        expected_cv_filename: str,
    ) -> LiveFormValidation: ...
    def reopen_form(self, application_url: str) -> FormInspection: ...


def detect_platform(application_url: str) -> str:
    if "jobs.lever.co" in application_url.casefold():
        return "LEVER"
    if "employmenthero.com" in application_url.casefold():
        return "EMPLOYMENT_HERO"
    if "pageuppeople.com" in application_url.casefold():
        return "PAGEUP"
    return "UNSUPPORTED"


def application_url_for(job_url: str) -> str:
    """Resolve a public ATS posting URL to its public application form URL."""
    if detect_platform(job_url) == "LEVER" and not job_url.rstrip("/").endswith("/apply"):
        return job_url.rstrip("/") + "/apply"
    return job_url


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _field_locator(page, field: FormField):
    """Find a field by stable name, then id, before falling back to its label.

    PageUp's prefill fields may use an ``id`` without a ``name``. Keeping that
    identifier in the serialised field lets a later worker action find the
    same live control without relying on visual label association.
    """
    if field.name:
        for attribute in ("name", "id"):
            locator = page.locator(f"[{attribute}={json.dumps(field.name)}]")
            if locator.count():
                return locator
    return page.get_by_label(field.label, exact=False)


def _preserve_non_editable_text_control(control) -> bool:
    """Return true for a server-populated field that must not be overwritten.

    ATS forms commonly expose required identity fields as read-only once an
    account or privacy step has supplied them.  Trying to ``fill`` such a
    value is both unnecessary and can block the entire guided run.  The live
    form remains the authority for the value; this helper deliberately does
    not attempt to remove the read-only state or otherwise alter the control.
    """
    try:
        return not control.is_editable()
    except (AttributeError, NotImplementedError):
        # If the provider does not support Playwright's editability check,
        # preserve an explicitly read-only control rather than trying to
        # force a candidate value into it.
        try:
            return control.get_attribute("readonly") is not None
        except (AttributeError, NotImplementedError):
            return False


def _verification_status(page, visible_text: str) -> str:
    """Return a safe status for an *active*, user-interactable challenge.

    Lever preloads a zero-height ``.h-captcha`` container on some public forms.
    It is neither a challenge nor a manual checkpoint. A challenge must have
    visible prompt text or an enabled, in-viewport control/iframe with a
    meaningful rendered area. This intentionally never interacts with it.
    """
    visible_text = visible_text.casefold()
    if any(token in visible_text for token in VERIFICATION_PATTERNS):
        return "Active human verification detected: visible verification prompt."
    selector = (
        ".h-captcha, [class*='hcaptcha' i], [class*='captcha' i], [data-sitekey], "
        "iframe[src*='captcha' i], iframe[src*='challenge' i], iframe[title*='captcha' i]"
    )
    evidence = page.locator(selector).evaluate_all(
        """nodes => {
            const inViewport = node => {
                const r = node.getBoundingClientRect();
                return r.width >= 20 && r.height >= 20 && r.bottom > 0 && r.right > 0
                    && r.top < window.innerHeight && r.left < window.innerWidth;
            };
            const rendered = node => {
                const style = getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden'
                    || Number(style.opacity || 1) <= 0.05 || style.pointerEvents === 'none') return false;
                if (node.disabled || node.getAttribute('aria-disabled') === 'true') return false;
                if (!inViewport(node)) return false;
                const r = node.getBoundingClientRect();
                const hit = document.elementFromPoint(r.left + Math.min(r.width / 2, 10), r.top + Math.min(r.height / 2, 10));
                return !!hit && (node === hit || node.contains(hit) || hit.contains(node));
            };
            const controls = nodes.flatMap(node => [
                node,
                ...node.querySelectorAll("iframe[src*='captcha' i], iframe[src*='challenge' i], iframe[title*='captcha' i], [role='checkbox']"),
            ]);
            const active = controls.some(node => rendered(node) && (
                node.tagName === 'IFRAME' || node.getAttribute('role') === 'checkbox'
                || node.matches("input[type='checkbox'], input[type='radio']")
            ));
            return { active, containerPresent: nodes.length > 0 };
        }"""
    )
    if evidence["active"]:
        return "Active human verification detected: visible, interactable verification control."
    if evidence["containerPresent"]:
        return "hCaptcha container present but no active challenge."
    return "No human verification detected."


def _human_verification_present(page, visible_text: str) -> bool:
    return _verification_status(page, visible_text).startswith("Active human verification detected")


def authentication_status_from_text(visible_text: str) -> str:
    """Classify a visible login gate without reading cookies or credentials."""
    text = visible_text.casefold()
    if any(pattern in text for pattern in LOGIN_PATTERNS):
        return "LOGIN_REQUIRED"
    return "AUTHENTICATED"


def _display_label(value: str, options: tuple[str, ...]) -> str:
    option_values = {_normalise(option) for option in options}
    lines = [line.strip(" ✱") for line in value.splitlines()]
    kept = [line for line in lines if line and _normalise(line) not in option_values]
    return " ".join(kept) or value


def _fact_value(facts: list[CandidateFact], types: set[str]) -> str | None:
    for fact in facts:
        if fact.approved and fact.active and fact.fact_type in types and fact.value.strip():
            return fact.value.strip()
    return None


def approved_profile_values(profile, facts: list[CandidateFact]) -> dict[str, str]:
    """Map only explicit profile values; absent data remains absent."""
    values: dict[str, str] = {}
    name = _fact_value(facts, {"name"}) or (
        profile["display_name"].strip() if profile and profile["display_name"] else ""
    )
    if name:
        values["full name"] = name
        pieces = name.split()
        if len(pieces) >= 2:
            values["first name"] = pieces[0]
            values["last name"] = pieces[-1]
    email = _fact_value(facts, {"email"}) or (
        profile["email"].strip() if profile and profile["email"] else ""
    )
    phone = _fact_value(facts, {"phone"}) or (
        profile["phone"].strip() if profile and profile["phone"] else ""
    )
    location = _fact_value(facts, {"location"}) or (
        profile["location"].strip() if profile and profile["location"] else ""
    )
    if email:
        values["email"] = email
    if phone:
        values["phone"] = phone
    if location:
        values["location"] = location
        parts = [part.strip() for part in location.split(",") if part.strip()]
        if parts:
            values["city"] = parts[0]
        if len(parts) >= 2:
            values["state"] = parts[1]
        if any(
            token in location.casefold()
            for token in ("australia", "au", "qld", "queensland", "nsw", "victoria")
        ):
            values["country"] = "Australia"
    for key, types in (
        ("linkedin", {"linkedin", "linkedin_url"}),
        ("work right", {"work_right", "work_rights"}),
        ("visa", {"visa", "visa_type"}),
        ("availability", {"availability"}),
    ):
        value = _fact_value(facts, types)
        if value:
            values[key] = value
    return values


def _employment_field_kind(field: FormField) -> str | None:
    description = _normalise(f"{field.label} {field.name}")
    is_current = "current" in description
    is_historical = any(
        term in description
        for term in (
            "previous",
            "most recent",
            "employment history",
            "work history",
            "internship history",
        )
    )
    if is_current and any(term in description for term in ("employer", "company", "workplace")):
        return "current_employer"
    if is_current and any(term in description for term in ("job title", "position", "job")):
        return "current_job_title"
    if is_historical and any(term in description for term in ("employer", "company", "workplace")):
        return "previous_employer"
    if is_historical and any(term in description for term in ("job title", "position", "job")):
        return "previous_job_title"
    return None


def _most_recent_experience(facts: list[CandidateFact]) -> tuple[str | None, str | None]:
    """Read explicitly structured past experience only; never call it current."""
    for fact in facts:
        if not (fact.approved and fact.active and fact.fact_type == "experience"):
            continue
        parts = [part.strip() for part in fact.value.split("|")]
        if len(parts) >= 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
    return None, None


def _explicit_current_context_plan(
    field: FormField, application_profile_values: dict[str, dict]
) -> FieldPlan | None:
    """Map present-tense questions only from explicit current-profile values."""
    kind = _employment_field_kind(field)
    if kind in {"current_employer", "current_job_title"}:
        stored = application_profile_values.get(kind, {})
        value = stored.get("value") if stored.get("reusable") else None
        if value not in (None, ""):
            return FieldPlan(
                field,
                "AUTOFILL",
                str(value),
                f"Explicit Application Profile value: {kind}",
                "Application Profile",
            )
        status = application_profile_values.get("employment_status", {})
        is_not_employed = (
            status.get("reusable")
            and _normalise(str(status.get("value") or "")) == "not currently employed"
        )
        if (
            is_not_employed
            and field.options
            and any(_normalise(option) == "not currently employed" for option in field.options)
        ):
            return FieldPlan(
                field,
                "AUTOFILL",
                "Not currently employed",
                "Explicit Application Profile employment status",
                "Application Profile",
            )
        if field.required:
            return FieldPlan(
                field,
                "USER_INPUT_REQUIRED",
                reason="No explicitly approved current employment value is available",
            )
        return FieldPlan(
            field, "LEAVE_BLANK", reason="Candidate is explicitly not currently employed"
        )
    return None


def is_sensitive(field: FormField) -> bool:
    description = _normalise(f"{field.label} {field.name}")
    return any(pattern in description for pattern in SENSITIVE_PATTERNS)


def answer_category(field: FormField) -> str:
    description = _normalise(f"{field.label} {field.name}")
    if any(pattern in description for pattern in LEGAL_PATTERNS):
        return "legal"
    if is_sensitive(field):
        return "sensitive"
    if any(pattern in description for pattern in PREFERENCE_PATTERNS):
        return "preference"
    return "screening"


def reusable_answer_allowed(field: FormField) -> bool:
    if answer_category(field) in {"sensitive", "legal"}:
        return False
    description = _normalise(f"{field.label} {field.name}")
    return answer_category(field) == "preference" or any(
        phrase in description
        for phrase in ("right to work", "authorized", "authorised", "visa", "notice period")
    )


def validate_user_answer(field: dict, value) -> None:
    """Reject malformed choices before they can be replayed into a public form."""
    field_type = field["field_type"]
    options = field.get("options", [])
    if field.get("category") == "legal" and field_type == "checkbox" and not value:
        raise ValueError("Select the consent choice explicitly before saving.")
    if (
        field_type in {"radio", "select"}
        and options
        and _normalise(str(value)) not in {_normalise(option) for option in options}
    ):
        raise ValueError("Choose one of the available options.")
    if field_type in {"checkbox", "multiselect"} and isinstance(value, list) and options:
        invalid = {_normalise(str(item)) for item in value} - {
            _normalise(option) for option in options
        }
        if invalid:
            raise ValueError("Choose only from the available options.")
    if field_type in {"text", "email", "tel", "textarea"} and not str(value).strip():
        raise ValueError("Enter a response before saving.")


def plan_form_fill(
    fields: tuple[FormField, ...],
    profile,
    facts: list[CandidateFact],
    application_profile_values: dict[str, dict] | None = None,
    job_location: str | None = None,
) -> list[FieldPlan]:
    values = approved_profile_values(profile, facts)
    application_profile_values = application_profile_values or {}
    plans: list[FieldPlan] = []
    for field in fields:
        label = _normalise(f"{field.label} {field.name}")
        if current_context := _explicit_current_context_plan(field, application_profile_values):
            plans.append(current_context)
            continue
        employment_kind = _employment_field_kind(field)
        if employment_kind in {"previous_employer", "previous_job_title"}:
            title, employer = _most_recent_experience(facts)
            value = employer if employment_kind == "previous_employer" else title
            if value:
                plans.append(
                    FieldPlan(
                        field,
                        "AUTOFILL",
                        value,
                        "Explicit past employment history",
                        "Candidate Profile",
                    )
                )
            elif field.required:
                plans.append(
                    FieldPlan(
                        field,
                        "USER_INPUT_REQUIRED",
                        reason="No explicit past employment value is available",
                    )
                )
            else:
                plans.append(
                    FieldPlan(
                        field,
                        "LEAVE_BLANK",
                        reason="Optional historical employment field without an approved value",
                    )
                )
            continue
        if any(
            term in label
            for term in ("currently studying", "current university", "currently enrolled")
        ):
            plans.append(
                FieldPlan(
                    field,
                    "USER_INPUT_REQUIRED" if field.required else "LEAVE_BLANK",
                    reason="No explicitly approved current study value is available",
                )
            )
            continue
        if (
            "citizenship" in label
            or "previous address" in label
            or "years of professional experience" in label
        ):
            plans.append(
                FieldPlan(
                    field,
                    "USER_INPUT_REQUIRED" if field.required else "LEAVE_BLANK",
                    reason="This cannot be inferred from work rights, location, internship, or project history",
                )
            )
            continue
        if field.field_type == "file":
            plans.append(
                FieldPlan(
                    field, "UPLOAD_APPROVED_DOCUMENT", reason="Approved document upload field"
                )
            )
            continue
        if answer_category(field) == "legal":
            plans.append(
                FieldPlan(
                    field,
                    "USER_INPUT_REQUIRED",
                    reason="Explicit legal or privacy decision required",
                )
            )
            continue
        profile_match = application_profile_match(field, application_profile_values, job_location)
        if profile_match:
            value, field_key = profile_match
            plans.append(
                FieldPlan(
                    field,
                    "AUTOFILL",
                    value,
                    f"Explicit Application Profile value: {field_key}",
                    "Application Profile",
                )
            )
            continue
        if label in {"name", "full name"}:
            match = "full name" if "full name" in values else None
        elif any(token in label for token in ("authorized", "authorised", "right to work")):
            match = "work right" if "work right" in values else None
        elif "linkedin" in label:
            match = "linkedin" if "linkedin" in values else None
        elif "visa" in label:
            match = "visa" if "visa" in values else None
        elif "available" in label or "start date" in label:
            match = "availability" if "availability" in values else None
        elif "location" in label or "located" in label:
            match = "location" if "location" in values else None
        else:
            match = next((key for key in values if key in label), None)
        if match:
            plans.append(
                FieldPlan(
                    field,
                    "AUTOFILL",
                    values[match],
                    "Explicit Candidate Profile value",
                    "Candidate Profile",
                )
            )
            continue
        if is_sensitive(field):
            plans.append(
                FieldPlan(
                    field, "USER_INPUT_REQUIRED", reason="Sensitive or legally meaningful question"
                )
            )
            continue
        elif field.required:
            plans.append(
                FieldPlan(field, "USER_INPUT_REQUIRED", reason="No grounded value is available")
            )
        else:
            plans.append(
                FieldPlan(field, "LEAVE_BLANK", reason="Optional field without a grounded value")
            )
    return plans


def serialise_fields(fields: tuple[FormField, ...]) -> list[dict]:
    return [asdict(field) for field in fields]


def serialise_plans(plans: list[FieldPlan], action: str) -> list[dict]:
    return [
        {
            "key": plan.field.key or plan.field.name,
            "name": plan.field.name,
            "field": plan.field.label or plan.field.name,
            "label": plan.field.label or plan.field.name,
            "field_type": plan.field.field_type,
            "options": list(plan.field.options),
            "current_value": plan.field.current_value,
            "policy_url": plan.field.policy_url,
            "category": answer_category(plan.field),
            "reusable_allowed": reusable_answer_allowed(plan.field),
            "value": plan.value,
            "reason": plan.reason,
            "source": plan.source,
        }
        for plan in plans
        if plan.action == action
    ]


class LeverApplicationAdapter:
    """Visible Lever adapter with an explicit final-review submission boundary."""

    platform = "LEVER"

    def _on_page(self, application_url: str, operation: Callable[[object], object]) -> object:
        return _BROWSER_WORKER.call(application_url, operation)

    def inspect(self, application_url: str) -> FormInspection:
        def inspect_owned_page(page):
            inspection = self._inspect_page(page)
            worker_id, context_id, page_id = _browser_identity(page)
            return FormInspection(
                inspection.platform, inspection.fields, inspection.human_verification,
                inspection.warnings, inspection.authentication_required, page.url,
                worker_id, context_id, page_id, inspection.pageup_stage,
                inspection.cv_confirmed, inspection.context_mismatch,
            )

        return self._on_page(application_url, inspect_owned_page)

    def _inspect_page(self, page) -> FormInspection:
        visible_text = (
            page.locator("body").inner_text().casefold()
        )  # pragma: no cover - live browser only
        fields = []
        seen_groups: set[str] = set()
        for index, element in enumerate(page.locator("input, textarea, select").all()):
            field_type = element.get_attribute("type") or element.evaluate(
                "node => node.tagName.toLowerCase()"
            )
            if field_type == "select" and element.get_attribute("multiple") is not None:
                field_type = "multiselect"
            if field_type in {"hidden", "submit", "button"}:
                continue
            if not element.is_visible() and field_type not in {"checkbox", "radio"}:
                continue
            name = element.get_attribute("name") or element.get_attribute("id") or ""
            field_key = name or f"{field_type}-{index}"
            if field_type in {"radio", "checkbox"} and field_key in seen_groups:
                continue
            seen_groups.add(field_key)
            details = element.evaluate(
                """node => {
                    const applicationField = node.closest('.application-field');
                    const group = node.closest('fieldset') || node.closest('.application-question') || applicationField?.parentElement;
                    const forLabel = node.id && document.querySelector(`label[for="${node.id}"]`);
                    const groupText = group?.innerText?.trim() || '';
                    const label = group?.querySelector('legend')?.innerText || groupText || forLabel?.innerText || node.closest('label')?.innerText || node.getAttribute('aria-label') || node.getAttribute('placeholder') || node.getAttribute('name') || '';
                    const link = group?.querySelector('a[href*="privacy"], a[href*="policy"], a[href*="terms"]') || node.closest('label')?.querySelector('a[href]');
                    const value = ['radio', 'checkbox'].includes(node.type) ? (node.checked ? node.value : '') : (node.value || '');
                    return { label, value, policyUrl: link?.href || null };
                }"""
            )
            label = details["label"] or name or f"field-{index}"
            if field_type in {"select", "multiselect"}:
                options = tuple(element.locator("option").all_text_contents())
            elif field_type in {"radio", "checkbox"} and name:
                options = tuple(
                    dict.fromkeys(
                        page.locator(f"input[name='{name}']").evaluate_all(
                            "els => els.map(node => node.closest('label')?.innerText || node.value || '')"
                        )
                    )
                )
            else:
                options = ()
            fields.append(
                FormField(
                    _display_label(label, options),
                    name,
                    field_type,
                    bool(element.get_attribute("required")),
                    options,
                    field_key,
                    details["value"] or None,
                    details["policyUrl"],
                )
            )
        verification_status = _verification_status(page, visible_text)
        verification = verification_status.startswith("Active human verification detected")
        auth_status = authentication_status_from_text(visible_text)
        return FormInspection(
            self.platform,
            tuple(fields),
            human_verification=verification,
            authentication_required=auth_status != "AUTHENTICATED",
            warnings=(verification_status,),
        )

    def fill(
        self,
        application_url: str,
        plans: list[FieldPlan],
        uploads: list[Path],
        answers: dict[str, object] | None = None,
    ) -> list[str]:
        """Fill only visible supported inputs; final submission is intentionally impossible."""
        return self._on_page(
            application_url, lambda page: self._fill_page(page, plans, uploads, answers)
        )

    def _fill_page(
        self,
        page,
        plans: list[FieldPlan],
        uploads: list[Path],
        answers: dict[str, object] | None = None,
    ) -> list[str]:
        uploaded: list[str] = []
        text = page.locator("body").inner_text().casefold()
        if _human_verification_present(page, text):
            return uploaded

        def first_visible(locator):
            for item in locator.all():
                if item.is_visible():
                    return item
            return locator.first

        for plan in plans:
            if answers and plan.field.key in answers:
                answer = answers[plan.field.key]
            elif plan.action == "AUTOFILL" and plan.value is not None:
                answer = plan.value
            else:
                continue
            locator = _field_locator(page, plan.field)
            if not locator.count():
                continue
            if plan.field.field_type in {"text", "email", "tel", "textarea"}:
                control = first_visible(locator)
                if _preserve_non_editable_text_control(control):
                    continue
                control.fill(str(answer))
            elif plan.field.field_type == "select":
                first_visible(locator).select_option(label=str(answer))
            elif plan.field.field_type == "multiselect" and isinstance(answer, list):
                first_visible(locator).select_option(label=[str(item) for item in answer])
            elif plan.field.field_type == "radio":
                for item in locator.all():
                    option = item.evaluate(
                        "node => node.closest('label')?.innerText || node.value || ''"
                    )
                    if _normalise(option) == _normalise(str(answer)):
                        if item.is_visible():
                            item.check()
                        else:
                            # Lever visually hides some standard inputs inside custom
                            # controls.  This invokes the ordinary input click handler
                            # for the user's explicitly saved option; it is not a
                            # submission, verification, or access-control bypass.
                            item.evaluate("node => { if (!node.checked) node.click(); }")
                        break
            elif plan.field.field_type == "checkbox":
                if isinstance(answer, bool):
                    if answer:
                        control = first_visible(locator)
                        if control.is_visible():
                            control.check()
                        else:
                            control.evaluate("node => { if (!node.checked) node.click(); }")
                    else:
                        control = first_visible(locator)
                        if control.is_visible():
                            control.uncheck()
                        else:
                            control.evaluate("node => { if (node.checked) node.click(); }")
                elif isinstance(answer, list):
                    for item in locator.all():
                        option = item.evaluate(
                            "node => node.closest('label')?.innerText || node.value || ''"
                        )
                        if _normalise(option) in {_normalise(str(value)) for value in answer}:
                            if item.is_visible():
                                item.check()
                            else:
                                item.evaluate("node => { if (!node.checked) node.click(); }")
                elif isinstance(answer, str):
                    # Lever represents some explicitly answered consent choices as
                    # checkbox groups.  Match the saved option label/value rather
                    # than treating a non-empty string as blanket consent.
                    for item in locator.all():
                        option = item.evaluate(
                            "node => node.closest('label')?.innerText || node.value || ''"
                        )
                        if _normalise(option) == _normalise(answer):
                            if item.is_visible():
                                item.check()
                            else:
                                item.evaluate("node => { if (!node.checked) node.click(); }")
                            break
        file_inputs = page.locator("input[type='file']")
        for index, path in enumerate(uploads):
            if index >= file_inputs.count():
                break
            file_inputs.nth(index).set_input_files(str(path))
            uploaded.append(path.name)
        return uploaded

    def validate_live(
        self,
        application_url: str,
        plans: list[FieldPlan],
        answers: dict[str, object],
        expected_cv_filename: str,
    ) -> LiveFormValidation:
        """Check the current browser DOM; historical replay events are not proof."""
        return self._on_page(
            application_url,
            lambda page: self._validate_page(
                page, application_url, plans, answers, expected_cv_filename
            ),
        )

    def _validate_page(
        self,
        page,
        application_url: str,
        plans: list[FieldPlan],
        answers: dict[str, object],
        expected_cv_filename: str,
    ) -> LiveFormValidation:
        expected_url = application_url.rstrip("/")
        current_url = page.url.rstrip("/")
        visible_text = page.locator("body").inner_text().casefold()
        verification = _human_verification_present(page, visible_text)
        safe_errors = tuple(
            phrase
            for phrase in (
                "there was an error verifying your application",
                "please try again",
                "please complete this field",
                "this field is required",
            )
            if phrase in visible_text
        )
        snapshot = page.locator("input, textarea, select").evaluate_all(
            """nodes => nodes.map((node, index) => {
                const visible = !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length) || ['checkbox', 'radio', 'file'].includes(node.type);
                const label = node.closest('fieldset')?.querySelector('legend')?.innerText || node.closest('label')?.innerText || node.getAttribute('aria-label') || node.getAttribute('name') || `field-${index}`;
                const peers = node.name ? [...document.querySelectorAll(`input[name="${CSS.escape(node.name)}"]`)] : [node];
                const selected = node.type === 'radio' ? peers.some(item => item.checked) : node.type === 'checkbox' ? node.checked : node.type === 'file' ? node.files.length > 0 : !!node.value;
                const filename = node.type === 'file' && node.files.length ? node.files[0].name : '';
                return {visible, required: !!node.required, type: node.type || node.tagName.toLowerCase(), name: node.name || '', label: label.trim(), selected, filename};
            })"""
        )
        missing: list[str] = []
        seen_required: set[tuple[str, str]] = set()
        for field in snapshot:
            if not field["visible"] or not field["required"]:
                continue
            key = (field["name"], field["type"])
            if key in seen_required:
                continue
            seen_required.add(key)
            if not field["selected"]:
                missing.append(field["label"])
        attached_names = [
            field["filename"] for field in snapshot if field["type"] == "file" and field["filename"]
        ]
        cv_attached = expected_cv_filename in attached_names
        if not cv_attached:
            missing.append("Resume/CV attachment")
        for plan in plans:
            saved_answer = plan.field.key in answers
            expected = (
                answers.get(plan.field.key)
                if saved_answer
                else plan.value
                if plan.action == "AUTOFILL"
                else None
            )
            if expected is None or plan.field.field_type == "file":
                continue
            # Optional profile suggestions should be applied where supported,
            # but their absence is never a required-field blocker. Explicitly
            # saved current-application answers remain checked even if optional.
            if not plan.field.required and not saved_answer:
                continue
            locator = _field_locator(page, plan.field)
            if not locator.count():
                missing.append(plan.field.label)
                continue
            if plan.field.field_type in {"radio", "checkbox"}:
                selected = locator.evaluate_all(
                    "nodes => nodes.filter(node => node.checked).map(node => node.closest('label')?.innerText || node.value || '')"
                )
                if isinstance(expected, bool):
                    if bool(selected) != expected:
                        missing.append(plan.field.label)
                    continue
                expected_values = expected if isinstance(expected, list) else [expected]
                if not all(
                    _normalise(str(value)) in {_normalise(str(item)) for item in selected}
                    for value in expected_values
                ):
                    missing.append(plan.field.label)
            elif plan.field.field_type in {"select", "multiselect"}:
                selected = locator.first.evaluate(
                    "node => [...node.selectedOptions].map(option => option.text)"
                )
                expected_values = expected if isinstance(expected, list) else [expected]
                if not all(
                    _normalise(str(value)) in {_normalise(str(item)) for item in selected}
                    for value in expected_values
                ):
                    missing.append(plan.field.label)
            elif (
                str(
                    next(
                        (item for item in locator.all() if item.is_visible()), locator.first
                    ).input_value()
                ).strip()
                != str(expected).strip()
            ):
                missing.append(plan.field.label)
        return LiveFormValidation(
            current_url,
            current_url == expected_url,
            cv_attached,
            not missing,
            tuple(dict.fromkeys(missing)),
            safe_errors,
            verification,
            None,
        )

    def reopen_form(self, application_url: str) -> FormInspection:
        """Return to the public application URL without submitting anything."""
        return self._on_page(application_url, lambda page: self._reopen_page(page, application_url))

    def _reopen_page(self, page, application_url: str) -> FormInspection:
        page.goto(application_url, wait_until="domcontentloaded")
        return self._inspect_page(page)

    def submit(self, application_url: str) -> SubmissionResult:
        """Click once only after explicit confirmation, then require success evidence."""
        return self._on_page(application_url, self._submit_page)

    def _submit_page(self, page) -> SubmissionResult:
        before = page.locator("body").inner_text().casefold()
        if _human_verification_present(page, before):
            return SubmissionResult(
                human_verification=True, message="Human verification is required before submission."
            )
        # Prefer the explicit final action.  A broad text match could select an
        # unrelated control on a changing third-party form.
        controls = page.get_by_role(
            "button", name=re.compile(r"^submit application$", re.IGNORECASE)
        )
        if not controls.count():
            controls = page.get_by_role("button", name=re.compile(r"^submit$", re.IGNORECASE))
        if not controls.count():
            controls = page.locator("input[type='submit']")
        if not controls.count():
            return SubmissionResult(
                message="The final submission control is not available on the public form."
            )
        controls.first.click()
        # Lever may render an hCaptcha widget asynchronously after the click.
        # Wait only for that immediate platform response; manual completion is
        # always outside this method and never authorises another submit click.
        page.wait_for_timeout(3000)
        after = page.locator("body").inner_text().casefold()
        if _human_verification_present(page, after):
            return SubmissionResult(
                human_verification=True,
                message="Human verification appeared after the submission action.",
            )
        if "there was an error verifying your application" in after:
            return SubmissionResult(
                verification_error=True,
                message="Lever reported a verification error; the form must be revalidated before any new confirmation.",
            )
        success_signals = (
            "application submitted",
            "application has been submitted",
            "thank you for applying",
            "thank you for your application",
            "nice work! ready for your next great role?",
        )
        if any(signal in after for signal in success_signals):
            return SubmissionResult(
                verified=True, message="Lever displayed an application-submitted confirmation."
            )
        return SubmissionResult(
            message="The submission action completed but no reliable success confirmation was found."
        )


class EmploymentHeroApplicationAdapter(LeverApplicationAdapter):
    """Public Employment Hero adapter using the same conservative visible-form operations.

    Authentication is never automated: its persistent local profile merely
    lets a user-authenticated session survive worker operations. Login and any
    CAPTCHA still return a resumable human checkpoint.
    """

    platform = "EMPLOYMENT_HERO"


class PageUpApplicationAdapter(LeverApplicationAdapter):
    """Public PageUp adapter using the same visible-form safety boundary.

    PageUp's initial privacy acknowledgement is deliberately surfaced as a
    legal, per-application question by the inherited planner.  It is never
    selected by automation, even when an email address can be safely filled.
    """

    platform = "PAGEUP"

    @staticmethod
    def _pageup_stage(page, form, visible_text: str) -> str:
        """Classify the rendered PageUp stage from live URL/DOM signatures."""
        path = page.url.casefold()
        text = visible_text.casefold()
        controls = form.locator("input, textarea, select").count()
        if "initapplication.asp" in path:
            return "PAGEUP_PRIVACY_READY" if "privacy" in text else "PAGEUP_INIT"
        if "prefill.asp" in path:
            if PageUpApplicationAdapter._cv_confirmation_present(form, visible_text):
                return "PAGEUP_CV_CONFIRMED"
            return "PAGEUP_PREFILL"
        if any(token in text for token in ("thank you", "application submitted")):
            return "PAGEUP_CONFIRMATION"
        if any(token in text for token in ("final review", "review your application")):
            return "PAGEUP_FINAL_REVIEW"
        if controls >= 3 or any(token in text for token in ("employment history", "education", "personal details", "save and continue")):
            return "PAGEUP_APPLICATION_FORM"
        return "PAGEUP_UNKNOWN"

    @staticmethod
    def _cv_confirmation_present(form, visible_text: str, expected_filename: str | None = None) -> bool:
        """Recognise PageUp's server-rendered upload acknowledgement.

        PageUp clears the native file input after accepting an upload. The
        visible filename paired with a Delete/Remove control is the reliable
        confirmation, not ``input.files`` after the rerender.
        """
        text = visible_text.casefold()
        filename_present = (
            expected_filename.casefold() in text
            if expected_filename
            else bool(re.search(r"\b[^\s]+\.(?:pdf|doc|docx)\s*\(\s*\d+\s*(?:kb|mb)\s*\)", text))
        )
        if not filename_present:
            return False
        controls = form.locator("button, input[type='button'], input[type='submit'], a").all()
        return any(
            control.is_visible()
            and any(token in _normalise(control.evaluate("node => (node.innerText || node.value || node.getAttribute('aria-label') || '').trim()")) for token in ("delete", "remove"))
            for control in controls
        )

    def _on_page(self, application_url: str, operation: Callable[[object], object]) -> object:
        """Follow PageUp's active public application tab within one context.

        PageUp can keep the prefill page open while its ordinary "Continue
        with application" control opens or activates a second public PageUp
        page.  The worker owns all of those tabs in the same local persistent
        context.  Selecting the newest public PageUp application page on the
        next serialised worker action lets inspection continue from the real
        form without re-clicking or reopening the prefill page.
        """

        def on_active_page(page):
            pages = [
                candidate
                for candidate in page.context.pages
                if not candidate.is_closed()
                # PageUp's hand-off can leave the ``/apply/`` prefill URL and
                # open the actual form under another public PageUp path.
                # Keep the scope to the same official PageUp host, rather
                # than assuming the destination keeps a particular path.
                and "pageuppeople.com" in candidate.url.casefold()
            ]
            # A PageUp prefill POST may leave a short-lived, empty processing
            # tab as the newest tab while the actual form remains in another
            # public PageUp tab. Prefer the rendered page with the most
            # user-input controls; fall back to the newest tab when the live
            # DOM is not available (for example while a page is navigating).
            def form_score(candidate) -> int:
                try:
                    contexts = [candidate, *candidate.frames]
                    return sum(
                        1
                        for context in contexts
                        for control in context.locator("input, textarea, select").all()
                        if (control.get_attribute("type") or "").casefold()
                        not in {"hidden", "submit", "button"}
                        and control.is_visible()
                    )
                except Exception:  # noqa: BLE001 - transient live PageUp tab
                    return 0

            if pages:
                scores = [form_score(candidate) for candidate in pages]
                active_page = pages[scores.index(max(scores))] if max(scores) else pages[-1]
            else:
                active_page = page
            return operation(active_page)

        return _BROWSER_WORKER.call(application_url, on_active_page)

    @staticmethod
    def _form_context(page):
        """Use PageUp's rendered child form when the shell has no controls.

        Public PageUp flows can retain their state in a same-origin frame after
        the privacy prefill step.  Reading the shell alone falsely reports an
        empty form and a missing CV.  This only selects the already-rendered
        form context; it does not navigate, submit, or interact with consent.
        """
        contexts = [page, *page.frames]
        return max(
            contexts,
            key=lambda context: context.locator("input, textarea, select").count(),
        )

    def _inspect_page(self, page) -> FormInspection:
        form = self._form_context(page)
        inspection = super()._inspect_page(form)
        visible_text = form.locator("body").inner_text()
        stage = self._pageup_stage(page, form, visible_text)
        cv_confirmed = self._cv_confirmation_present(form, visible_text)
        # This diagnostic intentionally reports only visible navigation labels,
        # never field values or page text. It makes an unfamiliar PageUp
        # intermediate step observable without exposing applicant data.
        labels: list[str] = []
        for control in form.locator("button, input[type='submit'], input[type='button']").all():
            if not control.is_visible():
                continue
            label = control.evaluate(
                "node => (node.innerText || node.value || node.getAttribute('aria-label') || '').trim()"
            )
            if label and len(label) <= 80:
                metadata = control.evaluate(
                    "node => ({type: node.type || node.tagName.toLowerCase(), id: node.id || '', name: node.name || ''})"
                )
                identity = metadata["id"] or metadata["name"] or "no-id"
                labels.append(
                    f"{label} [{metadata['type']} #{identity}; {'disabled' if control.is_disabled() else 'enabled'}]"
                )
        navigation = tuple(dict.fromkeys(labels))
        warning = (
            f"Visible PageUp navigation controls: {', '.join(navigation)}"
            if navigation
            else "No visible PageUp navigation control was detected."
        )
        field_status = ", ".join(
            f"{field.label} [{field.field_type}; {'value present' if field.current_value else 'empty'}]"
            for field in inspection.fields
        ) or "none"
        return FormInspection(
            inspection.platform,
            inspection.fields,
            inspection.human_verification,
            inspection.warnings + (warning, f"Visible PageUp fields: {field_status}"),
            inspection.authentication_required,
            pageup_stage=stage,
            cv_confirmed=cv_confirmed,
        )

    def _fill_page(self, page, plans, uploads, answers=None) -> list[str]:
        form = self._form_context(page)
        self._restore_saved_current_application_checkboxes(form, plans, answers or {})
        before_text = form.locator("body").inner_text()
        cv_already_confirmed = self._cv_confirmation_present(form, before_text)
        # When PageUp has already acknowledged the CV, never replace it merely
        # because the native file input was removed during its server rerender.
        uploaded = super()._fill_page(form, plans, [] if cv_already_confirmed else uploads, answers)
        if uploads and not cv_already_confirmed:
            form.wait_for_timeout(500)
            after_upload = form.locator("body").inner_text()
            if self._cv_confirmation_present(form, after_upload, uploads[0].name):
                uploaded = [uploads[0].name]
        # PageUp commonly presents a short prefill page before the actual
        # application. Advancing that page is not a submission: it is limited
        # to an explicitly-labelled Continue/Next control, only after every
        # ordinary field on that page has a grounded or user-saved value. A
        # final apply/submit control is never eligible here.
        if not any(
            plan.action == "USER_INPUT_REQUIRED" and not (answers and plan.field.key in answers)
            for plan in plans
        ):
            self._advance_completed_intermediate_page(form)
        return uploaded

    @staticmethod
    def _restore_saved_current_application_checkboxes(form, plans, answers: dict[str, object]) -> None:
        """Restore an explicit answer saved for this one application only.

        PageUp disables its intermediate ``Next`` control until the privacy
        checkbox emits a normal browser change event. This is not reusable
        consent: the value comes solely from the persisted answer for this
        execution session, which the user selected on this application. We
        first use Playwright's ordinary ``check`` interaction. Some PageUp
        themes visually hide the native input, so the associated visible label
        is the narrow fallback; neither path uses force-click or script.
        """
        for plan in plans:
            if plan.field.field_type != "checkbox" or not bool(answers.get(plan.field.key)):
                continue
            locator = _field_locator(form, plan.field)
            if not locator.count() or locator.first.is_checked():
                continue
            try:
                locator.first.check(timeout=2_000)
            except Exception:  # noqa: BLE001 - PageUp may hide its native checkbox
                control_id = locator.first.get_attribute("id")
                if control_id:
                    label = form.locator(f"label[for={json.dumps(control_id)}]")
                    if label.count() and label.first.is_visible():
                        label.first.click(timeout=2_000)
                if not locator.first.is_checked():
                    # Some PageUp themes bind their visible check target to a
                    # nested label/span rather than a ``for`` attribute. This
                    # remains a standard click on the user-visible consent
                    # text, scoped to the already saved current-app answer.
                    visible_label = form.get_by_text(plan.field.label, exact=False)
                    if visible_label.count() and visible_label.first.is_visible():
                        visible_label.first.click(timeout=2_000)
            form.wait_for_timeout(150)

    def _validate_page(self, page, application_url, plans, answers, expected_cv_filename):
        validation = super()._validate_page(
            self._form_context(page), application_url, plans, answers, expected_cv_filename
        )
        # PageUp changes from initApplication.asp to prefill/application pages
        # inside the same public PageUp flow. That expected navigation is not
        # a cross-site redirect and must not by itself fail final validation.
        from urllib.parse import urlparse

        expected = urlparse(application_url)
        current = urlparse(validation.page_url)
        same_public_pageup_flow = (
            expected.hostname
            and current.hostname
            and expected.hostname.endswith("pageuppeople.com")
            and current.hostname.endswith("pageuppeople.com")
            and current.path.startswith("/apply/")
        )
        cv_confirmed = self._cv_confirmation_present(
            self._form_context(page), self._form_context(page).locator("body").inner_text(), expected_cv_filename
        )
        if cv_confirmed:
            missing = tuple(item for item in validation.missing_fields if item != "Resume/CV attachment")
            validation = LiveFormValidation(
                validation.page_url, validation.page_url_matches, True, not missing,
                missing, validation.visible_errors, validation.human_verification, validation.page_reloaded,
            )
        if same_public_pageup_flow and not validation.page_url_matches:
            return LiveFormValidation(
                validation.page_url,
                True,
                validation.cv_attached,
                validation.required_complete,
                validation.missing_fields,
                validation.visible_errors,
                validation.human_verification,
                validation.page_reloaded,
            )
        return validation

    @staticmethod
    def _advance_completed_intermediate_page(form) -> None:
        text = form.locator("body").inner_text().casefold()
        if _human_verification_present(form, text):
            return
        controls = form.locator("button, input[type='submit'], input[type='button']")
        for control in controls.all():
            if not control.is_visible() or control.is_disabled():
                continue
            label = control.evaluate(
                "node => (node.innerText || node.value || node.getAttribute('aria-label') || '').trim()"
            )
            normalised = _normalise(label)
            # ``Continue with application`` is PageUp's ordinary intermediate
            # progress action.  It contains the word "application", so a
            # broad ``"apply"`` exclusion incorrectly stranded a completed
            # applicant on the upload hand-off page.  The allow-list already
            # requires a navigation verb; additionally exclude only a genuine
            # final-submit label.
            if (
                normalised in {"continue", "next", "save and continue", "proceed"}
                or normalised.startswith(("continue ", "next ", "proceed "))
            ) and not normalised.startswith(("submit", "final submit")):
                control.click()
                # PageUp may POST, replace the form in place, or navigate a
                # frame. Wait briefly for any of those ordinary browser
                # updates before the next live inspection decides state.
                form.wait_for_timeout(800)
                return
