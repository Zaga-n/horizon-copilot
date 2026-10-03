"""Process telemetry setup: this service's resource identity over the shared providers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from uuid import uuid4

from horizon_chat.observability.metrics import Measurements
from horizon_chat.observability.tracing import Telemetry
from horizon_observability import ResourceIdentity, open_providers


@asynccontextmanager
async def open_telemetry(
    *,
    environment: str,
    endpoint: str | None,
    instance_id: str | None,
) -> AsyncIterator[Telemetry]:
    identity = ResourceIdentity(
        namespace="horizon",
        name="horizon-chat",
        version=version("horizon-chat"),
        instance_id=instance_id or str(uuid4()),
        environment=environment,
    )
    async with open_providers(identity=identity, endpoint=endpoint) as providers:
        yield Telemetry(
            tracer=providers.tracer_provider.get_tracer("horizon_chat"),
            measurements=Measurements(meter=providers.meter_provider.get_meter("horizon_chat")),
        )
