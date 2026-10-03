"""The identity adapter maps the shared verifier's outcomes to this service's port errors."""

import httpx
import pytest

from horizon_google_identity import GoogleIdTokenVerifier, HttpCertificateRequest
from horizon_ingestion.adapters.identity import GoogleIdentityVerifier
from horizon_ingestion.ports.identity import IdentityUnavailableError, InvalidIdentityError

# Shaped like a JWT, so verification reaches the certificate fetch before the signature check.
SIGNED_LOOKING = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImsifQ.eyJzdWIiOiJzIn0.c2ln"


def adapter(client: httpx.Client) -> GoogleIdentityVerifier:
    return GoogleIdentityVerifier(
        verifier=GoogleIdTokenVerifier(
            audience="public-client", request=HttpCertificateRequest(client=client)
        )
    )


@pytest.mark.parametrize("token", [None, "", "not-a-jwt"])
async def test_invalid_bearers_map_to_invalid_identity(token: str | None) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    with httpx.Client(transport=transport) as client, pytest.raises(InvalidIdentityError):
        await adapter(client).verify(token=token)


async def test_unreachable_certificates_map_to_identity_unavailable() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    with httpx.Client(transport=transport) as client, pytest.raises(IdentityUnavailableError):
        await adapter(client).verify(token=SIGNED_LOOKING)
