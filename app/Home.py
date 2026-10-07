from __future__ import annotations

import json
import sys
from html import escape
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from openai import OpenAIError

from job_agent.config import get_settings
from job_agent.connectors.providers import (
    AdzunaProvider,
    EmploymentHeroProvider,
    GreenhouseProvider,
    LeverProvider,
)
from job_agent.connectors.registry import (
    DEFAULT_EMPLOYMENT_HERO_BOARDS,
    DEFAULT_GREENHOUSE_BOARDS,
    DEFAULT_LEVER_SITES,
)
from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    ExecutionState,
    JobInput,
    ScoreBreakdown,
    ScoreConfig,
)
from job_agent.domain.scoring import score_job
from job_agent.llm.application_writing import ApplicationWritingError, GroundedApplicationWriter
from job_agent.llm.cv_extraction import CVExtractionError, CVExtractor
from job_agent.llm.extraction import ExtractionError, OpenAIJobExtractor
from job_agent.llm.opportunity_evaluation import (
    OpportunityNarrativeError,
    OpportunityNarrativeWriter,
)
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.application_execution import (
    application_url_for,
    detect_platform,
    validate_user_answer,
)
from job_agent.services.application_profile import application_field_category
from job_agent.services.autonomy import route_for_ats
from job_agent.services.canonical_cv import (
    application_cv_status,
    canonical_cv_path,
    register_canonical_cv,
    use_current_canonical_cv,
)
from job_agent.services.cover_letter_docx import CoverLetterExportError, export_cover_letter_docx
from job_agent.services.cv_parser import CVParseError, parse_cv
from job_agent.services.cv_storage import replace_canonical_pdf, store_upload
from job_agent.services.discovery import run_broad_discovery
from job_agent.services.employer_resolution import (
    configured_search_resolution_finder,
    resolve_public_job,
)
from job_agent.services.execution_summary import current_cv_summary, normalise_autofill_summary
from job_agent.services.execution_workflow import (
    can_continue_waiting_session,
    fill_supported_fields,
    inspect_application,
    refresh_execution_questions,
    resume_all_authenticated_sessions,
    resume_execution,
    revalidate_after_human_verification,
    submit_execution,
    unanswered_questions,
)
from job_agent.services.job_enrichment import FullJobDescriptionEnricher
from job_agent.services.job_search import (
    analyze_job_description,
    assess_search_eligibility,
    broad_discovery_queries,
    extract_requirements,
    suggestion_from_profile,
)
from job_agent.services.materials import application_recommendation
from job_agent.services.opportunity_evaluation import build_opportunity_evaluation
from job_agent.services.portfolio import select_demo_opportunity
from job_agent.services.review_keys import review_widget_key

st.set_page_config(page_title="AI Job Hunter", page_icon="🎯", layout="wide")
settings = get_settings()
connection = connect(settings.database_path)
migrate(connection)
repo = JobRepository(connection)


def search_preference_facts(search: dict) -> list[CandidateFact]:
    """Turn explicit user search preferences into deterministic matching inputs."""
    preferences = []
    for role in search["roles"].split(","):
        if role.strip():
            preferences.append(CandidateFact("target_role", role.strip(), "Job search setting"))
    for location in search["locations"].split(","):
        if location.strip():
            preferences.append(
                CandidateFact("preferred_location", location.strip(), "Job search setting")
            )
    return preferences


def evaluate_existing_jobs(search: dict) -> tuple[int, int, int]:
    """Refresh active-feed eligibility without deleting historical job records."""
    config_hash = json.dumps(search, sort_keys=True)
    passed = blocked = strong = 0
    for row in repo.jobs_for_search_evaluation():
        job = JobInput(
            row["title"],
            row["company"],
            row["url"],
            row["description"],
            row["source"],
            row["location"],
            row["source_key"],
            row["external_id"],
        )
        result = assess_search_eligibility(job, search)
        repo.record_search_eligibility(row["id"], result.passed, result.reasons, config_hash)
        if not result.passed:
            blocked += 1
            continue
        passed += 1
        description = job.description
        if job.source == "ADZUNA":
            cached = repo.enrichment(row["id"])
            if cached is None:
                enrichment = FullJobDescriptionEnricher().enrich(job)
                repo.record_enrichment(row["id"], enrichment)
                description = enrichment.description or description
            else:
                description = cached["description"] or description
            job = JobInput(
                job.title,
                job.company,
                job.url,
                description,
                job.source,
                job.location,
                job.source_key,
                job.external_id,
            )
        analysis = analyze_job_description(job.description, job.location, job.title)
        score = score_job(
            analysis.requirements,
            job.title,
            repo.facts() + search_preference_facts(search),
            job.location,
            repo.get_score_config(),
        )
        repo.screen_and_assess(row["id"], score, analysis.as_dict())
        repo.create_application(row["id"])
        if score.total >= repo.get_score_config().strong_match_threshold:
            strong += 1
    return passed, blocked, strong


if "nav_target" in st.session_state:
    st.session_state["navigation"] = st.session_state.pop("nav_target")

with st.sidebar:
    page = st.radio(
        "Navigate",
        ["Dashboard", "Today", "Demo", "Candidate Profile", "Job Search", "Settings", "Review Queue", "Safety"],
        key="navigation",
    )

if page != "Demo":
    st.title("AI Job Hunter & Application Copilot")
    st.caption("Risk-based autonomy with explicit human-in-the-loop safeguards.")

if page == "Dashboard":
    metrics = repo.portfolio_metrics()
    st.subheader("Portfolio metrics")
    st.caption("LIVE / HISTORICAL counts from the local tracker. No synthetic results are included.")
    cols = st.columns(4)
    cols[0].metric("Jobs discovered", metrics["discovered"])
    cols[1].metric("Passed eligibility", metrics["eligible"])
    cols[2].metric("Direct vacancies verified", metrics["direct_verified"])
    cols[3].metric("Applications prepared", metrics["prepared"])
    cols = st.columns(4)
    cols[0].metric("Applications submitted", metrics["applied"])
    cols[1].metric("Verified submissions", metrics["verified_submissions"])
    cols[2].metric("Human checkpoints", metrics["human_checkpoints"])
    cols[3].metric("Duplicates prevented", metrics["duplicates_prevented"])
    st.subheader("Funnel")
    st.markdown(
        f"**Discovered** {metrics['discovered']} → **Eligible** {metrics['eligible']} → "
        f"**Direct verified** {metrics['direct_verified']} → **Prepared** {metrics['prepared']} → "
        f"**Applied** {metrics['applied']}"
    )
    st.caption(
        "Evaluation funnel · "
        f"Evaluated {metrics['evaluated']} → Saved {metrics['saved']} → "
        f"Prepared {metrics['prepared']} → Applied {metrics['applied']} → "
        f"Interview {metrics['interview']} → Offer {metrics['offer']}"
    )
    st.subheader("Ranked opportunities")
    ranked = repo.review_queue()
    if ranked:
        st.dataframe(
            [
                {
                    "Title": r["title"],
                    "Company": r["company"],
                    "Location": r["location"],
                    "Source": r["source"],
                    "Score": r["score"],
                    "Level": r["recommendation"],
                    "Status": r["state"],
                }
                for r in ranked
            ],
            use_container_width=True,
        )
    else:
        st.info("Run a public job search to populate opportunities.")
    st.subheader("Application Tracker")
    attention = repo.needs_attention()
    if attention:
        st.subheader("Needs Your Attention")
        grouped: dict[str, list] = {}
        for item in attention:
            grouped.setdefault(item["platform"], []).append(item)
        for platform, items in grouped.items():
            reasons = []
            for item in items:
                warnings = json.loads(item["warnings_json"])
                reason = next(
                    (
                        warning
                        for warning in warnings
                        if "LOGIN_REQUIRED" in warning or "Verification" in warning
                    ),
                    "Unresolved application question",
                )
                reasons.append(
                    f"{item['company']} — {item['title']}: {reason.replace('LOGIN_REQUIRED: ', '')}"
                )
            st.warning(
                f"{platform.replace('_', ' ').title()} · {len(items)} waiting\n\n"
                + "\n".join(reasons)
            )
        ready = repo.authentication_ready_sessions()
        if ready and st.button(
            "Resume all ready applications", key="resume-all-authenticated-applications"
        ):
            result = resume_all_authenticated_sessions(
                repo,
                repo.profile(),
                repo.facts(),
                ROOT / "data" / "artifacts" / "application_uploads",
            )
            st.success(
                f"Resumed {result['resumed']} application(s); {result['paused']} remained safely paused."
            )
            st.rerun()
    auth_states = repo.ats_authentication_states()
    if auth_states:
        with st.expander("ATS session status"):
            st.dataframe(
                [
                    {
                        "ATS": row["ats"],
                        "Status": row["authentication_status"].replace("_", " ").title(),
                        "Last verified": row["last_verified_at"],
                    }
                    for row in auth_states
                ],
                use_container_width=True,
                hide_index=True,
            )
    with st.expander("Add previous application"):
        st.caption(
            "Manual history participates in duplicate checks and is never auto-submitted again."
        )
        with st.form("previous_application"):
            company = st.text_input("Company")
            role_title = st.text_input("Role")
            applied_date = st.text_input("Date applied (optional)")
            application_url = st.text_input("Application URL (optional)")
            job_id = st.text_input("Job / requisition ID (optional)")
            location = st.text_input("Location (optional)")
            status = st.selectbox("Status", ["ALREADY_APPLIED", "APPLIED", "REJECTED", "INTERVIEW"])
            notes = st.text_area("Notes (optional)")
            if st.form_submit_button("Add previous application"):
                if not company.strip() or not role_title.strip():
                    st.error("Company and Role are required.")
                else:
                    repo.add_application_history(
                        company.strip(),
                        role_title.strip(),
                        application_url=application_url.strip() or None,
                        job_external_id=job_id.strip() or None,
                        location=location.strip() or None,
                        date_applied=applied_date.strip() or None,
                        status=status,
                        notes=notes.strip() or None,
                    )
                    st.success("Previous application added. Duplicate checks will use it.")
    batch = []
    for row in ranked:
        if row["company"].casefold().find("megaport") >= 0 or row["score"] < 55:
            continue
        decision = repo.refresh_duplicate_check(row["job_id"])
        batch.append(
            {
                "Company": row["company"],
                "Role": row["title"],
                "Match": f"{row['score']}%",
                "Source": row["source"],
                "Duplicate check": decision.status,
                "Reason": decision.reason,
            }
        )
    if batch:
        st.caption(
            "Current eligible batch candidates — duplicate checks run again before preparation and submission."
        )
        st.dataframe(batch, use_container_width=True, hide_index=True)

