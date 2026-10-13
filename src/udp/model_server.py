"""Real Laya model server for the decide pipeline (S5).

Exposes the exact ``POST /v1/systemone`` wire contract the decide pipeline's
http processor sends (and ``tests/mocks/mock_jev.py`` implements), backed by the
real checkpoint:

    POST /v1/systemone  {"model","state","questions":{<name>:{"type":...}}}
      → {"answers":{<name>: <answer-per-type>}}
        noul  → {"type","noul": p(true)}                    (p of yes)
        choice→ {"type","choice","probabilities": {...}, "confidence": ...}
    GET  /healthz     → {"status": "ok", ...}

laya 0.4.6 ships an HTTP surface of its own (``laya.serve``), but it forwards
questions verbatim to ``Agent.predict``, whose validation *requires*
``instructions`` and (for choice) ``criteria`` — the pipeline's wire contract
sends bare ``{"type": "noul"}`` and ``{"type": "choice", "options": [...]}``.
This server therefore sits between the two: it accepts the wire shape, injects
the instructions/option descriptions the checkpoint was trained with, and maps
``options`` → ``criteria``. Answers pass through unchanged, so decide.yaml's
bloblang mappings (``answers.escalate_now.noul``,
``answers.category.probabilities.scam_report``) keep working untouched.

Concurrency discipline: laya is single-worker (one forward pass at a time).
Requests are admitted through a bounded slot count; admission is drop-fast —
when every slot is taken the endpoint answers ``503`` with ``Retry-After: 0.1``
(mirroring decide.yaml's ``backoff_on: [429, 503]`` + 100 ms ``retry_period``).
Admitted requests serialize on a :class:`threading.Lock` around
``Agent.predict``. The slot count bounds worst-case queueing below decide's
750 ms endpoint timeout (8 slots × ~64 ms ≈ 512 ms).

The heavy runtime (torch + laya) is intentionally undeclared in pyproject and
lives in the project-local ``.venv`` (see the pyproject NOTE); run with
``make model`` (``uv run python -m udp.model_server``).

Standalone: ``uv run python -m udp.model_server`` (uvicorn on 127.0.0.1:8081).
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

# Environment defaults, read before any heavy import so the model runtime finds
# the project-local weights cache (HF_HOME=.hf-cache) and never deadlocks on a
# TF probe (laya README: USE_TF=0).
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("HF_HOME", ".hf-cache")

CHECKPOINT = "convaiinnovations/laya-typed-decisions"
"""Hugging Face checkpoint served by default (S0-verified, 421M)."""

DEFAULT_HOST = "127.0.0.1"
"""Default bind address; the decide pipeline reaches us via host.docker.internal."""

DEFAULT_PORT = 8081
"""Default bind port; docker-compose points MODEL_ENDPOINT_URL here."""

DEFAULT_DEVICE = "cuda"
"""Default torch device (S0: bf16 autocast is laya's CUDA default)."""

DEFAULT_MAX_QUEUE = 64
"""Default bounded-queue size; beyond it submissions are refused (503 + Retry-After)."""

DEFAULT_MAX_BATCH = 8
"""Default states collated into one predict_batch call (≈573 ms on this GPU)."""

DEFAULT_COALESCE_MS = 10.0
"""Default straggler wait before running a partially-filled batch."""

DEFAULT_SLO_MS = 5000.0
"""Admission SLO: refuse (503, retryable) requests projected past this latency.

INERT by default: on this box the steady pipeline's concurrent bursts fit one
batch-8 cohort (measured: 299/299 decisions non-degraded, zero refusals), and
an active SLO converts those would-succeed requests into 503-retry gambles
that occasionally exhaust into degraded rows. Lower it (e.g. 730, just under
decide.yaml's 750 ms endpoint timeout) for workloads whose decide bursts run
deeper than one cohort — refused requests retry with a fresh 750 ms budget
instead of being admitted into a guaranteed client timeout (unrecoverable →
degraded fallback). """

RETRY_AFTER_S = "0.1"
"""Retry-After hint for 503s; matches decide.yaml's 100 ms retry_period."""

#: Rough per-state forward-pass cost used for admission projections. Measured
#: fit on this GPU (2 questions/ticket): predict_batch ≈ 30 ms + 68 ms/state.
PER_STATE_MS = 70.0
BATCH_BASE_MS = 30.0
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

#: Option descriptions for the pipeline's six categories (mirrors the rule-table
#: semantics in docs/rules.md). Unknown option labels render as bare labels.
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
"""Question types this server understands; anything else mirrors the mock's error answer."""


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
    """Load the Laya checkpoint once at startup.

    Args:
        checkpoint: Hugging Face model id (weights come from ``HF_HOME``).
        device: Torch device (``cuda`` on this host; bf16 autocast is laya's
            CUDA default for this checkpoint).

    Returns:
        A ready-to-call agent. Import errors surface here, at startup.
    """
    import laya  # type: ignore[import-untyped]  # (no py.typed in laya 0.4.6)

    return cast(LayaAgent, laya.load(checkpoint, device=device))


def to_laya_questions(
    questions: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Translate wire questions into laya question definitions.

    The wire contract carries only ``type`` (plus ``options`` for choices);
    laya requires ``instructions`` and, for choices, ``criteria``. Known
    question ids/option labels get domain prompts matching the pipeline's rule
    table; unknown ones get generic fallbacks.

    Args:
        questions: Wire questions keyed by question id.

    Returns:
        ``(laya_questions, unsupported)`` — the definitions to answer, and the
        ids that must be answered with the mock's error shape instead.
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

    Requests are admitted into a bounded queue (drop-fast: full → the endpoint
    answers 503 + Retry-After) and served by ONE consumer thread — laya is
    single-worker. The consumer coalesces whatever is queued into
    ``predict_batch`` calls (grouped by identical question definitions), the
    throughput path laya exists for: one forward pass per group of states
    instead of one per state. Measured on this GPU (2 questions/ticket):
    batch 1 = 6.0 tickets/s, batch 8 = 14.3 tickets/s at 558 ms — a full
    cohort stays under decide.yaml's 750 ms endpoint timeout, which is what
    lets bursty concurrent decide traffic finish non-degraded.
    """

    def __init__(
        self,
        agent: LayaAgent,
        max_queue: int,
        max_batch: int,
        coalesce_ms: float,
        slo_ms: float = DEFAULT_SLO_MS,
    ) -> None:
        """Bind the agent and size admission/batching.

        Args:
            agent: Loaded laya agent (single worker; all predict calls happen
                on the consumer thread).
            max_queue: Bounded queue size; beyond it submissions are refused
                (the endpoint turns that into 503 + Retry-After).
            max_batch: Max states collated into one ``predict_batch`` call.
            coalesce_ms: How long the consumer waits for stragglers once it
                has at least one request (0 = serve single states untouched).
            slo_ms: Admission SLO — submissions projected to complete later
                than this are refused (503 + Retry-After) instead of being
                admitted to a guaranteed client timeout.
        """
        self._agent = agent
        self._queue: queue.Queue[_Job] = queue.Queue(maxsize=max_queue)
        self._max_batch = max_batch
        self._coalesce_s = coalesce_ms / 1000.0
        self._slo_ms = slo_ms
        self._busy_until = 0.0  # monotonic; projected end of the running batch
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        """Start the consumer thread (once, at app startup)."""
        self._thread.start()

    def try_submit(
        self, state: str, questions: dict[str, dict[str, Any]]
    ) -> Future[dict[str, Any]] | None:
        """Try to enqueue one request without blocking.

        Args:
            state: Ticket text (the wire ``state``).
            questions: laya question definitions (from :func:`to_laya_questions`).

        Returns:
            A future resolving to the raw laya result, or ``None`` when the
            queue is full or the projected completion misses the SLO (caller
            answers 503 + Retry-After either way).
        """
        now = time.monotonic()
        busy_ms = max(0.0, self._busy_until - now) * 1000.0
        position = self._queue.qsize() + 1  # 1 = next to be batched
        cohort_index = (position - 1) // self._max_batch  # 0-based cohort of mine
        my_cohort_size = position - cohort_index * self._max_batch
        # predict_batch serves a whole cohort in one forward pass: full cohorts
        # ahead of mine + my own (possibly partially-filled) cohort.
        projected_ms = (
            busy_ms
            + cohort_index * (BATCH_BASE_MS + self._max_batch * PER_STATE_MS)
            + BATCH_BASE_MS
            + my_cohort_size * PER_STATE_MS
        )
        if projected_ms > self._slo_ms:
            return None
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
                key = json.dumps(job[1], sort_keys=True)
                groups.setdefault(key, []).append(job)
            self._busy_until = (
                time.monotonic() + (len(batch) * PER_STATE_MS + BATCH_BASE_MS) / 1000.0
            )
            for items in groups.values():
                self._predict_group(items)

    def _predict_group(self, items: list[_Job]) -> None:
        """Run one ``predict_batch`` over a same-questions group.

        Args:
            items: ``(state, questions, future)`` jobs sharing one question
                definition; every future is settled (result or exception),
                cancelled ones are skipped.
        """
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
    Jev's ``/v1/systemone``, and this module keeps its own dependency-light
    models so decide.yaml's http processor spec stays untouched.
    """

    model: str | None = Field(default=None, description="Requested checkpoint id (informational).")
    state: str = Field(default="", description="Ticket text to decide over.")
    questions: dict[str, QuestionDef] = Field(
        description="Questions to answer, keyed by question id."
    )


def warmup(agent: LayaAgent) -> None:
    """Pre-fire the CUDA path with the pipeline's exact question shape.

    Without this, the first real request pays lazy kernel initialization
    (seconds) and blows decide.yaml's 750 ms endpoint timeout — the first
    flagged tickets would all land in the degraded fallback. Three passes
    also let the CUDA memory pool reach its steady footprint before serving.
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
    slo_ms: float = DEFAULT_SLO_MS,
) -> FastAPI:
    """Build the FastAPI app, loading the checkpoint once at startup.

    Args:
        agent_factory: Optional zero-arg callable producing the agent
            (dependency injection for tests; defaults to
            :func:`load_checkpoint` with the real CUDA checkpoint — including
            the startup warmup; injected fakes skip it).
        max_queue: Bounded queue size (queue-full answers 503 + Retry-After).
        max_batch: Max states collated into one ``predict_batch`` call.
        coalesce_ms: Straggler wait before running a partially-filled batch.
        slo_ms: Admission SLO — projected-slower submissions get 503 + Retry-After.

    Returns:
        The configured app. uvicorn serves it via :func:`main`.
    """
    gate: InferenceGate | None = None

    async def lifespan(app: FastAPI) -> Any:
        """Load the checkpoint once, build the gate, free it on shutdown."""
        nonlocal gate
        agent = agent_factory() if agent_factory is not None else load_checkpoint()
        if agent_factory is None:
            warmup(agent)
        gate = InferenceGate(agent, max_queue, max_batch, coalesce_ms, slo_ms)
        gate.start()
        logger.info(
            "model server ready: %s queue=%d batch=%d coalesce=%.0fms slo=%.0fms",
            CHECKPOINT,
            max_queue,
            max_batch,
            coalesce_ms,
            slo_ms,
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

        Args:
            req: The decide pipeline's request (model/state/questions).

        Returns:
            ``{"answers": {...}}`` exactly as the mock does (unsupported
            question types get per-id error answers, mock parity), with a
            ``Server-Timing: inference;dur=`` header covering queue wait +
            batched forward pass, for latency attribution.

        Raises:
            HTTPException: 503 + ``Retry-After`` when the admission queue is
                full; 500 on inference failure (decide records a degraded
                fallback either way).
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
    # interval the uvicorn event loop can stall long enough for decide's 750 ms
    # client timeout to fire on connections that are still unaccepted.
    sys.setswitchinterval(0.0005)
    uvicorn.run(
        create_app(
            max_batch=int(os.environ.get("MODEL_MAX_BATCH", str(DEFAULT_MAX_BATCH))),
            coalesce_ms=float(os.environ.get("MODEL_COALESCE_MS", str(DEFAULT_COALESCE_MS))),
            slo_ms=float(os.environ.get("MODEL_SLO_MS", str(DEFAULT_SLO_MS))),
        ),
        host=os.environ.get("MODEL_HOST", DEFAULT_HOST),
        port=int(os.environ.get("MODEL_PORT", str(DEFAULT_PORT))),
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
