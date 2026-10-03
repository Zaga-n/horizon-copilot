"""The shipped baseline and runtime agree on one application revision."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from horizon_migrations.db.migration_scope import include_name
from horizon_schema import SCHEMA_REVISION

pytestmark = pytest.mark.contract


def test_one_head_matches_runtime() -> None:
    config = Config(str(Path(__file__).resolve().parents[4] / "services/migrations/alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [SCHEMA_REVISION]


def test_reflection_restricts_schema() -> None:
    assert include_name("app", "schema", {})
    assert not include_name("langgraph", "schema", {})
    assert not include_name(None, "schema", {})
