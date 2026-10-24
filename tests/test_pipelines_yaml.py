"""Every pipeline config must remain parseable YAML.

Bloblang mappings live inside YAML block scalars, so a bad indent can change
what a pipeline executes. This test catches YAML structure errors. The smoke
job catches Bloblang syntax and runtime errors.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

PIPELINES = [
    "connect/ingest.yaml",
    "connect/decide.yaml",
    "connect/notify.yaml",
    "connect/cdc.yaml",
    "conduit/conduit.yaml",
]


def test_pipeline_configs_are_valid_yaml() -> None:
    for rel in PIPELINES:
        doc = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))
        assert isinstance(doc, dict), rel
