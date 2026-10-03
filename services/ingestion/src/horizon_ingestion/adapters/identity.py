"""Identity port adapters: shared Google verification mapped once, and local development."""

from dataclasses import dataclass

from horizon_google_identity import (
    CertificatesUnavailableError,
    GoogleIdTokenVerifier,
    InvalidIdTokenError,
)
from horizon_ingestion.ports.identity import IdentityUnavailableError, InvalidIdentityError


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleIdentityVerifier:
    """Maps the shared verifier's outcomes to this service's identity port errors."""

    verifier: GoogleIdTokenVerifier

    async def verify(self, *, token: str | None) -> str:
        try:
            return await self.verifier.verify(token=token)
        except InvalidIdTokenError as exc:
            raise InvalidIdentityError() from exc
        except CertificatesUnavailableError as exc:
            raise IdentityUnavailableError() from exc


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalIdentityVerifier:
    """Only constructed after loopback/local deployment validation."""

    subject: str

    async def verify(self, *, token: str | None) -> str:
        return self.subject
