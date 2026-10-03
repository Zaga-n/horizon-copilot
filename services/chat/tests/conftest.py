"""Explicit deployment policy shared by chat transport tests."""

from collections.abc import AsyncIterator

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from horizon_chat.config.settings import Settings
from horizon_chat_testing.streaming import ControlledAgent, StreamingRuntime, open_streaming


@pytest.fixture
def chat_settings() -> Settings:
    return Settings(
        environment_name="local",
        bind_host="127.0.0.1",
        bind_port=8080,
        frontend_origin="http://localhost:3000",
        aws_region="eu-west-1",
        main_model_id="terra",
        utility_model_id="luna",
        embedding_model_id="titan",
        google_client_id="public-client",
    )


@pytest.fixture
async def streaming(
    migrated_database: str,
) -> AsyncIterator[tuple[StreamingRuntime, ControlledAgent, InMemorySpanExporter]]:
    async with open_streaming(migrated_database) as opened:
        yield opened
