"""Liveness follows the supervised loops: a crash fails it, an outage does not."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from horizon_chat.bootstrap.app import create_app
from horizon_chat.bootstrap.supervisor import (
    LoopPolicy,
    ProcessHealth,
    SupervisedLoop,
    supervised_loops,
)
from horizon_chat.config.secrets import Secrets
from horizon_chat.config.settings import Settings
from horizon_chat.ports.conversations import ConversationStoreUnavailableError

POLICY = LoopPolicy(interval_seconds=0.001, max_backoff_seconds=0.004)


class Crashes:
    async def __call__(self) -> bool:
        raise RuntimeError("bug")


class Outage:
    async def __call__(self) -> bool:
        raise ConversationStoreUnavailableError("database_unavailable")


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_DSN", "postgresql://chat@127.0.0.1:1/horizon")
    monkeypatch.setenv("CHECKPOINT_DATABASE_DSN", "postgresql://chat@127.0.0.1:1/horizon")


async def probe_liveness(settings: Settings, iteration: SupervisedLoop) -> httpx.Response:
    """Run the app lifespan with one supervised loop, then probe once the loop has run."""

    @asynccontextmanager
    async def runtime(
        _: Settings, __: Secrets, health: ProcessHealth
    ) -> AsyncIterator[SimpleNamespace]:
        async with supervised_loops([iteration], health=health, grace_seconds=1):
            yield SimpleNamespace()

    app = create_app(settings=settings, runtime_builder=runtime)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/health")


async def test_a_crashed_loop_fails_liveness(chat_settings: Settings) -> None:
    response = await probe_liveness(
        chat_settings,
        SupervisedLoop(name="conversation-retention", iteration=Crashes(), policy=POLICY),
    )
    assert response.status_code == 503
    assert response.json() == {"status": "stopped"}


async def test_a_loop_backing_off_from_an_outage_stays_alive(chat_settings: Settings) -> None:
    response = await probe_liveness(
        chat_settings,
        SupervisedLoop(name="conversation-retention", iteration=Outage(), policy=POLICY),
    )
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
