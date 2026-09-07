from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_agent.config import get_settings
from job_agent.connectors.greenhouse import GreenhouseConnector
from job_agent.domain.models import (
    ApplicationState,
    CandidateFact,
    JobInput,
    JobRequirements,
    ScoreConfig,
)
from job_agent.domain.scoring import score_job
from job_agent.llm.extraction import ExtractionError, OpenAIJobExtractor
from job_agent.repositories.database import connect, migrate
from job_agent.repositories.job_repository import JobRepository
from job_agent.services.materials import grounded_materials

st.set_page_config(page_title="AI Job Hunter", page_icon="🎯", layout="wide")
settings = get_settings()
connection = connect(settings.database_path)
migrate(connection)
repo = JobRepository(connection)

def requirements_for(description: str, location: str | None) -> JobRequirements:
    text = description.casefold()
    skills = tuple(skill for skill in ("python", "sql", "excel", "power bi", "tableau", "r") if skill in text)
    rights = tuple(x for x in ("australian citizen", "permanent resident", "unrestricted work rights") if x in text)
    education = tuple(x for x in ("bachelor", "master", "phd") if x in text and ("required" in text or "must" in text))
    return JobRequirements(required_skills=skills, education_requirements=education, work_right_requirements=rights, required_locations=((location,) if location else ()))

st.title("AI Job Hunter & Application Agent")
st.caption("V1: grounded discovery, screening, review, approval, and tracking. No automated submission.")

with st.sidebar:
    page = st.radio("Navigate", ["Dashboard", "Candidate Profile", "Settings", "Add Job", "Review Queue", "Safety"])

if page == "Dashboard":
    counts = repo.dashboard_counts()
    cols = st.columns(4)
    cols[0].metric("Jobs discovered", sum(counts.values()))
    cols[1].metric("In review", counts.get("REVIEW", 0) + counts.get("MATERIALS_READY", 0))
    cols[2].metric("Approved / ready", counts.get("APPROVED", 0) + counts.get("READY_TO_APPLY", 0))
    cols[3].metric("Applied", counts.get("APPLIED", 0))
    st.subheader("Workflow")
    st.code("DISCOVERED → SCREENED → REVIEW → MATERIALS_READY → APPROVED → READY_TO_APPLY → [V2 executor] → APPLYING → HUMAN_VERIFICATION_REQUIRED → APPLIED")
    st.info("V1 stops at READY_TO_APPLY. CAPTCHA is a resumable V2 human checkpoint, never an automation target.")

elif page == "Candidate Profile":
    st.subheader("Candidate profile — source of truth")
    st.caption("Only approved facts with evidence may appear in generated application materials.")
    with st.form("profile"):
        name = st.text_input("Name")
        email = st.text_input("Email")
        phone = st.text_input("Phone")
        location = st.text_input("Location", value="Brisbane, Australia")
        if st.form_submit_button("Save profile"):
            repo.get_or_create_profile(name or "Candidate", email, phone, location)
            st.success("Profile saved.")
    with st.form("fact"):
        fact_type = st.selectbox("Fact type", ["skill", "education", "experience", "work_right", "preferred_location", "target_role"])
        value = st.text_input("Fact")
        evidence = st.text_input("Evidence / source", placeholder="CV, profile entry, or uploaded document reference")
        if st.form_submit_button("Add approved fact"):
            profile_id = repo.get_or_create_profile("Candidate")
            if value and evidence:
                repo.add_fact(profile_id, CandidateFact(fact_type, value, evidence))
                st.success("Approved fact added.")
            else:
                st.error("A fact and evidence are both required.")
    for fact in repo.facts():
        st.write(f"{'✓ active' if fact.active else '✕ revoked'} — {fact.fact_type}: {fact.value} — {fact.evidence}")
        if fact.active and st.button("Revoke fact", key=f"revoke-{fact.id}"):
            repo.set_fact_active(fact.id, False); st.rerun()

elif page == "Settings":
    st.subheader("Deterministic scoring settings")
    config = repo.get_score_config()
    with st.form("score_config"):
        values = [st.number_input(label, min_value=0, max_value=100, value=value) for label, value in zip(["Skills", "Role", "Experience", "Education", "Location", "Preferences"], config.weights)]
        strong = st.number_input("Strong-match threshold", min_value=0, max_value=100, value=config.strong_match_threshold)
        possible = st.number_input("Possible-match threshold", min_value=0, max_value=100, value=config.possible_match_threshold)
        if st.form_submit_button("Save scoring settings"):
            try:
                if possible > strong: raise ValueError("Possible threshold cannot exceed strong threshold.")
                repo.save_score_config(ScoreConfig(*map(int, values), int(strong), int(possible)))
                st.success("Saved. The deterministic engine remains the final authority.")
            except ValueError as exc: st.error(str(exc))

