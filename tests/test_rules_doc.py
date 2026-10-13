"""The decide pipeline's documented rule table must stay coherent.

`connect/decide.yaml` carries the executed rule table as a comment block;
`docs/rules.md` is the human-readable copy. These tests assert both documents
agree on priority order, categories, and confidences — the bloblang mapping
cannot be imported from pytest, so the doc contract is what's checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# (rule number, category, confidence) — the S4 fast-path cascade, in order.
EXPECTED_RULES = [
    (1, "scam_report", "0.95"),
    (2, "billing", "0.90"),
    (3, "bug", "0.90"),
    (4, "access", "0.90"),
    (5, "feature_request", "0.85"),
    (6, "complaint", "0.85"),
    (7, "(no category → flag)", "0.40"),
    (8, "(no category → flag)", "0.45"),
]


def _normalize(cell: str) -> str:
    """Strip markdown emphasis/backticks from a rules.md table cell."""
    return cell.replace("`", "").replace("*", "").strip()


def _decide_yaml_comments() -> str:
    text = (REPO / "connect" / "decide.yaml").read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))


def _rules_md() -> str:
    return (REPO / "docs" / "rules.md").read_text(encoding="utf-8")


def test_yaml_comment_rule_block_lists_every_rule_in_order() -> None:
    comments = _decide_yaml_comments()
    markers = re.findall(r"rule (\d): (.+?)\s+conf (\d\.\d\d)", comments)
    found = [(int(n), cat.strip(), conf) for n, cat, conf in markers]
    assert found == EXPECTED_RULES


def test_rules_md_table_matches_yaml_rule_block() -> None:
    table = _rules_md()
    rows = re.findall(r"^\| (\d) \| (.+?) \| (\d\.\d\d) \|", table, re.MULTILINE)
    found = [(int(n), _normalize(cat), conf) for n, cat, conf in rows]
    assert found[: len(EXPECTED_RULES)] == EXPECTED_RULES


def test_keyword_lists_match_between_yaml_and_rules_md() -> None:
    comments = _decide_yaml_comments()
    table = _rules_md()
    for number, category, _conf in EXPECTED_RULES[:6]:
        yaml_line = next(
            line for line in comments.splitlines() if line.startswith(f"#   rule {number}:")
        )
        md_line = next(line for line in table.splitlines() if line.startswith(f"| {number} |"))
        yaml_keywords = set(re.findall(r"`([^`]+)`", yaml_line))
        md_keywords = set(re.findall(r"`([^`]+)`", md_line))
        # The doc table quotes category/verdict names too; only require that
        # every keyword documented in the YAML block is present in rules.md.
        missing = yaml_keywords - md_keywords
        assert not missing, f"rule {number} ({category}): keywords missing in rules.md: {missing}"
        assert category in md_line


def test_rules_md_documents_flagged_path_semantics() -> None:
    table = _rules_md()
    assert "model_degraded_total" in table
    assert "/v1/systemone" in table
    assert "degraded = true" in table or "degraded=true" in table
