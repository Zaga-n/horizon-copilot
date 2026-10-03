"""Startup safety and secret masking for both ingestion processes."""

import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from horizon_config import ConfigurationError
from horizon_ingestion.config.secrets import Secrets
from horizon_ingestion.config.settings import Settings
from horizon_ingestion.domain.documents import CLAIM_RELEASE_TIMEOUT_SECONDS


@pytest.fixture
def deployment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith("INGESTION_") or name == "HORIZON_CONFIG_DIR":
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "ENVIRONMENT_NAME": "local",
        "BIND_HOST": "127.0.0.1",
        "BIND_PORT": "8081",
        "FRONTEND_ORIGIN": "http://localhost:3000",
        "AWS_REGION": "eu-west-1",
        "EMBEDDING_MODEL_ID": "amazon.titan-embed-text-v2:0",
        "GOOGLE_CLIENT_ID": "public-client",
        "MINIO_ENDPOINT": "http://localhost:9000",
        "MINIO_BUCKET": "documents",
        "DATABASE_DSN": "postgresql://ingestion:secret@localhost/horizon",
        "MINIO_ACCESS_KEY": "test",
        "MINIO_SECRET_KEY": "test-secret",
    }.items():
        monkeypatch.setenv(f"INGESTION_{name}", value)


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        pytest.param("OVERLAP", "2000", "overlap", id="invalid-overlap"),
        pytest.param("BIND_HOST", "0.0.0.0", "loopback", id="local-exposure"),
        pytest.param(
            "PERMIT_LEASE_SECONDS", "10", "physical provider timeout", id="unsafe-permit-lease"
        ),
        pytest.param("ORPHAN_GRACE_SECONDS", "60", "upload_timeout", id="unsafe-orphan-age"),
        pytest.param(
            "SHUTDOWN_GRACE_SECONDS", "5", "heartbeat_interval", id="drain-shorter-than-heartbeat"
        ),
        pytest.param("FRONTEND_ORIGIN", "https://example.com/path", "exact origin", id="cors-path"),
    ],
)
def test_unsafe_configuration_blocks_startup(
    deployment: None,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
    reason: str,
) -> None:
    monkeypatch.setenv(f"INGESTION_{name}", value)
    with pytest.raises(ValidationError, match=reason):
        Settings()


def test_policy_typo_fails(
    deployment: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "base.yaml").write_text("window_szie: 2000\n")
    (tmp_path / "local.yaml").write_text("{}\n")
    monkeypatch.setenv("HORIZON_CONFIG_DIR", str(tmp_path))
    with pytest.raises(ConfigurationError, match="window_szie"):
        Settings()


def test_secret_errors_and_representations_are_masked(deployment: None) -> None:
    sentinel = "secret-sentinel-83ad"
    with pytest.raises(ValidationError, match="PostgreSQL") as caught:
        Secrets(database_dsn=f"mysql://user:{sentinel}@db/horizon")
    assert sentinel not in str(caught.value)
    secrets = Secrets(
        database_dsn=f"postgresql://user:{sentinel}@db/horizon",
        minio_access_key=sentinel,
        minio_secret_key=sentinel,
    )
    assert sentinel not in repr(secrets)
    assert sentinel not in secrets.model_dump_json()


def test_production_local_identity_is_rejected(
    deployment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INGESTION_ENVIRONMENT_NAME", "production")
    monkeypatch.setenv("INGESTION_IDENTITY_MODE", "local")
    monkeypatch.setenv("INGESTION_FRONTEND_ORIGIN", "https://example.com")
    with pytest.raises(ValidationError, match="requires environment_name local"):
        Settings()


def test_the_drain_budget_must_fit_the_stop_grace(
    deployment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings()
    budget = (
        settings.shutdown_grace_seconds
        + settings.provider_connect_timeout_seconds
        + settings.provider_read_timeout_seconds
        + CLAIM_RELEASE_TIMEOUT_SECONDS
    )
    monkeypatch.setenv("INGESTION_STOP_GRACE_SECONDS", str(budget))
    assert Settings().stop_grace_seconds == budget
    monkeypatch.setenv("INGESTION_STOP_GRACE_SECONDS", str(budget - 1))
    with pytest.raises(ValidationError, match="stop_grace_seconds must cover"):
        Settings()


def test_the_worker_stop_grace_mirrors_the_deployment(deployment: None) -> None:
    compose = (Path(__file__).resolve().parents[4] / "compose.yaml").read_text()
    # The worker service block, up to its stop grace; no YAML parser in the toolchain.
    grace = re.search(
        r"^  ingestion-worker:\n(?:(?:    .*)?\n)*?    stop_grace_period: (\d+)s$",
        compose,
        re.MULTILINE,
    )
    assert grace is not None
    assert Settings().stop_grace_seconds == float(grace.group(1))
