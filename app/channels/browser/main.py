"""Browser demo channel: talk to the agent from a web page.

    GET /demo          the page
    WS  /ws/events     live transcript and tool-call feed

The page connects to the EXISTING /ws/media-stream endpoint and speaks Twilio's
Media Stream protocol -- the same JSON events, the same base64 mu-law in 20ms
frames. That is deliberate and is the whole point: the bridge needs no changes,
and this exercises the identical code path a phone call uses, rather than being
a parallel implementation that could work while the real one is broken.

It exists because both Twilio and Telnyx gate telephony features behind paid
accounts. This needs no provider, no phone number and no verification, and it
demonstrates the actual system.

Transport only. No persona or domain logic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.event_bus import subscribe

log = logging.getLogger(__name__)

router = APIRouter(tags=["browser"])

_PAGE = Path(__file__).parent / "static" / "demo.html"


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> HTMLResponse:
    return HTMLResponse(_PAGE.read_text())


@router.websocket("/ws/events")
async def events(ws: WebSocket) -> None:
    """Live feed of transcripts and tool calls for anything watching a call.

    Kept separate from the media socket so that socket stays pure Twilio
    protocol -- mixing our own event types into it would risk confusing a real
    Twilio connection.
    """
    await ws.accept()
    try:
        async for event in subscribe():
            await ws.send_text(json.dumps(event))
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.info("Event subscriber closed", exc_info=True)
