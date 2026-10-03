"""One JSON stdout owner; call sites use stdlib extra= and never emit content."""

from horizon_observability import JsonLogFormatter, install_json_logging

EVENTS = frozenset(
    {
        "ingestion_upload_accepted",
        "ingestion_job_ready",
        "ingestion_job_failed",
        "ingestion_job_retry",
        "ingestion_claim_fenced",
        "ingestion_listener_reconnecting",
        "ingestion_failure_record_unavailable",
        "ingestion_loop_unavailable",
        "ingestion_loop_recovered",
        "ingestion_loop_crashed",
        "ingestion_document_deleted",
        "ingestion_cleanup_complete",
        "ingestion_job_corrupt",
        "ingestion_claim_release_unavailable",
        "ingestion_heartbeat_unavailable",
        "ingestion_heartbeat_failed",
        "ingestion_status_corrupt",
        "request_failed",
    }
)
FIELDS = frozenset(
    {
        "job_id",
        "document_id",
        "version_id",
        "generation",
        "attempt",
        "category",
        "kind",
        "deduplicated",
        "state",
        "loop",
    }
)


class JsonFormatter(JsonLogFormatter):
    """Ingestion's allowlists over the shared safe JSON format."""

    def __init__(self, *, full_exception_trace: bool) -> None:
        super().__init__(
            service_name="horizon-ingestion",
            logger_prefix="horizon_ingestion",
            events=EVENTS,
            fields=FIELDS,
            full_exception_trace=full_exception_trace,
        )


def configure_logging(*, level: str, full_exception_trace: bool) -> None:
    install_json_logging(
        formatter=JsonFormatter(full_exception_trace=full_exception_trace), level=level
    )
