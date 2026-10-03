"""Startup safety and secret-exclusion regression checks."""

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.secrets import Secrets
from horizon_chat.config.settings import CONFIG_DIR_VARIABLE, Settings
from horizon_config import ConfigurationError


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.lower() in Settings.model_fields or name.lower() in Secrets.model_fields:
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(CONFIG_DIR_VARIABLE, raising=False)
    monkeypatch.setenv("ENVIRONMENT_NAME", "local")
    for name, value in {
        "BIND_HOST": "127.0.0.1",
        "BIND_PORT": "8080",
        "FRONTEND_ORIGIN": "http://localhost:3000",
        "AWS_REGION": "eu-west-1",
        "MAIN_MODEL_ID": "terra",
        "UTILITY_MODEL_ID": "luna",
        "EMBEDDING_MODEL_ID": "titan",
        "GOOGLE_CLIENT_ID": "public-client",
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("RETENTION_DAYS", "0", id="invalid-retention"),
        pytest.param("EMBEDDING_DIMENSIONS", "512", id="incompatible-embedding"),
        pytest.param("MAX_CHUNKS", "9", id="unbounded-chunks"),
        pytest.param("TURN_DEADLINE_SECONDS", "nan", id="nonfinite-deadline"),
        pytest.param("TURN_LEASE_SECONDS", "10", id="lease-shorter-than-run"),
        pytest.param("BIND_HOST", "0.0.0.0", id="local-public-binding"),
        pytest.param("FRONTEND_ORIGIN", "https://app.example/path", id="nonorigin-cors"),
    ],
)
def test_invalid_configuration_fails(
    field: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(field, value)
    with pytest.raises(ValidationError):
        Settings()


def test_production_rejects_local_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example")
    monkeypatch.setenv("IDENTITY_MODE", "local")
    with pytest.raises(ValidationError, match="requires environment_name local"):
        Settings()


def test_google_identity_allows_public_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example")
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    assert Settings().identity_mode == "google"


def test_policy_typo_is_not_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "base.yaml").write_text("retentoin_days: 30\n")
    (tmp_path / "local.yaml").write_text("{}\n")
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, str(tmp_path))
    with pytest.raises(ConfigurationError, match="retentoin_days"):
        Settings()


def test_secret_validation_does_not_echo_password() -> None:
    sentinel = "secret-sentinel-93fa"
    with pytest.raises(ValidationError) as caught:
        Secrets(database_dsn=f"mysql://user:{sentinel}@db/horizon", checkpoint_database_dsn="bad")
    assert sentinel not in str(caught.value)
    secrets = Secrets(
        database_dsn=f"postgresql://user:{sentinel}@db/horizon",
        checkpoint_database_dsn=f"postgresql://user:{sentinel}@db/horizon",
    )
    assert sentinel not in repr(secrets)
    assert sentinel not in secrets.model_dump_json()


def test_invalid_production_identity_blocks_asgi_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://app.example")
    monkeypatch.setenv("IDENTITY_MODE", "local")
    with (
        pytest.raises(ValidationError, match="requires environment_name local"),
        TestClient(create_app()),
    ):
        pass


def test_maintenance_shutdown_fits_the_deployment_stop_grace() -> None:
    compose = (Path(__file__).resolve().parents[5] / "compose.yaml").read_text()
    # The chat service block, up to the next top-level service; no YAML parser in the toolchain.
    grace = re.search(
        r"^  chat:\n(?:(?:    .*)?\n)*?    stop_grace_period: (\d+)s$", compose, re.MULTILINE
    )
    assert grace is not None
    stop_grace_seconds = float(grace.group(1))
    # Half the stop grace stays free for in-flight requests and lifespan teardown.
    assert Settings().maintenance_shutdown_grace_seconds <= stop_grace_seconds / 2
