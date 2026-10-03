"""Classification bases describing how an external capability failed."""


class DependencyUnavailableError(Exception):
    """Transient: the same request may succeed once the dependency recovers."""

    def __init__(self, error_code: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retry_after_seconds = retry_after_seconds


class RejectedError(Exception):
    """Permanent for this item: repeating the same request fails the same way."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class DataIntegrityError(RejectedError):
    """Stored data violates an invariant, so the affected item cannot be processed.

    Permanent like any rejection, but our own defect: it surfaces as a server error.
    """
