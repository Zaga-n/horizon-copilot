"""Public error table: exhaustive, resolved along the MRO, and one record per server failure."""

import inspect
import logging
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI

import horizon_ingestion.api.middleware
import horizon_ingestion.ports.errors
import horizon_ingestion.ports.identity
import horizon_ingestion.ports.uploads
from horizon_ingestion.api.errors import PUBLIC_ERRORS
from horizon_ingestion.bootstrap.app import create_app
from horizon_ingestion.ports.errors import DataIntegrityError
from horizon_ingestion.ports.identity import IdentityUnavailableError
from horizon_ingestion.ports.indexing import (
    EmbeddingUnavailableError,
    StatusConflictError,
    StatusNotFoundError,
)
from horizon_ingestion.ports.uploads import UploadValidationError
from horizon_ingestion_testing.settings import deployment_settings

# Every module whose exceptions a request can raise.
REQUEST_ERROR_MODULES: tuple[ModuleType, ...] = (
    horizon_ingestion.api.middleware,
    horizon_ingestion.ports.errors,
    horizon_ingestion.ports.identity,
    horizon_ingestion.ports.uploads,
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
    assert any(cls in PUBLIC_ERRORS for cls in error.__mro__)


def raising_app(failure: Exception) -> FastAPI:
    app = create_app(settings=deployment_settings())

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
    ("failure", "status", "detail"),
    [
        pytest.param(
            DataIntegrityError("database_integrity"), 500, "internal_error", id="integrity"
        ),
        pytest.param(
            EmbeddingUnavailableError("provider_unavailable"),
            503,
            "service_unavailable",
            id="unavailable-subclass",
        ),
        pytest.param(IdentityUnavailableError(), 503, "service_unavailable", id="identity"),
        pytest.param(
            UploadValidationError("unsupported_type"), 422, "unsupported_type", id="upload"
        ),
        pytest.param(StatusNotFoundError("job_not_found"), 404, "not_found", id="status-missing"),
        pytest.param(
            StatusConflictError("retry_unavailable"), 409, "request_conflict", id="status-conflict"
        ),
    ],
)
async def test_errors_resolve_through_their_base(
    failure: Exception, status: int, detail: str
) -> None:
    response = await get_failure(raising_app(failure))
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert response.headers["cache-control"] == "no-store"


async def test_retry_metadata_reaches_the_client() -> None:
    failure = EmbeddingUnavailableError("provider_unavailable", retry_after_seconds=2.5)
    response = await get_failure(raising_app(failure))
    assert response.headers["retry-after"] == "3"


async def test_unknown_failure_is_one_internal_error_and_one_record(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        response = await get_failure(raising_app(RuntimeError("defect")))
    assert response.status_code == 500
    assert response.json() == {"detail": "internal_error"}
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [record.msg for record in errors] == ["request_failed"]
    assert errors[0].exc_info is not None and isinstance(errors[0].exc_info[1], RuntimeError)
