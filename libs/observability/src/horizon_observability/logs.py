"""One JSON stdout format: allowlisted events and fields, never messages or exception text."""

import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import TypedDict

from opentelemetry import trace

BASE_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
MAX_EXCEPTION_FRAMES = 32
OTHER_EVENT = "library_log"
POOL_LOGGER = "psycopg.pool"
# Match reviewed, unformatted upstream templates; arguments can contain credentials.
POOL_EVENTS = {
    "error connecting in %r: %s": "database_connection_failed",
    "reconnection attempt in pool %r failed after %s sec": "database_reconnection_failed",
    "discarding broken connection: %s": "database_broken_connection_discarded",
    "discarding closed connection: %s": "database_closed_connection_discarded",
    "rolling back returned connection: %s": "database_returned_connection_rolled_back",
    "rollback failed: %s: %s. Discarding connection %s": "database_connection_rollback_failed",
    "closing returned connection: %s": "database_active_connection_closed",
    "error resetting connection: %s": "database_connection_reset_failed",
    "task run %s failed: %s: %s": "database_pool_task_failed",
}
SQLSTATE_PATTERN = re.compile(r"[0-9A-Z]{5}")
PoolErrorFields = TypedDict("PoolErrorFields", {"error.type": str, "db.sqlstate": str}, total=False)


def _library_event(record: logging.LogRecord) -> str:
    if record.name == POOL_LOGGER and isinstance(record.msg, str):
        return POOL_EVENTS.get(record.msg, OTHER_EVENT)
    return OTHER_EVENT


def _pool_error_fields(record: logging.LogRecord) -> PoolErrorFields:
    """Extract bounded error metadata without rendering the upstream log arguments."""
    fields: PoolErrorFields = {}
    if not isinstance(record.args, tuple):
        return fields
    error = next((arg for arg in record.args if isinstance(arg, BaseException)), None)
    if error is not None:
        fields["error.type"] = type(error).__name__
        sqlstate = getattr(error, "sqlstate", None)
        if isinstance(sqlstate, str) and SQLSTATE_PATTERN.fullmatch(sqlstate):
            fields["db.sqlstate"] = sqlstate
    return fields


class JsonLogFormatter(logging.Formatter):
    """Allowlisted metadata plus safe exception classification, with no raw message text."""

    def __init__(
        self,
        *,
        service_name: str,
        logger_prefix: str,
        events: frozenset[str],
        fields: frozenset[str],
        full_exception_trace: bool,
    ) -> None:
        super().__init__()
        self.service_name = service_name
        self.logger_prefix = logger_prefix
        self.events = events
        self.fields = fields
        self.full_exception_trace = full_exception_trace

    def format(self, record: logging.LogRecord) -> str:
        event = (
            record.msg
            if record.name.startswith(self.logger_prefix)
            and isinstance(record.msg, str)
            and record.msg in self.events
            else _library_event(record)
        )
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": event,
            "service.name": self.service_name,
            "logger.name": record.name,
        }
        payload.update({key: record.__dict__[key] for key in self.fields if key in record.__dict__})
        if record.name == POOL_LOGGER and event != OTHER_EVENT:
            payload.update(_pool_error_fields(record))
        unknown = set(record.__dict__) - BASE_RECORD_FIELDS - self.fields
        if unknown:
            payload["dropped_fields"] = sorted(unknown)
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload.update(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}")
        if record.exc_info and record.exc_info[1] is not None:
            payload["error.type"] = type(record.exc_info[1]).__name__
            if self.full_exception_trace:
                # File/function/line only: messages and source lines may carry secrets or content.
                payload["exception.frames"] = [
                    {"file": frame.filename, "function": frame.name, "line": frame.lineno}
                    for frame in traceback.extract_tb(record.exc_info[2])[-MAX_EXCEPTION_FRAMES:]
                ]
        return json.dumps(payload, ensure_ascii=False)


def install_json_logging(*, formatter: logging.Formatter, level: str) -> None:
    """Make `formatter` on stdout the root logger's only handler; bootstrap calls this once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