elif page == "Demo":
    st.markdown(
        """
        <style>
        .demo-hero {padding: 1.2rem 0 0.8rem 0; max-width: 900px;}
        .demo-hero h1 {font-size: 2.4rem; letter-spacing: -0.04em; margin: 0 0 .35rem 0;}
        .demo-subtitle {font-size: 1.2rem; font-weight: 600; color: #475569; margin: 0 0 .5rem 0;}
        .demo-support {font-size: 1rem; color: #64748b; margin: 0; line-height: 1.5;}
        .pipeline-stage {border: 1px solid #e2e8f0; border-radius: .7rem; padding: .7rem .55rem; min-height: 4.7rem; background: #fff;}
        .pipeline-stage strong {display:block; color:#0f172a; margin-bottom:.32rem;}
        .pipeline-stage span {font-size:.82rem; color:#64748b; line-height:1.25;}
        .funnel-card {border: 1px solid #dbeafe; border-radius: .7rem; padding: .75rem .55rem; min-height: 4.5rem; background: #f8fbff; text-align:center;}
        .funnel-number {display:block; font-size:1.5rem; font-weight:750; color:#1d4ed8; margin-bottom:.15rem;}
        .journey-card {border-left: 3px solid #6366f1; padding: .25rem 0 .25rem .7rem; margin:.35rem 0 .8rem 0;}
        .journey-card strong {display:block; color:#0f172a;}
        .journey-card span {font-size:.86rem; color:#475569;}
        .trust-card {border: 1px solid #e2e8f0; border-radius: .7rem; padding: .8rem; min-height: 8rem; background:#fff;}
        .trust-card strong {display:block; font-size:.78rem; letter-spacing:.06rem; color:#3730a3; margin-bottom:.4rem;}
        .trust-card span {font-size:.88rem; color:#475569; line-height:1.35;}
        .final-card {border: 1px solid #bfdbfe; border-radius: .85rem; padding: 1rem 1.1rem; background:#eff6ff;}
        .demo-card {border: 1px solid #e2e8f0; border-radius: .85rem; padding: 1.1rem 1.15rem; margin: .65rem 0; background: #fff;}
        .demo-label {font-size:.72rem; letter-spacing:.08em; font-weight:700; color:#64748b;}
        .demo-card h3 {margin:.35rem 0 .3rem 0; color:#0f172a;}
        .demo-company {color:#475569; margin-bottom:.7rem;}
        .badge {display:inline-block; padding:.22rem .55rem; margin:0 .3rem .45rem 0; border-radius:999px; background:#eef2ff; color:#3730a3; font-size:.8rem; font-weight:600;}
        .badge-neutral {background:#f1f5f9; color:#334155;}
        .demo-copy {color:#475569; line-height:1.5; margin:0;}
        </style>
        <section class="demo-hero">
          <h1>AI Job Hunter &amp; Application Agent</h1>
          <p class="demo-subtitle">An evidence-grounded AI workflow that discovers relevant jobs, verifies opportunities, evaluates candidate fit, and prepares tailored applications for human review.</p>
          <p class="demo-support">Deterministic rules and grounded AI work together: the model can explain and tailor, but cannot invent candidate claims.</p>
        </section>
        """,
        unsafe_allow_html=True,
    )
    metrics = repo.portfolio_metrics()
    st.markdown("#### Live job-hunting pipeline")
    funnel = [
        ("Discovered", metrics["discovered"]),
        ("Eligible", metrics["eligible"]),
        ("Direct verified", metrics["direct_verified"]),
        ("Evaluated", metrics["evaluated"]),
        ("Prepared", metrics["prepared"]),
    ]
    if metrics["applied"]:
        funnel.append(("Applied", metrics["applied"]))
    for column, (stage, count) in zip(st.columns(len(funnel)), funnel):
        column.markdown(
            f'<div class="funnel-card"><span class="funnel-number">{count}</span><span>{stage}</span></div>',
            unsafe_allow_html=True,
        )
    st.caption(
        "The agent narrows discovered opportunities through eligibility, source verification, evidence-based evaluation and application preparation."
    )
    st.caption(
        "Configured hard eligibility filters—such as saved location and seniority rules—reduce discovered roles to eligible opportunities before evaluation."
    )

    st.markdown("#### Follow one opportunity through the agent")
    demo = select_demo_opportunity(repo)
    if demo is None:
        st.info(
            "No current directly verified evaluation is available for the portfolio view. "
            "Stale evaluations remain out of this read-only showcase until explicitly refreshed in Review Queue."
        )
    else:
        evaluation = demo.evaluation
        source_label = {
            "EMPLOYMENT_HERO": "Employment Hero",
            "GREENHOUSE": "Greenhouse",
            "LEVER": "Lever",
            "PAGEUP": "PageUp",
        }.get(demo.source, demo.source.replace("_", " ").title())
        badges = (
            f'<span class="badge">{escape(str(evaluation["deterministic_recommendation"]))}</span>'
            f'<span class="badge">{escape(str(evaluation["deterministic_score"]))}% deterministic match</span>'
            f'<span class="badge badge-neutral">Direct source verified · {escape(source_label)}</span>'
        )
        st.markdown(
            f'''<section class="demo-card">
                <div class="demo-label">PERSISTED, DIRECT-VERIFIED OPPORTUNITY</div>
                <h3>{escape(demo.title)}</h3>
                <div class="demo-company">{escape(demo.company)} · {escape(demo.location or "Location not stated")}</div>
                <div>{badges}</div>
            </section>''',
            unsafe_allow_html=True,
        )
        journey_cols = st.columns(5)
        journey = (
            ("Discover", "Role identified"),
            ("Verify", f"{source_label} · {demo.evidence_quality.title()} evidence"),
            ("Evaluate", f"{evaluation['deterministic_score']}% · {evaluation['deterministic_recommendation']}"),
            ("Tailor", "Grounded evidence ranked"),
            ("Prepare", "Package ready" if demo.package_prepared else "Ready to prepare"),
        )
        for column, (heading, detail) in zip(journey_cols, journey):
            column.markdown(
                f'<div class="journey-card"><strong>{escape(heading)}</strong><span>{escape(detail)}</span></div>',
                unsafe_allow_html=True,
            )
        st.caption("Job description → Candidate evidence → Deterministic evaluation → Grounded tailoring → Application package → Human review")

        st.markdown("##### Why this candidate fits")
        st.write(demo.why_it_fits)
        st.markdown("##### Matched")
        st.markdown(
            " ".join(
                f'<span class="badge badge-neutral">{escape(str(item))}</span>'
                for item in evaluation["matched_requirements"]
            ),
            unsafe_allow_html=True,
        )
        st.markdown("##### Requirement → evidence mapping")
        if demo.requirement_mapping:
            for mapping in demo.requirement_mapping:
                st.markdown(
                    f'''<section class="demo-card">
                        <span class="badge badge-neutral">{escape(mapping.requirement)}</span>
                        <span class="demo-copy"> → {escape(' + '.join(mapping.evidence))} → </span>
                        <span class="badge">{escape(mapping.result)}</span>
                    </section>''',
                    unsafe_allow_html=True,
                )
        else:
            st.caption("No current requirement-to-evidence mapping is available for this example.")
        st.markdown("##### Strongest evidence")
        for item in demo.strongest_evidence:
            supports = " · ".join(item.supports)
            st.markdown(
                f'''<section class="demo-card">
                    <strong>{escape(item.value)}</strong><br>
                    <span class="demo-copy">{escape(item.why)}</span><br>
                    <span class="demo-label">Supports: {escape(supports)}</span>
                </section>''',
                unsafe_allow_html=True,
            )
        st.markdown("##### Tailoring plan")
        for action in demo.tailoring_actions:
            st.write(f"- {action}")
        st.write("**Application angle**")
        st.write(demo.application_angle)
        st.markdown("##### Gaps / do not claim")
        gaps = evaluation["missing_requirements"] + evaluation["tailoring_plan"]["do_not_claim"]
        if gaps:
            st.write(" · ".join(dict.fromkeys(gaps)))
        else:
            st.success("No material unsupported requirement was identified for this example.")
        st.markdown("#### Prepared application example")
        if demo.package_prepared:
            st.success("A prepared application package is recorded for this opportunity.")
            preparation_copy = (
                "The persisted package contains grounded preparation materials associated with the verified opportunity."
            )
        else:
            preparation_copy = (
                "No persisted prepared package currently meets the canonical-CV freshness check for this portfolio view. "
                "This verified example therefore shows the governed preparation outputs the agent can produce from the same approved evidence."
            )
        st.markdown(
            f'''<section class="demo-card">
                <div class="demo-label">APPLICATION PREPARATION</div>
                <p class="demo-copy">{escape(preparation_copy)}</p>
                <p class="demo-copy">✓ CV emphasis and tailoring guidance<br>
                ✓ Grounded cover-letter positioning<br>
                ✓ Screening-question preparation where approved answers exist<br>
                ✓ Unsupported claims blocked rather than added</p>
            </section>''',
            unsafe_allow_html=True,
        )

    st.markdown("#### Why not just ask an LLM?")
    safeguards = (
        ("GROUNDED CANDIDATE EVIDENCE", "Generated application claims must trace back to approved candidate facts."),
        ("NO INVENTED SKILLS", "Unsupported job requirements remain visible gaps instead of becoming candidate claims."),
        ("VERSION-AWARE", "A canonical CV or profile change makes prior evaluations stale until re-evaluated."),
        ("HUMAN CONTROL", "External application submission always requires explicit user approval."),
        ("DIRECT-SOURCE VERIFICATION", "The system verifies the employer source before treating an opportunity as application-ready."),
    )
    for column, (heading, copy) in zip(st.columns(len(safeguards)), safeguards):
        column.markdown(
            f'<section class="trust-card"><strong>{escape(heading)}</strong><span>{escape(copy)}</span></section>',
            unsafe_allow_html=True,
        )

    st.markdown("#### Product architecture")
    architecture_stages = (
        "Public employer job sources",
        "Source registry + verification",
        "Eligibility + deterministic scoring",
        "Candidate profile + provenance",
        "Grounded AI evaluation + tailoring",
        "Application preparation",
        "Human approval",
    )
    st.markdown(" → ".join(architecture_stages))
    st.caption("Persisted state, audited source registry, version-aware profile freshness, and hybrid deterministic-plus-AI decisioning support the workflow.")

    st.markdown("#### Ready for human review")
    st.markdown(
        '''<section class="final-card"><strong>Ready for human review</strong><br>
        <span class="demo-copy">The agent can discover, verify, evaluate and prepare an application while preserving evidence provenance. External submission remains an explicit user decision.</span></section>''',
        unsafe_allow_html=True,
    )

