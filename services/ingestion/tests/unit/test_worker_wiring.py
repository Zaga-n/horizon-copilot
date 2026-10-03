"""Worker bootstrap maps deployment credentials into the shared Bedrock connection."""

import pytest
from pydantic import SecretStr, ValidationError

from horizon_genai import bedrock_runtime_client
from horizon_ingestion.bootstrap.worker import bedrock_connection
from horizon_ingestion.config.secrets import Secrets
from horizon_ingestion_testing.settings import deployment_settings


def secrets(
    *, key: str | None = None, secret: str | None = None, token: str | None = None
) -> Secrets:
    return Secrets(
        database_dsn=SecretStr("postgresql://ingestion:secret@localhost/horizon"),
        minio_access_key=SecretStr("minio"),
        minio_secret_key=SecretStr("minio-secret"),
        aws_access_key_id=SecretStr(key) if key else None,
        aws_secret_access_key=SecretStr(secret) if secret else None,
        aws_session_token=SecretStr(token) if token else None,
    )


def test_static_credentials_reach_the_embedding_client() -> None:
    connection = bedrock_connection(
        deployment_settings(),
        secrets(key="static-key", secret="static-secret"),
    )
    client = bedrock_runtime_client(connection)
    try:
        # botocore exposes no public getter for the signer's credentials.
        credentials = client._request_signer._credentials  # type: ignore[attr-defined]
        assert (credentials.access_key, credentials.secret_key) == ("static-key", "static-secret")
    finally:
        client.close()


def test_absent_credentials_use_the_default_chain() -> None:
    connection = bedrock_connection(deployment_settings(), secrets())
    assert connection.aws_access_key_id is None
    assert connection.aws_secret_access_key is None


def test_partial_static_credentials_block_startup() -> None:
    with pytest.raises(ValidationError, match="supplied together"):
        secrets(key="only-key")
    with pytest.raises(ValidationError, match="session token"):
        secrets(token="only-token")
