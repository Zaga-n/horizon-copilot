"""SSE framing belongs to HTTP; routes hand it the application's event stream."""

from collections.abc import AsyncIterator

from horizon_chat.domain.streaming import StreamEvent


async def encode_events(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    async for item in events:
        yield f"id: {item.event_id}\nevent: {item.event}\ndata: {item.model_dump_json(exclude_none=True, by_alias=True)}\n\n"
