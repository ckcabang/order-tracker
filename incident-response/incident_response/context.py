"""Collect the metrics, logs and traces behind an alert."""

import os
from datetime import datetime, timedelta, timezone

import httpx


PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://localhost:3200")
SERVICE = "order-tracker"
METRIC = "http_server_request_duration_seconds_count"

# How far before the alert started to look, and how much to fetch.
LOOKBACK = timedelta(minutes=10)
LOG_LIMIT = 100
MAX_TRACES = 5


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def endpoint_of(alert):
    labels = alert.get("labels", {})
    return {"method": labels.get("http_request_method"), "route": labels.get("http_route")}


def collect(alert, client=None, now=None):
    """Return everything an engineer needs to understand the alert.

    Each source is fetched independently, so one unreachable backend only adds
    an entry to `errors` instead of losing the rest.
    """
    now = now or datetime.now(timezone.utc)
    start = parse_time(alert["startsAt"]) - LOOKBACK
    endpoint = endpoint_of(alert)
    context = {
        "endpoint": endpoint,
        "window": {"start": start.isoformat(), "end": now.isoformat()},
        "metrics": {},
        "logs": [],
        "traces": {},
        "errors": {},
    }
    client = client or httpx.Client(timeout=10)

    try:
        context["metrics"] = fetch_metrics(client, endpoint, start, now)
    except Exception as exc:
        context["errors"]["metrics"] = repr(exc)
    try:
        context["logs"] = fetch_logs(client, endpoint, start, now)
    except Exception as exc:
        context["errors"]["logs"] = repr(exc)

    trace_ids = [log["trace_id"] for log in context["logs"] if log.get("trace_id")]
    try:
        trace_ids += search_error_traces(client, endpoint, start, now)
    except Exception as exc:
        context["errors"]["trace_search"] = repr(exc)
    for trace_id in list(dict.fromkeys(trace_ids))[:MAX_TRACES]:
        try:
            context["traces"][trace_id] = fetch_trace(client, trace_id)
        except Exception as exc:
            context["errors"][f"trace {trace_id}"] = repr(exc)
    return context


def _selector(endpoint, extra=""):
    matchers = [f'job="{SERVICE}"']
    if endpoint["route"]:
        matchers.append(f'http_route="{endpoint["route"]}"')
    if endpoint["method"]:
        matchers.append(f'http_request_method="{endpoint["method"]}"')
    if extra:
        matchers.append(extra)
    return f"{METRIC}{{{', '.join(matchers)}}}"


def _prom_query(client, expr, at):
    response = client.get(
        f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr, "time": at.timestamp()}
    )
    response.raise_for_status()
    return [
        {"labels": row["metric"], "value": round(float(row["value"][1]), 2)}
        for row in response.json()["data"]["result"]
    ]


def fetch_metrics(client, endpoint, start, end):
    window = f"{max(int((end - start).total_seconds()), 60)}s"
    all_requests = _selector(endpoint)
    server_errors = _selector(endpoint, 'http_response_status_code=~"5.."')
    return {
        "requests_by_status": _prom_query(
            client,
            f"sum by (http_response_status_code) (increase({all_requests}[{window}]))",
            end,
        ),
        "server_errors_by_type": _prom_query(
            client,
            "sum by (http_response_status_code, error_type) "
            f"(increase({server_errors}[{window}]))",
            end,
        ),
    }


def fetch_logs(client, endpoint, start, end):
    query = f'{{service_name="{SERVICE}"}}'
    if endpoint["route"]:
        query += f' | http_route="{endpoint["route"]}"'
    query += ' | severity_text=~"WARN|ERROR"'
    response = client.get(
        f"{LOKI_URL}/loki/api/v1/query_range",
        params={
            "query": query,
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": LOG_LIMIT,
            "direction": "backward",
        },
    )
    response.raise_for_status()
    logs = []
    for stream in response.json()["data"]["result"]:
        for timestamp, line, *metadata in stream["values"]:
            labels = {**stream["stream"], **(metadata[0] if metadata else {})}
            logs.append({
                "time": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).isoformat(),
                "severity": labels.get("severity_text"),
                "message": line,
                "trace_id": labels.get("trace_id"),
                "attributes": {
                    key: value for key, value in labels.items()
                    if key not in {"trace_id", "severity_text"}
                },
            })
    return sorted(logs, key=lambda log: log["time"], reverse=True)


