"""Shared OpenTelemetry providers, JSON logging, boundary spans and trace carriers."""

from horizon_observability.logs import JsonLogFormatter, install_json_logging
from horizon_observability.providers import (
    DEFAULT_SAMPLER,
    Providers,
    ResourceIdentity,
    open_providers,
)
from horizon_observability.spans import (
    FailureOutcome,
    boundary_span,
    extract_link,
    inject_carrier,
    mark_error,
    outcome_of,
)

__all__ = [
    "DEFAULT_SAMPLER",
    "FailureOutcome",
    "JsonLogFormatter",
    "Providers",
    "ResourceIdentity",
    "boundary_span",
    "extract_link",
    "inject_carrier",
    "install_json_logging",
    "mark_error",
    "open_providers",
    "outcome_of",
]
