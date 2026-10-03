"""Application tables; LangGraph owns separate checkpoint metadata."""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from horizon_schema.metadata import metadata

EMBEDDING_DIMENSIONS = 1024
SCHEMA_REVISION = "20261002_0002"


def _timestamps() -> tuple[Column[datetime], Column[datetime]]:
    return (
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("subject", Text, nullable=False, unique=True),
    *_timestamps(),
)
conversations = Table(
    "conversations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey("app.users.id"), nullable=False),
    Column("status", Text, nullable=False, server_default="active"),
    Column("last_activity_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("active_run_id", Uuid),
    Column("lease_until", DateTime(timezone=True)),
    Column("next_message_order", BigInteger, nullable=False, server_default="1"),
    *_timestamps(),
    CheckConstraint("status IN ('active', 'purging')", name="status"),
    CheckConstraint("(active_run_id IS NULL) = (lease_until IS NULL)", name="lease_pair"),
    CheckConstraint("next_message_order > 0", name="message_order"),
    UniqueConstraint("id", "user_id"),
)
Index("ix_conversations_owner_activity", conversations.c.user_id, conversations.c.last_activity_at)
Index("ix_conversations_retention", conversations.c.status, conversations.c.last_activity_at)
turns = Table(
    "turns",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "conversation_id",
        Uuid,
        ForeignKey("app.conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("user_id", Uuid, nullable=False),
    Column("request_key", Text, nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("user_message_id", Uuid, nullable=False),
    Column("user_role", Text, nullable=False, server_default="user"),
    Column("status", Text, nullable=False),
    *_timestamps(),
    UniqueConstraint("id", "conversation_id"),
    UniqueConstraint("user_id", "conversation_id", "request_key"),
    ForeignKeyConstraint(
        ["conversation_id", "user_id"],
        ["app.conversations.id", "app.conversations.user_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("status IN ('active', 'completed', 'failed')", name="status"),
    CheckConstraint("user_role = 'user'", name="user_role"),
    CheckConstraint("char_length(request_key) BETWEEN 1 AND 200", name="request_key"),
)
messages = Table(
    "messages",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "conversation_id",
        Uuid,
        ForeignKey("app.conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("turn_id", Uuid, nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False, server_default=""),
    Column("status", Text, nullable=False),
    Column("sources", JSONB, nullable=False, server_default="[]"),
    Column("message_order", BigInteger, nullable=False),
    *_timestamps(),
    ForeignKeyConstraint(
        ["turn_id", "conversation_id"],
        ["app.turns.id", "app.turns.conversation_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    UniqueConstraint("conversation_id", "message_order"),
    UniqueConstraint("turn_id", "role", "attempt_number"),
    UniqueConstraint("id", "turn_id", "conversation_id", "role"),
    UniqueConstraint("id", "conversation_id"),
    CheckConstraint("role IN ('user', 'assistant')", name="role"),
    CheckConstraint("status IN ('pending', 'completed', 'failed')", name="status"),
    CheckConstraint(
        "(role = 'user' AND attempt_number = 0 AND status = 'completed') OR (role = 'assistant' AND attempt_number > 0)",
        name="attempt",
    ),
    CheckConstraint("jsonb_typeof(sources) = 'array'", name="sources_array"),
)
# The cyclic user-message link is attached after table creation by the baseline.
turns.append_constraint(
    ForeignKeyConstraint(
        ["user_message_id", "id", "conversation_id", "user_role"],
        [
            "app.messages.id",
            "app.messages.turn_id",
            "app.messages.conversation_id",
            "app.messages.role",
        ],
        name="fk_turns_user_message",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
agent_runs = Table(
    "agent_runs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "conversation_id",
        Uuid,
        ForeignKey("app.conversations.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("turn_id", Uuid, nullable=False),
    Column("user_message_id", Uuid, nullable=False),
    Column("user_role", Text, nullable=False, server_default="user"),
    Column("assistant_message_id", Uuid, nullable=False, unique=True),
    Column("assistant_role", Text, nullable=False, server_default="assistant"),
    Column("attempt_number", Integer, nullable=False),
    Column("trace_id", String(32), nullable=False),
    Column("root_span_id", String(16), nullable=False),
    Column("base_checkpoint_id", Text),
    Column("checkpoint_prepared", Boolean, nullable=False, server_default="false"),
    Column("status", Text, nullable=False),
    Column("failure_category", Text),
    Column("retry_of_run_id", Uuid),
    Column("agent_version", String(64), nullable=False),
    Column("prompt_version", String(64), nullable=False),
    Column("retrieval_version", String(64), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ended_at", DateTime(timezone=True)),
    UniqueConstraint("turn_id", "attempt_number"),
    UniqueConstraint("id", "conversation_id"),
    UniqueConstraint("id", "turn_id", "conversation_id"),
    ForeignKeyConstraint(
        ["turn_id", "conversation_id"],
        ["app.turns.id", "app.turns.conversation_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["user_message_id", "turn_id", "conversation_id", "user_role"],
        [
            "app.messages.id",
            "app.messages.turn_id",
            "app.messages.conversation_id",
            "app.messages.role",
        ],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["assistant_message_id", "turn_id", "conversation_id", "assistant_role"],
        [
            "app.messages.id",
            "app.messages.turn_id",
            "app.messages.conversation_id",
            "app.messages.role",
        ],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    ),
    ForeignKeyConstraint(
        ["retry_of_run_id", "turn_id", "conversation_id"],
        ["app.agent_runs.id", "app.agent_runs.turn_id", "app.agent_runs.conversation_id"],
        deferrable=True,
        initially="DEFERRED",
    ),
    CheckConstraint("status IN ('pending', 'completed', 'failed')", name="status"),
    CheckConstraint("attempt_number > 0", name="attempt"),
    CheckConstraint("user_role = 'user' AND assistant_role = 'assistant'", name="roles"),
    CheckConstraint("trace_id ~ '^[0-9a-f]{32}$' AND trace_id <> repeat('0',32)", name="trace_id"),
    CheckConstraint(
        "root_span_id ~ '^[0-9a-f]{16}$' AND root_span_id <> repeat('0',16)", name="root_span_id"
    ),
    CheckConstraint(
        "(status = 'pending' AND ended_at IS NULL) OR (status <> 'pending' AND ended_at IS NOT NULL)",
        name="terminal_time",
    ),
    CheckConstraint(
        "(status = 'failed' AND failure_category IS NOT NULL) OR (status <> 'failed' AND failure_category IS NULL)",
        name="failure",
    ),
)
Index("ix_agent_runs_conversation_started", agent_runs.c.conversation_id, agent_runs.c.started_at)
Index("ix_agent_runs_trace", agent_runs.c.trace_id)
conversations.append_constraint(
    ForeignKeyConstraint(
        ["active_run_id", "id"],
        ["app.agent_runs.id", "app.agent_runs.conversation_id"],
        name="fk_conversations_active_run",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
retry_requests = Table(
    "retry_requests",
    metadata,
    Column("user_id", Uuid, nullable=False, primary_key=True),
    Column("conversation_id", Uuid, nullable=False, primary_key=True),
    Column("request_key", Text, nullable=False, primary_key=True),
    Column("turn_id", Uuid, nullable=False),
    Column("expected_run_id", Uuid, nullable=False),
    Column("assigned_run_id", Uuid, nullable=False),
    *_timestamps(),
    ForeignKeyConstraint(
        ["conversation_id", "user_id"],
        ["app.conversations.id", "app.conversations.user_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["expected_run_id", "turn_id", "conversation_id"],
        ["app.agent_runs.id", "app.agent_runs.turn_id", "app.agent_runs.conversation_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["assigned_run_id", "turn_id", "conversation_id"],
        ["app.agent_runs.id", "app.agent_runs.turn_id", "app.agent_runs.conversation_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("char_length(request_key) BETWEEN 1 AND 200", name="request_key"),
)
message_feedback = Table(
    "message_feedback",
    metadata,
    Column("user_id", Uuid, primary_key=True),
    Column("assistant_message_id", Uuid, primary_key=True),
    Column("conversation_id", Uuid, nullable=False),
    Column("rating", Text, nullable=False),
    Column("comment", Text),
    *_timestamps(),
    ForeignKeyConstraint(
        ["assistant_message_id", "conversation_id"],
        ["app.messages.id", "app.messages.conversation_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["conversation_id", "user_id"],
        ["app.conversations.id", "app.conversations.user_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("rating IN ('like', 'dislike')", name="rating"),
    CheckConstraint("rating = 'dislike' OR comment IS NULL", name="dislike_comment"),
    CheckConstraint(
        "comment IS NULL OR char_length(comment) BETWEEN 1 AND 4000", name="comment_length"
    ),
)
thread_feedback = Table(
    "thread_feedback",
    metadata,
    Column("user_id", Uuid, primary_key=True),
    Column("conversation_id", Uuid, primary_key=True),
    Column("rating", Text),
    Column("comment", Text),
    Column("context_run_id", Uuid),
    Column("context_message_order", BigInteger, nullable=False),
    *_timestamps(),
    ForeignKeyConstraint(
        ["conversation_id", "user_id"],
        ["app.conversations.id", "app.conversations.user_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["context_run_id", "conversation_id"],
        ["app.agent_runs.id", "app.agent_runs.conversation_id"],
        ondelete="CASCADE",
    ),
    CheckConstraint("rating IS NULL OR rating IN ('like', 'dislike')", name="rating"),
    CheckConstraint(
        "rating IS NOT NULL OR (comment IS NOT NULL AND btrim(comment) <> '')", name="has_value"
    ),
    CheckConstraint(
        "comment IS NULL OR char_length(comment) BETWEEN 1 AND 4000", name="comment_length"
    ),
    CheckConstraint("context_message_order >= 0", name="context_order"),
)
documents = Table(
    "documents",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("user_id", Uuid, ForeignKey("app.users.id"), nullable=False),
    Column("visibility", Text, nullable=False, server_default="private"),
    Column("lifecycle", Text, nullable=False, server_default="live"),
    Column("filename", Text),
    Column("title", Text),
    Column("file_type", Text),
    Column("published_version_id", Uuid),
    Column("project_metadata", JSONB, nullable=False, server_default="{}"),
    *_timestamps(),
    CheckConstraint("visibility IN ('private', 'shared')", name="visibility"),
    CheckConstraint("lifecycle IN ('live', 'deleting', 'deleted')", name="lifecycle"),
    CheckConstraint("file_type IN ('pdf', 'docx')", name="file_type"),
    CheckConstraint("btrim(filename) <> ''", name="filename"),
)
document_versions = Table(
    "document_versions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("document_id", Uuid, ForeignKey("app.documents.id", ondelete="CASCADE"), nullable=False),
    Column("status", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("pipeline_fingerprint", String(64), nullable=False),
    Column("object_key", Text),
    Column("object_version_id", Text),
    Column("embedding_model_id", Text, nullable=False),
    Column("embedding_dimensions", Integer, nullable=False),
    Column("user_id", Uuid, ForeignKey("app.users.id")),
    Column("corpus", Text, nullable=False, server_default=""),
    Column("retired_at", DateTime(timezone=True)),
    Column("pipeline_configuration", JSONB, nullable=False, server_default="{}"),
    Column("manifest_complete", Boolean, nullable=False, server_default="false"),
    Column("filename", Text),
    Column("file_type", Text),
    Column("title", Text),
    Column("project_metadata", JSONB, nullable=False, server_default="{}"),
    *_timestamps(),
    UniqueConstraint("id", "document_id"),
    CheckConstraint("status IN ('candidate', 'published', 'superseded', 'failed')", name="status"),
    CheckConstraint("embedding_dimensions = 1024", name="embedding_dimensions"),
)
Index(
    "uq_document_versions_live_content",
    document_versions.c.user_id,
    document_versions.c.corpus,
    document_versions.c.sha256,
    document_versions.c.pipeline_fingerprint,
    unique=True,
    postgresql_where=document_versions.c.retired_at.is_(None),
)
documents.append_constraint(
    ForeignKeyConstraint(
        ["published_version_id", "id"],
        ["app.document_versions.id", "app.document_versions.document_id"],
        name="fk_documents_published_version",
        use_alter=True,
        deferrable=True,
        initially="DEFERRED",
    )
)
document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column(
        "version_id",
        Uuid,
        ForeignKey("app.document_versions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIMENSIONS)),
    Column("title", Text),
    Column("filename", Text, nullable=False),
    Column("file_type", Text, nullable=False),
    Column("page", Integer),
    Column("section_heading", Text),
    Column("locators", JSONB, nullable=False, server_default="[]"),
    Column("status", Text, nullable=False),
    Column("embedding_model_id", Text, nullable=False),
    Column("content_hash", String(64)),
    Column("start_offset", Integer),
    Column("end_offset", Integer),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("cycle_attempts", Integer, nullable=False, server_default="0"),
    Column("next_retry_at", DateTime(timezone=True)),
    Column("error_category", Text),
    *_timestamps(),
    UniqueConstraint("version_id", "ordinal"),
    CheckConstraint("ordinal >= 0", name="ordinal"),
    CheckConstraint("page IS NULL OR page > 0", name="page"),
    CheckConstraint(
        "status IN ('pending', 'processing', 'retrying', 'completed', 'failed')", name="status"
    ),
    CheckConstraint("status <> 'completed' OR embedding IS NOT NULL", name="completed_embedding"),
    CheckConstraint("jsonb_typeof(locators) = 'array'", name="locators_array"),
    CheckConstraint("btrim(filename) <> '' AND btrim(text) <> ''", name="required_metadata"),
    CheckConstraint("file_type IN ('pdf', 'docx')", name="file_type"),
)
Index(
    "ix_document_chunks_embedding",
    document_chunks.c.embedding,
    postgresql_using="hnsw",
    postgresql_ops={"embedding": "vector_cosine_ops"},
)
Index("ix_documents_visibility", documents.c.user_id, documents.c.visibility, documents.c.lifecycle)

ingestion_jobs = Table(
    "ingestion_jobs",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("document_id", Uuid, ForeignKey("app.documents.id"), nullable=False),
    Column("version_id", Uuid, ForeignKey("app.document_versions.id")),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("stage", Text, nullable=False, server_default="extraction"),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("cycle_attempts", Integer, nullable=False, server_default="0"),
    Column("retry_cycle", Integer, nullable=False, server_default="0"),
    Column("generation", BigInteger, nullable=False, server_default="0"),
    Column("lease_owner", Text),
    Column("lease_until", DateTime(timezone=True)),
    Column("next_retry_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("error_category", Text),
    Column("total_chunks", Integer),
    Column("trace_context", JSONB, nullable=False, server_default="{}"),
    *_timestamps(),
    CheckConstraint("kind IN ('index', 'delete', 'superseded_cleanup')", name="kind"),
    CheckConstraint(
        "status IN ('queued', 'processing', 'retrying', 'ready', 'failed', 'cancelled')",
        name="status",
    ),
    CheckConstraint(
        "stage IN ('extraction', 'embedding', 'publication', 'cleanup', 'done')", name="stage"
    ),
    CheckConstraint("attempts >= 0 AND cycle_attempts >= 0 AND generation >= 0", name="counters"),
    CheckConstraint("(lease_owner IS NULL) = (lease_until IS NULL)", name="lease_pair"),
    CheckConstraint("kind = 'delete' OR version_id IS NOT NULL", name="version_required"),
)
Index(
    "ix_ingestion_jobs_due",
    ingestion_jobs.c.next_retry_at,
    ingestion_jobs.c.lease_until,
    postgresql_where=ingestion_jobs.c.status.in_(("queued", "processing", "retrying")),
)
Index(
    "uq_ingestion_jobs_index_version",
    ingestion_jobs.c.version_id,
    unique=True,
    postgresql_where=ingestion_jobs.c.kind == "index",
)
Index(
    "uq_ingestion_jobs_active_index",
    ingestion_jobs.c.document_id,
    unique=True,
    postgresql_where=(ingestion_jobs.c.kind == "index")
    & ingestion_jobs.c.status.in_(("queued", "processing", "retrying")),
)
Index(
    "uq_ingestion_jobs_delete",
    ingestion_jobs.c.document_id,
    unique=True,
    postgresql_where=ingestion_jobs.c.kind == "delete",
)

upload_requests = Table(
    "upload_requests",
    metadata,
    Column("user_id", Uuid, ForeignKey("app.users.id"), primary_key=True),
    Column("request_key", Text, primary_key=True),
    Column("request_fingerprint", String(64), nullable=False),
    Column("document_id", Uuid, ForeignKey("app.documents.id"), nullable=False),
    Column("version_id", Uuid, ForeignKey("app.document_versions.id"), nullable=False),
    Column("job_id", Uuid, ForeignKey("app.ingestion_jobs.id"), nullable=False),
    *_timestamps(),
    CheckConstraint("char_length(request_key) BETWEEN 1 AND 200", name="request_key"),
)

vendor_permits = Table(
    "vendor_permits",
    metadata,
    Column("slot", Integer, primary_key=True),
    Column("token", Uuid),
    Column("lease_until", DateTime(timezone=True)),
    CheckConstraint("slot > 0", name="slot"),
    CheckConstraint("(token IS NULL) = (lease_until IS NULL)", name="lease_pair"),
)
