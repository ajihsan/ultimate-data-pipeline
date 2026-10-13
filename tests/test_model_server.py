"""Unit tests for the real model server's wire contract (fake laya agent — no GPU)."""

from typing import Any

from fastapi.testclient import TestClient

from udp.model_server import create_app, to_laya_questions

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
    # SLO far above the fake's zero-cost inference: this test asserts the
    # single-consumer plumbing, not the admission policy (covered by the SLO
    # constants' own math). Without it, an 8-deep burst's tail is correctly
    # refused as would-be-late.
    with TestClient(create_app(agent_factory=lambda: fake, slo_ms=10_000)) as client:
        import concurrent.futures

        def post(i: int) -> int:
            resp = client.post(
                "/v1/systemone",
                json={"model": "laya", "state": f"ticket {i}", "questions": PIPELINE_QUESTIONS},
            )
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(post, range(8)))
    assert codes == [200] * 8
    assert len(fake.seen_states) == 8


def test_slo_rejects_projected_late_submissions() -> None:
    """Burst tails that cannot finish inside the SLO get 503 + Retry-After."""
    fake = FakeAgent()
    app = create_app(agent_factory=lambda: fake, max_batch=2, coalesce_ms=0, slo_ms=1)
    with TestClient(app) as client:
        import concurrent.futures

        def post(i: int) -> int:
            resp = client.post(
                "/v1/systemone",
                json={"model": "laya", "state": f"ticket {i}", "questions": PIPELINE_QUESTIONS},
            )
            return resp.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            codes = list(pool.map(post, range(6)))
    # slo_ms=1 rejects everything projected past ~0ms: the whole burst is
    # refused (retryable), the endpoint never times a client out silently.
    assert set(codes) == {503}


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
