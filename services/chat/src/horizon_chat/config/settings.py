"""Typed deployment settings and strict layered application policy."""

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

type PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
type NonEmptyStr = Annotated[str, Field(min_length=1)]
type Environment = Literal["local", "staging", "production"]
type IdentityMode = Literal["google", "local"]
type ReasoningEffort = Literal["none", "low", "medium", "high"]
CONFIG_DIR_VARIABLE = "HORIZON_CONFIG_DIR"
ENV_ONLY_FIELDS = frozenset(
    {
        "environment_name",
        "bind_host",
        "bind_port",
        "frontend_origin",
        "aws_region",
        "main_model_id",
        "utility_model_id",
        "embedding_model_id",
        "google_client_id",
    }
)


class Settings(BaseSettings):
    """Resolved immutable chat configuration, loaded before opening dependencies."""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    environment_name: Environment
    bind_host: NonEmptyStr
    bind_port: Annotated[int, Field(ge=1, le=65535)]
    frontend_origin: NonEmptyStr
    aws_region: NonEmptyStr
    main_model_id: NonEmptyStr
    utility_model_id: NonEmptyStr
    embedding_model_id: NonEmptyStr
    google_client_id: NonEmptyStr

    identity_mode: IdentityMode
    local_subject: NonEmptyStr
    embedding_dimensions: Literal[1024]
    max_model_calls: PositiveInt
    max_tool_calls: Annotated[int, Field(ge=1, le=2)]
    max_rewrite_calls: PositiveInt
    max_physical_model_attempts: PositiveInt
    max_output_tokens: PositiveInt
    main_reasoning_effort: ReasoningEffort
    utility_reasoning_effort: ReasoningEffort
    max_chunks: Annotated[int, Field(ge=1, le=8)]
    max_excerpt_chars: PositiveInt
    max_evidence_chars: PositiveInt
    turn_deadline_seconds: PositiveSeconds
    retry_attempts: PositiveInt
    retry_initial_backoff_seconds: PositiveSeconds
    retry_max_backoff_seconds: PositiveSeconds
    summary_trigger_tokens: PositiveInt
    summary_keep_messages: PositiveInt
    retention_days: PositiveInt
    maintenance_batch_size: PositiveInt
    maintenance_interval_seconds: PositiveSeconds
    # Failure intents are small and local, so they are reconciled far more often than retention.
    recovery_interval_seconds: PositiveSeconds
    # How long shutdown waits for a maintenance pass before cancelling it; must fit the
    # deployment stop grace with room for in-flight requests.
    maintenance_shutdown_grace_seconds: PositiveSeconds
    turn_lease_seconds: PositiveSeconds
    database_pool_size: PositiveInt
    database_connect_timeout_seconds: PositiveInt
    database_statement_timeout_ms: PositiveInt
    provider_connect_timeout_seconds: PositiveSeconds
    provider_read_timeout_seconds: PositiveSeconds
    readiness_timeout_seconds: PositiveSeconds
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    log_full_exception_trace: bool
    capture_ai_content: bool

    otlp_endpoint: AnyHttpUrl | None = None
    service_instance_id: str | None = None

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.identity_mode == "local":
            if self.environment_name != "local":
                raise ValueError("identity_mode local requires environment_name local")
            if self.bind_host != "localhost":
                try:
                    loopback = ipaddress.ip_address(self.bind_host).is_loopback
                except ValueError:
                    loopback = False
                if not loopback:
                    raise ValueError("identity_mode local requires a loopback bind_host")
        return self

    @model_validator(mode="after")
    def validate_budgets(self) -> Self:
        if self.retry_initial_backoff_seconds > self.retry_max_backoff_seconds:
            raise ValueError("retry_initial_backoff_seconds exceeds retry_max_backoff_seconds")
        if self.max_physical_model_attempts <= self.max_model_calls:
            raise ValueError("max_physical_model_attempts must reserve utility/final capacity")
        if self.turn_lease_seconds <= self.turn_deadline_seconds:
            raise ValueError("turn_lease_seconds must exceed turn_deadline_seconds")
        return self

    @model_validator(mode="after")
    def validate_frontend_origin(self) -> Self:
        origin = AnyHttpUrl(self.frontend_origin)
        if origin.path not in (None, "/") or origin.query or origin.fragment:
            raise ValueError("frontend_origin must be an origin without path, query or fragment")
        if origin.username or origin.password or self.frontend_origin.endswith("/"):
            raise ValueError("frontend_origin must not contain credentials or a trailing slash")
        if self.environment_name == "production" and origin.scheme != "https":
            raise ValueError("production frontend_origin requires https")
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
        environment = _environment_name(init_settings, env_settings, dotenv_settings)
        policy = PolicyYamlSource(
            settings_cls, yaml_file=yaml_layers(environment), env_only_fields=ENV_ONLY_FIELDS
        )
        return (init_settings, env_settings, dotenv_settings, policy, file_secret_settings)


def _environment_name(*sources: PydanticBaseSettingsSource) -> str:
    for source in sources:
        value = source().get("environment_name")
        if isinstance(value, str) and value in ("local", "staging", "production"):
            return value
    raise ConfigurationError("environment_name must be local, staging or production")


def yaml_layers(environment: str) -> list[Path]:
    directory = discover_policy_directory(
        start=Path(__file__), override=os.environ.get(CONFIG_DIR_VARIABLE)
    )
    return policy_layers(directory=directory, service="chat", environment=environment)


def load_settings() -> Settings:
    return Settings()
