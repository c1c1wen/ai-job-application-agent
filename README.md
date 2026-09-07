# AI Job Hunter & Application Agent

A local-first, human-in-the-loop job-search workflow for one candidate. It is intentionally built to prioritise relevant, accurate applications over application volume.

## Current V1 capabilities

- Candidate profile facts with evidence references; these are the sole allowed source for personal claims.
- Manual job intake, duplicate prevention, deterministic screening, and an explainable score.
- Persistent SQLite workflow: `DISCOVERED → SCREENED → REVIEW → MATERIALS_READY → APPROVED → READY_TO_APPLY`.
- Grounded application-material drafts and claim ledger for human review.
- Explicit approval and append-only-by-application audit events (the local SQLite file remains user-controlled).
- A minimal Streamlit dashboard, profile editor, job intake form, and review queue.
- Manual public Greenhouse-board imports; repeated imports preserve existing workflow state.

## Intentionally not automated in V1

V1 does **not** log in to websites, fill application forms, upload files, submit applications, scrape restricted job boards, bypass rate limits, or solve CAPTCHAs. An approved item is moved to `READY_TO_APPLY`, preserving a clean handoff point for a later, permitted execution component.

## Safety and human-in-the-loop boundaries

- Every candidate claim must be an approved `profile_fact` with a human-readable evidence reference. The material generator uses only those facts and labels gaps rather than inventing information.
- Structured extraction can supply requirements; deterministic code calculates the score from those requirements and approved active facts.
- `REVIEW`, `APPROVED`, and `HUMAN_VERIFICATION_REQUIRED` are explicit human checkpoints.
- CAPTCHA, reCAPTCHA, hCaptcha, “are you human?”, and platform security controls are never targets for automation. A future executor must persist `HUMAN_VERIFICATION_REQUIRED`, notify the user, and resume only after manual completion.

## Platform and ATS limitations

The first supported discovery route is configured employer boards using published ATS feeds, starting with Greenhouse. The Greenhouse Job Board API exposes published job data without authentication for GET requests; it is suitable for discovery, not a blanket authorisation to submit applications. [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)

LinkedIn automation/scraping is out of scope: LinkedIn prohibits third-party crawlers, bots, plug-ins, and tools that scrape or automate activity. [LinkedIn policy](https://www.linkedin.com/help/linkedin/answer/a1341387/prohibited-software-and-extensions?lang=en) SEEK integration must use approved access; its API requires approval. [SEEK Developer API](https://developer.seek.com/)

## Planned architecture

```text
Permitted ATS feed → connector → normaliser/deduplicator → hard eligibility rules
→ structured extraction → deterministic score → review → materials → approval
→ READY_TO_APPLY → [future execution adapter] → human verification, if required → tracking
```

**V2:** configured Greenhouse/Lever discovery, local scheduling, document versioning, notifications, and supervised browser assistance only where permitted. Browser execution will be an adapter that consumes `READY_TO_APPLY` and can emit `APPLYING`, `HUMAN_VERIFICATION_REQUIRED`, or `APPLIED` without changing the current core tables.

**V3:** optional deployment, additional approved integrations, richer outcome analytics, evaluation datasets, and user-approved learning recommendations. The system will never silently change application rules from outcomes.

## Run locally

1. Create and activate a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`
2. Install the app: `pip install -e '.[dev]'`
3. Copy `.env.example` to `.env` and add an OpenAI key only when LLM extraction is enabled.
4. Run: `streamlit run app/Home.py`
5. Test: `pytest`

The initial UI works without an API key because its V1 material draft fallback is grounded and deterministic. SQLite contains sensitive personal data: keep the `data/` directory on an encrypted local disk, restrict device access, and back it up securely. Never commit `.env`, the SQLite database, uploaded CVs, session cookies, or credentials.
