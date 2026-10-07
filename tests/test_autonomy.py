from job_agent.services.autonomy import AutonomyLevel, route_for_ats


def test_employment_hero_is_auto_only_without_a_human_checkpoint() -> None:
    assert route_for_ats("EMPLOYMENT_HERO").level is AutonomyLevel.AUTO
    assert (
        route_for_ats("EMPLOYMENT_HERO", human_checkpoint="captcha").level
        is AutonomyLevel.HUMAN_REQUIRED
    )


def test_pageup_and_unknown_ats_are_assisted() -> None:
    assert route_for_ats("PAGEUP").level is AutonomyLevel.ASSISTED
    assert route_for_ats("UNSUPPORTED").level is AutonomyLevel.ASSISTED
