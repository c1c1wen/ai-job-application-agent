from __future__ import annotations

import certifi
import httpx
from openai import OpenAI


def create_openai_client(api_key: str) -> OpenAI:
    """Creates the app's sole OpenAI client with explicit certificate verification."""
    transport = httpx.Client(verify=certifi.where(), timeout=httpx.Timeout(60.0))
    return OpenAI(api_key=api_key, http_client=transport)
