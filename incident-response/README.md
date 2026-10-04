# Incident Response

A small service that plays the on-call engineer for the order tracker. It receives Grafana alerts at `POST /alerts`, saves what is needed to understand each one, and starts the coding assistant (Claude Code) in headless mode to investigate.

## Run it

It runs on the host, not in Docker, so it can use the Claude Code CLI you are already logged in to and work on this repository.

```bash
cd incident-response
uv run uvicorn incident_response.main:app --host 0.0.0.0 --port 8001
```

Run tests with `uv run --frozen pytest -q`.

## What happens on an alert

1. Grafana posts a notification to `/alerts`. Each firing alert becomes one incident in `incidents/<start time>-<fingerprint>/`. Grafana re-sends firing alerts, so repeats of the same episode are ignored. A resolved notification is saved as `resolved.json`.
2. In the background, the service collects telemetry from 10 minutes before the alert started until now, for the endpoint in the alert's `http_request_method` and `http_route` labels:
   - **Metrics** from Prometheus: requests by status code, 5xx by error type
   - **Logs** from Loki: warning and error logs, with stack traces
   - **Traces** from Tempo: traces referenced by the error logs, plus error traces on the route
3. It writes `alert.json`, `context.json` (raw data), and `summary.md` (a readable digest).
4. It starts `claude --print` in the repository root with a prompt that points at the incident. The agent finds the root cause, fixes it with a regression test if the fix is small and safe, or otherwise escalates. It writes `report.md` with an Escalation section. Its transcript goes to `agent.jsonl` and its status to `agent_status.json`.

Only one agent runs at a time, so two incidents never edit the code at once.

## Agent permissions

Headless mode cannot ask for approval, so the agent only gets the tools in `ALLOWED_TOOLS` (`incident_response/agent.py`): read and edit files, run the tests, read git status and diffs, query the local telemetry APIs, and read `docker compose` logs. It cannot commit, push, rebuild, or restart anything. A developer reviews the change and deploys it.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROMETHEUS_URL` | `http://localhost:9090` | Metrics |
| `LOKI_URL` | `http://localhost:3100` | Logs |
| `TEMPO_URL` | `http://localhost:3200` | Traces |
| `INCIDENTS_DIR` | `incident-response/incidents` | Where incidents are saved |
| `INCIDENT_AGENT_ENABLED` | `true` | Set to `false` to only collect telemetry |
| `INCIDENT_AGENT_COMMAND` | `claude` | Agent executable |
| `INCIDENT_AGENT_WORKDIR` | repository root | Where the agent runs |
| `INCIDENT_AGENT_TIMEOUT` | `1800` | Seconds before the agent is stopped |
