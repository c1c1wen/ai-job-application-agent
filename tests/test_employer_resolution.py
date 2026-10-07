from job_agent.services.employer_resolution import (
    DirectVacancy,
    EmployerAtsResolver,
    GoogleProgrammableSearchClient,
    PublicSearchHit,
    SearchBasedResolutionFinder,
    provider_for_url,
    resolution_queries,
)

JOB = {
    "company": "Example Analytics",
    "title": "Data Analyst",
    "location": "Brisbane, QLD",
    "description": "Use SQL and Power BI to analyse customer data.",
    "external_id": "REQ-42",
}


def test_exact_requisition_id_verifies_direct_vacancy():
    result = EmployerAtsResolver().resolve(
        JOB,
        [
            DirectVacancy(
                "https://jobs.lever.co/example/abc",
                "Example Analytics",
                "Data Analyst",
                "Brisbane QLD",
                "Use SQL and Power BI to analyse customer data.",
                "REQ-42",
            )
        ],
    )
    assert result.status == "DIRECT_VERIFIED"
    assert result.application_source == "LEVER"
    assert result.canonical_application_url.startswith("https://jobs.lever.co")


def test_wrong_employer_never_becomes_direct_application_source():
    result = EmployerAtsResolver().resolve(
        JOB,
        [
            DirectVacancy(
                "https://jobs.lever.co/other/abc",
                "Other Co",
                "Data Analyst",
                "Brisbane",
                "SQL Power BI",
            )
        ],
    )
    assert result.status == "NOT_FOUND"
    assert result.canonical_application_url is None


def test_title_alone_is_not_enough_to_verify():
    result = EmployerAtsResolver().resolve(
        JOB,
        [
            DirectVacancy(
                "https://careers.example.com/analyst",
                "Example Analytics",
                "Data Analyst",
                "Sydney",
                "Unrelated legal role",
            )
        ],
    )
    assert result.status != "DIRECT_VERIFIED"


def test_closed_direct_vacancy_cannot_be_used_for_execution():
    result = EmployerAtsResolver().resolve(
        JOB,
        [
            DirectVacancy(
                "https://careers.example.com/analyst",
                "Example Analytics",
                "Data Analyst",
                "Brisbane",
                "SQL Power BI",
                closed=True,
            )
        ],
    )
    assert result.status == "CLOSED"
    assert result.canonical_application_url is None


def test_known_ats_domains_are_classified_without_assuming_vacancy_identity():
    assert provider_for_url("https://careers.pageuppeople.com/889/cw/en/job/123") == "PAGEUP"
    assert provider_for_url("https://employmenthero.com/jobs/position/example") == "EMPLOYMENT_HERO"


def test_verified_resolution_keeps_direct_location_evidence_for_final_filtering():
    result = EmployerAtsResolver().resolve(
        JOB,
        [DirectVacancy("https://jobs.lever.co/example/abc", "Example Analytics", "Data Analyst", "Brisbane QLD", "SQL", "REQ-42")],
    )
    assert result.status == "DIRECT_VERIFIED"
    assert result.evidence["direct_location"] == "Brisbane QLD"


def test_country_level_index_location_can_be_verified_by_an_australian_direct_vacancy():
    generic = JOB | {"location": "Australia"}
    result = EmployerAtsResolver().resolve(
        generic,
        [DirectVacancy("https://careers.example.com/analyst", "Example Analytics", "Data Analyst", "Brisbane, Queensland, Australia", "Use SQL and Power BI to analyse customer data.")],
    )
    assert result.status == "DIRECT_VERIFIED"
    assert result.evidence["location_similarity"] == 0.5


def test_search_resolution_queries_are_bounded_and_include_exact_and_scoped_forms():
    queries = resolution_queries(JOB, ("Example",), "careers.example.com", "jobs.lever.co")
    assert queries[0] == '"Data Analyst" "Example Analytics"'
    assert any(query.startswith("site:careers.example.com") for query in queries)
    assert len(queries) <= 7


def test_search_based_finder_only_uses_verified_careers_domain_and_public_jobposting():
    class Response:
        def __init__(self, text): self.text = text
        def raise_for_status(self): pass

    class Client:
        def get(self, url):
            if url == "https://careers.example.com":
                return Response("<title>Example Analytics careers</title>")
            return Response('''<script type="application/ld+json">{
                "@type":"JobPosting","title":"Data Analyst",
                "hiringOrganization":{"name":"Example Analytics"},
                "jobLocation":{"address":{"addressLocality":"Brisbane","addressRegion":"QLD"}},
                "description":"Use SQL and Power BI to analyse customer data."}</script>''')

    class Search:
        def search(self, query):
            if "careers" in query:
                return [PublicSearchHit("https://careers.example.com", "Example Analytics Careers")]
            return [PublicSearchHit("https://careers.example.com/jobs/data-analyst", "Data Analyst")]

    finder = SearchBasedResolutionFinder(Search(), Client())
    result = EmployerAtsResolver().resolve(JOB, finder.candidates_for(JOB))
    assert finder.last_careers_domain == "careers.example.com"
    assert result.status == "DIRECT_VERIFIED"
    assert result.canonical_application_url == "https://careers.example.com/jobs/data-analyst"


def test_search_based_finder_rejects_third_party_result_after_domain_verification():
    class Response:
        text = "Example Analytics careers"
        def raise_for_status(self): pass

    class Client:
        def get(self, _url): return Response()

    class Search:
        def search(self, query):
            if "careers" in query:
                return [PublicSearchHit("https://careers.example.com", "Careers")]
            return [PublicSearchHit("https://jobs.example-index.test/data", "Data Analyst")]

    finder = SearchBasedResolutionFinder(Search(), Client())
    assert finder.candidates_for(JOB) == []


def test_official_search_api_client_normalises_results_without_exposing_configuration():
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"items": [{"link": "https://careers.example.com/job", "title": "Data Analyst", "snippet": "Brisbane"}]}

    class Client:
        def get(self, url, params):
            assert url == GoogleProgrammableSearchClient.endpoint
            assert params["q"] == "example"
            return Response()

    result = GoogleProgrammableSearchClient("key", "engine", Client()).search("example")
    assert result == [PublicSearchHit("https://careers.example.com/job", "Data Analyst", "Brisbane")]
