"""Published pgvector evidence is visible only to its owner or a shared corpus reader."""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.domain.retrieval import RetrievalIntegrityError, RetrievalPolicy
from horizon_chat_testing.telemetry import quiet_telemetry

pytestmark = pytest.mark.integration
VECTOR = (1.0, *(0.0 for _ in range(1023)))
POLICY = RetrievalPolicy(max_chunks=8, max_excerpt_chars=20, max_evidence_chars=30)
GRANTS_SQL = (
    Path(__file__).resolve().parents[4] / "services/migrations/sql/runtime_grants.sql"
).read_text()


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchDatabase:
    """Seed committed uploads separately from the read-only chat connection."""

    index: PgvectorEvidenceIndex
    admin: psycopg.AsyncConnection[tuple[object, ...]]

    async def document(
        self,
        *,
        subject: str,
        visibility: str = "private",
        lifecycle: str = "live",
        version_status: str = "published",
        content: str = "Horizon project evidence",
        locators: str = '[{"page":12},{"page":13}]',
    ) -> UUID:
        document_id, version_id, chunk_id, user_id = (uuid4() for _ in range(4))
        async with self.admin.transaction():
            await self.admin.execute(
                "INSERT INTO app.users (id, subject) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, subject),
            )
            await self.admin.execute(
                """INSERT INTO app.documents (id, user_id, visibility, lifecycle, filename, file_type)
                SELECT %s, id, %s, %s, 'project.pdf', 'pdf' FROM app.users WHERE subject = %s""",
                (document_id, visibility, lifecycle, subject),
            )
            await self.admin.execute(
                """INSERT INTO app.document_versions
                (id, document_id, status, sha256, pipeline_fingerprint, object_key, object_version_id,
                embedding_model_id, embedding_dimensions)
                VALUES (%s, %s, %s, %s, %s, 'project.pdf', 'immutable-1', 'titan', 1024)""",
                (version_id, document_id, version_status, "a" * 64, "b" * 64),
            )
            await self.admin.execute(
                """INSERT INTO app.document_chunks
                (id, version_id, ordinal, text, embedding, filename, file_type, page, locators,
                status, embedding_model_id)
                VALUES (%s, %s, 0, %s, %s::vector, 'project.pdf', 'pdf', 12,
                %s, 'completed', 'titan')""",
                (chunk_id, version_id, content, "[" + ",".join(map(str, VECTOR)) + "]", locators),
            )
            if version_status == "published":
                await self.admin.execute(
                    "UPDATE app.documents SET published_version_id = %s WHERE id = %s",
                    (version_id, document_id),
                )
        return chunk_id


@pytest.fixture
async def database(migrated_database: str) -> AsyncIterator[SearchDatabase]:
    async with await psycopg.AsyncConnection.connect(migrated_database, autocommit=True) as admin:
        await admin.execute(GRANTS_SQL)
        engine = create_async_engine(
            make_url(migrated_database).set(drivername="postgresql+psycopg"),
            connect_args={"options": "-crole=chat_runtime"},
        )
        try:
            yield SearchDatabase(
                index=PgvectorEvidenceIndex(
                    engine=engine, embedding_model_id="titan", telemetry=quiet_telemetry()
                ),
                admin=admin,
            )
        finally:
            await engine.dispose()


async def test_private_upload_is_hidden_and_source_metadata_is_preserved(
    database: SearchDatabase,
) -> None:
    chunk = await database.document(subject="alice")
    assert await database.index.search(subject="bob", vector=VECTOR, k=5, policy=POLICY) == ()
    results = await database.index.search(subject="alice", vector=VECTOR, k=5, policy=POLICY)
    assert len(results) == 1
    assert results[0].source.chunk_id == chunk
    assert results[0].source.title == "project.pdf"
    assert results[0].source.page == 12
    assert [locator.page for locator in results[0].source.locators] == [12, 13]
    assert results[0].excerpt == "Horizon project evid"
    assert results[0].score == pytest.approx(1)


