"""Browser preflights allow only the configured frontend origin and required headers."""

import httpx

from horizon_chat.bootstrap.app import create_app
from horizon_chat.config.settings import Settings


async def test_frontend_preflight_includes_authorization_and_idempotency(
    chat_settings: Settings,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=chat_settings)),
        base_url="http://test",
    ) as client:
        response = await client.options(
            "/v1/conversations",
            headers={
                "Origin": chat_settings.frontend_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type, idempotency-key",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == chat_settings.frontend_origin


async def test_unapproved_frontend_origin_is_rejected(chat_settings: Settings) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings=chat_settings)),
        base_url="http://test",
    ) as client:
        response = await client.options(
            "/v1/conversations",
            headers={
                "Origin": "https://attacker.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization",
            },
        )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
