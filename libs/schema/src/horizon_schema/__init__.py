"""Public application metadata and compatibility identity shared by both services."""

from horizon_schema.metadata import APP_SCHEMA, metadata
from horizon_schema.models import EMBEDDING_DIMENSIONS, SCHEMA_REVISION

__all__ = ["APP_SCHEMA", "EMBEDDING_DIMENSIONS", "SCHEMA_REVISION", "metadata"]
