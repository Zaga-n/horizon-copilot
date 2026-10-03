"""Driver failures split into integrity, rejection and outage; bugs are never relabelled."""

import pytest
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from horizon_ingestion.db.transactions import translate_database_error
from horizon_ingestion.ports.errors import (
    DataIntegrityError,
    DependencyUnavailableError,
    RejectedError,
)


def driver(error: type[DBAPIError], *, invalidated: bool = False) -> DBAPIError:
    return error("SELECT 1", {}, Exception("driver"), connection_invalidated=invalidated)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(driver(IntegrityError), DataIntegrityError, id="constraint"),
        pytest.param(driver(DataError), RejectedError, id="data"),
        pytest.param(driver(OperationalError), DependencyUnavailableError, id="operational"),
        pytest.param(driver(InterfaceError), DependencyUnavailableError, id="interface"),
        pytest.param(PoolTimeoutError(), DependencyUnavailableError, id="pool-timeout"),
        pytest.param(ConnectionResetError(), DependencyUnavailableError, id="os-error"),
        pytest.param(
            driver(DBAPIError, invalidated=True), DependencyUnavailableError, id="invalidated"
        ),
    ],
)
def test_driver_failures_map_to_their_classification(
    failure: Exception, expected: type[Exception]
) -> None:
    assert type(translate_database_error(failure)) is expected


def test_programming_errors_are_re_raised_unchanged() -> None:
    assert translate_database_error(driver(ProgrammingError)) is None
