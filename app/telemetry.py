"""OpenTelemetry traces, metrics and logs for the order tracker.

When OTEL_EXPORTER_OTLP_ENDPOINT is set (as in Docker Compose), signals are sent
over OTLP/HTTP to the OpenTelemetry Collector. Otherwise they are written to
stdout as one JSON document per line.
"""

import logging
import os
import time
from contextlib import contextmanager

from fastapi import HTTPException, Request
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogRecordExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.trace import SpanKind, StatusCode


SERVICE_NAME = "order-tracker"

logger = logging.getLogger("order_tracker")
tracer = trace.get_tracer("order_tracker")
meter = metrics.get_meter("order_tracker")

request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="Duration of HTTP server requests.",
    explicit_bucket_boundaries_advisory=[
        0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10,
    ],
)

_configured = False


def _one_line(item):
    return item.to_json(indent=None) + os.linesep


def _default_exporters():
    # The OTLP exporters read their endpoint from OTEL_EXPORTER_OTLP_ENDPOINT.
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
    return (
        ConsoleSpanExporter(formatter=_one_line),
        ConsoleMetricExporter(formatter=_one_line),
        ConsoleLogRecordExporter(formatter=_one_line),
    )


def setup_telemetry(span_exporter=None, metric_reader=None, log_exporter=None):
    """Install the global OpenTelemetry providers.

    Exporters default to OTLP when an endpoint is configured, else the console.
    """
    global _configured
    if _configured:
        return
    _configured = True

    resource = Resource.create({"service.name": SERVICE_NAME})
    default_span, default_metric, default_log = _default_exporters()

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter or default_span))
    trace.set_tracer_provider(tracer_provider)

    # The export interval comes from OTEL_METRIC_EXPORT_INTERVAL (default 60s).
    metric_reader = metric_reader or PeriodicExportingMetricReader(default_metric)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter or default_log))
    set_logger_provider(logger_provider)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    logger.setLevel(logging.INFO)
    logger.propagate = False


@contextmanager
def observe_order_lookup(request: Request, order_id: str):
    """Record a server span, a request duration metric and a log for an order lookup."""
    method = request.method
    route = request.scope["route"].path
    attributes = {"http.request.method": method, "http.route": route}
    log_extra = {"order.id": order_id, "http.route": route}
    status = 200
    start = time.perf_counter()

    with tracer.start_as_current_span(
        f"{method} {route}",
        kind=SpanKind.SERVER,
        attributes={**attributes, "order.id": order_id},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except HTTPException as exc:
            status = exc.status_code
            logger.warning(
                "Order lookup returned %s: %s", status, exc.detail,
                extra={**log_extra, "http.response.status_code": status},
            )
            raise
        except Exception as exc:
            status = 500
            attributes["error.type"] = type(exc).__qualname__
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            logger.exception(
                "Order lookup failed",
                extra={**log_extra, "http.response.status_code": status},
            )
            raise
        else:
            logger.info(
                "Order lookup succeeded",
                extra={**log_extra, "http.response.status_code": status},
            )
        finally:
            attributes["http.response.status_code"] = status
            span.set_attribute("http.response.status_code", status)
            request_duration.record(time.perf_counter() - start, attributes)
