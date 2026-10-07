"""Audited public employer boards used by direct discovery.

Each entry is intentionally small and declarative: extending coverage is a
registry change, not employer-specific discovery code. Identifiers are added
only after the public ATS board and employer association are verified.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EmployerBoard:
    company: str
    ats: str
    public_board_identifier: str
    country_scope: str
    location_priority: str
    enabled: bool
    notes: str


EMPLOYER_SOURCE_REGISTRY: tuple[EmployerBoard, ...] = (
    EmployerBoard("MYOB", "LEVER", "myob-2", "AU", "Australia", True, "Australian SaaS employer."),
    EmployerBoard("Megaport", "LEVER", "megaport", "AU", "Brisbane", True, "Brisbane technology employer."),
    EmployerBoard("Mable", "LEVER", "mable", "AU", "Australia", True, "Australian health-tech employer."),
    EmployerBoard("Macquarie Technology Group", "LEVER", "macquarietechnologygroup", "AU", "Australia", True, "Australian technology employer."),
    EmployerBoard("Analytic Partners", "LEVER", "analyticpartners", "AU", "Australia", True, "Public board includes Australian roles."),
    EmployerBoard("Emesent", "LEVER", "Emesent", "AU", "Brisbane", True, "Brisbane technology employer."),
    EmployerBoard("Culture Amp", "GREENHOUSE", "cultureamp", "AU", "Australia", True, "Verified public Greenhouse board; Australian data/AI employer."),
    EmployerBoard("hipages", "GREENHOUSE", "hipagesgroup", "AU", "Australia", True, "Verified public Greenhouse board; Australian technology employer."),
    EmployerBoard("CoAct", "EMPLOYMENT_HERO", "coact-20i78", "AU", "Brisbane", True, "Verified public Employment Hero employer board."),
    EmployerBoard("Australian Spatial Analytics", "EMPLOYMENT_HERO", "australian-spatial-analytics", "AU", "Brisbane", True, "Verified public Employment Hero employer board."),
    EmployerBoard("WPP Media", "GREENHOUSE", "wppmedia", "GLOBAL", "Brisbane", False, "Disabled: large global/senior inventory."),
    EmployerBoard("Binance", "LEVER", "binance", "GLOBAL", "Australia", False, "Disabled: mostly global/noisy inventory."),
    EmployerBoard("Ippon Technologies", "LEVER", "ippon", "GLOBAL", "Australia", False, "Disabled: no Australian coverage observed."),
    EmployerBoard("Deputy", "LEVER", "deputy", "GLOBAL", "Australia", False, "Disabled: no Australian coverage observed."),
    EmployerBoard("Blinq", "LEVER", "blinq", "GLOBAL", "Australia", False, "Disabled: no Australian coverage observed."),
)


def enabled_boards(ats: str) -> dict[str, str]:
    return {
        entry.public_board_identifier: entry.company
        for entry in EMPLOYER_SOURCE_REGISTRY
        if entry.ats == ats and entry.enabled
    }


DEFAULT_LEVER_SITES = enabled_boards("LEVER")
DEFAULT_GREENHOUSE_BOARDS = enabled_boards("GREENHOUSE")
DEFAULT_EMPLOYMENT_HERO_BOARDS = enabled_boards("EMPLOYMENT_HERO")
