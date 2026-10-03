"""One typed runtime view for technical probes and HTTP operations."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from horizon_ingestion.domain.documents import Pipeline
from horizon_ingestion.observability.tracing import Telemetry
from horizon_ingestion.ports.identity import IdentityVerifier
from horizon_ingestion.ports.indexing import StatusStore
from horizon_ingestion.ports.uploads import AcceptanceStore, ObjectStorage, UploadPreparer


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiRuntime:
    """Resolved collaborators; deployment settings and credentials stay in bootstrap."""

    check_readiness: Callable[[], Awaitable[bool]]
    readiness_timeout_seconds: float
    telemetry: Telemetry
    identity: IdentityVerifier
    pipeline: Pipeline
    preparer: UploadPreparer
    storage: ObjectStorage
    acceptance: AcceptanceStore
    status: StatusStore
    upload_timeout_seconds: float


def get_runtime(request: Request) -> ApiRuntime:
    runtime: ApiRuntime = request.app.state.runtime
    return runtime


type RuntimeDep = Annotated[ApiRuntime, Depends(get_runtime)]
