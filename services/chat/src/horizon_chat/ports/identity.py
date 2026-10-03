"""Server-verified stable identity capability."""

from typing import Protocol

from horizon_chat.ports.errors import DependencyUnavailableError


class InvalidIdentityError(Exception):
    """A bearer is missing, expired, malformed, or fails Google verification."""


class IdentityUnavailableError(DependencyUnavailableError):
    """The public certificate endpoint could not be reached."""

    def __init__(self) -> None:
        super().__init__("identity_unavailable")


class IdentityVerifier(Protocol):
    async def verify(self, *, token: str | None) -> str: ...
