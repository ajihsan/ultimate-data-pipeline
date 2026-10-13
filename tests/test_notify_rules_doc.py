"""The notify pipeline's documented alert rules must stay coherent.

`connect/notify.yaml` carries the executed rule table (qualifying rule, dedupe
contract, rate-limit semantics, provider matrix) as its comment header; the
bloblang mappings cannot be imported from pytest, so the doc contract is what
is checked here — the same pattern as `test_rules_doc.py`.

Structural assertions additionally check the YAML body (cache resource, input
groups, output table) by text so a refactor that drops a documented guarantee
fails loudly.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _notify_yaml() -> str:
    return (REPO / "connect" / "notify.yaml").read_text(encoding="utf-8")


def _notify_comments() -> str:
    text = _notify_yaml()
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))


def test_header_documents_qualifying_rule_table() -> None:
    comments = _notify_comments()
    assert "QUALIFYING RULE TABLE" in comments
    # Model path: either probability threshold, sourced from the env knobs.
    assert re.search(r"escalate_probability ≥ THRESHOLD_ESCALATE_P \(default 0\.75\)", comments)
    assert re.search(r"scam_probability ≥ THRESHOLD_SCAM_P \(default 0\.35\)", comments)
    # Fast path: rule-tier scam reports only.
    assert "category == scam_report AND confidence ≥ 0.85" in comments
    # Degraded rows never qualify; non-qualifying rows never alert.
    assert "degraded model rows NEVER qualify" in comments
    assert "Non-qualifying decisions produce NO alert row" in comments


def test_yaml_mapping_executes_the_documented_rule_table() -> None:
    body = _notify_yaml()
    # The executed thresholds must reference the env knobs (config-time
    # substitution) and the fast path must key on scam_report at rule tier.
    assert (
        "this.escalate_probability >= (${THRESHOLD_ESCALATE_P}) || "
        "this.scam_probability >= (${THRESHOLD_SCAM_P})" in body
    )
    assert 'this.category == "scam_report" && this.confidence >= 0.85' in body
    assert "this.degraded == true => false" in body


def test_header_documents_dedupe_contract() -> None:
    comments = _notify_comments()
    # Replay window from S5b carry-forward: ~10s of redelivery, keyed by
    # decision_id with the fast path synthesized.
    assert "fast:<event_id>" in comments
    assert "The alerts row IS the dedupe record" in comments
    # The synthesized key is also what the pipeline executes.
    body = _notify_yaml()
    assert '"decision_id": "fast:" + (this.event_id | "")' in body
    assert "where: decision_id = ?" in body


def test_header_documents_rate_limit_semantics() -> None:
    comments = _notify_comments()
    assert re.search(r"K = ALERT_RATE_LIMIT_PER_MIN \(default 10\)", comments)
    assert "DROP-AND-RECORD" in comments
    assert "alert-count-<epoch-minute>" in comments
    assert "suppressed=true" in comments
    assert "notify_suppressed_total" in comments
    # The counter key and limit comparison must exist in the executed pipeline.
    body = _notify_yaml()
    assert "alert-count-${! (timestamp_unix() / 60).floor() }" in body
    assert "this.rate_count < ${ALERT_RATE_LIMIT_PER_MIN}" in body
    assert "this.rate_count >= ${ALERT_RATE_LIMIT_PER_MIN}" in body


def test_rate_counter_uses_ttlru_resource_not_native_rate_limit() -> None:
    body = _notify_yaml()
    # The native rate_limit processor throttles/backpressures; the design
    # requires a cache-based minute-window counter instead.
    assert "rate_limit:" not in body
    assert re.search(r"- label: rate_window\n    ttlru:", body)
    assert "resource: rate_window" in body


def test_header_documents_provider_matrix_and_channels() -> None:
    comments = _notify_comments()
    for provider in ("log", "mock", "telegram", "discord"):
        assert re.search(rf"^\s*#\s+{provider}\b", comments, re.MULTILINE), provider
    assert "NOTIFY_PROVIDER=telegram,discord fans out to both" in comments
    assert "notify_delivery_failed_total" in comments
    # Alerts row is always written, delivered or suppressed.
    assert "ALWAYS writes the alerts row" in comments
    assert "channel column" in comments
    assert "telegram,discord" in comments


def test_header_documents_golive_env_vars() -> None:
    comments = _notify_comments()
    for env in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        assert env in comments
    assert "@BotFather" in comments


def test_pipeline_consumes_both_decision_topics_with_dedicated_groups() -> None:
    body = _notify_yaml()
    groups = (("udp-notify-fast", "fast_decisions"), ("udp-notify-model", "model_decisions"))
    for group, topic in groups:
        assert f"consumer_group: {group}" in body
        assert f"- {topic}" in body
    # Serialization guarantee for the rate counter + dedupe check.
    assert "threads: 1" in body


def test_output_writes_udp_alerts_table() -> None:
    body = _notify_yaml()
    assert "table: alerts" in body
    assert "strict_mode: true" in body
    columns = ("decision_id", "event_id", "channel", "suppressed", "created_at", "schema_version")
    for column in columns:
        assert f"- {column}" in body
