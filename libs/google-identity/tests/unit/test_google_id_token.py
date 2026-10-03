"""Google ID-token verification against a controlled public certificate transport."""

import base64
import json
import time
from dataclasses import dataclass

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from horizon_google_identity import (
    MAX_TOKEN_CHARS,
    CertificateResponse,
    CertificatesUnavailableError,
    GoogleIdTokenVerifier,
    HttpCertificateRequest,
    InvalidIdTokenError,
)


def encoded(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenIssuer:
    """Signs test tokens; its public key substitutes only the Google certificate HTTP fetch."""

    key: rsa.RSAPrivateKey

    def __call__(self, url: str, method: str = "GET") -> CertificateResponse:
        public = (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )
        return CertificateResponse(status=200, data=json.dumps({"test-key": public}).encode())

    def token(self, **overrides: str | int) -> str:
        now = int(time.time())
        claims: dict[str, str | int] = {
            "sub": "stable-subject",
            "email": "mutable@example.test",
            "iss": "https://accounts.google.com",
            "aud": "public-client",
            "iat": now - 10,
            "exp": now + 60,
        }
        claims.update(overrides)
        header = encoded(json.dumps({"alg": "RS256", "kid": "test-key", "typ": "JWT"}).encode())
        body = encoded(json.dumps(claims).encode())
        payload = header + b"." + body
        signature = self.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        return (payload + b"." + encoded(signature)).decode()


@pytest.fixture
def issuer() -> TokenIssuer:
    return TokenIssuer(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))


async def test_google_identity_uses_verified_subject_not_email(issuer: TokenIssuer) -> None:
    verifier = GoogleIdTokenVerifier(audience="public-client", request=issuer)
    assert (
        await verifier.verify(token=issuer.token(email="different@example.test"))
        == "stable-subject"
    )


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        pytest.param("aud", "other-client", id="wrong-audience"),
        pytest.param("iss", "https://attacker.example", id="wrong-issuer"),
        pytest.param("exp", 1, id="expired"),
        pytest.param("sub", "", id="empty-subject"),
        pytest.param("sub", "x" * 256, id="oversized-subject"),
    ],
)
async def test_invalid_signed_claim_is_rejected(
    issuer: TokenIssuer, claim: str, value: str | int
) -> None:
    verifier = GoogleIdTokenVerifier(audience="public-client", request=issuer)
    with pytest.raises(InvalidIdTokenError):
        await verifier.verify(token=issuer.token(**{claim: value}))


async def test_forged_signature_is_rejected(issuer: TokenIssuer) -> None:
    attacker = TokenIssuer(key=rsa.generate_private_key(public_exponent=65537, key_size=2048))
    verifier = GoogleIdTokenVerifier(audience="public-client", request=issuer)
    with pytest.raises(InvalidIdTokenError):
        await verifier.verify(token=attacker.token())


async def test_missing_subject_is_rejected(issuer: TokenIssuer) -> None:
    verifier = GoogleIdTokenVerifier(audience="public-client", request=issuer)
    token = issuer.token()
    header, body, _ = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    del claims["sub"]
    payload = (header + "." + encoded(json.dumps(claims).encode()).decode()).encode()
    signature = issuer.key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(InvalidIdTokenError):
        await verifier.verify(token=(payload + b"." + encoded(signature)).decode())


@pytest.mark.parametrize("token", [None, "", "x" * (MAX_TOKEN_CHARS + 1), "not-a-jwt"])
async def test_absent_oversized_or_malformed_bearers_are_rejected(
    issuer: TokenIssuer, token: str | None
) -> None:
    verifier = GoogleIdTokenVerifier(audience="public-client", request=issuer)
    with pytest.raises(InvalidIdTokenError):
        await verifier.verify(token=token)


async def test_certificate_fetch_failure_is_unavailable_not_invalid(issuer: TokenIssuer) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificates unreachable", request=request)

    with httpx.Client(transport=httpx.MockTransport(refuse)) as client:
        verifier = GoogleIdTokenVerifier(
            audience="public-client", request=HttpCertificateRequest(client=client)
        )
        with pytest.raises(CertificatesUnavailableError):
            await verifier.verify(token=issuer.token())


async def test_certificate_server_error_is_unavailable(issuer: TokenIssuer) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503))) as client:
        verifier = GoogleIdTokenVerifier(
            audience="public-client", request=HttpCertificateRequest(client=client)
        )
        with pytest.raises(CertificatesUnavailableError):
            await verifier.verify(token=issuer.token())