elif page == "Candidate Profile":
    st.subheader("Candidate profile — source of truth")
    st.caption("Only approved facts with evidence may appear in generated application materials.")
    st.subheader("Current CV")
    try:
        canonical_cv = register_canonical_cv(repo, ROOT)
        st.write(f"**{canonical_cv['filename']}**")
        st.caption("Canonical source: configured local candidate document · Status: Current")
        st.caption(
            "Future applications use this version by default. Changes are detected by file hash and modified time."
        )
        if st.button(
            "Extract proposed profile updates from current CV", key="canonical-cv-proposed-facts"
        ):
            if not settings.openai_api_key:
                st.error(
                    "Set OPENAI_API_KEY to extract proposed profile updates. Your existing profile has not changed."
                )
            else:
                profile_id = repo.get_or_create_profile()
                proposals = CVExtractor(settings.openai_api_key, settings.openai_model).extract(
                    canonical_cv["text_content"]
                )
                result = repo.reconcile_cv_facts(
                    profile_id,
                    canonical_cv["source_document_id"],
                    [
                        {
                            "fact": CandidateFact(fact.fact_type, fact.value, fact.evidence),
                            "snippet": fact.snippet,
                            "confidence": fact.confidence,
                        }
                        for fact in proposals
                    ],
                )
                st.success(
                    "CV profile reconciled: "
                    f"{result['added']} added · {result['updated']} updated · "
                    f"{result['duplicates']} duplicate records retired."
                )
    except (FileNotFoundError, CVParseError) as exc:
        st.error(str(exc))
    migrated = repo.promote_stale_cv_facts()
    if migrated:
        st.info(f"Trusted {migrated} explicit CV facts from an earlier extraction.")
    upload = st.file_uploader("Upload CV (PDF or DOCX)", type=["pdf", "docx"])
    if upload and st.button("Parse and reconcile CV facts"):
        try:
            content = upload.getvalue()
            text = parse_cv(upload.name, content)
            profile_id = repo.get_or_create_profile()
            if (
                upload.name.casefold() == canonical_cv_path(ROOT).name.casefold()
                and upload.name.casefold().endswith(".pdf")
            ):
                replace_canonical_pdf(ROOT, content)
                document_id = register_canonical_cv(repo, ROOT)["source_document_id"]
            else:
                path = store_upload(ROOT / "data" / "artifacts", upload.name, content)
                document_id = repo.add_document(
                    upload.name, str(path), upload.type or "application/octet-stream", text
                )
            if not settings.openai_api_key:
                st.error("Set OPENAI_API_KEY to run structured CV extraction.")
            else:
                extracted = CVExtractor(settings.openai_api_key, settings.openai_model).extract(text)
                result = repo.reconcile_cv_facts(
                    profile_id,
                    document_id,
                    [
                        {
                            "fact": CandidateFact(item.fact_type, item.value, item.evidence),
                            "snippet": item.snippet,
                            "confidence": item.confidence,
                        }
                        for item in extracted
                    ],
                )
                st.success(
                    "CV profile reconciled: "
                    f"{result['added']} added · {result['updated']} updated · "
                    f"{result['deactivated']} no-longer-supported facts retired."
                )
        except CVParseError as exc:
            st.error(str(exc))
        except (CVExtractionError, OpenAIError, ValueError):
            st.error("CV extraction failed safely; no facts were approved.")
    rows = repo.fact_review_rows()
    pending_details = {
        row["fact_type"]: row["value"]
        for row in rows
        if row["status"] in {"PENDING", "APPROVED"}
        and row["fact_type"] in {"name", "email", "phone", "location"}
    }
    profile = repo.profile()
    st.subheader("Personal details")
    st.caption(
        "CV-derived values are pre-filled for your review. Saving updates your profile details."
    )
    with st.form("profile"):
        name = st.text_input(
            "Name", value=pending_details.get("name", profile["display_name"] if profile else "")
        )
        email = st.text_input(
            "Email", value=pending_details.get("email", profile["email"] if profile else "")
        )
        phone = st.text_input(
            "Phone", value=pending_details.get("phone", profile["phone"] if profile else "")
        )
        location = st.text_input(
            "Location",
            value=pending_details.get("location", profile["location"] if profile else ""),
        )
        if st.form_submit_button("Save profile"):
            repo.get_or_create_profile(name or "Candidate", email, phone, location)
            for row in rows:
                if row["status"] == "PENDING" and row["fact_type"] in {
                    "name",
                    "email",
                    "phone",
                    "location",
                }:
                    repo.review_fact(
                        row["id"],
                        "APPROVED",
                        {"name": name, "email": email, "phone": phone, "location": location}[
                            row["fact_type"]
                        ],
                    )
            st.success("Profile saved.")
    st.subheader("Application Profile")
    st.caption(
        "These details are used to speed up future applications. Sensitive answers are never inferred."
    )
    application_profile = repo.application_profile()

    def application_value(key: str) -> str:
        value = application_profile.get(key, {}).get("value", "")
        return "" if value is None else str(value)

    with st.form("application_profile_reusable"):
        legal_name = st.text_input("Legal name", value=application_value("legal_name"))
        preferred_name = st.text_input("Preferred name", value=application_value("preferred_name"))
        linkedin_url = st.text_input("LinkedIn URL", value=application_value("linkedin_url"))
        current_location = st.text_input(
            "Current location", value=application_value("current_location")
        )
        employment_statuses = ["", "Not currently employed", "Employed"]
        employment_status = st.selectbox(
            "Employment status",
            employment_statuses,
            index=employment_statuses.index(application_value("employment_status"))
            if application_value("employment_status") in employment_statuses
            else 0,
        )
        current_employer = st.text_input(
            "Current employer / company",
            value=application_value("current_employer"),
            disabled=employment_status == "Not currently employed",
        )
        current_job_title = st.text_input(
            "Current job title / position",
            value=application_value("current_job_title"),
            disabled=employment_status == "Not currently employed",
        )
        yes_no = ["", "Yes", "No"]
        work_right = st.selectbox(
            "Authorised to work in Australia",
            yes_no,
            index=yes_no.index(application_value("authorized_to_work_in_australia"))
            if application_value("authorized_to_work_in_australia") in yes_no
            else 0,
        )
        visa_type = st.text_input("Visa type", value=application_value("visa_type"))
        visa_expiry = st.text_input(
            "Visa expiry",
            value=application_value("visa_expiry"),
            placeholder="Only if explicitly known",
        )
        sponsor_now = st.selectbox(
            "Sponsorship required now",
            yes_no,
            index=yes_no.index(application_value("sponsorship_required_now"))
            if application_value("sponsorship_required_now") in yes_no
            else 0,
        )
        sponsor_future = st.selectbox(
            "Sponsorship may be required in future",
            yes_no,
            index=yes_no.index(application_value("sponsorship_required_future"))
            if application_value("sponsorship_required_future") in yes_no
            else 0,
        )
        notice_period = st.text_input("Notice period", value=application_value("notice_period"))
        earliest_start = st.text_input(
            "Earliest start date / availability", value=application_value("earliest_start_date")
        )
        pronouns = st.text_input(
            "Preferred pronouns", value=application_value("preferred_pronouns")
        )
        work_modes = ["", "Remote", "Hybrid", "On-site"]
        work_mode = st.selectbox(
            "Preferred work mode",
            work_modes,
            index=work_modes.index(application_value("preferred_work_mode"))
            if application_value("preferred_work_mode") in work_modes
            else 0,
        )
        if st.form_submit_button("Save Application Profile"):
            values = {
                "legal_name": legal_name,
                "preferred_name": preferred_name,
                "linkedin_url": linkedin_url,
                "current_location": current_location,
                "employment_status": employment_status,
                "current_employer": current_employer or None,
                "current_job_title": current_job_title or None,
                "authorized_to_work_in_australia": work_right,
                "visa_type": visa_type,
                "visa_expiry": visa_expiry,
                "sponsorship_required_now": sponsor_now,
                "sponsorship_required_future": sponsor_future,
                "notice_period": notice_period,
                "earliest_start_date": earliest_start,
                "preferred_pronouns": pronouns,
                "preferred_work_mode": work_mode,
            }
            for key, value in values.items():
                repo.save_application_profile_value(
                    key, value, application_field_category(key), True
                )
            st.success("Application Profile saved.")
    with st.expander("Autofill grounding audit"):
        st.caption(
            "Current-context fields are filled only from explicit Application Profile values; past CV employment is never treated as current employment."
        )
        st.write(f"Employment status: {application_value('employment_status') or 'Not set'}")
        st.write(f"Current employer: {application_value('current_employer') or 'Not provided'}")
        st.write(f"Current job title: {application_value('current_job_title') or 'Not provided'}")
        st.write(
            "Completed education, past internships, projects, work-rights, and location history are not converted into current-study, current-job, citizenship, or professional-tenure claims."
        )
    with st.expander("Sensitive application answers (optional)"):
        st.caption(
            "Only save an answer you choose to provide. Reuse is off unless you opt in for that specific field."
        )
        with st.form("application_profile_sensitive"):
            sensitive_values = {}
            reuse = {}
            labels = {
                "indigenous_status": "Aboriginal or Torres Strait Islander status",
                "disability_adjustment": "Disability or medical adjustment",
                "gender": "Gender",
                "ethnicity_race": "Ethnicity or race",
                "veteran_status": "Veteran status",
                "criminal_history": "Criminal history",
            }
            for key, label in labels.items():
                sensitive_values[key] = st.text_input(label, value=application_value(key))
                reuse[key] = st.checkbox(
                    f"Reuse {label} only when I explicitly choose",
                    value=bool(application_profile.get(key, {}).get("reusable", False)),
                    key=f"application-profile-reuse-{key}",
                )
            if st.form_submit_button("Save sensitive answers"):
                for key, value in sensitive_values.items():
                    if value:
                        repo.save_application_profile_value(key, value, "sensitive", reuse[key])
                st.success("Sensitive answers saved with your selected reuse settings.")
    trusted = [row for row in rows if row["status"] == "APPROVED"]
    pending = [row for row in rows if row["status"] == "PENDING"]
    st.subheader("Profile summary")
    st.caption(
        "Facts explicitly stated in your CV are trusted automatically. Remove or edit anything that is incorrect."
    )
    category = {
        "skill": "Skills",
        "programming_language": "Programming languages",
        "tool": "Tools / technologies",
        "education": "Education",
        "experience": "Experience",
        "project": "Projects",
        "certification": "Certifications",
        "work_right": "Work rights",
    }
    for group in [
        "Education",
        "Experience",
        "Skills",
        "Programming languages",
        "Tools / technologies",
        "Projects",
        "Certifications",
        "Work rights",
        "Other",
    ]:
        group_rows = [row for row in trusted if category.get(row["fact_type"], "Other") == group]
        if not group_rows:
            continue
        st.markdown(f"#### {group}")
        for row in group_rows:
            cols = st.columns([8, 2])
            cols[0].write(row["value"])
            if cols[1].button("Remove", key=f"remove-{row['id']}"):
                repo.set_fact_active(row["id"], False)
                st.rerun()
    with st.expander("Advanced details"):
        st.caption(
            "Source evidence is retained for grounding and audit, but hidden during normal profile use."
        )
        for row in trusted:
            st.caption(
                f"{row['fact_type']}: {row['value']} · {row['filename'] or 'manual entry'} · {row['confidence'] or 'user-entered'}"
            )
    if pending:
        st.warning(
            "A small number of facts need review because their extraction was malformed or ambiguous."
        )
        for row in pending:
            edited = st.text_input(row["fact_type"], row["value"], key=f"pending-{row['id']}")
            cols = st.columns(2)
            if cols[0].button("Keep", key=f"confirm-{row['id']}"):
                repo.review_fact(row["id"], "APPROVED", edited)
                st.rerun()
            if cols[1].button("Remove", key=f"discard-{row['id']}"):
                repo.review_fact(row["id"], "REJECTED")
                st.rerun()
    with st.expander("+ Add information manually"), st.form("fact"):
        fact_type = st.selectbox(
            "Fact type",
            [
                "skill",
                "education",
                "experience",
                "work_right",
                "preferred_location",
                "target_role",
                "salary_preference",
                "availability",
                "employment_type_preference",
                "relocation_willingness",
            ],
        )
        value = st.text_input("Fact")
        evidence = st.text_input(
            "Evidence / source", placeholder="CV, profile entry, or uploaded document reference"
        )
        if st.form_submit_button("Add approved fact"):
            profile_id = repo.get_or_create_profile("Candidate")
            if value and evidence:
                repo.add_fact(profile_id, CandidateFact(fact_type, value, evidence))
                st.success("Approved fact added.")
            else:
                st.error("A fact and evidence are both required.")

