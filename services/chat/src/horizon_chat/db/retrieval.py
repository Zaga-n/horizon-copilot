"""Parameterized pgvector search applies publication and ownership before ranking."""

import logging
import math
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import RowMapping, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from horizon_chat.db.tables import CHUNKS, DOCUMENTS, USERS, VERSIONS
from horizon_chat.domain.retrieval import (
    RetrievalIntegrityError,
    RetrievalPolicy,
    RetrievalUnavailableError,
    SearchHit,
)
from horizon_chat.domain.sources import Source
from horizon_chat.observability.tracing import Telemetry
from horizon_schema import EMBEDDING_DIMENSIONS

logger = logging.getLogger(__name__)


def _source(row: RowMapping) -> Source | None:
    """The citation source of a stored chunk; None when the row violates the index contract."""
    try:
        return Source.model_validate(
            {
                "marker": str(row["id"]),
                "chunk_id": row["id"],
                "document_id": row["document_id"],
                "document_version_id": row["version_id"],
                "title": row["title"],
                "filename": row["filename"],
                "file_type": row["file_type"],
                "page": row["page"],
                "section_heading": row["section_heading"],
                "locators": row["locators"],
                "start_offset": row["start_offset"],
                "end_offset": row["end_offset"],
            }
        )
    except ValidationError:
        # One corrupt chunk must not fail the turn; the rest of the evidence stays usable.
        logger.exception("retrieval_row_corrupt", extra={"chunk_id": str(row["id"])})
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class PgvectorEvidenceIndex:
    """Returns only completed chunks of a live published version the caller can read."""

    engine: AsyncEngine
    embedding_model_id: str
    telemetry: Telemetry

    async def search(
        self,
        *,
        subject: str,
        vector: tuple[float, ...],
        k: int,
        policy: RetrievalPolicy,
    ) -> tuple[SearchHit, ...]:
        with self.telemetry.work("retrieval") as span:
            if len(vector) != EMBEDDING_DIMENSIONS or not all(
                math.isfinite(value) for value in vector
            ):
                raise RetrievalIntegrityError("embedding_dimensions_or_values")
            distance = CHUNKS.c.embedding.cosine_distance(list(vector))
            query = (
                select(
                    CHUNKS.c.id,
                    DOCUMENTS.c.id.label("document_id"),
                    CHUNKS.c.version_id,
                    func.coalesce(
                        func.nullif(func.btrim(CHUNKS.c.title), ""),
                        func.nullif(func.btrim(DOCUMENTS.c.title), ""),
                        CHUNKS.c.filename,
                    ).label("title"),
                    CHUNKS.c.filename,
                    CHUNKS.c.file_type,
                    CHUNKS.c.page,
                    CHUNKS.c.section_heading,
                    CHUNKS.c.locators,
                    CHUNKS.c.start_offset,
                    CHUNKS.c.end_offset,
                    func.left(CHUNKS.c.text, policy.max_excerpt_chars).label("excerpt"),
                    (1 - distance).label("score"),
                )
                .select_from(
                    CHUNKS.join(VERSIONS, CHUNKS.c.version_id == VERSIONS.c.id)
                    .join(DOCUMENTS, DOCUMENTS.c.published_version_id == VERSIONS.c.id)
                    .join(USERS, DOCUMENTS.c.user_id == USERS.c.id)
                )
                .where(
                    DOCUMENTS.c.lifecycle == "live",
                    VERSIONS.c.status == "published",
                    VERSIONS.c.document_id == DOCUMENTS.c.id,
                    VERSIONS.c.embedding_dimensions == EMBEDDING_DIMENSIONS,
                    CHUNKS.c.status == "completed",
                    CHUNKS.c.embedding.is_not(None),
                    CHUNKS.c.embedding_model_id == self.embedding_model_id,
                    VERSIONS.c.embedding_model_id == self.embedding_model_id,
                    or_(DOCUMENTS.c.visibility == "shared", USERS.c.subject == subject),
                )
                .order_by(distance, CHUNKS.c.id)
                .limit(min(k, policy.max_chunks))
            )
            try:
                async with self.engine.connect() as conn:
                    rows = (await conn.execute(query)).mappings().all()
            except (DBAPIError, PoolTimeoutError, OSError) as exc:
                # The pool timeout and a dropped socket are outages, like a driver failure.
                raise RetrievalUnavailableError("index_unavailable") from exc
            remaining = policy.max_evidence_chars
            hits: list[SearchHit] = []
            seen: set[str] = set()
            for row in rows:
                excerpt = row["excerpt"][:remaining]
                if not excerpt or row["excerpt"] in seen:
                    continue
                source = _source(row)
                if source is None:
                    continue
                seen.add(row["excerpt"])
                remaining -= len(excerpt)
                hits.append(SearchHit(source=source, excerpt=excerpt, score=row["score"]))
            span.set_attributes(
                {
                    "app.retrieval.version": "pgvector-v1",
                    "app.retrieval.embedding_model": self.embedding_model_id,
                    "app.retrieval.k": min(k, policy.max_chunks),
                    "app.retrieval.chunk_ids": tuple(str(hit.source.chunk_id) for hit in hits),
                    "app.retrieval.document_version_ids": tuple(
                        str(hit.source.document_version_id) for hit in hits
                    ),
                    "app.retrieval.scores": tuple(hit.score for hit in hits),
                    "app.retrieval.ranks": tuple(range(1, len(hits) + 1)),
                }
            )
            self.telemetry.measurements.results.record(len(hits))
            return tuple(hits)
