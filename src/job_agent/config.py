from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_path: Path
    browser_profile_path: Path
    canonical_cv_path: Path
    openai_api_key: str | None
    openai_model: str
    adzuna_app_id: str | None
    adzuna_app_key: str | None
    google_cse_api_key: str | None
    google_cse_id: str | None


def get_settings() -> Settings:
    load_dotenv()
    return Settings(
        database_path=Path(os.getenv("DATABASE_PATH", "data/job_agent.sqlite3")),
        browser_profile_path=Path(os.getenv("BROWSER_PROFILE_PATH", "data/browser_profile")),
        canonical_cv_path=Path(
            os.getenv("CANONICAL_CV_PATH", "candidate_documents/canonical_cv.pdf")
        ),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        adzuna_app_id=os.getenv("ADZUNA_APP_ID") or None,
        adzuna_app_key=os.getenv("ADZUNA_APP_KEY") or None,
        google_cse_api_key=os.getenv("GOOGLE_CSE_API_KEY") or None,
        google_cse_id=os.getenv("GOOGLE_CSE_ID") or None,
    )