elif page == "Add Job":
    st.subheader("Add a job for V1 screening")
    with st.expander("Import published jobs from a Greenhouse board"):
        st.caption("Reads the public job feed only. It does not submit applications.")
        board_token = st.text_input("Greenhouse board token", key="greenhouse_token", placeholder="e.g. companyname")
        if st.button("Import Greenhouse jobs"):
            if not board_token:
                st.error("Enter a board token.")
            else:
                try:
                    imported = GreenhouseConnector().fetch(board_token)
                    for imported_job in imported:
                        job_id, created = repo.add_job(imported_job)
                        if created:
                            score = score_job(requirements_for(imported_job.description, imported_job.location), imported_job.title, repo.facts(), imported_job.location, repo.get_score_config())
                            repo.screen_and_assess(job_id, score)
                            repo.create_application(job_id)
                    st.success(f"Imported and screened {len(imported)} published jobs.")
                except httpx.HTTPError:
                    st.error("The Greenhouse board could not be retrieved. Check the board token and connection.")
    with st.form("job"):
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
                        extractor = OpenAIJobExtractor(settings.openai_api_key, settings.openai_model)
                        extraction = extractor.as_dict(extractor.extract(title, description))
                    except ExtractionError:
                        st.warning("Structured extraction was unavailable; using deterministic screening only.")
                score = score_job(requirements_for(description, location), title, repo.facts(), location, repo.get_score_config())
                if created:
                    repo.screen_and_assess(job_id, score, extraction)
                    app_id = repo.create_application(job_id)
                    st.success(f"Added: {score.total}% — {'queued for review' if app_id else 'blocked by hard eligibility rules'}.")
                else:
                    st.info("Existing job preserved; its current workflow state was not changed.")

elif page == "Review Queue":
    st.subheader("Human review and approval queue")
    rows = repo.review_queue()
    if not rows:
        st.info("No jobs are ready for review.")
    for row in rows:
        with st.expander(f"{row['title']} — {row['company']} | {row['score']}% | {row['state']}"):
            st.write(f"Recommendation: **{row['recommendation']}**")
            st.link_button("View job", row["url"])
            artifacts = repo.artifacts(row["application_id"])
            if row["state"] == ApplicationState.REVIEW and st.button(
                "Generate grounded materials", key=f"generate-{row['application_id']}"
            ):
                drafts = grounded_materials(row["title"], row["company"], repo.facts())
                for kind, (content, fact_ids) in drafts.items():
                    repo.add_artifact(row["application_id"], kind, content, fact_ids)
                repo.transition(
                    row["application_id"], ApplicationState.MATERIALS_READY, "user", "Materials generated for review"
                )
                st.rerun()
            for artifact in artifacts:
                edited = st.text_area(f"{artifact['kind']} v{artifact['version']}", artifact["content"], key=f"{row['application_id']}-{artifact['id']}", height=180)
                if edited != artifact["content"] and st.button("Save as new version", key=f"save-{artifact['id']}"):
                    repo.add_artifact(row["application_id"], artifact["kind"], edited, json.loads(artifact["source_fact_ids_json"]))
                    st.rerun()
            if row["state"] == ApplicationState.MATERIALS_READY and st.button("Approve & mark ready to apply", key=f"approve-{row['application_id']}"):
                repo.transition(row["application_id"], ApplicationState.APPROVED, "user", "User explicitly approved application")
                repo.transition(row["application_id"], ApplicationState.READY_TO_APPLY, "system", "V1 handoff; execution deferred")
                st.rerun()
            if row["state"] == ApplicationState.MATERIALS_READY and st.button("Return for revision", key=f"revise-{row['application_id']}"):
                repo.transition(row["application_id"], ApplicationState.REVIEW, "user", "Returned for revision")
                st.rerun()
            if row["state"] in {ApplicationState.REVIEW, ApplicationState.MATERIALS_READY} and st.button("Skip", key=f"skip-{row['application_id']}"):
                repo.transition(row["application_id"], ApplicationState.SKIPPED, "user", "User skipped application")
                st.rerun()
            if row["state"] in {ApplicationState.APPROVED, ApplicationState.READY_TO_APPLY}:
                st.success("Approved. This application is ready for a future permitted execution step; V1 will not submit it.")
            st.caption("Audit trail")
            st.dataframe([dict(event) for event in repo.events(row["application_id"])], use_container_width=True)

else:
    st.subheader("Safety boundaries")
    st.markdown("""
    - Candidate claims are drawn only from approved profile facts with evidence.
    - Scores are deterministic; models may later extract evidence but do not choose the score.
    - V1 does not log in, fill forms, submit applications, or solve CAPTCHAs.
    - A future executor must pause at `HUMAN_VERIFICATION_REQUIRED` and wait for the user.
    """)
