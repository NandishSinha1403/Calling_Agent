"""Voice channel HTTP + WebSocket routes.

    POST /call               trigger an outbound call
    POST /twiml              what Twilio fetches when the callee answers
    WS   /ws/media-stream    the bidirectional audio pipe

Transport only. No persona or domain logic -- see core/ for that.

STEP 3 NOTE: the WebSocket is currently a pure ECHO. It sends the caller's own
audio back to them, with Gemini deliberately not involved. That proves Twilio's
framing, base64 handling and streamSid routing work on their own. If a call is
silent at this stage the fault is unambiguously on the Twilio side, which is
worth far more than debugging a full pipeline where either half could be wrong.
Step 4 replaces the echo with the real bridge.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.channels.voice.twilio_call import trigger_call
from app.config import ConfigError, settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice"])


@router.post("/call")
async def start_call(to: str = Form(...)) -> dict[str, str]:
    """Place an outbound call.

        curl -X POST localhost:8000/call -d "to=+15551234567"

    No auth: single operator, MVP scope (PRD section 6).
    """
    try:
        sid = trigger_call(to)
    except ConfigError as exc:
        # Missing config is our fault, not a bad request -- say so clearly
        # rather than letting a Twilio SDK error surface half-explained.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to place call to %s", to)
        raise HTTPException(status_code=502, detail=f"Twilio rejected: {exc}") from exc
    return {"status": "calling", "to": to, "call_sid": sid}


@router.post("/twiml")
async def twiml() -> Response:
    """TwiML telling Twilio to open a bidirectional Media Stream to us.

    <Connect><Stream> requires an ABSOLUTE wss:// URL. A relative path or an
    https:// URL fails, sometimes silently. config.websocket_url does that
    conversion in one place and raises on a bad scheme.
    """
    try:
        ws_url = settings.websocket_url
    except ConfigError as exc:
        log.error("Cannot build TwiML: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{ws_url}" />'
        "</Connect>"
        "</Response>"
    )
    log.info("Serving TwiML -> %s", ws_url)
    return Response(content=body, media_type="application/xml")


@router.websocket("/ws/media-stream")
async def media_stream(ws: WebSocket) -> None:
    """Twilio Media Stream endpoint. Step 3: echoes audio straight back.

    Twilio's message sequence is: connected, start (carries streamSid), then
    media every ~20ms, then stop. Outbound media MUST carry the streamSid from
    the start event or Twilio silently discards it -- a classic cause of "the
    call connects but the caller hears nothing".
    """
    await ws.accept()
    stream_sid: str | None = None
    frames = 0
    log.info("Media stream connected")

    try:
        while True:
            message = json.loads(await ws.receive_text())
            event = message.get("event")

            if event == "start":
                stream_sid = message["start"]["streamSid"]
                log.info("Stream started: %s", stream_sid)

            elif event == "media":
                frames += 1
                if stream_sid is None:
                    # Media before start would be undeliverable anyway.
                    continue
                await ws.send_text(
                    json.dumps(
                        {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": message["media"]["payload"]},
                        }
                    )
                )

            elif event == "stop":
                log.info("Stream stopped after %d frames", frames)
                break

    except WebSocketDisconnect:
        log.info("Caller hung up after %d frames", frames)
    except Exception:  # noqa: BLE001
        # One bad call must not take the server down mid-demo.
        log.exception("Media stream failed after %d frames", frames)
    finally:
        log.info("Media stream closed (%d frames)", frames)
