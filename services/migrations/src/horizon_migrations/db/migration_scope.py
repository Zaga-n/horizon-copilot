"""Alembic reflection allowlist for the application-owned schema."""

from alembic.runtime.environment import NameFilterParentNames

from horizon_schema.metadata import APP_SCHEMA


def include_name(name: str | None, type_: str, parent_names: NameFilterParentNames) -> bool:
    if type_ == "schema":
        return name == APP_SCHEMA
    return parent_names.get("schema_name") == APP_SCHEMA
