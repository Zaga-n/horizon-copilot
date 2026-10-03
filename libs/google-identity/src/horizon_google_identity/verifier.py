"""Google ID-token verification outside the event loop, with library-owned outcomes."""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Protocol

import httpx
from google.auth.exceptions import GoogleAuthError, TransportError
from google.oauth2 import id_token
from pydantic import BaseModel, Field, ValidationError

MAX_TOKEN_CHARS = 16384  # far above real Google ID tokens; bounds work done on hostile input


class GoogleIdentityError(Exception):
    """Base of every verification outcome other than a verified subject."""


class InvalidIdTokenError(GoogleIdentityError):
    """The bearer is missing, oversized, malformed, forged, expired or for another audience."""


class CertificatesUnavailableError(GoogleIdentityError):
    """Google's public certificates could not be fetched; the same token may verify later."""


@dataclass(frozen=True, slots=True, kw_only=True)
class CertificateResponse:
    """Google's certificate fetcher consumes status and raw response bytes."""

    status: int
    data: bytes


class CertificateRequest(Protocol):
    """The narrow transport google-auth calls to fetch certificates."""

    def __call__(self, url: str, method: str = "GET") -> CertificateResponse: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class HttpCertificateRequest:
    """Borrows a client with bounded timeouts; bootstrap creates and closes it."""

    client: httpx.Client

    def __call__(self, url: str, method: str = "GET") -> CertificateResponse:
        response = self.client.request(method, url)
        response.raise_for_status()
        return CertificateResponse(status=response.status_code, data=response.content)


class VerifiedClaims(BaseModel):
    """Additional provider claims are ignored after cryptographic verification."""

    sub: Annotated[str, Field(min_length=1, max_length=255)]


@dataclass(frozen=True, slots=True, kw_only=True)
class GoogleIdTokenVerifier:
    """Google ID tokens only; identity comes from `sub`, never email."""

    audience: str
    request: CertificateRequest

    def _verify(self, token: str) -> str:
        try:
            claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]  # Google SDK's public verification API is unannotated.
                token,
                self.request,
                audience=self.audience,
            )
            verified = VerifiedClaims.model_validate(claims)
        except (httpx.HTTPError, TransportError) as exc:
            raise CertificatesUnavailableError() from exc
        except (GoogleAuthError, ValueError, ValidationError) as exc:
            raise InvalidIdTokenError() from exc
        return verified.sub

    async def verify(self, *, token: str | None) -> str:
        """The verified subject of `token`."""
        if not token or len(token) > MAX_TOKEN_CHARS:
            raise InvalidIdTokenError()
        return await asyncio.to_thread(self._verify, token)
