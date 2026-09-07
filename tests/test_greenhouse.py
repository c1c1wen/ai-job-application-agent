import httpx

from job_agent.connectors.greenhouse import GreenhouseConnector


def test_greenhouse_skips_partial_and_normalizes(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"jobs":[{"id":1,"title":"Analyst","absolute_url":"https://x/a?src=1","content":"<p>Python</p>","location":{"name":"Brisbane"}},{"id":2,"title":"Bad"}]}
    monkeypatch.setattr(httpx,"get",lambda *a,**k:Response())
    jobs=GreenhouseConnector().fetch("co")
    assert len(jobs)==1 and jobs[0].external_id=="1" and jobs[0].description=="Python"


def test_greenhouse_malformed_payload_is_empty(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return []
    monkeypatch.setattr(httpx,"get",lambda *a,**k:Response())
    assert GreenhouseConnector().fetch("co")==[]
