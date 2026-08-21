"""Voice channel HTTP + WebSocket routes.

    POST /call               trigger an outbound call
    POST /twiml              what Twilio fetches when the callee answers
    WS   /ws/media-stream    the bidirectional audio pipe

Transport only. No persona or domain logic -- see core/ for that.

The bridge runs two concurrent tasks per call:

    caller -> Gemini   mu-law 8k  -> PCM16 16k -> Live session
    Gemini -> caller   PCM16 24k  -> mu-law 8k -> Twilio media event

An echo mode is kept for diagnosis (--echo / ECHO_MODE=1). When a call is
silent, being able to flip back to the echo in one env var immediately answers
"is this Twilio's side or Gemini's side?", which is the question that otherwise
costs an hour.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from fastapi import APIRouter, Form, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from app.channels.voice.audio_utils import (
    TWILIO_FRAME_BYTES,
    CallAudioBridge,
    pcm16_rms,
)
from app.channels.voice.twilio_call import trigger_call
from app.config import ConfigError, settings
from app.core.gemini_live import GeminiLiveSession

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
async def twiml(mode: str = "") -> Response:
    """TwiML telling Twilio to open a bidirectional Media Stream to us.

    <Connect><Stream> requires an ABSOLUTE wss:// URL. A relative path or an
    https:// URL fails, sometimes silently. config.websocket_url does that
    conversion in one place and raises on a bad scheme.
    """
    if mode == "say":
        # Diagnostic: does TwiML execute at all? If this speaks but <Stream>
        # produces silence, the fault is specific to Media Streams rather than
        # to our TwiML being served or parsed.
        log.info("Serving DIAGNOSTIC <Say> TwiML")
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                "<Response><Say>TwiML is working. If you can hear this, the "
                "problem is the media stream, not the webhook.</Say>"
                "<Pause length='2'/></Response>"
            ),
            media_type="application/xml",
        )

    try:
        ws_url = settings.websocket_url
    except ConfigError as exc:
        log.error("Cannot build TwiML: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if mode == "external":
        # Diagnostic: point <Stream> at a PUBLIC WebSocket service instead of our
        # ngrok tunnel. This isolates one variable — if Twilio can open a stream
        # to somewhere else, the fault is our tunnel; if it cannot open one
        # anywhere, the restriction is on Twilio's side.
        external = os.getenv("EXTERNAL_WS", "wss://ws.postman-echo.com/raw")
        log.info("Serving DIAGNOSTIC external <Stream> -> %s", external)
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Response><Connect><Stream url="{external}" /></Connect></Response>'
            ),
            media_type="application/xml",
        )

    if mode == "start":
        # Diagnostic: <Start><Stream> is one-way (caller audio to us, nothing
        # back) and is a DIFFERENT Twilio feature path from <Connect><Stream>.
        # If the WebSocket connects here but not with <Connect>, the account
        # can do Media Streams and only bidirectional streaming is blocked.
        # The <Say> keeps the call alive long enough for the stream to open.
        log.info("Serving DIAGNOSTIC <Start><Stream> TwiML -> %s", ws_url)
        return Response(
            content=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                f'<Response><Start><Stream url="{ws_url}" /></Start>'
                "<Say>Testing one way media stream. Please say something now.</Say>"
                "<Pause length='8'/></Response>"
            ),
            media_type="application/xml",
        )

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


def _echo_mode() -> bool:
    """Diagnostic fallback, off by default. ECHO_MODE=1 to enable."""
    return os.getenv("ECHO_MODE", "").strip() in {"1", "true", "yes"}


@router.websocket("/ws/media-stream")
async def media_stream(ws: WebSocket) -> None:
    """Twilio Media Stream endpoint: bridge the caller to a Gemini Live session.

    Twilio's sequence is: connected, start (carries streamSid), media every
    ~20ms, then stop.

    Two hazards handled here, both of which produce confusing symptoms:

    1. STARTUP RACE. Twilio streams audio the instant the socket upgrades, but
       connecting to Gemini takes a moment. We open the Gemini session
       immediately rather than waiting for the start event, and buffer inbound
       frames until it is ready, so the caller's opening words are not lost.

    2. BARGE-IN. If the caller talks over the agent, Gemini says so, but audio
       we already handed Twilio keeps playing regardless. Without forwarding a
       `clear` event, the agent talks over the caller for as long as the buffer
       lasts, which makes a demo feel broken.
    """
    await ws.accept()
    log.info("Media stream connected (echo=%s)", _echo_mode())

    if _echo_mode():
        await _run_echo(ws)
        return

    audio = CallAudioBridge()
    state: dict[str, object] = {
        "stream_sid": None, "frames_in": 0, "frames_out": 0,
        "loudest": 0, "heard_speech": False,
    }
    # Buffers the caller's first words while the Gemini session is still
    # connecting. Bounded, because unbounded growth on a stalled connect would
    # add latency that never recovers.
    pending: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
    ready = asyncio.Event()

    async def send_media(ulaw: bytes) -> None:
        """Send agent audio to Twilio in 20ms frames.

        Two things matter here:

        - Outbound media MUST carry the streamSid or Twilio discards it.
        - Gemini returns audio in large chunks (hundreds of ms). Forwarding one
          of those as a single media event hands Twilio a burst far bigger than
          the 20ms frames it streams, which risks choppy playback. Cutting it to
          frame-sized pieces matches what Twilio expects in both directions.
        """
        sid = state["stream_sid"]
        if not sid or not ulaw:
            return
        for i in range(0, len(ulaw), TWILIO_FRAME_BYTES):
            frame = ulaw[i : i + TWILIO_FRAME_BYTES]
            await ws.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": sid,
                        "media": {"payload": base64.b64encode(frame).decode()},
                    }
                )
            )
            state["frames_out"] = int(state["frames_out"]) + 1

    async def clear_playback() -> None:
        """Tell Twilio to drop audio it has queued but not yet played."""
        sid = state["stream_sid"]
        if sid:
            await ws.send_text(json.dumps({"event": "clear", "streamSid": sid}))

    async def from_twilio(session: GeminiLiveSession) -> None:
        """Read Twilio events; convert and forward caller audio to Gemini."""
        while True:
            message = json.loads(await ws.receive_text())
            event = message.get("event")

            if event == "start":
                state["stream_sid"] = message["start"]["streamSid"]
                log.info("Stream started: %s", state["stream_sid"])

            elif event == "media":
                state["frames_in"] = int(state["frames_in"]) + 1
                ulaw = base64.b64decode(message["media"]["payload"])
                pcm16k = audio.caller_to_gemini(ulaw)
                # Loudness tracking. A connected-but-silent call is nearly
                # always a format error, and this shows whether the caller's
                # audio is arriving at all. Track the PEAK across a window
                # rather than one frame: a single 20ms sample lands in a pause
                # as often as in speech, and reads as silence either way.
                loudest = max(int(state["loudest"]), pcm16_rms(pcm16k))
                state["loudest"] = loudest
                if loudest >= 200:
                    state["heard_speech"] = True
                if int(state["frames_in"]) % 250 == 0:
                    # Only flag silence if we have heard NOTHING all call. A
                    # quiet window mid-call is just someone not talking, and
                    # crying wolf there would train us to ignore the warning.
                    never_heard = not state["heard_speech"]
                    log.info(
                        "%s frames in | %d bytes -> %d bytes PCM16 | peak RMS %d%s",
                        state["frames_in"], len(ulaw), len(pcm16k), loudest,
                        "  <-- NO AUDIO ALL CALL, check format" if never_heard else "",
                    )
                    state["loudest"] = 0
                if not ready.is_set():
                    # Gemini is still connecting; hold the audio briefly.
                    if not pending.full():
                        await pending.put(pcm16k)
                    continue
                await session.send_audio(pcm16k)

            elif event == "stop":
                log.info("Twilio sent stop")
                return

    async def drain_pending(session: GeminiLiveSession) -> None:
        """Flush anything buffered during connect, then mark the path open."""
        while not pending.empty():
            await session.send_audio(pending.get_nowait())
        ready.set()

    async def to_twilio(session: GeminiLiveSession) -> None:
        """Convert Gemini's replies to mu-law and hand them to Twilio."""
        async for kind, payload in session.events():
            if kind == "audio":
                await send_media(audio.gemini_to_caller(payload))
            elif kind == "interrupted":
                # Drop whatever Twilio has queued, or the agent keeps talking
                # over the caller for the length of the buffer.
                await clear_playback()
                log.info("Barge-in: cleared queued audio")
            elif kind == "transcript":
                log.info("agent: %s", payload)
            elif kind == "input_transcript":
                log.info("caller: %s", payload)

    try:
        # Connect to Gemini FIRST, before Twilio's start event arrives.
        async with GeminiLiveSession() as session:
            await drain_pending(session)
            # Speak first: on an outbound call the callee has just said hello
            # and is waiting. Silence reads as a dropped call.
            await session.send_text("The call has connected. Greet the person now.")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(from_twilio(session))
                tg.create_task(to_twilio(session))

    except* WebSocketDisconnect:
        log.info("Caller hung up")
    except* Exception as group:  # noqa: BLE001
        # One failed call must not take the server down mid-demo.
        for exc in group.exceptions:
            log.error("Call failed: %s: %s", type(exc).__name__, exc)
    finally:
        log.info(
            "Call ended: %s frames in, %s frames out",
            state["frames_in"],
            state["frames_out"],
        )


async def _run_echo(ws: WebSocket) -> None:
    """Diagnostic mode: send the caller's own audio back, Gemini uninvolved.

    Kept because when a call goes silent, flipping ECHO_MODE=1 answers "Twilio
    side or Gemini side?" in one restart, which is the question that otherwise
    costs an hour of guessing.
    """
    stream_sid: str | None = None
    frames = 0
    try:
        while True:
            message = json.loads(await ws.receive_text())
            event = message.get("event")
            if event == "start":
                stream_sid = message["start"]["streamSid"]
                log.info("Echo stream started: %s", stream_sid)
            elif event == "media":
                frames += 1
                if stream_sid:
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
                break
    except WebSocketDisconnect:
        log.info("Caller hung up during echo")
    except Exception:  # noqa: BLE001
        log.exception("Echo failed")
    finally:
        log.info("Echo ended after %d frames", frames)