def search_error_traces(client, endpoint, start, end):
    query = "{ status = error"
    if endpoint["route"]:
        query += f' && span.http.route = "{endpoint["route"]}"'
    query += " }"
    response = client.get(
        f"{TEMPO_URL}/api/search",
        params={
            "q": query,
            "start": int(start.timestamp()),
            "end": int(end.timestamp()) + 1,
            "limit": MAX_TRACES,
        },
    )
    response.raise_for_status()
    return [trace["traceID"] for trace in response.json().get("traces", [])]


def fetch_trace(client, trace_id):
    response = client.get(f"{TEMPO_URL}/api/traces/{trace_id}")
    response.raise_for_status()
    return response.json()


def _attribute_value(value):
    return next(iter(value.values()), None) if value else None


def flatten_spans(trace):
    """Turn an OTLP trace into a flat list of spans with plain attributes."""
    spans = []
    for batch in trace.get("batches", trace.get("resourceSpans", [])):
        for scope in batch.get("scopeSpans", []):
            for span in scope.get("spans", []):
                spans.append({
                    "name": span["name"],
                    "span_id": span.get("spanId"),
                    "parent_span_id": span.get("parentSpanId"),
                    "status": span.get("status", {}),
                    "duration_ms": round(
                        (int(span["endTimeUnixNano"]) - int(span["startTimeUnixNano"])) / 1e6, 2
                    ),
                    "attributes": {
                        a["key"]: _attribute_value(a["value"]) for a in span.get("attributes", [])
                    },
                    "events": [
                        {
                            "name": event["name"],
                            "attributes": {
                                a["key"]: _attribute_value(a["value"])
                                for a in event.get("attributes", [])
                            },
                        }
                        for event in span.get("events", [])
                    ],
                })
    return spans


def render_summary(alert, context):
    """A Markdown digest of the alert, readable by a person or an agent."""
    endpoint = context["endpoint"]
    annotations = alert.get("annotations", {})
    lines = [
        f"# Incident: {alert.get('labels', {}).get('alertname', 'alert')}",
        "",
        f"- **Endpoint:** `{endpoint['method'] or '?'} {endpoint['route'] or '?'}`",
        f"- **Alert started:** {alert['startsAt']}",
        f"- **Telemetry window:** {context['window']['start']} to {context['window']['end']}",
        f"- **Summary:** {annotations.get('summary', '')}",
        f"- **Description:** {annotations.get('description', '')}",
        f"- **Dashboard:** {alert.get('panelURL') or alert.get('dashboardURL') or ''}",
        f"- **Alert rule:** {alert.get('generatorURL', '')}",
        "",
        "## Metrics (whole window)",
        "",
        "| Status | Requests |",
        "| --- | --- |",
    ]
    for row in context["metrics"].get("requests_by_status", []):
        lines.append(f"| {row['labels'].get('http_response_status_code')} | {row['value']} |")
    lines += ["", "| Status | Error type | Requests |", "| --- | --- | --- |"]
    for row in context["metrics"].get("server_errors_by_type", []):
        labels = row["labels"]
        lines.append(
            f"| {labels.get('http_response_status_code')} | {labels.get('error_type', '')} "
            f"| {row['value']} |"
        )

    errors = [log for log in context["logs"] if log["severity"] == "ERROR"]
    lines += ["", f"## Error logs ({len(errors)} of {len(context['logs'])} warning/error logs)", ""]
    seen = set()
    for log in errors:
        attributes = log["attributes"]
        lines.append(
            f"- {log['time']} `{log['message']}` order_id=`{attributes.get('order_id')}` "
            f"trace_id=`{log['trace_id']}`"
        )
        signature = (attributes.get("exception_type"), attributes.get("exception_message"))
        if attributes.get("exception_stacktrace") and signature not in seen:
            seen.add(signature)
            lines += ["", "```", attributes["exception_stacktrace"].rstrip(), "```", ""]

    lines += ["", "## Traces", ""]
    for trace_id, trace in context["traces"].items():
        lines.append(f"### `{trace_id}`")
        for span in flatten_spans(trace):
            status = span["status"].get("code", "UNSET").replace("STATUS_CODE_", "")
            message = span["status"].get("message", "")
            lines.append(
                f"- `{span['name']}` {span['duration_ms']} ms, status {status}"
                + (f": {message}" if message else "")
            )
            route_attributes = {
                key: span["attributes"][key]
                for key in ("order.id", "http.response.status_code")
                if key in span["attributes"]
            }
            if route_attributes:
                lines.append(f"  - {route_attributes}")
        lines.append("")

    if context["errors"]:
        lines += ["## Collection errors", ""]
        lines += [f"- {source}: {error}" for source, error in context["errors"].items()]
    return "\n".join(lines) + "\n"
