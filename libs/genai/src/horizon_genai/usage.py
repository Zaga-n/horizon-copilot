"""Titan token usage observed through botocore's event hooks, without patching the client."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.response import StreamingBody

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

INVOKE_MODEL_EVENT = "after-call.bedrock-runtime.InvokeModel"


@dataclass(frozen=True, slots=True, kw_only=True)
class _UsageBody:
    """Read once for the consumer, then report `inputTextTokenCount` when present."""

    body: StreamingBody
    on_usage: Callable[[int], None]

    def read(self) -> bytes:
        try:
            data = self.body.read()
        finally:
            self.body.close()
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return data
        count = payload.get("inputTextTokenCount") if isinstance(payload, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            self.on_usage(count)
        return data


def observe_titan_usage(*, client: "BedrockRuntimeClient", on_usage: Callable[[int], None]) -> None:
    """Report each InvokeModel response's input token count; adds no SDK attempts."""

    def wrap(parsed: dict[str, object], **_: object) -> None:
        body = parsed.get("body")
        if isinstance(body, StreamingBody):
            parsed["body"] = _UsageBody(body=body, on_usage=on_usage)

    client.meta.events.register(INVOKE_MODEL_EVENT, wrap)
