"""One JSON stdout owner; call sites use stdlib extra= and never emit content."""

from horizon_observability import JsonLogFormatter, install_json_logging

EVENTS = frozenset(
    {
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "retention_completed",
        "loop_dependency_unavailable",
        "loop_recovered",
        "loop_crashed",
        "retention_item_failed",
        "recovery_item_failed",
        "recovery_completed",
        "request_failed",
        "guardrail_blocked",
        "loop_shutdown_cancelled",
        "retrieval_row_corrupt",
    }
)
FIELDS = frozenset(
    {
        "conversation_id",
        "turn_id",
        "run_id",
        "assistant_message_id",
        "attempt_number",
        "failure_category",
        "persistence_pending",
        "purged_count",
        "failed_count",
        "loop",
        "recovery_path",
        "repaired_count",
        "chunk_id",
    }
)


class JsonFormatter(JsonLogFormatter):
    """Chat's allowlists over the shared safe JSON format."""

    def __init__(self, *, full_exception_trace: bool) -> None:
        super().__init__(
            service_name="horizon-chat",
            logger_prefix="horizon_chat",
            events=EVENTS,
            fields=FIELDS,
            full_exception_trace=full_exception_trace,
        )


def configure_logging(*, level: str, full_exception_trace: bool) -> None:
    install_json_logging(
        formatter=JsonFormatter(full_exception_trace=full_exception_trace), level=level
    )
