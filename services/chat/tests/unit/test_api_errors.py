"""Public error table: exhaustive, resolved along the MRO, and one record per server failure."""

import inspect
import logging
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI

import horizon_chat.domain.conversations
import horizon_chat.domain.feedback
import horizon_chat.domain.runs
import horizon_chat.ports.agent
import horizon_chat.ports.checkpoints
import horizon_chat.ports.conversations
import horizon_chat.ports.errors
import horizon_chat.ports.identity
from horizon_chat.api.exception_handlers import PROBLEMS
from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import Settings
from horizon_chat.ports.checkpoints import (
    CheckpointStoreIntegrityError,
    CheckpointStoreUnavailableError,
)

# Every module whose exceptions a request can raise.
REQUEST_ERROR_MODULES: tuple[ModuleType, ...] = (
    horizon_chat.domain.conversations,
    horizon_chat.domain.feedback,
    horizon_chat.domain.runs,
    horizon_chat.ports.agent,
    horizon_chat.ports.checkpoints,
    horizon_chat.ports.conversations,
    horizon_chat.ports.errors,
    horizon_chat.ports.identity,
)


def request_errors() -> list[type[Exception]]:
    return [
        value
        for module in REQUEST_ERROR_MODULES
        for _, value in inspect.getmembers(module, inspect.isclass)
        if issubclass(value, Exception) and value.__module__ == module.__name__
    ]


@pytest.mark.parametrize("error", request_errors(), ids=lambda error: error.__name__)
def test_every_request_error_is_mapped(error: type[Exception]) -> None:
    assert any(cls in PROBLEMS for cls in error.__mro__)


def raising_app(settings: Settings, failure: Exception) -> FastAPI:
    app = create_app(settings=settings)

    async def fail() -> None:
        raise failure

    app.add_api_route("/test-failure", fail)
    return app


async def get_failure(app: FastAPI) -> httpx.Response:
    # The default transport re-raises app exceptions, so a pass proves nothing reaches the server.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/test-failure")


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        pytest.param(
            CheckpointStoreUnavailableError("checkpoint_timeout"),
            503,
            "service_unavailable",
            id="unavailable-subclass",
        ),
        pytest.param(
            CheckpointStoreIntegrityError("checkpoint_corrupt"),
            500,
            "internal_error",
            id="integrity-subclass",
        ),
    ],
)
async def test_port_errors_resolve_through_their_base(
    chat_settings: Settings, failure: Exception, status: int, code: str
) -> None:
    response = await get_failure(raising_app(chat_settings, failure))
    assert response.status_code == status
    assert response.json()["title"] == code
    assert response.headers["cache-control"] == "no-store"


async def test_retry_metadata_reaches_the_client(chat_settings: Settings) -> None:
    failure = CheckpointStoreUnavailableError("checkpoint_timeout", retry_after_seconds=2.5)
    response = await get_failure(raising_app(chat_settings, failure))
    assert response.headers["retry-after"] == "3"


async def test_unknown_failure_is_one_internal_problem_and_one_record(
    chat_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await get_failure(raising_app(chat_settings, RuntimeError("defect")))
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {
        "type": "urn:horizon:internal_error",
        "title": "internal_error",
        "status": 500,
    }
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [record.msg for record in errors] == ["request_failed"]
    assert errors[0].exc_info is not None and isinstance(errors[0].exc_info[1], RuntimeError)
