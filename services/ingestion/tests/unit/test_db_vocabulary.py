"""Fitness: db/ writes statuses decided in domain/, never a literal it chose itself."""

import ast
from pathlib import Path

import pytest

DB = Path(__file__).resolve().parents[2] / "src/horizon_ingestion/db"
STATUS_COLUMNS = frozenset({"status", "stage", "lifecycle", "kind"})


def literal_status_writes(source: str) -> list[str]:
    """`column="literal"` keywords and `{"column": "literal"}` entries in a module."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            found += [
                f"{keyword.arg}={keyword.value.value!r}"
                for keyword in node.keywords
                if keyword.arg in STATUS_COLUMNS
                and isinstance(keyword.value, ast.Constant)
                and isinstance(keyword.value.value, str)
            ]
        if isinstance(node, ast.Dict):
            found += [
                f"{key.value}={value.value!r}"
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant)
                and key.value in STATUS_COLUMNS
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ]
    return found


def test_the_check_catches_a_literal_write() -> None:
    sample = 'update(JOBS).values(status="failed")\ninsert(CHUNKS).values([{"stage": "done"}])'
    assert literal_status_writes(sample) == ["status='failed'", "stage='done'"]


@pytest.mark.parametrize("module", sorted(DB.glob("*.py")), ids=lambda path: path.name)
def test_db_writes_domain_states(module: Path) -> None:
    assert literal_status_writes(module.read_text()) == []
