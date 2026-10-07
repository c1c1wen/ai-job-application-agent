import httpx

from job_agent.connectors.adzuna import AdzunaConnector


def test_adzuna_normalizes_public_role_search_results(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self): pass

        def json(self):
            return {
                "results": [
                    {
                        "id": "123",
                        "title": "Data Analyst",
                        "redirect_url": "https://example.test/job/123?source=api",
                        "description": "Use Python &amp; SQL",
                        "company": {"display_name": "Example AU"},
                        "location": {"display_name": "Brisbane QLD"},
                    },
                    {"id": "missing-url", "title": "Incomplete"},
                ]
            }

    def fake_get(*args, **kwargs):
        calls.append(kwargs["params"])
        return Response()

    monkeypatch.setattr(httpx, "get", fake_get)

    jobs = AdzunaConnector("id", "key").search(["Data Analyst"], ["Brisbane"])

    assert len(jobs) == 1
    assert jobs[0].source == "ADZUNA"
    assert jobs[0].source_key == "adzuna-au"
    assert jobs[0].external_id == "123"
    assert jobs[0].url == "https://example.test/job/123?source=api"
    assert jobs[0].location == "Brisbane QLD"
    assert jobs[0].description == "Use Python & SQL"
    assert "where" not in calls[0]
