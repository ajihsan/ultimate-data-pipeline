"""Render ``docs/schemas.md`` from the pydantic domain models.

The markdown is fully generated from ``model_json_schema()`` plus the example
payloads attached to each model — run ``uv run python -m udp.schemas_doc`` to
refresh it after changing ``src/udp/schemas.py``. A test fails on drift, so the
committed doc always matches the code.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from udp.schemas import SCHEMA_VERSION, AlertEvent, FastDecision, ModelDecision, Ticket

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "schemas.md"
"""Where the generated markdown is written (repo root / docs)."""

MODELS: tuple[type[BaseModel], ...] = (Ticket, FastDecision, ModelDecision, AlertEvent)
"""Models documented, in pipeline order."""

_WIRE_NOTES: dict[type[BaseModel], str] = {
    Ticket: (
        "wire format, as published to topic `tickets`; `category_hint` is "
        "generator-only metadata — stripped before publish, kept only in "
        "`--dry-run` output"
    ),
    FastDecision: "wire format, as it would appear on a Kafka topic / ClickHouse row.",
    ModelDecision: "wire format, as it would appear on a Kafka topic / ClickHouse row.",
    AlertEvent: "wire format, as it would appear on a Kafka topic / ClickHouse row.",
}
"""One-line explanation shown above each example payload."""


def _escape_cell(text: str) -> str:
    """Make a string safe inside a markdown table cell.

    Args:
        text: Raw text (field names, types, descriptions).

    Returns:
        Text with pipes escaped so table layout survives.
    """
    return text.replace("|", "\\|")


def _fmt_type(annotation: Any) -> str:
    """Render a field annotation compactly for the doc table.

    Args:
        annotation: The raw annotation from ``FieldInfo.annotation``.

    Returns:
        Short human-readable form, e.g. ``str``, ``str | None``,
        ``Literal['choice', 'score', 'noul']``.
    """
    if isinstance(annotation, type):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _field_table(model: type[BaseModel]) -> str:
    """Render the field table for one model from its JSON schema.

    Args:
        model: The pydantic model class to document.

    Returns:
        Markdown table of field name, type, requirement, default, description.
    """
    rows = ["| Field | Type | Required | Default | Description |", "|---|---|---|---|---|"]
    schema = model.model_json_schema()
    properties: dict[str, dict[str, Any]] = schema.get("properties", {})
    required: set[str] = set(schema.get("required", []))
    for name, field in model.model_fields.items():
        default = "—"
        if not field.is_required():
            raw = field.get_default(call_default_factory=False)
            default = "—" if raw is PydanticUndefined else f"`{raw!r}`"
        description = properties.get(name, {}).get("description", "")
        rows.append(
            f"| `{name}` | `{_escape_cell(_fmt_type(field.annotation))}` "
            f"| {'yes' if name in required else 'no'} | {default} "
            f"| {_escape_cell(description)} |"
        )
    return "\n".join(rows)


def _wire_example(model: type[BaseModel]) -> str:
    """Render one realistic example payload in wire format.

    The example comes from the model's ``json_schema_extra`` examples, is
    validated through the model itself, and is serialized exactly as it would
    be published (wire-excluded fields removed), then pretty-printed.

    Args:
        model: The pydantic model class to render an example for.

    Returns:
        Pretty-printed JSON of a valid wire payload.
    """
    schema = model.model_json_schema()
    example: dict[str, Any] = schema["examples"][0]
    instance = model.model_validate(example)
    excluded = set(getattr(model, "WIRE_EXCLUDED_FIELDS", frozenset()))
    wire = instance.model_dump_json(exclude=excluded)
    return json.dumps(json.loads(wire), indent=2)


def _json_schema_block(model: type[BaseModel]) -> str:
    """Render the full JSON schema of a model inside a collapsed block.

    Args:
        model: The pydantic model class to document.

    Returns:
        Markdown with a ``<details>`` block containing the complete schema.
    """
    body = json.dumps(model.model_json_schema(), indent=2)
    return f"<details><summary>JSON Schema</summary>\n\n```json\n{body}\n```\n\n</details>"


def _summary_line(model: type[BaseModel]) -> str:
    """Extract the first paragraph of a model's docstring as its doc summary.

    Args:
        model: The pydantic model class to summarize.

    Returns:
        The docstring's first paragraph, single-spaced.
    """
    doc = model.__doc__ or ""
    return " ".join(doc.strip().split("\n\n")[0].split("\n")).replace("``", "`")


def render_schemas_doc() -> str:
    """Render the complete ``docs/schemas.md`` content.

    Returns:
        The full markdown document (head, one section per model).
    """
    parts = [
        "# Domain Schemas (S2)",
        "",
        "> Generated by `uv run python -m udp.schemas_doc` — do not edit by hand.",
        "> A test (`test_schemas_doc_matches_models`) fails when this file drifts",
        "> from `src/udp/schemas.py`.",
        "",
        "Every payload on a Kafka topic or ClickHouse row is one of the four",
        "pydantic v2 models in `src/udp/schemas.py`, each stamped `schema_version`",
        f"(currently `{SCHEMA_VERSION}`) and validated on both produce and consume.",
        "",
        "**Latency measurement contract:** producers set `Ticket.ingested_at` when",
        "the ticket enters the stream; deciders stamp `decided_at`. End-to-end",
        "latency is `p95 = decided_at - ingested_at`.",
        "",
    ]
    for model in MODELS:
        parts.extend(
            [
                f"## {model.__name__}",
                "",
                _summary_line(model),
                "",
                _field_table(model),
                "",
                f"Example payload — {_WIRE_NOTES[model]}",
                "",
                "```json",
                _wire_example(model),
                "```",
                "",
                _json_schema_block(model),
                "",
            ]
        )
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m udp.schemas_doc``.

    Args:
        argv: CLI arguments; ``--check`` verifies freshness instead of writing.

    Returns:
        ``0`` on success (written or fresh); ``1`` when ``--check`` finds drift.

    Raises:
        SystemExit: Never directly; the module guard forwards the exit code.
    """
    parser = argparse.ArgumentParser(
        prog="python -m udp.schemas_doc",
        description="Render docs/schemas.md from src/udp/schemas.py.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail with exit code 1 if docs/schemas.md is stale",
    )
    args = parser.parse_args(argv)
    rendered = render_schemas_doc()
    if args.check:
        if DOC_PATH.read_text(encoding="utf-8") == rendered:
            print(f"{DOC_PATH} is up to date")
            return 0
        print(f"{DOC_PATH} is stale — run: uv run python -m udp.schemas_doc")
        return 1
    DOC_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {DOC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
