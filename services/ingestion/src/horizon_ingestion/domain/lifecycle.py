"""Job, version and document lifecycle decisions that every store applies the same way."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from horizon_ingestion.domain.documents import (
    ChunkState,
    JobKind,
    JobState,
    Lifecycle,
    Stage,
    VersionState,
)

ACTIVE_STATES = frozenset({JobState.QUEUED, JobState.PROCESSING, JobState.RETRYING})


class RetryDecision(StrEnum):
    UNAVAILABLE = "unavailable"  # deleted, superseded or not an indexing job
    NOT_FAILED = "not_failed"  # nothing to restart; report the current status
    RESTART = "restart"


def retry_decision(
    *, status: JobState, kind: JobKind, lifecycle: Lifecycle, retired: bool
) -> RetryDecision:
    """Only a failed indexing job of a live, unretired version restarts."""
    if kind != JobKind.INDEX or lifecycle != Lifecycle.LIVE or retired:
        return RetryDecision.UNAVAILABLE
    if status != JobState.FAILED:
        return RetryDecision.NOT_FAILED
    return RetryDecision.RESTART


def retry_available(
    *, status: JobState, kind: JobKind, lifecycle: Lifecycle, retired: bool
) -> bool:
    """The one rule that status polling and upload replay both report."""
    return (
        retry_decision(status=status, kind=kind, lifecycle=lifecycle, retired=retired)
        == RetryDecision.RESTART
    )


def claim_lifecycle(kind: JobKind) -> Lifecycle:
    """The document lifecycle a claim of this kind requires; anything else fences it."""
    return Lifecycle.DELETING if kind == JobKind.DELETE else Lifecycle.LIVE


def delete_available(lifecycle: Lifecycle) -> bool:
    """False means deletion already began; repeating it changes nothing."""
    return lifecycle == Lifecycle.LIVE


def manifest_publishable(
    *, manifest_complete: bool, total_chunks: int | None, stored_chunks: int, unfinished_chunks: int
) -> bool:
    """A version publishes only with its whole manifest stored and every chunk embedded."""
    return (
        manifest_complete
        and unfinished_chunks == 0
        and stored_chunks > 0
        and total_chunks == stored_chunks
    )


def superseded_version(*, published_version_id: UUID | None, candidate_id: UUID) -> UUID | None:
    """The previously published version that publishing the candidate retires, if any."""
    if published_version_id is None or published_version_id == candidate_id:
        return None
    return published_version_id


@dataclass(frozen=True, slots=True, kw_only=True)
class JobStart:
    """The states a new job is inserted with."""

    status: JobState
    stage: Stage


def new_job(kind: JobKind) -> JobStart:
    """Index jobs start with extraction; cleanup jobs go straight to cleanup."""
    return JobStart(
        status=JobState.QUEUED,
        stage=Stage.EXTRACTION if kind == JobKind.INDEX else Stage.CLEANUP,
    )


NEW_VERSION_STATE = VersionState.CANDIDATE
NEW_CHUNK_STATE = ChunkState.PENDING


def chunk_stage(*, unfinished_chunks: int) -> Stage:
    """The job stage after a chunk completes."""
    return Stage.PUBLICATION if unfinished_chunks == 0 else Stage.EMBEDDING


@dataclass(frozen=True, slots=True, kw_only=True)
class RestartStates:
    """A restarted job, its unfinished chunks and its version; also a re-queued cleanup."""

    job: JobState
    chunks: ChunkState
    version: VersionState


RESTART = RestartStates(
    job=JobState.QUEUED, chunks=ChunkState.PENDING, version=VersionState.CANDIDATE
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicationStates:
    """A published candidate retires the version it replaces and finishes its job."""

    candidate: VersionState
    replaced: VersionState
    job: JobState
    stage: Stage


PUBLICATION = PublicationStates(
    candidate=VersionState.PUBLISHED,
    replaced=VersionState.SUPERSEDED,
    job=JobState.READY,
    stage=Stage.DONE,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DeletionStates:
    """Deletion hides the document at once and cancels its other jobs."""

    document: Lifecycle
    other_jobs: JobState


DELETION = DeletionStates(document=Lifecycle.DELETING, other_jobs=JobState.CANCELLED)


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanupScope:
    """What one cleanup job removes."""

    single_retired_version: bool  # superseded cleanup touches only its own retired version
    deletes_document: bool


def cleanup_scope(kind: JobKind) -> CleanupScope:
    return CleanupScope(
        single_retired_version=kind == JobKind.SUPERSEDED_CLEANUP,
        deletes_document=kind == JobKind.DELETE,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class CleanupStates:
    """A finished cleanup: its job, the cancelled index jobs and the document lifecycle."""

    job: JobState
    stage: Stage
    index_jobs: JobState
    document: Lifecycle | None  # None: the document stays as it is (superseded cleanup)


def cleanup_states(kind: JobKind) -> CleanupStates:
    return CleanupStates(
        job=JobState.READY,
        stage=Stage.DONE,
        index_jobs=JobState.CANCELLED,
        document=Lifecycle.DELETED if cleanup_scope(kind).deletes_document else None,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class FailureSettlement:
    """A failed job's state; a terminal index failure also fails its version and open chunks."""

    state: JobState
    version: VersionState | None  # None: the version and its chunks are left as they are
    chunks: ChunkState | None


def failure_settlement(*, kind: JobKind, retrying: bool) -> FailureSettlement:
    fails_version = kind == JobKind.INDEX and not retrying
    return FailureSettlement(
        state=JobState.RETRYING if retrying else JobState.FAILED,
        version=VersionState.FAILED if fails_version else None,
        chunks=ChunkState.FAILED if fails_version else None,
    )
