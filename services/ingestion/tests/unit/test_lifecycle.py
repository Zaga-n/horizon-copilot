"""Lifecycle and acceptance decisions that the stores apply without re-deciding."""

from uuid import uuid4

import pytest

from horizon_ingestion.domain.acceptance import (
    AcceptanceConflict,
    UploadMetadata,
    content_match_conflict,
    replacement_conflict,
    replay_conflict,
    request_fingerprint,
)
from horizon_ingestion.domain.documents import (
    ChunkState,
    JobKind,
    JobState,
    Lifecycle,
    VersionState,
)
from horizon_ingestion.domain.lifecycle import (
    CleanupScope,
    FailureSettlement,
    RetryDecision,
    claim_lifecycle,
    cleanup_scope,
    delete_available,
    failure_settlement,
    manifest_publishable,
    retry_available,
    retry_decision,
    superseded_version,
)


@pytest.mark.parametrize(
    ("status", "kind", "lifecycle", "retired", "decision"),
    [
        (JobState.FAILED, JobKind.INDEX, Lifecycle.LIVE, False, RetryDecision.RESTART),
        (JobState.READY, JobKind.INDEX, Lifecycle.LIVE, False, RetryDecision.NOT_FAILED),
        (JobState.FAILED, JobKind.INDEX, Lifecycle.DELETING, False, RetryDecision.UNAVAILABLE),
        (JobState.FAILED, JobKind.INDEX, Lifecycle.LIVE, True, RetryDecision.UNAVAILABLE),
        (JobState.FAILED, JobKind.DELETE, Lifecycle.DELETING, False, RetryDecision.UNAVAILABLE),
    ],
)
def test_retry_decision_and_availability_agree(
    status: JobState, kind: JobKind, lifecycle: Lifecycle, retired: bool, decision: RetryDecision
) -> None:
    assert (
        retry_decision(status=status, kind=kind, lifecycle=lifecycle, retired=retired) == decision
    )
    assert retry_available(status=status, kind=kind, lifecycle=lifecycle, retired=retired) == (
        decision == RetryDecision.RESTART
    )


def test_claims_require_the_lifecycle_of_their_kind() -> None:
    assert claim_lifecycle(JobKind.INDEX) == Lifecycle.LIVE
    assert claim_lifecycle(JobKind.SUPERSEDED_CLEANUP) == Lifecycle.LIVE
    assert claim_lifecycle(JobKind.DELETE) == Lifecycle.DELETING


def test_deletion_starts_once() -> None:
    assert delete_available(Lifecycle.LIVE)
    assert not delete_available(Lifecycle.DELETING)
    assert not delete_available(Lifecycle.DELETED)


@pytest.mark.parametrize(
    ("complete", "total", "stored", "unfinished", "publishable"),
    [
        (True, 3, 3, 0, True),
        (False, 3, 3, 0, False),
        (True, 3, 3, 1, False),
        (True, 4, 3, 0, False),
        (True, 0, 0, 0, False),
        (True, None, 3, 0, False),
    ],
)
def test_only_complete_embedded_manifests_publish(
    complete: bool, total: int | None, stored: int, unfinished: int, publishable: bool
) -> None:
    assert (
        manifest_publishable(
            manifest_complete=complete,
            total_chunks=total,
            stored_chunks=stored,
            unfinished_chunks=unfinished,
        )
        is publishable
    )


def test_publishing_retires_only_a_different_previous_version() -> None:
    candidate, previous = uuid4(), uuid4()
    assert superseded_version(published_version_id=previous, candidate_id=candidate) == previous
    assert superseded_version(published_version_id=candidate, candidate_id=candidate) is None
    assert superseded_version(published_version_id=None, candidate_id=candidate) is None


def test_cleanup_scope_by_kind() -> None:
    assert cleanup_scope(JobKind.SUPERSEDED_CLEANUP) == CleanupScope(
        single_retired_version=True, deletes_document=False
    )
    assert cleanup_scope(JobKind.DELETE) == CleanupScope(
        single_retired_version=False, deletes_document=True
    )


def test_only_terminal_index_failures_fail_the_version() -> None:
    assert failure_settlement(kind=JobKind.INDEX, retrying=False) == FailureSettlement(
        state=JobState.FAILED, version=VersionState.FAILED, chunks=ChunkState.FAILED
    )
    assert failure_settlement(kind=JobKind.INDEX, retrying=True) == FailureSettlement(
        state=JobState.RETRYING, version=None, chunks=None
    )
    assert failure_settlement(kind=JobKind.DELETE, retrying=False).version is None


def test_replayed_key_must_carry_the_original_input() -> None:
    metadata = UploadMetadata(corpus="project")
    fingerprint = request_fingerprint(sha256="a" * 64, metadata=metadata)
    assert replay_conflict(stored_fingerprint=fingerprint, fingerprint=fingerprint) is None
    other = request_fingerprint(sha256="a" * 64, metadata=UploadMetadata(corpus="other"))
    assert (
        replay_conflict(stored_fingerprint=fingerprint, fingerprint=other)
        == AcceptanceConflict.IDEMPOTENCY_KEY
    )


def test_identical_content_cannot_be_claimed_by_another_document() -> None:
    matched = uuid4()
    assert content_match_conflict(requested_document_id=None, matched_document_id=matched) is None
    assert (
        content_match_conflict(requested_document_id=uuid4(), matched_document_id=matched)
        == AcceptanceConflict.CONTENT_OF_ANOTHER_DOCUMENT
    )


def test_replacement_needs_a_live_idle_document() -> None:
    assert replacement_conflict(lifecycle=Lifecycle.LIVE, has_active_index_job=False) is None
    assert (
        replacement_conflict(lifecycle=Lifecycle.DELETING, has_active_index_job=False)
        == AcceptanceConflict.DOCUMENT_NOT_LIVE
    )
    assert (
        replacement_conflict(lifecycle=Lifecycle.LIVE, has_active_index_job=True)
        == AcceptanceConflict.REPLACEMENT_IN_PROGRESS
    )
