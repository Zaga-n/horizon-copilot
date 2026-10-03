"""Titan responses of the wrong shape are protocol failures; a bad model id is an outage."""

import io
import json
from collections.abc import Iterator

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

from horizon_genai import (
    BedrockConnection,
    bedrock_runtime_client,
    build_bedrock_embeddings,
)
from horizon_ingestion.application.failures import failure_category
from horizon_ingestion.application.process_job import Ready, Retrying, process_job
from horizon_ingestion.domain.documents import ErrorCategory, JobKind
from horizon_ingestion.domain.retry_policy import Retry, decide_job_failure
from horizon_ingestion.genai.embeddings import TitanEmbeddings
from horizon_ingestion.ports.indexing import EmbeddingProtocolError, EmbeddingUnavailableError
from horizon_ingestion_testing.jobs import (
    POLICY,
    RecordingIndex,
    RecordingQueue,
    index_claim,
    job_context,
    quiet_job_telemetry,
)

MODEL_ID = "amazon.titan-embed-text-v2:0"


@pytest.fixture
def client() -> Iterator[BedrockRuntimeClient]:
    runtime = bedrock_runtime_client(
        BedrockConnection(region="eu-west-1", connect_timeout_seconds=1, read_timeout_seconds=1)
    )
    yield runtime
    runtime.close()


def titan(client: BedrockRuntimeClient) -> TitanEmbeddings:
    return TitanEmbeddings(
        model=build_bedrock_embeddings(client=client, model_id=MODEL_ID, dimensions=2),
        model_id=MODEL_ID,
        dimensions=2,
        telemetry=quiet_job_telemetry(),
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([0.6, 0.8], id="list-body"),
        pytest.param({"embedding": ["0.6", "0.8"]}, id="string-elements"),
        pytest.param({"vector": [0.6, 0.8]}, id="missing-embedding"),
    ],
)
async def test_malformed_success_bodies_are_protocol_failures(
    client: BedrockRuntimeClient, body: object
) -> None:
    payload = json.dumps(body).encode()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(payload), len(payload)), "contentType": "json"},
        )
        with pytest.raises(EmbeddingProtocolError) as caught:
            await titan(client).embed(text="Horizon")
    assert failure_category(caught.value) == ErrorCategory.PROTOCOL


async def test_an_invalid_model_identifier_is_retried_as_unavailable(
    client: BedrockRuntimeClient,
) -> None:
    with Stubber(client) as stub:
        stub.add_client_error(
            "invoke_model",
            service_error_code="ValidationException",
            service_message="The provided model identifier is invalid.",
            http_status_code=400,
        )
        with pytest.raises(EmbeddingUnavailableError) as caught:
            await titan(client).embed(text="Horizon")
    category = failure_category(caught.value)
    assert category == ErrorCategory.PROVIDER
    decision = decide_job_failure(
        category=category, kind=JobKind.INDEX, attempt=1, policy=POLICY, jitter=lambda _, high: high
    )
    assert isinstance(decision, Retry)


async def test_throttled_indexing_retries_the_chunk_and_publishes(
    client: BedrockRuntimeClient,
) -> None:
    index = RecordingIndex()
    queue = RecordingQueue()
    context = job_context(embeddings=titan(client), queue=queue, index=index)
    payload = json.dumps({"embedding": [0.6, 0.8]}).encode()
    with Stubber(client) as stub:
        stub.add_client_error(
            "invoke_model", service_error_code="ThrottlingException", http_status_code=429
        )
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(payload), len(payload)), "contentType": "json"},
        )
        outcome = await process_job(claim=index_claim(), context=context)
        stub.assert_no_pending_responses()
    assert isinstance(outcome, Ready)
    assert index.completed == list(index.chunk_ids)
    assert index.published
    assert queue.failures == []


async def test_persistent_indexing_throttle_stops_at_the_call_budget(
    client: BedrockRuntimeClient,
) -> None:
    index = RecordingIndex()
    queue = RecordingQueue()
    context = job_context(embeddings=titan(client), queue=queue, index=index)
    with Stubber(client) as stub:
        for _ in range(3):
            stub.add_client_error(
                "invoke_model", service_error_code="ThrottlingException", http_status_code=429
            )
        outcome = await process_job(claim=index_claim(), context=context)
        stub.assert_no_pending_responses()
    assert isinstance(outcome, Retrying)
    assert outcome.category == ErrorCategory.PROVIDER
    assert index.completed == []
    assert not index.published
