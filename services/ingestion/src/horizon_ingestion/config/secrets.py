"""Masked ingestion credentials loaded only by bootstrap."""

from typing import Annotated, Self

from pydantic import AfterValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def _postgres_dsn(value: SecretStr) -> SecretStr:
    try:
        url = make_url(value.get_secret_value())
    except ArgumentError:
        raise ValueError("database DSN must be a PostgreSQL URL") from None
    if (
        url.drivername not in ("postgresql", "postgresql+psycopg")
        or not url.host
        or not url.database
    ):
        raise ValueError("database DSN must specify PostgreSQL host and database")
    return value


class Secrets(BaseSettings):
    """Independent bucket credentials and ingestion-only database login."""

    model_config = SettingsConfigDict(
        env_prefix="INGESTION_",
        env_file=".env",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    database_dsn: Annotated[SecretStr, Field(min_length=1), AfterValidator(_postgres_dsn)]
    minio_access_key: Annotated[SecretStr, Field(min_length=1)]
    minio_secret_key: Annotated[SecretStr, Field(min_length=1)]
    # Static Bedrock credentials; unset means the default AWS credential chain.
    aws_access_key_id: SecretStr | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_session_token: SecretStr | None = None

    @model_validator(mode="after")
    def complete_aws_credentials(self) -> Self:
        if (self.aws_access_key_id is None) != (self.aws_secret_access_key is None):
            raise ValueError("AWS access key and secret key must be supplied together")
        if self.aws_session_token is not None and self.aws_access_key_id is None:
            raise ValueError("AWS session token requires access and secret keys")
        return self


def load_secrets() -> Secrets:
    return Secrets()
