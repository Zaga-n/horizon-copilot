"""Verified bearer identities; subjects never come from upload metadata."""

from typing import Protocol

from horizon_ingestion.ports.errors import DependencyUnavailableError


class InvalidIdentityError(Exception):
    """The bearer is absent, invalid or not a Google ID token for this client."""


class IdentityUnavailableError(DependencyUnavailableError):
    """Google verification keys are temporarily unavailable."""

    def __init__(self) -> None:
        super().__init__("identity_unavailable")


class IdentityVerifier(Protocol):
    async def verify(self, *, token: str | None) -> str: ...
