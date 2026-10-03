# horizon-google-identity

Google ID-token verification shared by `horizon-chat` and `horizon-ingestion`: signature,
issuer, audience and expiry through google-auth, a token-size bound, the subject taken from `sub`
only, and a bounded certificate fetch over a borrowed `httpx.Client`.

**Kind:** client. **Importers:** each service's `adapters/` (a thin wrapper that maps this
library's errors to the service's identity port errors) and `bootstrap/` (which creates and closes
the HTTP client). Enforced by import-linter contracts in the workspace `pyproject.toml`.

## Why this is shared

Both services verify the same `google_client_id` audience with byte-identical code. This is
security-sensitive: if one copy fixes a verification rule and the other does not, the two
services accept different tokens. A sync comment does not prevent that drift.

**Extraction trigger:** two deployables must agree on one rule (who a bearer is).

**Stays in each service:** the identity port and its errors, the error-mapping adapter,
`LocalIdentityVerifier` (development only, gated by each service's settings validator) and the
choice between local and Google mode.

## Contract

- One verification attempt per call; the blocking google-auth call runs in a worker thread.
- No environment reads; the audience and HTTP client are passed in.
- `InvalidIdTokenError`: the bearer is not a valid Google ID token for the audience.
  `CertificatesUnavailableError`: Google's public keys could not be fetched; retry later.
