"""Authenticate the caller of every document operation."""

from horizon_ingestion.ports.identity import IdentityVerifier


async def authenticate(*, token: str | None, verifier: IdentityVerifier) -> str:
    return await verifier.verify(token=token)
