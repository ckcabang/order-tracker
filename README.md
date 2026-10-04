# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## Observability

The order lookup endpoint emits OpenTelemetry metrics, logs, and traces. Docker Compose sends them over OTLP to an OpenTelemetry Collector, which forwards metrics to Prometheus, logs to Loki, and traces to Tempo. Grafana reads all three.

| Service | URL | Notes |
| --- | --- | --- |
| Grafana | <http://127.0.0.1:3000> | Opens on the **Order Tracker** dashboard. No login (anonymous admin, local only). |
| Prometheus | <http://127.0.0.1:9090> | Metric: `http_server_request_duration_seconds_count` with `http_route` and `http_response_status_code` labels |

The Collector, Loki, and Tempo are only reachable inside the Compose network. Change the host ports with `GRAFANA_PORT` and `PROMETHEUS_PORT`.

The dashboard shows request counts and rates by status code, 5xx errors by exception type, the error rate, and recent warning and error logs. In the logs panel, expand a line and follow the `TraceID` link to open the trace in Tempo.

All configuration lives in `observability/`:

- `otel-collector.yaml`: OTLP receiver and the per-signal pipelines
- `prometheus.yaml`, `loki.yaml`, `tempo.yaml`: backend settings
- `grafana/provisioning/`: data sources and the dashboard provider
- `grafana/dashboards/order-tracker.json`: the dashboard

When `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, for example when you run the app outside Compose, the app prints the signals to stdout instead.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
