from job_agent.services.duplicate_detection import detect_duplicate


def job(**updates):
    value = {"company": "Example Co", "title": "Business Intelligence Analyst", "location": "Brisbane", "url": "https://jobs.example.com/roles/123?utm_source=index", "external_id": "123"}
    value.update(updates)
    return value


def test_exact_job_id_and_url_are_confirmed_duplicates():
    history = [{"id": 1, "company": "Other", "role_title": "Other", "job_external_id": "123", "method": "MANUAL"}]
    assert detect_duplicate(job(), history).status == "ALREADY_APPLIED"
    history = [{"id": 2, "company": "Other", "role_title": "Other", "application_url": "https://jobs.example.com/roles/123", "method": "AGENT"}]
    assert detect_duplicate(job(external_id=""), history).status == "DUPLICATE_CONFIRMED"


def test_same_title_at_different_companies_is_not_a_duplicate():
    history = [{"id": 1, "company": "Different Co", "role_title": "Business Intelligence Analyst", "location": "Brisbane", "method": "MANUAL"}]
    assert detect_duplicate(job(external_id="", url=""), history).status == "CLEAR"


def test_cross_source_same_company_role_location_is_confirmed():
    history = [{"id": 1, "company": "Example Co", "role_title": "Business Intelligence Analyst", "location": "Brisbane", "application_url": "https://aggregator.example/jobs/a", "method": "AGENT"}]
    assert detect_duplicate(job(external_id="", url="https://jobs.example.com/direct"), history).status == "DUPLICATE_CONFIRMED"


def test_known_employer_group_aliases_deduplicate_cross_source_postings():
    history = [{"id": 1, "company": "Ardent Leisure Group", "role_title": "AI Solutions Analyst", "location": "Coomera, Gold Coast North", "method": "AGENT"}]
    candidate = job(company="DreamWorld", title="AI Solutions Analyst", location="Coomera, Gold Coast North", external_id="", url="")
    assert detect_duplicate(candidate, history).status == "DUPLICATE_CONFIRMED"


def test_similar_same_company_title_is_possible_and_never_clear():
    history = [{"id": 1, "company": "Example Co", "role_title": "Business Intelligence Analyst", "location": "Brisbane", "method": "MANUAL"}]
    assert detect_duplicate(job(title="Business Intelligence Analytics Analyst", external_id="", url=""), history).status == "DUPLICATE_POSSIBLE"
