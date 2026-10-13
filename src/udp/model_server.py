"""Real Laya model server for the decide pipeline (S5).

``POST /v1/systemone`` (``{"model","state","questions":{<name>:{"type":…}}}``)
→ ``{"answers":{…}}`` — the exact wire contract decide.yaml sends and
``tests/mocks/mock_jev.py`` implements; ``GET /healthz`` → ``{"status": "ok"}``.

laya's own ``laya.serve`` can't serve this contract (``Agent.predict``
validation requires ``instructions`` and — for choice — ``criteria``, which
the wire never sends), so this server accepts the wire shape, injects the
prompts the checkpoint was trained with, and maps ``options`` → ``criteria``.
Answers pass through unchanged, so decide.yaml's bloblang mappings
(``answers.escalate_now.noul``, ``answers.category.probabilities.scam_report``)
keep working untouched.

Concurrency: laya is single-worker (one forward pass at a time). Requests are
admitted into a bounded queue (full → ``503`` + ``Retry-After: 1``, matching
decide.yaml's ``backoff_on: [429, 503]`` + 100ms ``retry_period``) and served
by one consumer thread that coalesces queued requests into ``predict_batch``
cohorts grouped by identical question definitions — the throughput path laya
exists for. Measured on this GPU (2 questions/ticket): batch 1 = 6.0
tickets/s, batch 8 = 14.3 tickets/s at 558ms, so a full cohort stays under
decide.yaml's 1500ms endpoint timeout — which is what lets bursty concurrent
decide traffic finish non-degraded.

The heavy runtime (torch + laya) is intentionally undeclared in pyproject and
lives in the project-local ``.venv`` (see the pyproject NOTE); run with
``make model`` (``uv run python -m udp.model_server``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from typing import Any, Protocol, cast

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("udp.model_server")

# Read before any heavy import: weights cache path, and no TF probe deadlock
# (laya README: USE_TF=0).
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("HF_HOME", ".hf-cache")

CHECKPOINT = "convaiinnovations/laya-typed-decisions"
DEFAULT_HOST = "127.0.0.1"  # decide reaches us via host.docker.internal
DEFAULT_PORT = 8081  # docker-compose points MODEL_ENDPOINT_URL here
DEFAULT_DEVICE = "cuda"  # bf16 autocast is laya's CUDA default for this checkpoint

# Queue sized so worst-case queueing stays far under decide.yaml's 1500ms
# endpoint timeout (~8 × 64ms ≈ 512ms; the 503 backpressure keeps it bounded).
DEFAULT_MAX_QUEUE = 64

# States collated into one predict_batch call (≈573ms on this GPU); the
# straggler wait before running a partially-filled batch.
DEFAULT_MAX_BATCH = 8
DEFAULT_COALESCE_MS = 10.0

RETRY_AFTER_S = "1"  # RFC 7231 delay-seconds is an integer

#: One queued inference request: ``(state, questions, future)``.
_Job = tuple[str, dict[str, dict[str, Any]], "Future[dict[str, Any]]"]

#: Wire question ids the decide pipeline sends, with the instructions the
#: checkpoint needs to answer them well. Unknown ids get a generic prompt.
DEFAULT_INSTRUCTIONS: Mapping[tuple[str, str], str] = {
    ("escalate_now", "noul"): (
        "Should this ticket be escalated to a human support agent right now — "
        "an active security incident, a blocking outage, or a customer "
        "threatening to cancel?"
    ),
    ("category", "choice"): "Which category best describes this support ticket?",
}

_NOUL_FALLBACK = "Answer yes or no for question '{qid}' based on the state."
_CHOICE_FALLBACK = "Choose the option that best matches the state."

#: Option descriptions for the pipeline's six categories (mirrors docs/rules.md).
_CATEGORY_CRITERIA: Mapping[str, str] = {
    "complaint": "frustration or anger about the product without a concrete technical problem",
    "scam_report": "a report of phishing, scam, fraud, fake licenses, or malware",
    "billing": "invoices, charges, refunds, subscriptions, payment failures",
    "bug": "crashes, errors, broken functionality, stale or duplicated data",
    "access": "login, password, 2FA, SSO, account lockout problems",
    "feature_request": "suggestions or requests for new functionality",
}

_NOULD_CRITERIA: Mapping[str, str] = {
    "true": "yes, escalate now",
    "false": "no, handle through the normal queue",
}

SUPPORTED_TYPES = ("choice", "score", "noul")


class LayaAgent(Protocol):
    """Structural type of the (untyped) laya agent surface this server needs."""

    def predict(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Run one typed-decision forward pass over ``state``."""
        ...

    def predict_batch(
        self, states: list[str], questions: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Collate many states into shared forward passes (throughput path)."""
        ...


def load_checkpoint(checkpoint: str = CHECKPOINT, device: str = DEFAULT_DEVICE) -> LayaAgent:
    """Load the Laya checkpoint once at startup; import errors surface here."""
    import laya

    return cast(LayaAgent, laya.load(checkpoint, device=device))


def to_laya_questions(
    questions: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Translate wire questions into laya question definitions.

    The wire carries only ``type`` (plus ``options`` for choices); laya needs
    ``instructions`` and ``criteria``. Known ids/labels get domain prompts,
    unknown ones generic fallbacks. Returns ``(laya_questions, unsupported)``
    — the ids that must be answered with the mock's error shape instead.
    """
    laya_questions: dict[str, dict[str, Any]] = {}
    unsupported: dict[str, dict[str, Any]] = {}
    for qid, wire in questions.items():
        qtype = str(wire.get("type") or "")
        if qtype not in SUPPORTED_TYPES:
            unsupported[qid] = {
                "type": qtype,
                "error": f"unsupported question type: {qtype}",
            }
            continue
        laya_q: dict[str, Any] = {
            "type": qtype,
            "instructions": DEFAULT_INSTRUCTIONS.get(
                (qid, qtype),
                _NOUL_FALLBACK.format(qid=qid) if qtype == "noul" else _CHOICE_FALLBACK,
            ),
        }
        if qtype == "choice":
            options = [str(option) for option in (wire.get("options") or [])]
            laya_q["criteria"] = {option: _CATEGORY_CRITERIA.get(option) for option in options}
        elif qtype == "noul":
            laya_q["criteria"] = dict(_NOULD_CRITERIA)
        elif qtype == "score" and isinstance(wire.get("criteria"), list):
            laya_q["criteria"] = list(wire["criteria"])
        laya_questions[qid] = laya_q
    return laya_questions, unsupported


class InferenceGate:
    """Bounded admission + single-consumer coalescing loop around one agent.

    One consumer thread (laya is single-worker) drains the queue into
    ``predict_batch`` cohorts; ``try_submit`` is drop-fast — a full queue
    returns ``None`` and the endpoint answers 503 + Retry-After.
    """

    def __init__(
        self,
        agent: LayaAgent,
        max_queue: int,
        max_batch: int,
        coalesce_ms: float,
    ) -> None:
        self._agent = agent
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=max_queue)
        self._max_batch = max_batch
        self._coalesce_s = coalesce_ms / 1000.0
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        """Start the consumer thread (once, at app startup)."""
        self._thread.start()

    def try_submit(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> Future[dict[str, Any]] | None:
        """Enqueue one request without blocking; ``None`` = queue full (→ 503)."""
        future: Future[dict[str, Any]] = Future()
        try:
            self._queue.put_nowait((state, questions, future))
        except queue.Full:
            return None
        return future

    def _serve(self) -> None:
        """Consumer loop: coalesce the queue into ``predict_batch`` calls."""
        while True:
            state, questions, future = self._queue.get()
            batch: list[_Job] = [(state, questions, future)]
            deadline = time.monotonic() + self._coalesce_s
            while len(batch) < self._max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break
            groups: dict[str, list[_Job]] = {}
            for job in batch:
                groups.setdefault(json.dumps(job[1], sort_keys=True), []).append(job)
            for items in groups.values():
                self._predict_group(items)

    def _predict_group(self, items: list[_Job]) -> None:
        """Run one ``predict_batch`` over a same-questions group; settle futures."""
        try:
            results = self._agent.predict_batch([state for state, _, _ in items], items[0][1])
            for (_, _, future), result in zip(items, results, strict=True):
                if not future.cancelled():
                    future.set_result(result)
        except Exception as exc:  # noqa: BLE001 - surfaced via the endpoint's 500
            for _, _, future in items:
                if not future.cancelled():
                    future.set_exception(exc)


class QuestionDef(BaseModel):
    """One wire question: ``{"type": "choice", "options": [...]}`` etc."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(default="noul", description="Question type: choice, score, or noul.")
    options: list[str] | None = Field(
        default=None, description="Choice option labels (wire name for laya's criteria)."
    )


class SystemOneRequest(BaseModel):
    """The ``POST /v1/systemone`` request the decide pipeline sends.

    Deliberately NOT the domain envelope (schemas.py): the wire contract is
    Jev's ``/v1/systemone``, and this module keeps dependency-light models.
    """

    model: str | None = Field(default=None, description="Requested checkpoint id (informational).")
    state: str = Field(default="", description="Ticket text to decide over.")
    questions: dict[str, QuestionDef] = Field(
        description="Questions to answer, keyed by question id."
    )


def warmup(agent: LayaAgent) -> None:
    """Pre-fire the CUDA path with the pipeline's exact question shape.

    The first real request would otherwise pay lazy kernel initialization
    (seconds) and blow decide's 1500ms timeout; three passes also let the CUDA
    memory pool reach its steady footprint before serving.
    """
    questions, _ = to_laya_questions(
        {
            "escalate_now": {"type": "noul"},
            "category": {
                "type": "choice",
                "options": [
                    "complaint",
                    "scam_report",
                    "billing",
                    "bug",
                    "access",
                    "feature_request",
                ],
            },
        }
    )
    for _ in range(3):
        agent.predict(state="warmup: the app works fine, thanks.", questions=questions)


def create_app(
    agent_factory: Callable[[], LayaAgent] | None = None,
    *,
    max_queue: int = DEFAULT_MAX_QUEUE,
    max_batch: int = DEFAULT_MAX_BATCH,
    coalesce_ms: float = DEFAULT_COALESCE_MS,
) -> FastAPI:
    """Build the FastAPI app, loading the checkpoint once at startup.

    ``agent_factory`` is dependency injection for tests (defaults to
    :func:`load_checkpoint` + the startup warmup; injected fakes skip warmup).
    """
    gate: InferenceGate | None = None

    async def lifespan(app: FastAPI) -> Any:
        """Load the checkpoint once, build the gate, free it on shutdown."""
        nonlocal gate
        agent = agent_factory() if agent_factory is not None else load_checkpoint()
        if agent_factory is None:
            warmup(agent)
        gate = InferenceGate(agent, max_queue, max_batch, coalesce_ms)
        gate.start()
        logger.info(
            "model server ready: %s queue=%d batch=%d coalesce=%.0fms",
            CHECKPOINT,
            max_queue,
            max_batch,
            coalesce_ms,
        )
        yield
        gate = None

    app = FastAPI(title="udp model server", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness probe (mirrors the mock's response shape)."""
        return {"status": "ok"}

    @app.post("/v1/systemone", response_model=None)
    async def systemone(req: SystemOneRequest) -> JSONResponse | dict[str, Any]:
        """Answer the wire questions with real laya answers (mock's shape).

        503 + ``Retry-After`` when the admission queue is full (decide retries
        with backoff); 500 on inference failure. ``Server-Timing:
        inference;dur=`` covers queue wait + batched forward pass.
        """
        assert gate is not None, "gate must be built by the lifespan"
        wire_questions = {qid: q.model_dump() for qid, q in req.questions.items()}
        laya_questions, unsupported = to_laya_questions(wire_questions)
        started = time.perf_counter()
        try:
            if laya_questions:
                future = gate.try_submit(req.state, laya_questions)
                if future is None:
                    return JSONResponse(
                        status_code=503,
                        headers={"Retry-After": RETRY_AFTER_S},
                        content={"detail": "model server busy"},
                    )
                result = await asyncio.wrap_future(future)
                answers: dict[str, Any] = dict(result["answers"])
            else:
                answers = {}
        except Exception:
            logger.exception("inference failed")
            return JSONResponse(status_code=500, content={"detail": "inference failed"})
        infer_ms = (time.perf_counter() - started) * 1000.0
        answers.update(unsupported)
        return JSONResponse(
            content={"answers": answers},
            headers={"Server-Timing": f"inference;dur={infer_ms:.2f}"},
        )

    return app


def main() -> None:
    """Run the model server standalone (``make model``)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s")
    # The consumer thread's per-batch decode work (numpy answer shaping between
    # torch ops) holds the GIL in long stretches; without a tighter switch
    # interval the uvicorn event loop can stall long enough for decide's
    # 1500ms client timeout to fire on connections that are still unaccepted.
    sys.setswitchinterval(0.0005)
    uvicorn.run(
        create_app(
            max_batch=int(os.environ.get("MODEL_MAX_BATCH", str(DEFAULT_MAX_BATCH))),
            coalesce_ms=float(os.environ.get("MODEL_COALESCE_MS", str(DEFAULT_COALESCE_MS))),
        ),
        host=os.environ.get("MODEL_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MODEL_PORT", str(DEFAULT_PORT))),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
