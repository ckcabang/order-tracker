import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from incident_response import agent, context, main

ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "Order Tracker 5xx responses",
        "http_request_method": "GET",
        "http_route": "/api/orders/{order_id}",
    },
    "annotations": {"summary": "GET /api/orders/{order_id} returned 2 5xx responses"},
    "startsAt": "2026-10-04T18:30:00Z",
    "fingerprint": "abc123",
    "panelURL": "http://localhost:3000/d/order-tracker/order-tracker?viewPanel=6",
}
NOW = datetime(2026, 10, 4, 18, 31, tzinfo=timezone.utc)
STACKTRACE = "Traceback ...\nValueError: day is out of range for month\n"


def fake_backends(request):
    path = request.url.path
    if path == "/api/v1/query":
        query = request.url.params["query"]
        assert 'http_route="/api/orders/{order_id}"' in query
        if "error_type" in query:
            rows = [{"metric": {"http_response_status_code": "500", "error_type": "ValueError"},
                     "value": [0, "2"]}]
        else:
            rows = [{"metric": {"http_response_status_code": "200"}, "value": [0, "10"]},
                    {"metric": {"http_response_status_code": "500"}, "value": [0, "2"]}]
        return httpx.Response(200, json={"data": {"result": rows}})
    if path == "/loki/api/v1/query_range":
        assert '| http_route="/api/orders/{order_id}"' in request.url.params["query"]
        stream = {"severity_text": "ERROR", "trace_id": "t1", "order_id": "express-1002",
                  "exception_type": "ValueError", "exception_message": "day is out of range",
                  "exception_stacktrace": STACKTRACE}
        return httpx.Response(200, json={"data": {"result": [
            {"stream": stream, "values": [["1791139800000000000", "Order lookup failed"]]}
        ]}})
    if path == "/api/search":
        return httpx.Response(200, json={"traces": [{"traceID": "t1"}, {"traceID": "t2"}]})
    if path.startswith("/api/traces/"):
        return httpx.Response(200, json={"batches": [{"scopeSpans": [{"spans": [{
            "name": "GET /api/orders/{order_id}", "spanId": "s1",
            "startTimeUnixNano": "1000000", "endTimeUnixNano": "3000000",
            "status": {"code": "STATUS_CODE_ERROR", "message": "day is out of range"},
            "attributes": [{"key": "order.id", "value": {"stringValue": "express-1002"}}],
        }]}]}]})
    return httpx.Response(404)


def test_collect_gathers_metrics_logs_and_traces():
    client = httpx.Client(transport=httpx.MockTransport(fake_backends))
    collected = context.collect(ALERT, client=client, now=NOW)

    assert collected["endpoint"] == {"method": "GET", "route": "/api/orders/{order_id}"}
    assert collected["window"]["start"] == "2026-10-04T18:20:00+00:00"
    assert collected["metrics"]["server_errors_by_type"][0]["value"] == 2
    assert collected["logs"][0]["trace_id"] == "t1"
    assert list(collected["traces"]) == ["t1", "t2"]
    assert collected["errors"] == {}

    summary = context.render_summary(ALERT, collected)
    assert "`GET /api/orders/{order_id}`" in summary
    assert "ValueError: day is out of range for month" in summary
    assert "order_id=`express-1002`" in summary
    assert "status ERROR: day is out of range" in summary
    assert "viewPanel=6" in summary


def test_collect_survives_unreachable_backend():
    def broken(request):
        raise httpx.ConnectError("refused")

    collected = context.collect(ALERT, client=httpx.Client(transport=httpx.MockTransport(broken)),
                                now=NOW)
    assert set(collected["errors"]) == {"metrics", "logs", "trace_search"}
    assert "Collection errors" in context.render_summary(ALERT, collected)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "INCIDENTS_DIR", tmp_path)
    monkeypatch.setattr(context, "collect", lambda alert: {
        "endpoint": context.endpoint_of(alert), "window": {"start": "s", "end": "e"},
        "metrics": {}, "logs": [], "traces": {}, "errors": {},
    })
    started = []
    monkeypatch.setattr(agent, "run_agent", lambda incident_dir: started.append(incident_dir))
    with TestClient(main.app) as test_client:
        test_client.started = started
        yield test_client


def test_alert_creates_incident_and_starts_agent(client, tmp_path):
    response = client.post("/alerts", json={"status": "firing", "alerts": [ALERT]})
    assert response.status_code == 202
    incident = "20261004T183000Z-abc123"
    assert response.json()["created"] == [incident]

    saved = json.loads((tmp_path / incident / "alert.json").read_text())
    assert saved["endpoint"] == {"method": "GET", "route": "/api/orders/{order_id}"}
    assert (tmp_path / incident / "summary.md").exists()
    assert (tmp_path / incident / "context.json").exists()
    assert client.started == [tmp_path / incident]


def test_repeated_notification_does_not_start_second_agent(client):
    client.post("/alerts", json={"alerts": [ALERT]})
    response = client.post("/alerts", json={"alerts": [ALERT]})
    assert response.json()["duplicates"] == ["20261004T183000Z-abc123"]
    assert len(client.started) == 1


def test_resolved_alert_is_recorded(client, tmp_path):
    client.post("/alerts", json={"alerts": [ALERT]})
    response = client.post("/alerts", json={"alerts": [{**ALERT, "status": "resolved"}]})
    assert response.json()["resolved"] == ["20261004T183000Z-abc123"]
    assert (tmp_path / "20261004T183000Z-abc123" / "resolved.json").exists()
    assert len(client.started) == 1


def test_agent_runs_headless_in_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "WORKDIR", tmp_path)
    incident_dir = tmp_path / "incident-response" / "incidents" / "i1"
    incident_dir.mkdir(parents=True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        (incident_dir / "report.md").write_text("done")
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    status = agent.run_agent(incident_dir)

    cmd, kwargs = calls[0]
    assert "--print" in cmd and "--allowedTools" in cmd
    assert not any("git commit" in arg or "git push" in arg for arg in cmd)
    assert kwargs["cwd"] == tmp_path
    assert "`incident-response/incidents/i1/`" in kwargs["input"]
    assert "incident-response/incidents/i1/report.md" in kwargs["input"]
    assert status["state"] == "finished" and status["report_written"]


def test_alert_without_start_time_or_fingerprint(client, tmp_path):
    alert = {"status": "firing", "labels": {"alertname": "ResponderTest", "test": "true"},
             "annotations": {"summary": "Test notification; no incident to fix"}}
    response = client.post("/alerts", json={"alerts": [alert]})
    assert response.status_code == 202
    [incident] = response.json()["created"]
    saved = json.loads((tmp_path / incident / "alert.json").read_text())
    assert saved["alert"]["startsAt"] == saved["received_at"]
    assert saved["endpoint"] == {"method": None, "route": None}
    assert len(client.started) == 1
