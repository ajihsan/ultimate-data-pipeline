from __future__ import annotations

from pathlib import Path

from factory.dag_generator import DagFactory

_DEFINITIONS = Path(__file__).resolve().parent / "definitions"
_FACTORY = DagFactory(_DEFINITIONS)

for _dag_id, _dag in _FACTORY.build_dags().items():
    globals()[_dag_id] = _dag
