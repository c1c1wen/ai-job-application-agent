"""Stable Streamlit widget keys for the Review Queue."""


def review_widget_key(
    action: str,
    application_id: int,
    job_id: int,
    context: str,
    item_id: int | None = None,
) -> str:
    """Create a page-global, deterministic key for a Review Queue control."""
    parts = (action, application_id, job_id, context, item_id)
    return "-".join(str(part) for part in parts if part is not None)
