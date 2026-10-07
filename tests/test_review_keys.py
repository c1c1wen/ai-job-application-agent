from job_agent.services.review_keys import review_widget_key


def test_review_widget_keys_include_action_record_and_context() -> None:
    card_skip = review_widget_key("skip", 44, 12, "card")
    materials_skip = review_widget_key("skip", 44, 12, "materials")
    artifact_editor = review_widget_key("artifact-editor", 44, 12, "materials", 8)
    artifact_save = review_widget_key("artifact-save", 44, 12, "materials", 8)

    assert card_skip == "skip-44-12-card"
    assert len({card_skip, materials_skip, artifact_editor, artifact_save}) == 4
