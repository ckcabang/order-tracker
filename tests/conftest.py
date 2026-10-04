import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry

# Install in-memory exporters before app.main configures the console ones.
span_exporter = InMemorySpanExporter()
metric_reader = InMemoryMetricReader()
log_exporter = InMemoryLogRecordExporter()
telemetry.setup_telemetry(span_exporter, metric_reader, log_exporter)

from app import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


class Telemetry:
    def spans(self):
        trace.get_tracer_provider().force_flush()
        return span_exporter.get_finished_spans()

    def logs(self):
        get_logger_provider().force_flush()
        return log_exporter.get_finished_logs()

    def request_points(self):
        data = metric_reader.get_metrics_data()
        return [
            point
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
            if metric.name == "http.server.request.duration"
            for point in metric.data.data_points
        ]


@pytest.fixture
def telemetry_data():
    trace.get_tracer_provider().force_flush()
    get_logger_provider().force_flush()
    span_exporter.clear()
    log_exporter.clear()
    return Telemetry()