elif page == "Settings":
    st.subheader("Job search settings")
    st.caption("Choose what you want to find. You can adjust any suggestion before saving.")
    search = repo.get_search_config()
    for key, value in (
        ("search_roles", search["roles"]),
        ("search_locations", search["locations"]),
        ("search_work_mode", search["work_mode"]),
        ("search_employment", search["employment_type"]),
        ("search_include", search["include"]),
        ("search_exclude", search["exclude"]),
        ("search_level", search.get("level", "")),
    ):
        if key not in st.session_state:
            st.session_state[key] = value
    if st.button("Suggest from my CV"):
        profile = repo.profile()
        suggestions = suggestion_from_profile(
            repo.facts(), profile["location"] if profile else None
        )
        for field in ("roles", "locations", "level"):
            if suggestions[field]:
                st.session_state[f"search_{field}"] = suggestions[field]
        st.info("Suggestions are ready to review. Save job search settings to apply them.")
    with st.form("job_search_config"):
        roles = st.text_input(
            "Target roles", help="Examples: Data Analyst, AI Analyst", key="search_roles"
        )
        locations = st.text_input(
            "Preferred locations",
            help="Examples: Brisbane, Queensland, Remote Australia",
            key="search_locations",
        )
        work_mode = st.selectbox(
            "Work mode", ["", "Remote", "Hybrid", "On-site"], key="search_work_mode"
        )
        employment = st.text_input("Employment type", key="search_employment")
        level = st.selectbox(
            "Experience level",
            ["", "Graduate / entry-level", "Junior", "Mid-level", "Senior"],
            key="search_level",
        )
        include = st.text_input("Optional keywords to include", key="search_include")
        exclude = st.text_input("Optional keywords to exclude", key="search_exclude")
        if st.form_submit_button("Save job search settings"):
            updated = {
                "roles": roles,
                "locations": locations,
                "work_mode": work_mode,
                "employment_type": employment,
                "include": include,
                "exclude": exclude,
                "level": level,
                "greenhouse_boards": search.get("greenhouse_boards", ""),
            }
            repo.save_search_config(updated)
            passed, blocked, _ = evaluate_existing_jobs(updated)
            st.success(
                f"Job search settings saved. Re-evaluated existing jobs: {passed} eligible, {blocked} kept out of the active feed."
            )
    with st.expander("Advanced settings"):
        st.caption("Optional technical controls. They are not needed for normal job searching.")
        config = repo.get_score_config()
        with st.form("score_config"):
            values = [
                st.number_input(label, min_value=0, max_value=100, value=value)
                for label, value in zip(
                    ["Skills", "Role", "Experience", "Education", "Location", "Preferences"],
                    config.weights,
                )
            ]
            strong = st.number_input(
                "Strong-match threshold",
                min_value=0,
                max_value=100,
                value=config.strong_match_threshold,
            )
            good = st.number_input(
                "Good-match threshold",
                min_value=0,
                max_value=100,
                value=config.good_match_threshold,
            )
            possible = st.number_input(
                "Possible-match threshold",
                min_value=0,
                max_value=100,
                value=config.possible_match_threshold,
            )
            if st.form_submit_button("Save scoring settings"):
                try:
                    repo.save_score_config(
                        ScoreConfig(*map(int, values), int(strong), int(good), int(possible))
                    )
                    st.success("Saved. The deterministic engine remains the final authority.")
                except ValueError as exc:
                    st.error(str(exc))
        boards = st.text_area(
            "Additional Greenhouse board tokens",
            search.get("greenhouse_boards", ""),
            help="One public board token per line. Normal discovery also searches the internal public Lever registry.",
        )
        if st.button("Save additional sources"):
            search["greenhouse_boards"] = boards
            repo.save_search_config(search)
            st.success("Additional sources saved.")

elif page == "Today":
    st.subheader("Today")
    st.caption("New opportunities from the latest autonomous discovery run. No application is submitted from this view.")
    latest_run = repo.latest_discovery_run()
    if not latest_run:
        st.info("Use Find New Jobs to create today’s inbox.")
    else:
        funnel = json.loads(latest_run["funnel_json"])
        inbox = repo.discovery_inbox(latest_run["id"])
        new_rows = [row for row in inbox if row["freshness"] == "NEW"]
        actionable_rows = [
            row
            for row in new_rows
            if row["resolution_status"] == "DIRECT_VERIFIED"
            and row["score"] >= repo.get_score_config().possible_match_threshold
        ]
        routes = {
            row["job_id"]: route_for_ats(row["application_source"] or row["source"])
            for row in actionable_rows
        }
        auto_capable = sum(route.level.value == "AUTO" for route in routes.values())
        assisted = sum(route.level.value == "ASSISTED" for route in routes.values())
        summary_cols = st.columns(5)
        summary_cols[0].metric("New jobs", funnel.get("new_jobs", 0))
        summary_cols[1].metric("Eligible", funnel.get("eligible", 0))
        summary_cols[2].metric("Good / Possible", funnel.get("good_or_possible", 0))
        summary_cols[3].metric("Direct verified", funnel.get("live_direct_vacancy", 0))
        summary_cols[4].metric("Filtered", funnel.get("raw_jobs_discovered", 0) - funnel.get("eligible", 0))
        route_cols = st.columns(4)
        route_cols[0].metric("Auto-capable", auto_capable)
        route_cols[1].metric("Assisted", assisted)
        route_cols[2].metric("Duplicates", funnel.get("duplicates", 0))
        route_cols[3].metric("Closed / stale", funnel.get("closed_stale", 0))
        if not new_rows:
            st.info("No new jobs were found in the latest run. Previously seen vacancies remain in history and will not be re-surfaced as new.")
        categories = (
            ("New good matches", lambda row: row["score"] >= repo.get_score_config().good_match_threshold),
            ("New possible matches", lambda row: repo.get_score_config().possible_match_threshold <= row["score"] < repo.get_score_config().good_match_threshold),
            ("Needs review / assisted", lambda row: row["score"] >= repo.get_score_config().possible_match_threshold and route_for_ats(row["application_source"] or row["source"]).level.value == "ASSISTED"),
            ("Filtered", lambda row: row["preliminary_status"] == "LIKELY_INELIGIBLE" or row["resolution_status"] in {"DIRECT_NOT_FOUND", "DIRECT_CLOSED"}),
        )
        shown: set[int] = set()
        for heading, predicate in categories:
            rows = [row for row in new_rows if row["job_id"] not in shown and predicate(row)]
            if not rows:
                continue
            st.markdown(f"#### {heading}")
            for row in rows:
                shown.add(row["job_id"])
                details = json.loads(row["score_json"])
                components = details.get("components", {})
                missing = ", ".join(components.get("missing_skills", [])) or "No explicit gap extracted"
                route = route_for_ats(row["application_source"] or row["source"])
                with st.container(border=True):
                    st.markdown(f"**{row['title']}** · {row['company']}")
                    st.caption(
                        f"{row['location'] or 'Location not stated'} · {row['source']} · "
                        f"{row['resolution_status'] or 'AGGREGATOR_LEAD'} · First seen {row['discovered_at']}"
                    )
                    st.write(f"{row['score']}% · {row['recommendation']} · {route.level.value}")
                    st.caption("Why it matches: " + " ".join(components.get("reasons", [])[:2]))
                    st.caption(f"Main gaps: {missing}")
                    if row["canonical_application_url"]:
                        st.link_button("Open verified vacancy", row["canonical_application_url"], key=f"today-source-{row['job_id']}")
                    if row["application_id"] and row["application_state"] == ApplicationState.REVIEW:
                        controls = st.columns(3)
                        if controls[0].button("Prepare application", key=f"today-prepare-{row['job_id']}"):
                            st.session_state["nav_target"] = "Review Queue"
                            st.rerun()
                        if controls[1].button("Approve", key=f"today-approve-{row['job_id']}"):
                            st.session_state["nav_target"] = "Review Queue"
                            st.rerun()
                        if controls[2].button("Skip", key=f"today-skip-{row['job_id']}"):
                            repo.transition(row["application_id"], ApplicationState.SKIPPED, "user", "Skipped from Today inbox")
                            st.rerun()
        remaining = [row for row in new_rows if row["job_id"] not in shown]
        if remaining:
            st.markdown("#### Other new leads")
            st.caption("These remain outside the active application queue until direct verification or eligibility evidence improves.")
            st.dataframe(
                [{"Company": row["company"], "Title": row["title"], "Location": row["location"], "Source": row["source"], "Direct status": row["resolution_status"] or "AGGREGATOR_LEAD"} for row in remaining],
                hide_index=True,
                use_container_width=True,
            )

