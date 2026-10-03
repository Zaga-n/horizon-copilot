"""Send deterministic content-redaction and ancestor-tree canaries over OTLP/HTTP."""

# ruff: noqa: INP001 -- standalone development CLI, not an importable package.

import argparse
import json
import sys
import time
import urllib.request
from uuid import uuid4


def attribute(key: str, value: str | int) -> dict[str, object]:
    encoded = {"stringValue": value} if isinstance(value, str) else {"intValue": str(value)}
    return {"key": key, "value": encoded}


def send(endpoint: str, signal: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(
        endpoint.rstrip("/") + "/v1/" + signal,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
    partial = result.get("partialSuccess", {})
    rejected = partial.get("rejectedSpans", 0) or partial.get("rejectedDataPoints", 0)
    if int(rejected):
        raise RuntimeError("Collector rejected canary telemetry")


def spans(
    *, trace_id: str, conversation_id: str, attempt: int, service: str
) -> list[dict[str, object]]:
    started = time.time_ns() - 2_000_000_000
    definitions = [
        ("gen_ai.invoke_agent", "1111111111111111", "", "invoke_agent"),
        ("app.agent", "2222222222222222", "1111111111111111", ""),
        ("app.retrieval", "3333333333333333", "2222222222222222", ""),
        ("app.embedding", "4444444444444444", "3333333333333333", "embeddings"),
        ("gen_ai.chat", "5555555555555555", "2222222222222222", "chat"),
        ("app.persistence", "6666666666666666", "1111111111111111", ""),
    ]
    if service == "horizon-ingestion":
        definitions = [
            ("app.job", "1111111111111111", "", ""),
            ("app.embedding", "4444444444444444", "1111111111111111", "embeddings"),
            ("app.publication", "6666666666666666", "1111111111111111", ""),
        ]
    span_ids = {definition[1]: uuid4().hex[:16] for definition in definitions}
    result = []
    for name, span_id, parent_id, operation in definitions:
        attrs = [attribute("gen_ai.operation.name", operation)] if operation else []
        attrs += [attribute("gen_ai.input.messages", "CANARY_PRIVATE_CONTENT")]
        attrs += [attribute("authorization", "CANARY_PRIVATE_SECRET")]
        if not parent_id:
            attrs += [
                attribute("app.conversation.id", conversation_id),
                attribute("app.thread.id", conversation_id),
                attribute("app.turn.id", conversation_id),
                attribute("app.run.id", str(uuid4())),
                attribute("app.user_message.id", conversation_id),
                attribute("app.assistant_message.id", str(uuid4())),
                attribute("app.attempt.number", attempt),
                attribute("app.prompt.version", "canary-v1"),
                attribute("app.agent.version", "canary-agent-v1"),
                attribute("app.retrieval.version", "canary-retrieval-v1"),
                attribute("app.chunk.ids", "canary-chunk-1"),
                attribute("app.document_version.ids", "canary-version-1"),
            ]
        result.append(
            {
                "traceId": trace_id,
                "spanId": span_ids[span_id],
                "parentSpanId": span_ids[parent_id] if parent_id else "",
                "name": name,
                "kind": 1,
                "startTimeUnixNano": str(started),
                "endTimeUnixNano": str(started + 1_000_000_000),
                "attributes": attrs,
                "status": {"code": 1, "message": "CANARY_PRIVATE_STATUS"},
            }
        )
    return result


def metrics(*, attempt: int) -> list[dict[str, object]]:
    now = time.time_ns()
    definitions = [
        ("app.boundary.duration", "s", "http"),
        ("app.boundary.duration", "s", "agent"),
        ("app.boundary.duration", "s", "retrieval"),
        ("gen_ai.client.operation.duration", "s", "model"),
        ("gen_ai.client.operation.time_to_first_chunk", "s", "model"),
        ("app.agent.time_to_first_chunk", "s", "agent"),
        ("gen_ai.invoke_agent.tool_calls", "", "agent"),
        ("gen_ai.client.token.usage", "", "model"),
        ("app.ingestion.duration", "s", "job"),
        ("app.ingestion.queue.age", "s", "job"),
    ]
    result: list[dict[str, object]] = [
        {
            "name": name,
            "unit": unit,
            "histogram": {
                "aggregationTemporality": 2,
                "dataPoints": [
                    {
                        "startTimeUnixNano": str(now - 10_000_000_000),
                        "timeUnixNano": str(now),
                        "count": str(2 * attempt),
                        "sum": 1.5 * attempt,
                        "explicitBounds": [1, 2],
                        "bucketCounts": [str(attempt), str(attempt), "0"],
                        "attributes": [attribute("app.boundary", boundary)],
                    }
                ],
            },
        }
        for name, unit, boundary in definitions
    ]
    result.extend(
        {
            "name": name,
            "sum": {
                "aggregationTemporality": 2,
                "isMonotonic": True,
                "dataPoints": [
                    {
                        "startTimeUnixNano": str(now - 10_000_000_000),
                        "timeUnixNano": str(now),
                        "asInt": str(2 * attempt),
                        "attributes": [attribute("app.boundary", "http")],
                    }
                ],
            },
        }
        for name in [
            "app.boundary.failures",
            "app.ingestion.jobs",
            "app.ingestion.failures",
            "app.ingestion.chunks.completed",
            "app.ingestion.vendor.calls",
            "app.ingestion.retries",
            "app.ingestion.orphans.removed",
        ]
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otlp", default="http://localhost:4318")
    parser.add_argument(
        "--service", choices=("horizon-chat", "horizon-ingestion"), default="horizon-chat"
    )
    args = parser.parse_args()
    conversation_id = str(uuid4())
    for attempt in (1, 2):
        if attempt > 1:
            time.sleep(1)
        trace_id = uuid4().hex
        resource = {
            "attributes": [
                attribute("service.name", args.service),
                attribute("deployment.environment.name", "local"),
            ]
        }
        tree = spans(
            trace_id=trace_id,
            conversation_id=conversation_id,
            attempt=attempt,
            service=args.service,
        )
        send(
            args.otlp,
            "traces",
            {
                "resourceSpans": [
                    {
                        "resource": resource,
                        "scopeSpans": [
                            {
                                "scope": {"name": "local-stack-canary"},
                                "spans": tree,
                            }
                        ],
                    }
                ]
            },
        )
        send(
            args.otlp,
            "metrics",
            {
                "resourceMetrics": [
                    {
                        "resource": resource,
                        "scopeMetrics": [
                            {
                                "scope": {"name": "local-stack-canary"},
                                "metrics": metrics(attempt=attempt),
                            }
                        ],
                    }
                ]
            },
        )
        # Running this script in an opted-in container exercises Alloy's stdout path.
        sys.stdout.write(
            json.dumps(
                {
                    "service.name": args.service,
                    "event": "local_stack_canary",
                    "level": "INFO",
                    "trace_id": trace_id,
                    "span_id": tree[0]["spanId"],
                    "conversation_id": conversation_id,
                    "attempt": attempt,
                }
            )
            + "\n",
        )


if __name__ == "__main__":
    main()
