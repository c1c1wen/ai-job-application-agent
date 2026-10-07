from job_agent.domain.models import JobInput
from job_agent.services.job_enrichment import FullJobDescriptionEnricher


class RedirectResponse:
    def __init__(self):
        self.is_redirect = True
        self.headers = {"location": "https://jobs.lever.co/example/posting-1"}


class FinalResponse:
    def __init__(self):
        self.is_redirect = False
        self.headers = {}


class AdzunaRedirectResponse:
    def __init__(self):
        self.is_redirect = True
        self.headers = {"location": "https://www.adzuna.com.au/details/123"}


class DetailResponse:
    def __init__(self):
        self.text = '<script type="application/ld+json">{"@type":"JobPosting","description":"<p>Python, SQL, and Tableau are required for this data role.</p>"}</script>'

    def raise_for_status(self): pass


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)

    def get(self, *_args, **_kwargs):
        return next(self.responses)


def test_adzuna_enrichment_uses_richer_public_lever_posting():
    job = JobInput("Data Analyst", "Example", "https://adzuna.test/123", "Python", "ADZUNA")
    full = JobInput("Data Analyst", "Example", "https://jobs.lever.co/example/posting-1", "What We Require: Python, SQL, Tableau", "LEVER")
    result = FullJobDescriptionEnricher(Client([RedirectResponse(), FinalResponse()]), lever_fetcher=lambda _site: [full]).enrich(job)
    assert result.jd_status == "FULL"
    assert result.evidence_quality == "HIGH"
    assert result.enrichment_source == "LEVER"
    assert result.description == full.description


def test_adzuna_enrichment_retains_partial_summary_for_unknown_destination():
    job = JobInput("Data Analyst", "Example", "https://adzuna.test/123", "Python", "ADZUNA")
    result = FullJobDescriptionEnricher(Client([FinalResponse()])).enrich(job)
    assert result.jd_status == "PARTIAL"
    assert result.description == "Python"
    assert result.retrieval_status == "UNSUPPORTED_ORIGINAL_SOURCE"


def test_adzuna_public_detail_is_used_when_it_contains_richer_jobposting_metadata():
    job = JobInput("Data Analyst", "Example", "https://adzuna.test/123", "Python", "ADZUNA")
    result = FullJobDescriptionEnricher(
        Client([
            AdzunaRedirectResponse(),
            FinalResponse(),
            DetailResponse(),
        ])
    ).enrich(job)
    assert result.jd_status == "FULL"
    assert result.evidence_quality == "MEDIUM"
    assert result.enrichment_source == "ADZUNA_PUBLIC_DETAIL"
    assert "Tableau" in result.description
