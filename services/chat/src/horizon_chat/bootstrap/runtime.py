"""Acquire resources once and release earlier resources if later startup fails."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial

import httpx
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from horizon_chat.adapters.identity import GoogleIdentityVerifier, LocalIdentityVerifier
from horizon_chat.bootstrap.supervisor import (
    Iteration,
    LoopPolicy,
    ProcessHealth,
    ProcessReadiness,
    SupervisedLoop,
    supervised_loops,
)
from horizon_chat.config.secrets import Secrets
from horizon_chat.config.settings import Settings
from horizon_chat.db.checkpoints import PostgresCheckpointStore
from horizon_chat.db.conversations import SqlConversationStore
from horizon_chat.db.feedback import SqlFeedbackStore
from horizon_chat.db.readiness import DatabaseReadiness
from horizon_chat.db.retention import ConversationRetention
from horizon_chat.db.retrieval import PgvectorEvidenceIndex
from horizon_chat.db.runs import SqlRunLedger
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.retrieval import RetrievalPolicy
from horizon_chat.domain.runs import TurnPolicy
from horizon_chat.genai.horizon_agent.agent import AGENT_VERSION, AgentPolicy, build_agent
from horizon_chat.genai.horizon_agent.llms import build_models
from horizon_chat.genai.horizon_agent.prompts import PROMPT_VERSION
from horizon_chat.genai.horizon_agent.runner import LangChainHorizonAgent
from horizon_chat.genai.retrieval.embeddings import build_query_embeddings
from horizon_chat.genai.retrieval.retriever import RETRIEVAL_VERSION, Retriever
from horizon_chat.observability.logging import configure_logging
from horizon_chat.observability.setup import open_telemetry
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.agent import HorizonAgent
from horizon_chat.ports.identity import IdentityVerifier
from horizon_chat.workers.recovery import recovery_iteration
from horizon_chat.workers.retention import retention_iteration
from horizon_genai import BedrockConnection, bedrock_runtime_client
from horizon_google_identity import GoogleIdTokenVerifier, HttpCertificateRequest


@dataclass(frozen=True, slots=True, kw_only=True)
class Runtime:
    """Typed capabilities exposed to adapters, without raw clients or secrets."""

    readiness: ProcessReadiness
    readiness_timeout_seconds: float
    conversations: SqlConversationStore
    runs: SqlRunLedger
    feedback: SqlFeedbackStore
    identity: IdentityVerifier
    checkpoints: PostgresCheckpointStore
    agent: HorizonAgent
    telemetry: Telemetry
    turn_policy: TurnPolicy
    recovery: RecoveryBuffer


@asynccontextmanager
async def build_runtime(
    settings: Settings, secrets: Secrets, health: ProcessHealth
) -> AsyncIterator[Runtime]:
    configure_logging(
        level=settings.log_level, full_exception_trace=settings.log_full_exception_trace
    )
    async with AsyncExitStack() as stack:
        telemetry = await stack.enter_async_context(
            open_telemetry(
                environment=settings.environment_name,
                endpoint=str(settings.otlp_endpoint) if settings.otlp_endpoint else None,
                instance_id=settings.service_instance_id,
            )
        )
        engine, pool = await _open_databases(settings=settings, secrets=secrets, stack=stack)
        saver = AsyncPostgresSaver(pool)
        checkpoints = PostgresCheckpointStore(saver=saver)
        agent = await _build_agent(
            settings=settings,
            secrets=secrets,
            stack=stack,
            telemetry=telemetry,
            engine=engine,
            saver=saver,
        )
        runs = SqlRunLedger(engine=engine, turn_lease_seconds=settings.turn_lease_seconds)
        recovery = RecoveryBuffer()
        conversations = SqlConversationStore(engine=engine)
        database = DatabaseReadiness(
            engine=engine, checkpoints=pool, embedding_model_id=settings.embedding_model_id
        )
        runtime = Runtime(
            readiness=ProcessReadiness(database=database, health=health),
            readiness_timeout_seconds=settings.readiness_timeout_seconds,
            agent=agent,
            checkpoints=checkpoints,
            telemetry=telemetry,
            conversations=conversations,
            runs=runs,
            feedback=SqlFeedbackStore(engine=engine),
            recovery=recovery,
            turn_policy=_turn_policy(settings),
            identity=_build_identity(settings=settings, stack=stack),
        )
        retention = ConversationRetention(
            engine=engine,
            checkpoints=checkpoints,
            retention_days=settings.retention_days,
            batch_size=settings.maintenance_batch_size,
            checkpoint_timeout_seconds=settings.database_statement_timeout_ms / 1000,
            telemetry=telemetry,
        )
        loops = maintenance_loops(
            settings=settings,
            recovery=partial(recovery_iteration, buffer=recovery, store=runs, telemetry=telemetry),
            retention=partial(retention_iteration, job=retention, readiness=database),
        )
        async with supervised_loops(
            loops, health=health, grace_seconds=settings.maintenance_shutdown_grace_seconds
        ):
            yield runtime


async def _open_databases(
    *, settings: Settings, secrets: Secrets, stack: AsyncExitStack
) -> tuple[AsyncEngine, AsyncConnectionPool[AsyncConnection[DictRow]]]:
    """The application engine and the LangGraph checkpoint pool, both closed by `stack`."""
    engine = create_async_engine(
        make_url(secrets.database_dsn.get_secret_value()).set(drivername="postgresql+psycopg"),
        pool_size=settings.database_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.database_connect_timeout_seconds,
            "options": f"-cstatement_timeout={settings.database_statement_timeout_ms}",
        },
    )
    stack.push_async_callback(engine.dispose)
    pool = AsyncConnectionPool[AsyncConnection[DictRow]](
        secrets.checkpoint_database_dsn.get_secret_value(),
        open=False,
        min_size=1,
        max_size=settings.database_pool_size,
        max_waiting=settings.database_pool_size,
        timeout=settings.database_connect_timeout_seconds,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "options": f"-csearch_path=langgraph -cstatement_timeout={settings.database_statement_timeout_ms}",
        },
    )
    stack.push_async_callback(pool.close)
    await pool.open(wait=True, timeout=settings.database_connect_timeout_seconds)
    return engine, pool


async def _build_agent(
    *,
    settings: Settings,
    secrets: Secrets,
    stack: AsyncExitStack,
    telemetry: Telemetry,
    engine: AsyncEngine,
    saver: AsyncPostgresSaver,
) -> LangChainHorizonAgent:
    """Models, query embeddings, retrieval and the compiled agent graph."""
    connection = BedrockConnection(
        region=settings.aws_region,
        connect_timeout_seconds=settings.provider_connect_timeout_seconds,
        read_timeout_seconds=settings.provider_read_timeout_seconds,
        aws_access_key_id=secrets.aws_access_key_id,
        aws_secret_access_key=secrets.aws_secret_access_key,
        aws_session_token=secrets.aws_session_token,
    )
    # botocore client construction is CPU-bound; keep it off the event loop.
    models = await asyncio.to_thread(
        build_models,
        connection=connection,
        main_model_id=settings.main_model_id,
        main_reasoning_effort=settings.main_reasoning_effort,
        utility_model_id=settings.utility_model_id,
        utility_reasoning_effort=settings.utility_reasoning_effort,
        max_output_tokens=settings.max_output_tokens,
        telemetry=telemetry,
    )
    # Bootstrap owns the runtime client: it is closed with the process's other resources, and
    # its construction blocks, so it runs off the event loop here rather than inside genai/.
    bedrock = await asyncio.to_thread(bedrock_runtime_client, connection)
    stack.callback(bedrock.close)
    embeddings = build_query_embeddings(
        client=bedrock,
        model_id=settings.embedding_model_id,
        dimensions=settings.embedding_dimensions,
    )
    retriever = Retriever(
        rewrite_model=models.utility,
        embeddings=embeddings,
        index=PgvectorEvidenceIndex(
            engine=engine, embedding_model_id=settings.embedding_model_id, telemetry=telemetry
        ),
        policy=RetrievalPolicy(
            max_chunks=settings.max_chunks,
            max_excerpt_chars=settings.max_excerpt_chars,
            max_evidence_chars=settings.max_evidence_chars,
        ),
        dimensions=settings.embedding_dimensions,
        embedding_model_id=settings.embedding_model_id,
        telemetry=telemetry,
    )
    return LangChainHorizonAgent(
        graph=build_agent(
            models=models,
            retriever=retriever,
            checkpointer=saver,
            telemetry=telemetry,
            policy=AgentPolicy(
                max_model_calls=settings.max_model_calls,
                max_tool_calls=settings.max_tool_calls,
                retry_attempts=settings.retry_attempts,
                initial_backoff_seconds=settings.retry_initial_backoff_seconds,
                max_backoff_seconds=settings.retry_max_backoff_seconds,
                summary_trigger_tokens=settings.summary_trigger_tokens,
                summary_keep_messages=settings.summary_keep_messages,
            ),
        ),
        physical_limit=settings.max_physical_model_attempts,
        rewrite_limit=settings.max_rewrite_calls,
        search_limit=settings.max_tool_calls,
        deadline_seconds=settings.turn_deadline_seconds,
        telemetry=telemetry,
    )


def _build_identity(*, settings: Settings, stack: AsyncExitStack) -> IdentityVerifier:
    return (
        LocalIdentityVerifier(subject=settings.local_subject)
        if settings.identity_mode == "local"
        else GoogleIdentityVerifier(
            verifier=GoogleIdTokenVerifier(
                audience=settings.google_client_id,
                request=HttpCertificateRequest(
                    client=stack.enter_context(httpx.Client(timeout=5, trust_env=False))
                ),
            )
        )
    )


def _turn_policy(settings: Settings) -> TurnPolicy:
    return TurnPolicy(
        agent_version=AGENT_VERSION,
        prompt_version=PROMPT_VERSION,
        retrieval_version=RETRIEVAL_VERSION,
        persistence_attempts=settings.retry_attempts,
        persistence_backoff_seconds=settings.retry_initial_backoff_seconds,
        persistence_max_backoff_seconds=settings.retry_max_backoff_seconds,
        # Every bounded failure write plus the longest backoff between them.
        cleanup_timeout_seconds=settings.database_statement_timeout_ms
        / 1000
        * settings.retry_attempts
        + settings.retry_max_backoff_seconds,
    )


def maintenance_loops(
    *, settings: Settings, recovery: Iteration, retention: Iteration
) -> tuple[SupervisedLoop, ...]:
    """Failure reconciliation and retention, supervised for the lifetime of the runtime."""
    return (
        SupervisedLoop(
            name="failure-reconciliation",
            iteration=recovery,
            policy=LoopPolicy(
                interval_seconds=settings.recovery_interval_seconds,
                max_backoff_seconds=settings.maintenance_interval_seconds,
            ),
        ),
        SupervisedLoop(
            name="conversation-retention",
            iteration=retention,
            policy=LoopPolicy(
                interval_seconds=settings.maintenance_interval_seconds,
                max_backoff_seconds=settings.maintenance_interval_seconds,
            ),
        ),
    )
