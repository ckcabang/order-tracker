"""Receive Grafana alerts, save what is needed to understand them, and start an agent."""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI

from incident_response import agent, context


INCIDENTS_DIR = Path(
    os.getenv("INCIDENTS_DIR", Path(__file__).resolve().parent.parent / "incidents")
)
AGENT_ENABLED = os.getenv("INCIDENT_AGENT_ENABLED", "true").lower() == "true"

logger = logging.getLogger("uvicorn.error")
app = FastAPI(title="Incident Response")


def incident_id(alert):
    """One incident per firing episode: Grafana re-sends a firing alert on every
    notification cycle, with the same fingerprint and start time."""
    started = context.parse_time(alert["startsAt"]).strftime("%Y%m%dT%H%M%SZ")
    fingerprint = alert.get("fingerprint") or hashlib.sha256(
        json.dumps(alert.get("labels", {}), sort_keys=True).encode()
    ).hexdigest()[:16]
    return f"{started}-{fingerprint}"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def investigate(incident_dir, alert):
    """Collect telemetry for the alert, then hand the incident to the agent."""
    collected = context.collect(alert)
    write_json(incident_dir / "context.json", collected)
    (incident_dir / "summary.md").write_text(
        context.render_summary(alert, collected), encoding="utf-8"
    )
    logger.info("Incident %s: saved telemetry", incident_dir.name)
    if AGENT_ENABLED:
        status = agent.run_agent(incident_dir)
        logger.info("Incident %s: agent %s", incident_dir.name, status)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts", status_code=202)
def receive_alerts(payload: dict, background_tasks: BackgroundTasks):
    received_at = datetime.now(timezone.utc).isoformat()
    created, duplicates, resolved = [], [], []

    for alert in payload.get("alerts", []):
        incident = incident_id(alert)
        incident_dir = INCIDENTS_DIR / incident

        if alert.get("status") == "resolved":
            if incident_dir.exists():
                write_json(incident_dir / "resolved.json", {"received_at": received_at, **alert})
                resolved.append(incident)
            continue

        if incident_dir.exists():
            duplicates.append(incident)
            continue

        incident_dir.mkdir(parents=True)
        write_json(incident_dir / "alert.json", {
            "received_at": received_at,
            "endpoint": context.endpoint_of(alert),
            "alert": alert,
            "notification": {key: value for key, value in payload.items() if key != "alerts"},
        })
        logger.info("Incident %s: alert received for %s", incident, context.endpoint_of(alert))
        background_tasks.add_task(investigate, incident_dir, alert)
        created.append(incident)

    return {"created": created, "duplicates": duplicates, "resolved": resolved}
