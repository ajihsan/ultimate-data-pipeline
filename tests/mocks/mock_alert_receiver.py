"""Mock alert receiver for the S6 notify pipeline (compose.mock.yaml).

Stands in for Telegram/Discord during verification: records every POSTed
alert payload in memory so tests can join receiver receipts against
ClickHouse `udp.alerts` rows (event_id) and measure delivery latency.

    POST /alerts   {"event_id", "decision_id", "source", "channel",
                    "suppressed", "decided_at", "created_at"}
                     → {"stored": n}   (payload + received_at kept in memory)
    GET  /alerts   → {"alerts": [...]}  (dump everything recorded)
    DELETE /alerts → {"cleared": n}     (reset between verification stages)
    GET  /healthz  → {"status": "ok"}

Standalone: ``python -m tests.mocks.mock_alert_receiver`` (uvicorn on :8082) —
needs fastapi + uvicorn on the host, which the mock container image bakes in
(docker/alert-receiver.Dockerfile; compose.mock.yaml runs it as
`alert-receiver`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI

app = FastAPI(title="udp mock alert receiver", version="0.1.0")

_alerts: list[dict[str, Any]] = []
"""Recorded alert payloads (in-memory; one compose service per run)."""


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe (compose healthcheck)."""
    return {"status": "ok"}


@app.post("/alerts")
def record_alert(payload: dict[str, Any]) -> dict[str, int]:
    """Record one alert payload with its receipt time."""
    _alerts.append({**payload, "received_at": datetime.now(UTC).isoformat()})
    return {"stored": len(_alerts)}


@app.get("/alerts")
def list_alerts() -> dict[str, list[dict[str, Any]]]:
    """Dump every recorded alert payload."""
    return {"alerts": _alerts}


@app.delete("/alerts")
def clear_alerts() -> dict[str, int]:
    """Reset the record (between verification stages)."""
    cleared = len(_alerts)
    _alerts.clear()
    return {"cleared": cleared}


def main() -> None:
    """Run the mock standalone (``python -m tests.mocks.mock_alert_receiver``)."""
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="info")


if __name__ == "__main__":
    main()
