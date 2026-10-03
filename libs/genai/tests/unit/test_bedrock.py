"""Bedrock classification table, connection policy and usage capture, without contacting AWS."""

import io
import json
from collections.abc import Iterator

import pytest
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from botocore.response import StreamingBody
from botocore.stub import Stubber
from langchain_aws import BedrockEmbeddings, ChatBedrockConverse
from mypy_boto3_bedrock_runtime import BedrockRuntimeClient
from pydantic import SecretStr

from horizon_genai import (
    BedrockConnection,
    GenAIError,
    GenAIProtocolError,
    GenAIProviderRejectedError,
    GenAIProviderUnavailableError,
    bedrock_runtime_client,
    build_bedrock_chat_model,
    build_bedrock_embeddings,
    checked_vector,
    classify_bedrock_error,
    observe_titan_usage,
)

CONNECTION = BedrockConnection(
    region="eu-west-1",
    connect_timeout_seconds=5,
    read_timeout_seconds=30,
    aws_access_key_id=SecretStr("synthetic-key"),
    aws_secret_access_key=SecretStr("synthetic-secret"),
)


def client_error(
    code: str, status: int, *, retry_after: str = "", message: str = "PRIVATE"
) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {"retry-after": retry_after} if retry_after else {},
                "HostId": "",
                "RequestId": "",
                "RetryAttempts": 0,
            },
        },
        "InvokeModel",
    )


def retries(client: BedrockRuntimeClient) -> object:
    return vars(client.meta.config)["retries"]  # untyped in botocore stubs


@pytest.fixture
def client() -> Iterator[BedrockRuntimeClient]:
    runtime = bedrock_runtime_client(CONNECTION)
    yield runtime
    runtime.close()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(client_error("ThrottlingException", 429), GenAIProviderUnavailableError),
        pytest.param(client_error("InternalServerException", 500), GenAIProviderUnavailableError),
        pytest.param(client_error("ModelTimeoutException", 408), GenAIProviderUnavailableError),
        pytest.param(client_error("AccessDeniedException", 403), GenAIProviderUnavailableError),
        pytest.param(
            client_error("UnrecognizedClientException", 403), GenAIProviderUnavailableError
        ),
        pytest.param(client_error("ExpiredTokenException", 403), GenAIProviderUnavailableError),
        pytest.param(client_error("ResourceNotFoundException", 404), GenAIProviderUnavailableError),
        pytest.param(client_error("ValidationException", 400), GenAIProviderRejectedError),
        pytest.param(EndpointConnectionError(endpoint_url="x"), GenAIProviderUnavailableError),
        pytest.param(ReadTimeoutError(endpoint_url="x"), GenAIProviderUnavailableError),
        pytest.param(NoCredentialsError(), GenAIProviderUnavailableError),
        pytest.param(ValueError("Error raised by inference endpoint"), GenAIProtocolError),
    ],
    ids=lambda value: (
        str(value.response.get("Error", {}).get("Code"))
        if isinstance(value, ClientError)
        else type(value).__name__
    ),
)
def test_bedrock_failures_classify_by_what_is_wrong(
    failure: Exception, expected: type[GenAIError]
) -> None:
    assert type(classify_bedrock_error(failure)) is expected


def test_retry_after_hint_is_kept_and_unrelated_errors_are_not_classified() -> None:
    throttled = classify_bedrock_error(client_error("ThrottlingException", 429, retry_after="7"))
    assert isinstance(throttled, GenAIProviderUnavailableError)
    assert throttled.retry_after_seconds == 7
    assert classify_bedrock_error(RuntimeError("bug")) is None


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        pytest.param(
            "The provided model identifier is invalid.",
            GenAIProviderUnavailableError,
            id="invalid-model-identifier",
        ),
        pytest.param(
            "Invocation of model ID amazon.titan-embed-text-v2:0 with on-demand throughput "
            "isn't supported. Retry your request with the ID or ARN of an inference profile "
            "that contains this model.",
            GenAIProviderUnavailableError,
            id="on-demand-throughput",
        ),
        pytest.param(
            "The requested Model ID is not supported in this region.",
            GenAIProviderUnavailableError,
            id="model-id-region",
        ),
        pytest.param(
            "400 Bad Request: Too many input tokens. Max input tokens: 8192, "
            "request input token count: 9001",
            GenAIProviderRejectedError,
            id="input-too-long",
        ),
        pytest.param("PRIVATE", GenAIProviderRejectedError, id="unknown-message"),
    ],
)
def test_validation_naming_the_model_identifier_is_unavailable(
    message: str, expected: type[GenAIError]
) -> None:
    failure = client_error("ValidationException", 400, message=message)
    assert type(classify_bedrock_error(failure)) is expected


@pytest.mark.parametrize(
    "vector",
    [
        pytest.param([1.0], id="short"),
        pytest.param([1.0, float("nan")], id="nan"),
        pytest.param([1.0, float("inf")], id="inf"),
        pytest.param(["0.6", "0.8"], id="string-elements"),
        pytest.param([[0.6, 0.8]], id="nested-list"),
        pytest.param([True, False], id="booleans"),
        pytest.param({"embedding": [0.6, 0.8]}, id="object-body"),
        pytest.param("ab", id="string-body"),
    ],
)
def test_unusable_vectors_are_protocol_failures(vector: object) -> None:
    with pytest.raises(GenAIProtocolError):
        checked_vector(vector, dimensions=2)


def test_static_credentials_and_one_attempt_reach_the_runtime_client(
    client: BedrockRuntimeClient,
) -> None:
    # botocore exposes no public getter for the signer's credentials.
    credentials = client._request_signer._credentials  # type: ignore[attr-defined]
    assert (credentials.access_key, credentials.secret_key) == ("synthetic-key", "synthetic-secret")
    assert retries(client) == {"total_max_attempts": 1, "mode": "legacy"}


async def test_embeddings_are_normalized_and_report_titan_usage(
    client: BedrockRuntimeClient,
) -> None:
    usage: list[int] = []
    observe_titan_usage(client=client, on_usage=usage.append)
    embeddings = build_bedrock_embeddings(
        client=client, model_id="amazon.titan-embed-text-v2:0", dimensions=2
    )
    assert isinstance(embeddings, BedrockEmbeddings)
    payload = json.dumps({"embedding": [0.6, 0.8], "inputTextTokenCount": 4}).encode()
    with Stubber(client) as stub:
        stub.add_response(
            "invoke_model",
            {"body": StreamingBody(io.BytesIO(payload), len(payload)), "contentType": "json"},
        )
        vector = await embeddings.aembed_query("Horizon")
    assert checked_vector(vector, dimensions=2) == pytest.approx((0.6, 0.8))
    assert embeddings.normalize
    assert usage == [4]


def test_chat_models_share_the_connection_policy() -> None:
    model = build_bedrock_chat_model(
        connection=CONNECTION,
        model_id="global.openai.gpt-5.6-luna",
        reasoning_effort="none",
        max_tokens=512,
        streaming=False,
        tags=("utility",),
    )
    assert isinstance(model, ChatBedrockConverse)
    assert model.model_id == "global.openai.gpt-5.6-luna"
    assert model.disable_streaming
    assert retries(model.client) == {"total_max_attempts": 1, "mode": "legacy"}
