"""Explicit, conservative routing for ATS execution.

This policy is deliberately separate from browser adapters.  An adapter being
implemented does not, by itself, make an ATS safe for autonomous submission.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AutonomyLevel(StrEnum):
    AUTO = "AUTO"
    ASSISTED = "ASSISTED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


@dataclass(frozen=True)
class AutonomyRoute:
    ats: str
    level: AutonomyLevel
    summary: str


_AUTO_ATS = {"EMPLOYMENT_HERO"}
_ASSISTED_ATS = {"PAGEUP", "WORKDAY", "SUCCESSFACTORS"}


def route_for_ats(ats: str | None, *, human_checkpoint: str | None = None) -> AutonomyRoute:
    """Return the safest execution mode for the current platform condition."""
    platform = (ats or "UNKNOWN").upper()
    if human_checkpoint:
        return AutonomyRoute(
            platform,
            AutonomyLevel.HUMAN_REQUIRED,
            "A human decision is required for consent, verification, authentication, or an unresolved question.",
        )
    if platform in _AUTO_ATS:
        return AutonomyRoute(
            platform,
            AutonomyLevel.AUTO,
            "Agent can complete this application subject to live validation and normal safety checkpoints.",
        )
    if platform in _ASSISTED_ATS:
        return AutonomyRoute(
            platform,
            AutonomyLevel.ASSISTED,
            "Agent prepares grounded materials and opens the official form; complex steps are handed to the user.",
        )
    return AutonomyRoute(
        platform,
        AutonomyLevel.ASSISTED,
        "This ATS has not been validated for autonomous submission, so the agent uses assisted execution.",
    )
