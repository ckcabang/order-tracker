import pytest

from app import main

ROUTE = "/api/orders/{order_id}"


def lookup_point(telemetry_data, status):
    return next(
        point
        for point in telemetry_data.request_points()
        if point.attributes["http.response.status_code"] == status
    )


def server_span(telemetry_data):
    return next(span for span in telemetry_data.spans() if span.name == f"GET {ROUTE}")


def test_successful_lookup(client, telemetry_data):
    assert client.get("/api/orders/standard-1001").status_code == 200

    point = lookup_point(telemetry_data, 200)
    assert point.attributes["http.route"] == ROUTE
    assert point.attributes["http.request.method"] == "GET"
    assert point.count >= 1

    span = server_span(telemetry_data)
    assert span.attributes["order.id"] == "standard-1001"
    assert span.attributes["http.response.status_code"] == 200
    db_span = next(s for s in telemetry_data.spans() if s.name == "SELECT orders")
    assert db_span.parent.span_id == span.context.span_id

    log = telemetry_data.logs()[-1].log_record
    assert log.body == "Order lookup succeeded"
    assert log.trace_id == span.context.trace_id


def test_missing_lookup(client, telemetry_data):
    assert client.get("/api/orders/missing").status_code == 404

    assert lookup_point(telemetry_data, 404).attributes["http.route"] == ROUTE
    log = telemetry_data.logs()[-1].log_record
    assert log.severity_text == "WARN"
    assert log.attributes["order.id"] == "missing"


def test_failed_lookup(client, telemetry_data, monkeypatch):
    def broken(_row):
        raise ValueError("day is out of range for month")

    monkeypatch.setattr(main, "order_detail", broken)
    with pytest.raises(ValueError):
        client.get("/api/orders/standard-1001")

    point = lookup_point(telemetry_data, 500)
    assert point.attributes["http.route"] == ROUTE
    assert point.attributes["error.type"] == "ValueError"

    span = server_span(telemetry_data)
    assert not span.status.is_ok
    assert span.events[0].name == "exception"

    log = telemetry_data.logs()[-1].log_record
    assert log.severity_text == "ERROR"
    assert "ValueError" in log.attributes["exception.type"]
