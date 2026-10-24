"""Mock Jev /v1/systemone decision server.

The mock is used for CI and mock-mode runs. The real Laya server is
``src/udp/model_server.py``. The decide pipeline runs end-to-end against this
canned, wire-compatible stand-in:

    POST /v1/systemone  {"model","state","questions":{<name>:{"type":...}}}
      → {"answers":{<name>: <answer-per-type>}}
        noul  → {"noul": 0.92}                      (p of yes, fixed)
        score → {"scores": {"escalate": 0.92, "scam": 0.07}}
        choice→ {"choice": ..., "probabilities": {...sum to 1...}, "confidence": 0.87}
      state containing FAULT_PROBE_TEXT → the choice answer is dropped: a 200
        whose shape decide cannot read (degraded-fallback regression hook).
    GET  /healthz     → {"status": "ok"}

Choice probabilities are keyword-derived from ``state`` when possible, else
uniform across the requested options — deterministic per input.

Standalone: ``python -m tests.mocks.mock_jev`` (uvicorn on :8081) — needs
fastapi + uvicorn on the host, which the mock container image bakes in
(docker/mock.Dockerfile; compose.mock.yaml runs it as `jev-mock`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import uvicorn
from fastapi import FastAPI

NOUL_P_YES = 0.92
"""Fixed noul answer: probability of ``yes`` for the escalate_now head."""

CONFIDENCE = 0.87
"""Canned confidence reported with every choice answer."""

FAULT_PROBE_TEXT = "smoke fault probe malformed response"
"""State marker that drops the choice answer from the response — a 200 whose
shape decide's extraction cannot read. Pinned by tests/integration/smoke.sh to
prove an unreadable response degrades instead of dropping the decision."""

#: Simple keyword hints the mock uses to skew choice probabilities; mirrors the
#: pipeline's rule table loosely enough for downstream keyword assertions.
KEYWORD_HINTS: Mapping[str, str] = {
    "scam": "scam_report",
    "phishing": "scam_report",
    "fraud": "scam_report",
    "invoice": "billing",
    "refund": "billing",
    "charged": "billing",
    "crash": "bug",
    "error": "bug",
    "broken": "bug",
    "login": "access",
    "locked": "access",
    "password": "access",
    "suggest": "feature_request",
    "feature": "feature_request",
    "angry": "complaint",
    "terrible": "complaint",
    "cancel": "complaint",
    "worst": "complaint",
}

app = FastAPI(title="udp mock jev", version="0.1.0")


def _choice_answer(options: list[str], state: str) -> dict[str, Any]:
    """Build a choice answer: keyword-skewed when possible, else uniform."""
    probabilities = {option: 0.0 for option in options}
    picked: str | None = None
    lowered = state.lower()
    for keyword, category in KEYWORD_HINTS.items():
        if keyword in lowered and category in probabilities:
            picked = category
            break
    if picked is None:
        share = 1.0 / len(options)
        probabilities = {option: round(share, 4) for option in options}
    else:
        probabilities[picked] = 0.90
        share = 0.10 / (len(options) - 1)
        for option in probabilities:
            if option != picked:
                probabilities[option] = round(share, 4)
    # Rounding must never break the sum-to-1 contract; push the residue into
    # the largest bucket.
    residue = round(1.0 - sum(probabilities.values()), 4)
    picked_bucket = max(probabilities, key=lambda option: probabilities[option])
    probabilities[picked_bucket] = round(probabilities[picked_bucket] + residue, 4)
    return {
        "type": "choice",
        "choice": picked or max(probabilities, key=lambda option: probabilities[option]),
        "probabilities": probabilities,
        "confidence": CONFIDENCE,
    }


def _answer_question(question: Mapping[str, Any], state: str) -> dict[str, Any]:
    """Answer one /v1/systemone question by its declared type."""
    qtype = question.get("type")
    if qtype == "noul":
        return {"type": "noul", "noul": NOUL_P_YES}
    if qtype == "score":
        return {"type": "score", "scores": {"escalate": NOUL_P_YES, "scam": 0.07}}
    if qtype == "choice":
        options = list(question.get("options") or [])
        return _choice_answer(options, state)
    return {"type": str(qtype), "error": f"unsupported question type: {qtype}"}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe (compose healthcheck)."""
    return {"status": "ok"}


@app.post("/v1/systemone")
def systemone(req: Mapping[str, Any]) -> dict[str, Any]:
    """Answer every question in the request with canned per-type values."""
    state = str(req.get("state") or "")
    questions = req.get("questions") or {}
    answers = {name: _answer_question(q, state) for name, q in questions.items()}
    if FAULT_PROBE_TEXT in state:
        answers.pop("category", None)
    return {"answers": answers}


def main() -> None:
    """Run the mock standalone (``python -m tests.mocks.mock_jev``)."""
    uvicorn.run(app, host="0.0.0.0", port=8081, log_level="info")


if __name__ == "__main__":
    main()
