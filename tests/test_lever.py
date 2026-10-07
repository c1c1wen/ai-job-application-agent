import httpx

from job_agent.connectors.lever import LeverConnector
from job_agent.connectors.providers import LeverProvider


def test_lever_normalizes_public_postings(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return [{"id": "a1", "text": "Data Analyst", "hostedUrl": "https://jobs/a1",
                     "descriptionPlain": "Use Python", "categories": {"location": "Brisbane"}},
                    {"id": "a3", "text": "Analyst", "hostedUrl": "https://jobs/a3", "country": "FR"},
                    {"id": "a2", "text": "Missing URL"}]

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    jobs = LeverConnector().fetch("example", "Example Co")
    assert len(jobs) == 2
    assert jobs[0].source == "LEVER"
    assert jobs[0].company == "Example Co"
    assert jobs[0].external_id == "a1"
    assert jobs[1].location == "France"


def test_lever_rejects_malformed_payload(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"postings": []}

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    assert LeverConnector().fetch("example") == []


def test_lever_provider_uses_registered_company_names(monkeypatch):
    monkeypatch.setattr(LeverConnector, "fetch", lambda self, site, company: [site, company])
    assert LeverProvider({"one": "One Co"}).discover_jobs() == ["one", "One Co"]


def test_lever_provider_continues_when_one_public_board_fails(monkeypatch):
    def fetch(_self, site, company):
        if site == "broken":
            raise httpx.ReadTimeout("timeout")
        return [company]

    monkeypatch.setattr(LeverConnector, "fetch", fetch)
    assert LeverProvider({"broken": "Broken", "working": "Working"}).discover_jobs() == ["Working"]


def test_public_registry_keeps_australian_boards_and_disables_global_noise():
    from job_agent.connectors.registry import DEFAULT_LEVER_SITES, EMPLOYER_SOURCE_REGISTRY

    assert DEFAULT_LEVER_SITES["megaport"] == "Megaport"
    assert "binance" not in DEFAULT_LEVER_SITES
    assert any(entry.company == "Binance" and not entry.enabled for entry in EMPLOYER_SOURCE_REGISTRY)


def test_lever_keeps_all_public_description_sections(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return [{"id": "a1", "text": "Analyst", "hostedUrl": "https://jobs/a1", "descriptionPlain": "Overview", "lists": [{"text": "What We Require", "content": "<li>Strong SQL</li>"}]}]

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: Response())
    assert "What We Require" in LeverConnector().fetch("example")[0].description
    assert "Strong SQL" in LeverConnector().fetch("example")[0].description
