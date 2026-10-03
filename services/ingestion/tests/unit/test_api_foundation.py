"""Probe error projection and exact browser-origin policy through the ASGI boundary."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider

from horizon_ingestion.adapters.identity import LocalIdentityVerifier
from horizon_ingestion.api.dependencies import ApiRuntime, get_runtime
from horizon_ingestion.bootstrap.app import create_app
from horizon_ingestion.config.settings import Settings
from horizon_ingestion.domain.documents import Acceptance, FileType, JobStatus, Pipeline
from horizon_ingestion.observability.tracing import Measurements, Telemetry
from horizon_ingestion.ports.uploads import PreparedUpload, UploadStream
from horizon_ingestion_testing.jobs import UnusedStorage


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeRuntime:
    check_readiness: Callable[[], Awaitable[bool]]
    readiness_timeout_seconds: float


def deployment_settings() -> Settings:
    return Settings(
        environment_name="local",
        bind_host="127.0.0.1",
        bind_port=8081,
        frontend_origin="http://localhost:3000",
        aws_region="eu-west-1",
        embedding_model_id="amazon.titan-embed-text-v2:0",
        google_client_id="public-client",
        minio_endpoint="http://localhost:9000",
        minio_bucket="documents",
    )


async def test_unready_probe_does_not_claim_healthy_dependencies() -> None:
    async def unavailable() -> bool:
        return False

    app = create_app(settings=deployment_settings())
    runtime = ProbeRuntime(check_readiness=unavailable, readiness_timeout_seconds=0.1)
    app.dependency_overrides[get_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).json() == {"status": "alive"}
        result = await client.get("/ready")
        assert result.status_code == 503
        assert result.json() == {"status": "unready"}


async def test_only_configured_origin_can_preflight_document_mutations() -> None:
    app = create_app(settings=deployment_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        headers = {
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,Content-Type,Idempotency-Key",
        }
        response = await client.options("/v1/documents", headers=headers)
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        headers["Origin"] = "http://unapproved.example"
        response = await client.options("/v1/documents", headers=headers)
        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("headers", [[], [(b"content-length", b"1")]])
async def test_actual_stream_limit_rejects_missing_or_false_content_length(
    headers: list[tuple[bytes, bytes]],
) -> None:
    from starlette.types import Message, Receive, Scope, Send

    from horizon_ingestion.api.middleware import RequestBodyLimit

    accepted = False

    async def consumer(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal accepted
        while (await receive()).get("more_body"):
            pass
        accepted = True

    received = iter(
        [
            {"type": "http.request", "body": b"123456", "more_body": True},
            {"type": "http.request", "body": b"789012", "more_body": False},
        ]
    )
    sent: list[Message] = []

    async def receive() -> Message:
        return next(received)

    async def send(message: Message) -> None:
        sent.append(message)

    await RequestBodyLimit(consumer, max_bytes=10)(
        {"type": "http", "headers": headers}, receive, send
    )
    assert not accepted
    assert sent[0]["status"] == 413


@dataclass(frozen=True, slots=True, kw_only=True)
class BrokenPreparer:
    """A programming error deep inside the upload must not look like bad client input."""

    def prepare(
        self, *, stream: UploadStream, filename: str, content_type: str
    ) -> AbstractAsyncContextManager[PreparedUpload]:
        raise ValueError("unrelated defect")


class UnusedAcceptance:
    """The request fails during preparation, before any acceptance decision."""

    async def find_existing(self, **_: object) -> Acceptance | None:
        raise NotImplementedError

    async def accept(self, **_: object) -> Acceptance:
        raise NotImplementedError


class UnusedStatus:
    async def document_status(self, *, subject: str, document_id: UUID) -> JobStatus:
        raise NotImplementedError

    async def job_status(self, *, subject: str, job_id: UUID) -> JobStatus:
        raise NotImplementedError

    async def list_documents(
        self, *, subject: str, limit: int, offset: int
    ) -> tuple[JobStatus, ...]:
        raise NotImplementedError

    async def retry(self, *, subject: str, job_id: UUID) -> JobStatus:
        raise NotImplementedError

    async def delete(self, *, subject: str, document_id: UUID) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True, kw_only=True)
class StalledPreparer:
    """Preparation never finishes, so only the upload timeout ends the request."""

    @asynccontextmanager
    async def prepare(
        self, *, stream: UploadStream, filename: str, content_type: str
    ) -> AsyncIterator[PreparedUpload]:
        await asyncio.Event().wait()
        yield PreparedUpload(
            path=Path("unused"), sha256="", filename="a.pdf", file_type=FileType.PDF
        )


def upload_runtime() -> ApiRuntime:
    traces, metrics = TracerProvider(), MeterProvider()
    return ApiRuntime(
        check_readiness=lambda: asyncio.sleep(0, result=True),
        readiness_timeout_seconds=1,
        telemetry=Telemetry(
            tracer=traces.get_tracer("test"),
            measurements=Measurements(meter=metrics.get_meter("test")),
        ),
        identity=LocalIdentityVerifier(subject="owner"),
        pipeline=Pipeline(
            window_size=2000, overlap=200, embedding_model_id="amazon.titan-embed-text-v2:0"
        ),
        preparer=BrokenPreparer(),
        storage=UnusedStorage(),
        acceptance=UnusedAcceptance(),
        status=UnusedStatus(),
        upload_timeout_seconds=5,
    )


@pytest.mark.parametrize(
    ("metadata", "status"),
    [
        pytest.param('{"corpus": ', 422, id="malformed-metadata"),
        pytest.param("{}", 500, id="unrelated-value-error"),
    ],
)
async def test_only_metadata_errors_are_client_errors(metadata: str, status: int) -> None:
    app = create_app(settings=deployment_settings())
    runtime = upload_runtime()
    app.dependency_overrides[get_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/documents",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
            data={"metadata": metadata},
            headers={"Idempotency-Key": "upload"},
        )
    assert response.status_code == status


@pytest.mark.parametrize(
    ("form", "files", "status", "detail"),
    [
        pytest.param(
            {"metadata": '{"corpus": '},
            True,
            422,
            "invalid_upload_metadata",
            id="malformed-metadata",
        ),
        pytest.param(
            {"document_id": "not-a-uuid"}, True, 422, "invalid_document_id", id="bad-document-id"
        ),
        pytest.param(
            {
                "document_id": "00000000-0000-0000-0000-000000000001",
                "metadata": '{"document_id": "00000000-0000-0000-0000-000000000002"}',
            },
            True,
            422,
            "invalid_document_id",
            id="document-id-twice",
        ),
        pytest.param({"metadata": "{}"}, False, 422, "file_and_metadata_required", id="no-file"),
    ],
)
async def test_upload_form_errors_keep_their_public_status_and_code(
    form: dict[str, str], files: bool, status: int, detail: str
) -> None:
    app = create_app(settings=deployment_settings())
    runtime = upload_runtime()
    app.dependency_overrides[get_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/documents",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}
            if files
            else {"other": ("x", b"x")},
            data=form,
            headers={"Idempotency-Key": "upload"},
        )
    assert (response.status_code, response.json()) == (status, {"detail": detail})


async def test_an_upload_past_its_timeout_is_408() -> None:
    app = create_app(settings=deployment_settings())
    runtime = replace(upload_runtime(), preparer=StalledPreparer(), upload_timeout_seconds=0.05)
    app.dependency_overrides[get_runtime] = lambda: runtime
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/documents",
            files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
            headers={"Idempotency-Key": "upload"},
        )
    assert (response.status_code, response.json()) == (408, {"detail": "upload_timeout"})