elif page == "Job Search":
    st.subheader("Find new jobs")
    st.caption(
        "Searches public sources broadly, then promotes only live, verified employer/ATS vacancies. No login, CAPTCHA bypass, or restricted-source scraping."
    )
    with st.container(border=True):
        st.caption("Role-first discovery uses your saved roles plus related early-career analytics queries. Aggregator listings are leads only; a verified direct vacancy is required before review.")
        if st.button("Find New Jobs", type="primary"):
            search = repo.get_search_config()
            additional_boards = [
                x.strip() for x in search.get("greenhouse_boards", "").splitlines() if x.strip()
            ]
            try:
                providers = [
                    (
                        "EMPLOYMENT_HERO",
                        lambda: EmploymentHeroProvider(DEFAULT_EMPLOYMENT_HERO_BOARDS).discover_jobs(
                            broad_discovery_queries(search)
                        ),
                    ),
                    ("LEVER", lambda: LeverProvider(DEFAULT_LEVER_SITES).discover_jobs()),
                ]
                greenhouse_boards = DEFAULT_GREENHOUSE_BOARDS | {
                    board: board for board in additional_boards
                }
                if greenhouse_boards:
                    providers.append(
                        ("GREENHOUSE", lambda: GreenhouseProvider(greenhouse_boards).discover_jobs())
                    )
                if settings.adzuna_app_id and settings.adzuna_app_key:
                    providers.append(
                        (
                            "ADZUNA",
                            lambda: AdzunaProvider(
                                settings.adzuna_app_id, settings.adzuna_app_key
                            ).discover_jobs(broad_discovery_queries(search), [], pages_per_query=1),
                        )
                    )
                result = run_broad_discovery(repo, search, providers)
                st.success(
                    f"Discovery complete: {result.funnel['new_jobs']} new · {result.funnel['eligible']} eligible · {result.funnel['good_or_possible']} good/possible · {result.funnel['live_direct_vacancy']} direct verified. Open Today for the ranked inbox."
                )
            except httpx.HTTPError:
                st.error(
                    "A public job source could not be retrieved. Check the connection or Advanced source configuration."
                )
    latest_run = repo.latest_discovery_run()
    if latest_run:
        funnel = json.loads(latest_run["funnel_json"])
        st.subheader("Latest discovery funnel")
        st.dataframe(
            [{"Stage": key.replace("_", " ").title(), "Jobs": value} for key, value in funnel.items()],
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("Source coverage and diagnostics"):
            st.dataframe(
                [
                    {
                        "Source": row["source"], "Raw": row["raw_count"],
                        "New": row["new_count"], "Eligible": row["eligible_count"],
                        "Good / Possible": row["good_possible_count"],
                        "Likely eligible": row["likely_eligible_count"], "Possible": row["possible_count"],
                        "Direct verified": row["direct_verified_count"], "Live": row["live_count"],
                        "Diagnostic": row["error_message"] or "",
                    }
                    for row in repo.discovery_run_sources(latest_run["id"])
                ],
                use_container_width=True,
                hide_index=True,
            )
    with st.expander("Manual job entry (fallback/testing only)"), st.form("job"):
        title = st.text_input("Job title")
        company = st.text_input("Company")
        url = st.text_input("Job URL")
        location = st.text_input("Location")
        description = st.text_area("Job description", height=220)
        if st.form_submit_button("Screen and add to review"):
            if not all([title, company, url, description]):
                st.error("Title, company, URL, and description are required.")
            else:
                job = JobInput(title, company, url, description, "MANUAL", location)
                job_id, created = repo.add_job(job)
                extraction = None
                if settings.openai_api_key:
                    try:
                        extractor = OpenAIJobExtractor(
                            settings.openai_api_key, settings.openai_model
                        )
                        extraction = extractor.as_dict(extractor.extract(title, description))
                    except ExtractionError:
                        st.warning(
                            "Structured extraction was unavailable; using deterministic screening only."
                        )
                score = score_job(
                    extract_requirements(description, location),
                    title,
                    repo.facts(),
                    location,
                    repo.get_score_config(),
                )
                if created:
                    repo.screen_and_assess(job_id, score, extraction)
                    app_id = repo.create_application(job_id)
                    st.success(
                        f"Added: {score.total}% — {'queued for review' if app_id else 'blocked by hard eligibility rules'}."
                    )
                else:
                    st.info("Existing job preserved; its current workflow state was not changed.")

elif page == "Review Queue":
    st.subheader("Your opportunities")
    st.caption(
        "Review the evidence, decide whether to prepare an application, then edit and approve your materials. Nothing is submitted automatically."
    )
    rows = repo.review_queue()
    unresolved_aggregators = [
        row
        for row in rows
        if row["source"] == "ADZUNA"
        and row["resolution_status"] not in {"DIRECT_VERIFIED", "CLOSED"}
    ]
    if unresolved_aggregators and st.button(
        "Resolve direct application sources", key="resolve-direct-sources-batch"
    ):
        outcomes = []
        search_finder = configured_search_resolution_finder(
            settings.google_cse_api_key, settings.google_cse_id
        )
        for candidate in unresolved_aggregators:
            try:
                result = resolve_public_job(repo, candidate["job_id"], search_finder)
                outcomes.append(
                    f"{candidate['company']}: {result.status.replace('_', ' ').title()}"
                )
            except (ValueError, httpx.HTTPError):
                outcomes.append(f"{candidate['company']}: unavailable")
        st.info(" · ".join(outcomes))
        st.rerun()
    if not rows:
        st.info("No opportunities are ready to review.")
    for row in rows:
        components = json.loads(row["score_json"]).get("components", {})
        score = ScoreBreakdown(**components)
        recommendation = application_recommendation(score, repo.get_score_config())
        requirements = analyze_job_description(
            row["description"], row["location"], row["title"]
        ).requirements
        with st.container(border=True):
            st.markdown(
                f"### {row['title']}\n{row['company']} · {row['location'] or 'Location not stated'}"
            )
            st.write(
                f"**{row['score']}% · {row['recommendation']} · Recommendation: {recommendation.label}**"
            )
            st.caption(
                f"Job-description evidence: {row['jd_status'].title()} · {row['evidence_quality'].title()} confidence · {row['enrichment_source'] or row['source']}"
            )
            resolution = row["resolution_status"] or (
                "DIRECT_VERIFIED" if row["source"] in {"LEVER", "GREENHOUSE"} else "NOT_RESOLVED"
            )
            if resolution == "DIRECT_VERIFIED":
                st.caption(
                    f"Discovered via: {row['source']} · Apply via: {row['application_source'] or row['source']} · Resolution: Verified"
                )
                if row["canonical_application_url"]:
                    st.link_button(
                        "Open verified application source",
                        row["canonical_application_url"],
                        key=review_widget_key(
                            "direct-source", row["application_id"], row["job_id"], "resolution"
                        ),
                    )
            else:
                st.caption(
                    f"Discovered via: {row['source']} · Direct application resolution: {resolution.replace('_', ' ').title()}"
                )
                if row["source"] == "ADZUNA" and st.button(
                    "Resolve direct application source",
                    key=review_widget_key(
                        "resolve-direct-source", row["application_id"], row["job_id"], "resolution"
                    ),
                ):
                    try:
                        result = resolve_public_job(
                            repo,
                            row["job_id"],
                            configured_search_resolution_finder(
                                settings.google_cse_api_key, settings.google_cse_id
                            ),
                        )
                        st.info(
                            f"Resolution result: {result.status.replace('_', ' ').title()} ({result.confidence:.0%})."
                        )
                        st.rerun()
                    except (ValueError, httpx.HTTPError) as exc:
                        st.warning(f"Direct-source resolution could not complete safely: {exc}")
            ats = detect_platform(row["canonical_application_url"] or row["url"])
            route = route_for_ats(ats)
            st.caption(
                f"Execution route: **{route.level.value}** · {route.summary}"
            )
            st.write(recommendation.explanation)
            if score.matched_skills:
                st.caption("Matched requirements: " + ", ".join(score.matched_skills))
            if score.missing_skills:
                st.caption("Missing requirements: " + ", ".join(score.missing_skills))
            if score.experience_evidence:
                st.caption("Experience evidence used: " + " · ".join(score.experience_evidence))
            if resolution == "DIRECT_VERIFIED":
                evaluation = build_opportunity_evaluation(
                    job_id=row["job_id"],
                    title=row["title"],
                    score=score,
                    config=repo.get_score_config(),
                    requirements=requirements,
                    facts=repo.facts(),
                    direct_source_status=resolution,
                    eligible=True,
                    evidence_quality=row["evidence_quality"],
                )
                evaluation_status = repo.opportunity_evaluation_status(row["job_id"])
                if evaluation_status["stored"] is None:
                    repo.save_opportunity_evaluation(
                        row["job_id"], evaluation.as_dict(), evaluation.as_dict()["tailoring_plan"]
                    )
                    evaluation_status = repo.opportunity_evaluation_status(row["job_id"])
                stored_evaluation = evaluation_status["stored"]
                with st.expander("Opportunity evaluation", expanded=row["state"] == ApplicationState.REVIEW):
                    if evaluation_status["stale"]:
                        st.warning("Your candidate profile has changed since this evaluation was created.")
                        st.caption(
                            "The earlier fit narrative and tailoring advice are retained in history, "
                            "but are not shown as current advice."
                        )
                        if st.button(
                            "Re-evaluate with current profile",
                            key=review_widget_key(
                                "refresh-evaluation", row["application_id"], row["job_id"], "evaluation"
                            ),
                        ):
                            repo.save_opportunity_evaluation(
                                row["job_id"],
                                evaluation.as_dict(),
                                evaluation.as_dict()["tailoring_plan"],
                                refresh_stale=True,
                            )
                            st.rerun()
                    else:
                        st.markdown("##### Recommendation")
                        st.write(
                            f"**{evaluation.deterministic_recommendation} · "
                            f"{evaluation.deterministic_score}% deterministic match**"
                        )
                        st.caption(
                            f"Verified direct source · {evaluation.evidence_quality.title()} job-description evidence"
                        )
                        narrative = (
                            json.loads(stored_evaluation["narrative_json"])
                            if stored_evaluation and stored_evaluation["narrative_json"]
                            else None
                        )
                        if narrative:
                            st.markdown("##### Why this role fits")
                            st.write(narrative["fit_summary"]["text"])
                            for item in narrative["strongest_matches"]:
                                st.write(f"- {item['text']}")
                            if narrative["risk_or_stretch_factors"]:
                                st.caption(
                                    "Stretch factors: "
                                    + " · ".join(narrative["risk_or_stretch_factors"])
                                )
                            st.caption(narrative["recommendation_reasoning"])
                        elif settings.openai_api_key:
                            if st.button(
                                "Generate grounded fit narrative",
                                key=review_widget_key(
                                    "fit-narrative", row["application_id"], row["job_id"], "evaluation"
                                ),
                            ):
                                try:
                                    narrative = OpportunityNarrativeWriter(
                                        settings.openai_api_key, settings.openai_model
                                    ).generate(evaluation)
                                    repo.save_opportunity_evaluation(
                                        row["job_id"],
                                        evaluation.as_dict(),
                                        evaluation.as_dict()["tailoring_plan"],
                                        narrative.as_dict(),
                                    )
                                    st.rerun()
                                except OpportunityNarrativeError:
                                    st.info(
                                        "The grounded AI summary is unavailable. The deterministic evaluation below remains complete."
                                    )
                        else:
                            st.caption(
                                "A grounded AI summary is optional. The deterministic evaluation remains available without it."
                            )
                        st.markdown("##### Your strongest evidence")
                        for item in evaluation.strongest_candidate_evidence:
                            st.write(f"- **{item.value}** · {item.source}")
                        st.markdown("##### Gaps / stretch factors")
                        if evaluation.missing_requirements:
                            st.write(" · ".join(evaluation.missing_requirements))
                        else:
                            st.caption("No explicit unsupported skill requirement was found in the job description.")
                        st.markdown("##### Tailoring plan")
                        st.write("**Emphasise**")
                        for item in evaluation.tailoring_plan.emphasise:
                            st.write(f"- {item.value}")
                        if evaluation.tailoring_plan.de_emphasise:
                            st.write("**De-emphasise**")
                            for item in evaluation.tailoring_plan.de_emphasise:
                                st.write(f"- {item.value}")
                        if evaluation.tailoring_plan.do_not_claim:
                            st.write("**Do not claim**")
                            st.write(" · ".join(evaluation.tailoring_plan.do_not_claim))
                    history = repo.opportunity_evaluation_history(row["job_id"])
                    if history:
                        with st.expander("Evaluation history", expanded=False):
                            st.caption(f"{len(history)} previous evaluation revision(s) retained for audit.")
                            for item in history:
                                st.write(
                                    f"Revision {item['revision']} · {item['evaluated_at']} · "
                                    f"{item['archive_reason'].replace('_', ' ').title()}"
                                )
                    st.write("**Application angle**")
                    st.write(evaluation.tailoring_plan.application_angle)
                    st.caption("Next safe action: " + evaluation.next_recommended_action)
            action_cols = st.columns(4)
            prepare_label = (
                "Prepare application"
                if row["state"] == ApplicationState.REVIEW
                else "Regenerate materials"
            )
            if row["state"] in {
                ApplicationState.REVIEW,
                ApplicationState.MATERIALS_READY,
            } and action_cols[0].button(
                prepare_label,
                key=review_widget_key("prepare", row["application_id"], row["job_id"], "card"),
            ):
                if not settings.openai_api_key:
                    st.error(
                        "Set OPENAI_API_KEY to generate polished grounded application materials."
                    )
                else:
                    try:
                        draft = GroundedApplicationWriter(
                            settings.openai_api_key, settings.openai_model
                        ).generate(
                            row["title"],
                            row["company"],
                            requirements,
                            score,
                            repo.facts(),
                            row["description"],
                        )
                        for kind, content in draft.artifacts.items():
                            repo.add_artifact(
                                row["application_id"],
                                kind,
                                content,
                                draft.fact_ids,
                                settings.openai_model,
                            )
                        if row["state"] == ApplicationState.REVIEW:
                            repo.transition(
                                row["application_id"],
                                ApplicationState.MATERIALS_READY,
                                "user",
                                "Generated and validated grounded materials for review",
                            )
                        st.rerun()
                    except ApplicationWritingError as exc:
                        st.error(str(exc))
            if action_cols[1].button(
                "Save for later",
                key=review_widget_key(
                    "save-for-later", row["application_id"], row["job_id"], "card"
                ),
            ):
                repo.save_for_later(row["application_id"])
                st.toast("Saved for later.")
            if row["state"] in {
                ApplicationState.REVIEW,
                ApplicationState.MATERIALS_READY,
            } and action_cols[2].button(
                "Skip",
                key=review_widget_key("skip", row["application_id"], row["job_id"], "card"),
            ):
                repo.transition(
                    row["application_id"],
                    ApplicationState.SKIPPED,
                    "user",
                    "User skipped opportunity",
                )
                st.rerun()
            action_cols[3].link_button(
                "View job",
                row["url"],
                key=review_widget_key("view-job", row["application_id"], row["job_id"], "card"),
            )
            artifacts = repo.artifacts(row["application_id"])
            candidate_artifacts = [
                artifact for artifact in artifacts if artifact["kind"] != "claim_ledger"
            ]
            latest_by_kind = {}
            for artifact in candidate_artifacts:
                latest_by_kind.setdefault(artifact["kind"], artifact)
            if candidate_artifacts:
                st.markdown("#### Application materials")

            latest_cover = latest_by_kind.pop("cover_letter", None)
            if latest_cover:
                st.markdown("##### Cover letter")
                st.caption(f"Current version · v{latest_cover['version']}")
                edited = st.text_area(
                    "Cover letter",
                    latest_cover["content"],
                    key=review_widget_key(
                        "cover-letter-editor",
                        row["application_id"],
                        row["job_id"],
                        "current",
                        latest_cover["id"],
                    ),
                    height=320,
                )
                if edited != latest_cover["content"] and st.button(
                    "Save cover letter as new version",
                    key=review_widget_key(
                        "cover-letter-save",
                        row["application_id"],
                        row["job_id"],
                        "current",
                        latest_cover["id"],
                    ),
                ):
                    repo.add_artifact(
                        row["application_id"],
                        "cover_letter",
                        edited,
                        json.loads(latest_cover["source_fact_ids_json"]),
                        "user_edit",
                    )
                    st.rerun()
                try:
                    st.download_button(
                        "Download formatted DOCX",
                        data=export_cover_letter_docx(latest_cover["content"]),
                        file_name=f"{row['company']}-{row['title']}-cover-letter.docx".replace(
                            "/", "-"
                        ),
                        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        key=review_widget_key(
                            "cover-letter-download",
                            row["application_id"],
                            row["job_id"],
                            "current",
                            latest_cover["id"],
                        ),
                    )
                except CoverLetterExportError as exc:
                    st.warning(f"DOCX export is unavailable for this version: {exc}")

            for kind, artifact in latest_by_kind.items():
                edited = st.text_area(
                    f"{kind.replace('_', ' ').title()} · v{artifact['version']}",
                    artifact["content"],
                    key=review_widget_key(
                        "artifact-editor",
                        row["application_id"],
                        row["job_id"],
                        "current",
                        artifact["id"],
                    ),
                    height=180,
                )
                if edited != artifact["content"] and st.button(
                    "Save as new version",
                    key=review_widget_key(
                        "artifact-save",
                        row["application_id"],
                        row["job_id"],
                        "current",
                        artifact["id"],
                    ),
                ):
                    repo.add_artifact(
                        row["application_id"],
                        kind,
                        edited,
                        json.loads(artifact["source_fact_ids_json"]),
                        "user_edit",
                    )
                    st.rerun()

            latest_artifact_ids = {artifact["id"] for artifact in latest_by_kind.values()}
            if latest_cover:
                latest_artifact_ids.add(latest_cover["id"])
            historical = [
                artifact
                for artifact in candidate_artifacts
                if artifact["id"] not in latest_artifact_ids
            ]
            if historical:
                with st.expander("Version history"):
                    for artifact in historical:
                        st.text_area(
                            f"{artifact['kind'].replace('_', ' ').title()} · v{artifact['version']}",
                            artifact["content"],
                            key=review_widget_key(
                                "artifact-history",
                                row["application_id"],
                                row["job_id"],
                                "history",
                                artifact["id"],
                            ),
                            height=160,
                            disabled=True,
                        )
            if row["state"] == ApplicationState.MATERIALS_READY and st.button(
                "Approve & mark ready to apply",
                key=review_widget_key("approve", row["application_id"], row["job_id"], "approval"),
            ):
                repo.transition(
                    row["application_id"],
                    ApplicationState.APPROVED,
                    "user",
                    "User explicitly approved application",
                )
                repo.transition(
                    row["application_id"],
                    ApplicationState.READY_TO_APPLY,
                    "system",
                    "V1 handoff; execution deferred",
                )
                st.rerun()
            if row["state"] == ApplicationState.MATERIALS_READY and st.button(
                "Return for revision",
                key=review_widget_key(
                    "return-for-revision", row["application_id"], row["job_id"], "approval"
                ),
            ):
                repo.transition(
                    row["application_id"], ApplicationState.REVIEW, "user", "Returned for revision"
                )
                st.rerun()
            if row["state"] in {ApplicationState.APPROVED, ApplicationState.READY_TO_APPLY}:
                st.success(
                    "Materials approved and ready for guided application. This app will never submit without your explicit final confirmation."
                )
            if row["state"] in {
                ApplicationState.READY_TO_APPLY,
                ApplicationState.APPLYING,
                ApplicationState.HUMAN_VERIFICATION_REQUIRED,
            }:
                execution = repo.latest_execution(row["application_id"])
                if execution is None:
                    if row["state"] == ApplicationState.READY_TO_APPLY and st.button(
                        "Start application",
                        key=review_widget_key(
                            "start-application", row["application_id"], row["job_id"], "execution"
                        ),
                    ):
                        try:
                            use_current_canonical_cv(
                                repo, ROOT, row["application_id"], explicit_refresh=True
                            )
                            direct_url = repo.canonical_application_url(row["job_id"])
                            if row["source"] == "ADZUNA" and not direct_url:
                                raise ValueError(
                                    "Resolve and verify a direct employer application source before starting guided execution."
                                )
                            application_url = application_url_for(direct_url or row["url"])
                            platform = detect_platform(application_url)
                            if platform == "UNSUPPORTED":
                                raise ValueError(
                                    "The direct source is verified, but guided browser execution is not supported for this ATS yet. Materials remain available for review."
                                )
                            repo.start_execution(row["application_id"], platform, application_url)
                            st.rerun()
                        except (FileNotFoundError, CVParseError, ValueError) as exc:
                            st.error(str(exc))
                else:
                    state = ExecutionState(execution["state"])
                    execution_route = route_for_ats(execution["platform"])
                    progress = {
                        ExecutionState.OPENING_APPLICATION: 1,
                        ExecutionState.FILLING_FORM: 2,
                        ExecutionState.WAITING_FOR_USER: 3,
                        ExecutionState.RESUMING: 3,
                        ExecutionState.HUMAN_VERIFICATION_REQUIRED: 3,
                        ExecutionState.BROWSER_CONTEXT_MISMATCH: 3,
                        ExecutionState.READY_FOR_FINAL_REVIEW: 4,
                        ExecutionState.FAILED: 0,
                    }.get(state, 0)
                    st.markdown("##### Guided application")
                    st.progress(progress / 4)
                    st.caption(
                        "✓ Opened application  →  ✓ Filled profile  →  ✓ Uploaded CV  →  ● Waiting for your answers  →  ○ Final review  →  ○ Submit"
                    )
                    st.write(f"**Current step: {state.replace('_', ' ').title()}**")
                    st.caption(
                        f"Execution policy: **{execution_route.level.value}** · {execution_route.summary}"
                    )
                    if execution_route.level.value == "ASSISTED":
                        st.info(
                            "Assisted handoff: the prepared materials, application URL, and tracking history are retained. "
                            "Complete remaining PageUp steps in the official form; this V1 copilot will not repeatedly retry complex ATS navigation."
                        )
                    application_cv_label = None
                    try:
                        cv_status = application_cv_status(repo, ROOT, row["application_id"])
                        if cv_status.outdated:
                            st.warning(
                                "Newer canonical CV available. This application still has an older or legacy CV attachment."
                            )
                            if st.button(
                                "Update application CV",
                                key=review_widget_key(
                                    "update-application-cv",
                                    row["application_id"],
                                    row["job_id"],
                                    "execution",
                                    execution["id"],
                                ),
                            ):
                                use_current_canonical_cv(
                                    repo, ROOT, row["application_id"], explicit_refresh=True
                                )
                                repo.record_execution_cv_refresh(execution["id"])
                                st.success(
                                    "The application CV was updated locally. No form action or submission was performed."
                                )
                                st.rerun()
                        elif cv_status.application_cv:
                            application_cv_label = current_cv_summary(cv_status.application_cv)
                            st.caption(f"Current application CV: {application_cv_label}")
                    except (FileNotFoundError, CVParseError, ValueError) as exc:
                        st.warning(f"CV status needs attention: {exc}")
                    st.link_button(
                        "Open application page",
                        execution["application_url"],
                        key=review_widget_key(
                            "open-application-page",
                            row["application_id"],
                            row["job_id"],
                            "execution",
                            execution["id"],
                        ),
                    )
                    if execution_route.level.value != "ASSISTED" and state == ExecutionState.OPENING_APPLICATION and st.button(
                        "Inspect visible form",
                        key=review_widget_key(
                            "inspect-form",
                            row["application_id"],
                            row["job_id"],
                            "execution",
                            execution["id"],
                        ),
                    ):
                        try:
                            inspect_application(repo, execution, repo.profile(), repo.facts())
                            st.rerun()
                        except (RuntimeError, ValueError) as exc:
                            repo.update_execution(
                                execution["id"],
                                ExecutionState.FAILED,
                                "Form inspection could not start",
                                error_message=str(exc),
                            )
                            st.rerun()
                    if execution_route.level.value != "ASSISTED" and state == ExecutionState.FILLING_FORM and st.button(
                        "Fill supported fields and upload approved documents",
                        key=review_widget_key(
                            "fill-form",
                            row["application_id"],
                            row["job_id"],
                            "execution",
                            execution["id"],
                        ),
                    ):
                        try:
                            fill_supported_fields(
                                repo,
                                execution,
                                repo.profile(),
                                repo.facts(),
                                ROOT
                                / "data"
                                / "artifacts"
                                / "application_uploads"
                                / str(execution["id"]),
                            )
                            st.rerun()
                        except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                            repo.update_execution(
                                execution["id"],
                                ExecutionState.FAILED,
                                "Guided form fill could not complete",
                                error_message=str(exc),
                            )
                            st.rerun()
                    detected = json.loads(execution["detected_fields_json"])
                    autofilled = json.loads(execution["autofilled_fields_json"])
                    unresolved = json.loads(execution["unresolved_fields_json"])
                    warnings = json.loads(execution["warnings_json"])
                    if autofilled:
                        st.caption(
                            "Auto-filled: " + ", ".join(normalise_autofill_summary(autofilled))
                        )
                    if warnings:
                        st.warning(" ".join(warnings))
                    uploads = repo.execution_uploads(execution["id"])
                    saved_answers = repo.execution_answers(execution["id"])
                    if saved_answers:
                        st.caption(
                            "Current application user input: "
                            + ", ".join(answer["label"] for answer in saved_answers)
                        )
                    if execution["error_message"]:
                        st.error("Guided application paused: " + execution["error_message"])
                    if state == ExecutionState.BROWSER_CONTEXT_MISMATCH:
                        st.error(
                            "The PageUp page you are viewing is not controlled by the active agent browser session. "
                            "Use the agent-owned headed browser to recover the official application, then inspect the visible form."
                        )
                        if execution_route.level.value != "ASSISTED" and st.button(
                            "Recover PageUp application from official URL",
                            key=review_widget_key(
                                "recover-pageup-context",
                                row["application_id"], row["job_id"], "execution", execution["id"],
                            ),
                        ):
                            try:
                                inspect_application(repo, execution, repo.profile(), repo.facts())
                                st.rerun()
                            except (RuntimeError, ValueError) as exc:
                                st.error(f"PageUp recovery remains paused: {exc}")
                    if execution_route.level.value != "ASSISTED" and state == ExecutionState.FAILED and st.button(
                        "Start a new guided session",
                        key=review_widget_key(
                            "restart-application",
                            row["application_id"],
                            row["job_id"],
                            "execution",
                            execution["id"],
                        ),
                    ):
                        repo.start_execution(
                            row["application_id"],
                            execution["platform"],
                            application_url_for(row["url"]),
                        )
                        st.rerun()
                    if state == ExecutionState.WAITING_FOR_USER and execution_route.level.value != "ASSISTED":
                        st.markdown("##### Your input is needed")
                        saved_answers = repo.answer_map(execution["id"])
                        unresolved = unanswered_questions(unresolved, saved_answers)
                        if can_continue_waiting_session(
                            execution,
                            json.loads(execution["unresolved_fields_json"]),
                            saved_answers,
                        ):
                            st.success(
                                "All previously requested answers are saved. Continue to apply them to the public form."
                            )
                            if st.button(
                                "Continue application",
                                key=review_widget_key(
                                    "continue-application",
                                    row["application_id"],
                                    row["job_id"],
                                    "execution",
                                    execution["id"],
                                ),
                            ):
                                try:
                                    resume_execution(
                                        repo,
                                        execution,
                                        repo.profile(),
                                        repo.facts(),
                                        ROOT
                                        / "data"
                                        / "artifacts"
                                        / "application_uploads"
                                        / str(execution["id"]),
                                    )
                                    st.rerun()
                                except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                    repo.update_execution(
                                        execution["id"],
                                        ExecutionState.WAITING_FOR_USER,
                                        "Replay paused after a recoverable form error",
                                        json.loads(execution["detected_fields_json"]),
                                        json.loads(execution["autofilled_fields_json"]),
                                        json.loads(execution["unresolved_fields_json"]),
                                        [
                                            "The saved answers were retained. Retry after reviewing the public form."
                                        ],
                                        error_message=str(exc),
                                    )
                                    st.rerun()
                        if any(not isinstance(item, dict) or "key" not in item for item in unresolved):
                            st.info(
                                "Refresh the form questions once to make this earlier execution session safely resumable."
                            )
                            if st.button(
                                "Refresh questions from application page",
                                key=review_widget_key(
                                    "refresh-questions",
                                    row["application_id"],
                                    row["job_id"],
                                    "execution",
                                    execution["id"],
                                ),
                            ):
                                try:
                                    refresh_execution_questions(
                                        repo, execution, repo.profile(), repo.facts()
                                    )
                                    st.rerun()
                                except (RuntimeError, ValueError) as exc:
                                    repo.update_execution(
                                        execution["id"],
                                        ExecutionState.FAILED,
                                        "Question refresh could not complete",
                                        error_message=str(exc),
                                    )
                                    st.rerun()
                        else:
                            for question in unresolved:
                                question_key = question["key"]
                                field_type = question["field_type"]
                                options = question.get("options", [])
                                with st.container(border=True):
                                    st.write(f"**{question['label']}**")
                                    st.caption(
                                        f"{field_type.replace('_', ' ').title()} · {question['category'].title()} · {question['reason']}"
                                    )
                                    if question.get("current_value"):
                                        st.caption(
                                            "Current value: " + str(question["current_value"])
                                        )
                                    if question.get("policy_url"):
                                        st.link_button(
                                            "View related policy",
                                            question["policy_url"],
                                            key=review_widget_key(
                                                "policy",
                                                row["application_id"],
                                                row["job_id"],
                                                "execution",
                                                question_key,
                                            ),
                                        )
                                    with st.form(
                                        review_widget_key(
                                            "answer-form",
                                            row["application_id"],
                                            row["job_id"],
                                            "execution",
                                            question_key,
                                        )
                                    ):
                                        previous = saved_answers.get(question_key)
                                        if field_type == "radio":
                                            answer = st.radio(
                                                "Your answer",
                                                options,
                                                index=options.index(previous)
                                                if previous in options
                                                else None,
                                                key=review_widget_key(
                                                    "answer-radio",
                                                    row["application_id"],
                                                    row["job_id"],
                                                    "execution",
                                                    question_key,
                                                ),
                                            )
                                        elif field_type == "select":
                                            answer = st.selectbox(
                                                "Your answer",
                                                options,
                                                index=options.index(previous)
                                                if previous in options
                                                else None,
                                                key=review_widget_key(
                                                    "answer-select",
                                                    row["application_id"],
                                                    row["job_id"],
                                                    "execution",
                                                    question_key,
                                                ),
                                            )
                                        elif field_type == "multiselect" or (
                                            field_type == "checkbox" and len(options) > 1
                                        ):
                                            answer = st.multiselect(
                                                "Your answers",
                                                options,
                                                default=previous
                                                if isinstance(previous, list)
                                                else [],
                                                key=review_widget_key(
                                                    "answer-multiselect",
                                                    row["application_id"],
                                                    row["job_id"],
                                                    "execution",
                                                    question_key,
                                                ),
                                            )
                                        elif field_type == "checkbox":
                                            answer = st.checkbox(
                                                "I explicitly agree",
                                                value=bool(previous)
                                                if previous is not None
                                                else False,
                                                key=review_widget_key(
                                                    "answer-checkbox",
                                                    row["application_id"],
                                                    row["job_id"],
                                                    "execution",
                                                    question_key,
                                                ),
                                            )
                                        else:
                                            answer = st.text_area(
                                                "Your answer",
                                                value=str(previous or ""),
                                                key=review_widget_key(
                                                    "answer-text",
                                                    row["application_id"],
                                                    row["job_id"],
                                                    "execution",
                                                    question_key,
                                                ),
                                            )
                                        remember = st.checkbox(
                                            "Remember this answer for similar future applications",
                                            disabled=not question.get("reusable_allowed", False),
                                            key=review_widget_key(
                                                "remember-answer",
                                                row["application_id"],
                                                row["job_id"],
                                                "execution",
                                                question_key,
                                            ),
                                        )
                                        if not question.get("reusable_allowed", False):
                                            st.caption(
                                                "This answer is not reusable because it is sensitive or legally meaningful."
                                            )
                                        save = st.form_submit_button("Save answer")
                                        save_continue = st.form_submit_button("Save and continue")
                                    if save or save_continue:
                                        try:
                                            validate_user_answer(question, answer)
                                            repo.save_execution_answer(
                                                execution["id"],
                                                question,
                                                answer,
                                                question["category"],
                                                remember,
                                            )
                                            if save_continue:
                                                resume_execution(
                                                    repo,
                                                    repo.latest_execution(row["application_id"]),
                                                    repo.profile(),
                                                    repo.facts(),
                                                    ROOT
                                                    / "data"
                                                    / "artifacts"
                                                    / "application_uploads"
                                                    / str(execution["id"]),
                                                )
                                            st.rerun()
                                        except (
                                            RuntimeError,
                                            ValueError,
                                            CoverLetterExportError,
                                        ) as exc:
                                            st.error(str(exc))
                    with st.expander("Execution session history"):
                        st.caption(f"Session {execution['id']} · {state.replace('_', ' ').title()}")
                        execution_events = repo.execution_events(execution["id"])
                        st.dataframe(
                            [
                                {
                                    "When": event["created_at"],
                                    "State": event["state"].replace("_", " ").title(),
                                    "Event": event["step"],
                                }
                                for event in execution_events
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )
                        if uploads:
                            st.caption("Historical document upload events")
                            st.dataframe(
                                [
                                    {
                                        "When": upload["uploaded_at"],
                                        "Document": upload["filename"],
                                        "Field": upload["upload_field"],
                                    }
                                    for upload in uploads
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )
                        st.caption(
                            "Technical details remain hidden unless needed for troubleshooting."
                        )
                    if state == ExecutionState.HUMAN_VERIFICATION_REQUIRED:
                        verification_detail = next(
                            (
                                warning
                                for warning in warnings
                                if "verification" in warning.casefold()
                                or "hcaptcha" in warning.casefold()
                            ),
                            "Active human verification detected.",
                        )
                        st.warning(
                            verification_detail
                            + " Complete it yourself in the visible application page; automation will not bypass it."
                        )
                        if st.button(
                            "Revalidate live form after verification",
                            key=review_widget_key(
                                "revalidate-after-verification",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                revalidate_after_human_verification(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                st.error(f"Live form could not be revalidated safely: {exc}")
                    if state == ExecutionState.POST_VERIFICATION_REVALIDATION:
                        st.warning(
                            "Lever requires a complete form revalidation after verification. Nothing has been submitted, and another explicit confirmation will be required."
                        )
                        if st.button(
                            "Revalidate application form",
                            key=review_widget_key(
                                "post-verification-revalidate",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                revalidate_after_human_verification(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                st.error(f"Live form could not be revalidated safely: {exc}")
                    if state == ExecutionState.RESUMING:
                        st.info("The live PageUp form is ready for a safe revalidation. Nothing will be submitted.")
                        if st.button(
                            "Continue live-form revalidation",
                            key=review_widget_key(
                                "continue-live-revalidation",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                revalidate_after_human_verification(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                st.error(f"Live form could not be revalidated safely: {exc}")
                    if state == ExecutionState.READY_FOR_FINAL_REVIEW:
                        st.markdown("##### Final review")
                        st.success(
                            "Nothing has been submitted yet. Review this package before explicitly authorising final submission."
                        )
                        st.caption(
                            f"{row['title']} · {row['company']} · {execution['application_url']}"
                        )
                        st.caption(
                            f"CV to upload if you confirm: {application_cv_label or 'No current application CV selected'}"
                        )
                        st.caption("Canonical source: configured local candidate document")
                        st.caption(
                            f"Auto-filled fields: {len(normalise_autofill_summary(autofilled))} · Saved answers: {len(saved_answers)} · Unresolved fields: {len(unresolved)}"
                        )
                        verification = (
                            "No human verification detected in the most recent form check."
                        )
                        st.caption("Verification status: " + verification)
                        latest_diagnostic = next(
                            iter(repo.execution_diagnostics(execution["id"])), None
                        )
                        if latest_diagnostic:
                            st.caption(
                                "Latest live check: "
                                + f"CV attached: {'Yes' if latest_diagnostic['cv_attached'] else 'No'} · "
                                + f"Required fields complete: {'Yes' if latest_diagnostic['required_complete'] else 'No'} · "
                                + f"Human verification: {'Yes' if latest_diagnostic['human_verification'] else 'No'}"
                            )
                        if st.button(
                            "Revalidate live form and documents",
                            key=review_widget_key(
                                "revalidate-final-review",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                revalidate_after_human_verification(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                st.error(f"Live form could not be revalidated safely: {exc}")
                        if st.button(
                            "Confirm and submit application",
                            type="primary",
                            key=review_widget_key(
                                "confirm-submit",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                submit_execution(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                    explicit_confirmation=True,
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                repo.update_execution(
                                    execution["id"],
                                    ExecutionState.FAILED,
                                    "Final submission failed before verification",
                                    error_message=str(exc),
                                )
                                st.rerun()
                    if state == ExecutionState.FAILED:
                        st.error(
                            "Lever verification failed. Nothing was submitted. The live form will be revalidated before any future attempt."
                        )
                        if st.button(
                            "Revalidate live form before another attempt",
                            key=review_widget_key(
                                "revalidate-failed-form",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                revalidate_after_human_verification(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                st.error(f"Live form could not be revalidated safely: {exc}")
                        if st.button(
                            "Retry final submission",
                            type="primary",
                            key=review_widget_key(
                                "retry-submit",
                                row["application_id"],
                                row["job_id"],
                                "execution",
                                execution["id"],
                            ),
                        ):
                            try:
                                submit_execution(
                                    repo,
                                    execution,
                                    repo.profile(),
                                    repo.facts(),
                                    ROOT
                                    / "data"
                                    / "artifacts"
                                    / "application_uploads"
                                    / str(execution["id"]),
                                    explicit_confirmation=True,
                                )
                                st.rerun()
                            except (RuntimeError, ValueError, CoverLetterExportError) as exc:
                                repo.update_execution(
                                    execution["id"],
                                    ExecutionState.FAILED,
                                    "Final submission retry failed before verification",
                                    error_message=str(exc),
                                )
                                st.rerun()
            with st.expander("Activity and provenance"):
                ledgers = [artifact for artifact in artifacts if artifact["kind"] == "claim_ledger"]
                if ledgers:
                    st.caption("Internal grounding validation")
                    st.code(ledgers[0]["content"])
                st.dataframe(
                    [dict(event) for event in repo.events(row["application_id"])],
                    use_container_width=True,
                )

else:
    st.subheader("Safety boundaries")
    st.markdown("""
    - Candidate claims are drawn only from approved profile facts with evidence.
    - Scores are deterministic; models may later extract evidence but do not choose the score.
    - V1 does not log in, fill forms, submit applications, or solve CAPTCHAs.
    - A future executor must pause at `HUMAN_VERIFICATION_REQUIRED` and wait for the user.
    """)
