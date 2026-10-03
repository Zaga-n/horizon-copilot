"""Typed runtime views and bearer extraction for routes."""

from typing import Annotated, Protocol

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from horizon_chat.application.conversations import authenticate
from horizon_chat.domain.recovery import RecoveryBuffer
from horizon_chat.domain.runs import TurnPolicy
from horizon_chat.observability.tracing import Telemetry
from horizon_chat.ports.agent import HorizonAgent
from horizon_chat.ports.checkpoints import CheckpointStore
from horizon_chat.ports.conversations import ConversationStore
from horizon_chat.ports.feedback import FeedbackStore
from horizon_chat.ports.identity import IdentityVerifier
from horizon_chat.ports.runs import RunLedger


class ReadinessProbe(Protocol):
    """Compatibility probe used by HTTP and replaceable in lifecycle tests."""

    async def check(self) -> bool: ...


class Liveness(Protocol):
    """Whether every required background loop of the process is still running."""

    def loops_alive(self) -> bool: ...


def get_liveness(request: Request) -> Liveness:
    liveness: Liveness = request.app.state.liveness
    return liveness


type LivenessDep = Annotated[Liveness, Depends(get_liveness)]


class ApiRuntime(Protocol):
    """Resource capabilities and probe policy read by the HTTP adapter."""

    @property
    def readiness(self) -> ReadinessProbe: ...
    @property
    def readiness_timeout_seconds(self) -> float: ...


def get_runtime(request: Request) -> ApiRuntime:
    runtime: ApiRuntime = request.app.state.runtime
    return runtime


type RuntimeDep = Annotated[ApiRuntime, Depends(get_runtime)]


class ChatApiRuntime(Protocol):
    """Business capabilities supplied by the composition root."""

    @property
    def conversations(self) -> ConversationStore: ...
    @property
    def runs(self) -> RunLedger: ...
    @property
    def feedback(self) -> FeedbackStore: ...
    @property
    def recovery(self) -> RecoveryBuffer: ...
    @property
    def checkpoints(self) -> CheckpointStore: ...
    @property
    def agent(self) -> HorizonAgent: ...
    @property
    def telemetry(self) -> Telemetry: ...
    @property
    def turn_policy(self) -> TurnPolicy: ...
    @property
    def identity(self) -> IdentityVerifier: ...


def get_chat_runtime(request: Request) -> ChatApiRuntime:
    runtime: ChatApiRuntime = request.app.state.runtime
    return runtime


type ChatRuntimeDep = Annotated[ChatApiRuntime, Depends(get_chat_runtime)]
bearer = HTTPBearer(auto_error=False)


async def current_subject(
    runtime: ChatRuntimeDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    return await authenticate(
        verifier=runtime.identity,
        token=credentials.credentials if credentials else None,
    )


type SubjectDep = Annotated[str, Depends(current_subject)]