async def test_shared_upload_can_be_read_by_another_subject(database: SearchDatabase) -> None:
    chunk = await database.document(subject="alice", visibility="shared")
    results = await database.index.search(subject="bob", vector=VECTOR, k=5, policy=POLICY)
    assert [hit.source.chunk_id for hit in results] == [chunk]


@pytest.mark.parametrize(
    ("lifecycle", "version_status"),
    [
        pytest.param("deleting", "published", id="deletion-accepted"),
        pytest.param("deleted", "published", id="deleted"),
        pytest.param("live", "candidate", id="candidate"),
        pytest.param("live", "failed", id="failed-ingestion"),
    ],
)
async def test_unavailable_document_lifecycle_is_not_searchable(
    database: SearchDatabase,
    lifecycle: str,
    version_status: str,
) -> None:
    await database.document(subject="alice", lifecycle=lifecycle, version_status=version_status)
    assert await database.index.search(subject="alice", vector=VECTOR, k=5, policy=POLICY) == ()


async def test_empty_index_returns_no_evidence(database: SearchDatabase) -> None:
    assert await database.index.search(subject="alice", vector=VECTOR, k=5, policy=POLICY) == ()


async def test_deduplication_and_total_evidence_are_bounded(database: SearchDatabase) -> None:
    await database.document(subject="alice", content="Identical content text")
    await database.document(subject="alice", content="Identical content text")
    await database.document(subject="alice", content="Different evidence text")
    results = await database.index.search(subject="alice", vector=VECTOR, k=8, policy=POLICY)
    assert len(results) == 2
    assert sum(len(hit.excerpt) for hit in results) == 30


async def test_incompatible_query_vector_is_rejected(database: SearchDatabase) -> None:
    with pytest.raises(RetrievalIntegrityError, match="embedding_dimensions_or_values"):
        await database.index.search(subject="alice", vector=(1.0,), k=5, policy=POLICY)


async def test_readiness_rejects_incompatible_published_index(
    database: SearchDatabase,
    migrated_database: str,
) -> None:
    from psycopg.rows import DictRow, dict_row
    from psycopg_pool import AsyncConnectionPool
    from pydantic import SecretStr

    from horizon_chat.db.readiness import DatabaseReadiness
    from horizon_migrations.db.checkpoints import setup_checkpoints

    await setup_checkpoints(dsn=SecretStr(migrated_database), expected_role="postgres")
    pool = AsyncConnectionPool[psycopg.AsyncConnection[DictRow]](
        migrated_database,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row, "options": "-csearch_path=langgraph"},
    )
    await pool.open(wait=True)
    try:
        chunk = await database.document(subject="alice")
        readiness = DatabaseReadiness(
            engine=database.index.engine, checkpoints=pool, embedding_model_id="titan"
        )
        assert await readiness.check()
        await database.admin.execute(
            "UPDATE app.document_chunks SET embedding_model_id='different' WHERE id=%s", (chunk,)
        )
        assert not await readiness.check()
        assert await database.index.search(subject="alice", vector=VECTOR, k=5, policy=POLICY) == ()
    finally:
        await pool.close()


async def test_unknown_locator_keys_are_read_and_a_corrupt_row_is_skipped(
    database: SearchDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    extended = await database.document(
        subject="alice", content="Extended", locators='[{"page":3,"sheet":"Budget"}]'
    )
    corrupt = await database.document(
        subject="alice", content="Corrupt", locators='[{"page":"twelve"}]'
    )
    with caplog.at_level(logging.ERROR):
        results = await database.index.search(subject="alice", vector=VECTOR, k=5, policy=POLICY)
    assert [hit.source.chunk_id for hit in results] == [extended]
    assert [locator.page for locator in results[0].source.locators] == [3]
    assert [(record.msg, getattr(record, "chunk_id", None)) for record in caplog.records] == [
        ("retrieval_row_corrupt", str(corrupt))
    ]
