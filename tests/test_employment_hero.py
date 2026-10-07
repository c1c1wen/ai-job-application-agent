import httpx

from job_agent.connectors.employment_hero import EmploymentHeroConnector
from job_agent.connectors.providers import EmploymentHeroProvider


def test_public_employment_hero_board_normalizes_full_job_data(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"items": [{
                "id": "job-1", "title": "Data Analyst", "organisation_name": "Example Co",
                "friendly_id": "example-data-analyst-ab12",
                "vendor_location_name": "Brisbane, Queensland", "workplace_type": "hybrid",
                "description": "<p>Use SQL and Power BI</p>",
            }], "totalPages": 1}}

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    jobs = EmploymentHeroConnector().fetch("example", "Example Co")

    assert len(jobs) == 1
    assert jobs[0].source == "EMPLOYMENT_HERO"
    assert jobs[0].external_id == "job-1"
    assert jobs[0].url == "https://employmenthero.com/jobs/position/example-data-analyst-ab12/"
    assert "Work mode: hybrid" in jobs[0].description
    assert "SQL and Power BI" in jobs[0].description


def test_employment_hero_provider_continues_when_one_public_board_fails(monkeypatch):
    def fetch(_self, board, company):
        if board == "broken":
            raise httpx.ReadTimeout("timeout")
        return [company]

    monkeypatch.setattr(EmploymentHeroConnector, "fetch", fetch)
    assert EmploymentHeroProvider({"broken": "Broken", "working": "Working"}).discover_jobs() == ["Working"]


def test_public_search_normalizes_role_first_results(monkeypatch):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, url, **_kwargs):
            if "admin-ajax" in url:
                return Response({"nonce": "public-nonce"})
            return Response({"data": {"items": [{
                "id": "search-1", "title": "Junior Data Analyst", "friendly_id": "junior-data-analyst-x1",
                "organisation_name": "Example Co", "vendor_location_name": "Brisbane, Queensland",
                "description": "<p>SQL reporting</p>",
            }], "total_pages": 1}})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: Client())
    jobs = EmploymentHeroConnector().search(["Junior Data Analyst"])

    assert [(job.title, job.company, job.source_key) for job in jobs] == [
        ("Junior Data Analyst", "Example Co", "employmenthero-au-search")
    ]
