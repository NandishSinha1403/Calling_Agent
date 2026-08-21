"""A tiny in-process pub/sub for call events.

The voice bridge publishes transcripts and tool calls here; anything that wants
to watch a call live subscribes. Used by the browser demo page, and by the admin
panel later.

Why this is separate from the media WebSocket: that socket carries Twilio's
protocol and nothing else. Sending our own event types down it would risk
confusing a real Twilio connection, and would tie observability to one
transport. A separate channel keeps the media path exactly as Twilio expects.

In-process only, no persistence -- matches the PRD's no-database scope. If a
subscriber is slow, its events are dropped rather than allowed to back up:
observability must never add latency to a live call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

_subscribers: set[asyncio.Queue] = set()

# Small on purpose. A backed-up subscriber means a slow browser tab, and the
# right response is to drop its events, never to slow the call down.
QUEUE_SIZE = 64


def publish(kind: str, **fields: Any) -> None:
    """Publish an event. Never raises, never blocks."""
    event = {"kind": kind, **fields}
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow subscriber; dropping is the correct behaviour here


async def subscribe() -> AsyncIterator[dict[str, Any]]:
    """Yield events until the consumer stops listening."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _subscribers.add(queue)
    log.info("Event subscriber added (%d total)", len(_subscribers))
    try:
        while True:
            yield await queue.get()
    finally:
        _subscribers.discard(queue)
        log.info("Event subscriber removed (%d left)", len(_subscribers))


def subscriber_count() -> int:
    return len(_subscribers)
