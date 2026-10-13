"""Unit tests for the real model server's wire contract (fake laya agent — no GPU)."""

import threading
from typing import Any

from fastapi.testclient import TestClient

from udp.model_server import InferenceGate, create_app, to_laya_questions

PIPELINE_QUESTIONS: dict[str, Any] = {
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


class FakeAgent:
    """Laya stand-in that echoes the wire contract's canned mock answers."""

    def __init__(self) -> None:
        """Track what the server sent, for translation assertions."""
        self.seen_questions: dict[str, dict[str, Any]] = {}
        self.seen_states: list[str] = []

    def predict(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Record the questions and answer in laya's native shape."""
        self.seen_questions = questions
        self.seen_states.append(state)
        return {"model": "laya", "answers": self._answers(questions), "usage": {}}

    def predict_batch(
        self, states: list[str], questions: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Batch form: same answer shape per state (laya's own contract)."""
        self.seen_questions = questions
        self.seen_states.extend(states)
        return [{"model": "laya", "answers": self._answers(questions), "usage": {}} for _ in states]

    def _answers(self, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Canned laya-shaped answers for the given (already translated) questions."""
        answers: dict[str, Any] = {}
        for qid, q in questions.items():
            if q["type"] == "noul":
                answers[qid] = {
                    "type": "noul",
                    "noul": 0.9123,
                    "confidence": 0.9123,
                    "answer_confidence": 0.9123,
                    "action": {"act_probability": 0.5},
                }
            elif q["type"] == "choice":
                keys = list(q["criteria"])
                share = round(1.0 / len(keys), 4)
                probabilities = {key: share for key in keys}
                first = keys[0]
                probabilities[first] = round(probabilities[first] + 1.0 - share * len(keys), 4)
                answers[qid] = {
                    "type": "choice",
                    "choice": first,
                    "probabilities": probabilities,
                    "confidence": 0.8712,
                    "answer_confidence": 0.8712,
                    "action": {"act_probability": 0.5},
                }
        return answers


class BlockingAgent(FakeAgent):
    """FakeAgent whose predict_batch blocks until released (queue-full tests)."""

    def __init__(self) -> None:
        """Start blocked; tests await :attr:`serving`, then call :meth:`release`."""
        super().__init__()
        self.serving = threading.Event()
        self._resume = threading.Event()

    def release(self) -> None:
        """Unblock the serving loop."""
        self._resume.set()

    def predict_batch(
        self, states: list[str], questions: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Block until released, then answer like FakeAgent."""
        self.serving.set()
        self._resume.wait(timeout=5)
        return super().predict_batch(states, questions)


def test_answer_shape_matches_mock() -> None:
    """The /v1/systemone response shape must equal the mock's (decide.yaml contract)."""
    fake = FakeAgent()
    with TestClient(create_app(agent_factory=lambda: fake)) as client:
        resp = client.post(
            "/v1/systemone",
            json={"model": "laya", "state": "a ticket", "questions": PIPELINE_QUESTIONS},
        )
    assert resp.status_code == 200
    answers = resp.json()["answers"]
    # decide.yaml maps answers.escalate_now.noul and answers.category.probabilities.
    assert isinstance(answers["escalate_now"]["noul"], float)
    assert set(answers["category"]["probabilities"]) == {
        "complaint",
        "scam_report",
        "billing",
        "bug",
        "access",
        "feature_request",
    }
    assert abs(sum(answers["category"]["probabilities"].values()) - 1.0) < 1e-3
    assert answers["category"]["choice"] in answers["category"]["probabilities"]


def test_wire_questions_are_translated_for_laya() -> None:
    """Bare wire questions gain instructions + criteria; answers stay verbatim."""
    fake = FakeAgent()
    with TestClient(create_app(agent_factory=lambda: fake)) as client:
        client.post(
            "/v1/systemone",
            json={"model": "laya", "state": "a ticket", "questions": PIPELINE_QUESTIONS},
        )
    seen = fake.seen_questions
    assert "instructions" in seen["escalate_now"]
    assert seen["escalate_now"]["criteria"]["true"]
    assert seen["category"]["criteria"] == {
        "complaint": "frustration or anger about the product without a concrete technical problem",
        "scam_report": "a report of phishing, scam, fraud, fake licenses, or malware",
        "billing": "invoices, charges, refunds, subscriptions, payment failures",
        "bug": "crashes, errors, broken functionality, stale or duplicated data",
        "access": "login, password, 2FA, SSO, account lockout problems",
        "feature_request": "suggestions or requests for new functionality",
    }


def test_concurrent_requests_are_answered() -> None:
    """Concurrent posts each get their own answer (single-consumer loop)."""
    fake = FakeAgent()
    with TestClient(create_app(agent_factory=lambda: fake)) as client:
        import concurrent.futures

        codes: list[int] = []

        def post(i: int) -> None:
            resp = client.post(
                "/v1/systemone",
                json={"model": "laya", "state": f"ticket {i}", "questions": PIPELINE_QUESTIONS},
            )
            codes.append(resp.status_code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(post, range(8)))
    assert codes == [200] * 8
    assert len(fake.seen_states) == 8


def test_full_queue_refuses_submissions() -> None:
    """Bounded queue full → try_submit returns None (the endpoint answers 503)."""
    fake = BlockingAgent()
    gate = InferenceGate(fake, max_queue=1, max_batch=1, coalesce_ms=0)
    gate.start()
    in_flight = gate.try_submit("a", {})
    assert in_flight is not None
    assert fake.serving.wait(timeout=5), "consumer never picked up the request"
    # The consumer blocks inside predict_batch, so exactly one slot stays free.
    queued = gate.try_submit("b", {})
    assert queued is not None
    assert gate.try_submit("c", {}) is None  # queue full → refused
    fake.release()
    assert in_flight.result(timeout=5) is not None
    assert queued.result(timeout=5) is not None
    assert gate.try_submit("d", {}) is not None  # slot freed → admitted again


def test_unsupported_question_type_gets_mock_parity_error() -> None:
    """Unknown types answer with the mock's error shape instead of failing."""
    with TestClient(create_app(agent_factory=FakeAgent)) as client:
        resp = client.post(
            "/v1/systemone",
            json={
                "model": "laya",
                "state": "a ticket",
                "questions": {"weird": {"type": "quantum"}},
            },
        )
    assert resp.status_code == 200
    assert resp.json()["answers"]["weird"] == {
        "type": "quantum",
        "error": "unsupported question type: quantum",
    }


def test_to_laya_questions_passes_through_score_criteria() -> None:
    """Score questions forward their level list unchanged."""
    laya_questions, unsupported = to_laya_questions(
        {"urgency": {"type": "score", "criteria": ["low", "high"]}}
    )
    assert unsupported == {}
    assert laya_questions["urgency"]["criteria"] == ["low", "high"]
