"""Library-owned provider failures and the one Bedrock error-code classification."""

from botocore.exceptions import BotoCoreError, ClientError

# Only a refusal of this request is the caller's input; throttling, outages, credentials,
# permissions and missing models fail every request alike and wait for recovery.
REJECTED_CODES = frozenset({"ValidationException"})
# Bedrock reports an unknown or region-unsupported model as ValidationException with no
# separate code, so only the message tells a misconfigured model apart from bad input.
# Each phrase is pinned by a unit test; an unknown message keeps the rejected default.
MODEL_IDENTIFIER_PHRASES = ("model identifier", "model id", "on-demand throughput")


class GenAIError(Exception):
    """Base of every failure this library classifies."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class GenAIProviderUnavailableError(GenAIError):
    """Transient or operator-fixable: throttling, 5xx, timeouts, credentials, missing model."""

    def __init__(self, error_code: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(error_code)
        self.retry_after_seconds = retry_after_seconds


class GenAIProviderRejectedError(GenAIError):
    """The provider refused this request itself; resending it fails the same way."""


class GenAIProtocolError(GenAIError):
    """A successful response did not carry a usable result."""


def _retry_after(exc: ClientError) -> float | None:
    hint = exc.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after", "")
    return float(hint) if hint.isdecimal() else None


def _names_model_identifier(exc: ClientError) -> bool:
    message = str(exc.response.get("Error", {}).get("Message", "")).casefold()
    return any(phrase in message for phrase in MODEL_IDENTIFIER_PHRASES)


def classify_bedrock_error(exc: BaseException) -> GenAIError | None:
    """The library error for an SDK or LangChain provider failure; None means not a provider one.

    LangChain reports a 2xx body it cannot parse as ValueError, so ValueError raised by a
    provider call is a protocol failure.
    """
    if isinstance(exc, ClientError):
        rejected = exc.response.get("Error", {}).get("Code") in REJECTED_CODES
        if rejected and not _names_model_identifier(exc):
            return GenAIProviderRejectedError("provider_rejected")
        return GenAIProviderUnavailableError(
            "provider_unavailable", retry_after_seconds=_retry_after(exc)
        )
    if isinstance(exc, BotoCoreError):
        return GenAIProviderUnavailableError("provider_unavailable")
    if isinstance(exc, ValueError):
        return GenAIProtocolError("provider_protocol")
    return None
