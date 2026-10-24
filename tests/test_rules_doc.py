"""docs/rules.md must mirror decide.yaml's executable rule table.

The mapping in connect/decide.yaml is the source of truth. The keyword lists,
the first-match order, and the confidences in the human-readable mirror are
pinned here. Rows 7-8 are ambiguous heuristics, not category rules, so this
test does not parse them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: bloblang: let has_scam = ["scam", ...].any(kw -> $low.contains(kw))
_KEYWORDS = re.compile(r"let has_(\w+) = (\[[^\]]*\])\.any\(", re.MULTILINE)
#: bloblang: $has_scam => "scam_report" — the match order IS the rule priority.
_CATEGORY = re.compile(r'\$has_(\w+) => "([a-z_]+)"')
#: bloblang: $category == "scam_report" => 0.95
_CONFIDENCE = re.compile(r'\$category == "([a-z_]+)" => ([\d.]+)')
#: markdown: | 1 | `scam_report` | 0.95 | `scam`, `fraud`, ... |
_DOC_ROW = re.compile(r"^\| (\d) \| .*?`([a-z_]+)`.*? \| ([\d.]+) \| (.*) \|$", re.MULTILINE)
_DOC_KEYWORD = re.compile(r"`([^`]+)`")


def _bloblang_rules() -> list[tuple[str, float, list[str]]]:
    """(category, confidence, keywords) per confident rule, in match order."""
    text = (REPO / "connect/decide.yaml").read_text(encoding="utf-8")
    keywords = {name: json.loads(payload) for name, payload in _KEYWORDS.findall(text)}
    confidences = dict(_CONFIDENCE.findall(text))
    return [
        (category, float(confidences[category]), keywords[name])
        for name, category in _CATEGORY.findall(text)
    ]


def _doc_rules() -> list[tuple[str, float, list[str]]]:
    """(category, confidence, keywords) per confident rule, in table order."""
    text = (REPO / "docs/rules.md").read_text(encoding="utf-8")
    return [
        (category, float(confidence), _DOC_KEYWORD.findall(keywords))
        for _, category, confidence, keywords in _DOC_ROW.findall(text)
    ]


def test_rules_doc_matches_decide_yaml() -> None:
    assert _doc_rules() == _bloblang_rules()
