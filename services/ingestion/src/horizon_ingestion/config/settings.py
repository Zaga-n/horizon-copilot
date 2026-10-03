"""Immutable ingestion configuration validated before either process starts."""

import ipaddress
import os
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AnyHttpUrl, Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from horizon_config import (
    ConfigurationError,
    PolicyYamlSource,
    discover_policy_directory,
    policy_layers,
)
from horizon_ingestion.domain.documents import CLAIM_RELEASE_TIMEOUT_SECONDS

type PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type NonEmptyStr = Annotated[str, Field(min_length=1)]
ENV_ONLY_FIELDS = frozenset(
    {
        "environment_name",
        "bind_host",
        "bind_port",
        "frontend_origin",
        "aws_region",
        "embedding_model_id",
        "google_client_id",
        "minio_endpoint",
        "minio_bucket",
    }
)


class Settings(BaseSettings):
    """Deployment topology uses INGESTION_ variables; tunable policy comes from YAML."""

    model_config = SettingsConfigDict(
        env_prefix="INGESTION_",
        env_file=".env",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    environment_name: Literal["local", "staging", "production"]
    bind_host: NonEmptyStr
    bind_port: Annotated[int, Field(ge=1, le=65535)]
    frontend_origin: NonEmptyStr
    aws_region: NonEmptyStr
    embedding_model_id: Literal["amazon.titan-embed-text-v2:0"]
    google_client_id: NonEmptyStr
    minio_endpoint: AnyHttpUrl
    minio_bucket: NonEmptyStr
    identity_mode: Literal["google", "local"]
    local_subject: NonEmptyStr
    embedding_dimensions: Literal[1024]
    database_pool_size: PositiveInt
    database_connect_timeout_seconds: PositiveInt
    database_statement_timeout_ms: PositiveInt
    provider_connect_timeout_seconds: PositiveSeconds
    provider_read_timeout_seconds: PositiveSeconds
    readiness_timeout_seconds: PositiveSeconds
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    log_full_exception_trace: bool
    max_upload_bytes: PositiveInt
    upload_timeout_seconds: PositiveSeconds
    max_extracted_chars: PositiveInt
    max_extraction_units: PositiveInt
    extraction_timeout_seconds: PositiveSeconds
    parser_memory_bytes: PositiveInt
    chunk_timeout_seconds: PositiveSeconds
    job_timeout_seconds: PositiveSeconds
    window_size: PositiveInt
    overlap: Annotated[int, Field(ge=0)]
    worker_concurrency: PositiveInt
    vendor_concurrency: PositiveInt
    scan_interval_seconds: PositiveSeconds
    job_lease_seconds: PositiveSeconds
    heartbeat_interval_seconds: PositiveSeconds
    permit_lease_seconds: PositiveSeconds
    max_call_attempts: PositiveInt
    max_chunk_attempts: PositiveInt
    max_job_attempts: PositiveInt
    shutdown_grace_seconds: PositiveSeconds
    # The deployment's stop grace (compose `stop_grace_period`); the worker refuses to start
    # when its drain budget cannot finish inside it.
    stop_grace_seconds: PositiveSeconds
    retry_initial_backoff_seconds: PositiveSeconds
    retry_max_backoff_seconds: PositiveSeconds
    orphan_grace_seconds: PositiveSeconds
    reconciliation_interval_seconds: PositiveSeconds
    maintenance_batch_size: PositiveInt
    object_prefix: NonEmptyStr
    otlp_endpoint: AnyHttpUrl | None = None
    service_instance_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.identity_mode == "local":
            if self.environment_name != "local":
                raise ValueError("identity_mode local requires environment_name local")
            try:
                loopback = (
                    self.bind_host == "localhost"
                    or ipaddress.ip_address(self.bind_host).is_loopback
                )
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("identity_mode local requires a loopback bind_host")
        return self

    @model_validator(mode="after")
    def validate_budgets(self) -> Self:
        if self.chunk_timeout_seconds >= self.job_timeout_seconds:
            raise ValueError("job_timeout_seconds must exceed chunk_timeout_seconds")
        if self.overlap >= self.window_size:
            raise ValueError("overlap must be smaller than window_size")
        if self.heartbeat_interval_seconds * 3 >= self.job_lease_seconds:
            raise ValueError("job_lease_seconds must exceed three heartbeat intervals")
        if self.shutdown_grace_seconds <= self.heartbeat_interval_seconds:
            raise ValueError("shutdown_grace_seconds must exceed heartbeat_interval_seconds")
        if (
            self.permit_lease_seconds
            <= self.provider_read_timeout_seconds + self.provider_connect_timeout_seconds
        ):
            raise ValueError("permit_lease_seconds must exceed the physical provider timeout")
        drain_budget = (
            self.shutdown_grace_seconds
            + self.provider_connect_timeout_seconds
            + self.provider_read_timeout_seconds
            + CLAIM_RELEASE_TIMEOUT_SECONDS
        )
        if drain_budget > self.stop_grace_seconds:
            raise ValueError(
                "stop_grace_seconds must cover shutdown grace, the provider timeouts and the"
                " claim release timeout"
            )
        if self.orphan_grace_seconds <= self.upload_timeout_seconds:
            raise ValueError("orphan_grace_seconds must exceed upload_timeout_seconds")
        if self.max_call_attempts > self.max_chunk_attempts:
            raise ValueError("max_chunk_attempts must cover max_call_attempts")
        if self.retry_initial_backoff_seconds > self.retry_max_backoff_seconds:
            raise ValueError("retry_initial_backoff_seconds exceeds retry_max_backoff_seconds")
        return self

    @model_validator(mode="after")
    def validate_origins(self) -> Self:
        origin = AnyHttpUrl(self.frontend_origin)
        if origin.path not in (None, "/") or origin.query or origin.fragment:
            raise ValueError("frontend_origin must be an exact origin")
        if origin.username or origin.password or self.frontend_origin.endswith("/"):
            raise ValueError("frontend_origin must not contain credentials or a trailing slash")
        if self.environment_name == "production" and origin.scheme != "https":
            raise ValueError("production frontend_origin requires https")
        if not self.object_prefix.endswith("/") or self.object_prefix.startswith("/"):
            raise ValueError("object_prefix must be a relative prefix ending in /")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        environment = next(
            (
                source().get("environment_name")
                for source in (init_settings, env_settings, dotenv_settings)
                if source().get("environment_name") is not None
            ),
            None,
        )
        if environment not in ("local", "staging", "production"):
            raise ConfigurationError(
                "INGESTION_ENVIRONMENT_NAME must be local, staging or production"
            )
        directory = discover_policy_directory(
            start=Path(__file__), override=os.environ.get("HORIZON_CONFIG_DIR")
        )
        policy = PolicyYamlSource(
            settings_cls,
            env_only_fields=ENV_ONLY_FIELDS,
            yaml_file=policy_layers(
                directory=directory, service="ingestion", environment=environment
            ),
        )
        return (init_settings, env_settings, dotenv_settings, policy, file_secret_settings)


def load_settings() -> Settings:
    return Settings()
