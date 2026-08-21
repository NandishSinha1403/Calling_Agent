"""Bridge tests with Gemini faked out.

The two hazards CLAUDE.md flags -- the startup race and barge-in -- are timing
bugs that are miserable to reproduce on a real call and trivial to pin down with
a stub session. Neither the network nor trial minutes are involved here.
"""

import asyncio
import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.channels.voice.audio_utils import TWILIO_FRAME_BYTES, pcm16_to_ulaw
from app.main import app


class FakeSession:
    """Stands in for GeminiLiveSession.

    Records the audio it was sent, and emits whatever events a test queues.
    """

    instances: list["FakeSession"] = []

    def __init__(self, *_args, **_kwargs):
        self.sent_audio: list[bytes] = []
        self.sent_text: list[str] = []
        self.outbound: asyncio.Queue = asyncio.Queue()
        self.model = "fake"
        FakeSession.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def events(self):
        while True:
            item = await self.outbound.get()
            if item is None:
                return
            yield item


@pytest.fixture
def bridge_client(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.ngrok-free.app")
    monkeypatch.delenv("ECHO_MODE", raising=False)
    FakeSession.instances.clear()
    import app.channels.voice.main as voice

    monkeypatch.setattr(voice, "GeminiLiveSession", FakeSession)
    return TestClient(app)


def _media(payload: bytes) -> str:
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(payload).decode()}}
    )


def _start(sid: str = "MZbridge") -> str:
    return json.dumps({"event": "start", "start": {"streamSid": sid}})


# ---------------------------------------------------------------------------
# Audio actually reaches Gemini, in the right format
# ---------------------------------------------------------------------------

def test_caller_audio_is_converted_and_forwarded(bridge_client):
    """mu-law from Twilio must arrive at Gemini as PCM16 @ 16kHz."""
    # 160 PCM16 samples (320 bytes) encodes to 160 mu-law bytes = one 20ms frame
    ulaw = pcm16_to_ulaw(b"\x10\x27" * 160)
    assert len(ulaw) == TWILIO_FRAME_BYTES

    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_start())
        for _ in range(5):
            ws.send_text(_media(ulaw))
        ws.send_text(json.dumps({"event": "stop"}))

    session = FakeSession.instances[0]
    assert session.sent_audio, "no audio reached Gemini"
    # 160 mu-law bytes @8k -> 640 bytes PCM16 @16k (2x samples, 2 bytes each).
    # The first chunk is slightly short because ratecv's filter starts cold.
    assert all(600 <= len(c) <= 640 for c in session.sent_audio)


def test_agent_speaks_first(bridge_client):
    """On an outbound call the callee has just said hello and is waiting.

    Silence reads as a dropped call, so the agent must open the conversation.
    """
    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_start())
        ws.send_text(json.dumps({"event": "stop"}))

    assert FakeSession.instances[0].sent_text, "agent did not greet"


# ---------------------------------------------------------------------------
# Hazard 1: the startup race
# ---------------------------------------------------------------------------

def test_audio_arriving_before_start_is_not_lost(bridge_client):
    """Twilio streams immediately; the caller's first words must survive.

    Dropping them loses the opening of every call, which is exactly when the
    caller is saying who they are.
    """
    ulaw = pcm16_to_ulaw(b"\x10\x27" * 160)

    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_media(ulaw))      # before start
        ws.send_text(_media(ulaw))
        ws.send_text(_start())
        ws.send_text(_media(ulaw))
        ws.send_text(json.dumps({"event": "stop"}))

    assert len(FakeSession.instances[0].sent_audio) >= 3


# ---------------------------------------------------------------------------
# Hazard 2: barge-in
# ---------------------------------------------------------------------------

def test_interruption_sends_clear_to_twilio(bridge_client):
    """Without a clear event, queued agent audio keeps playing over the caller.

    That is the failure that makes a demo feel broken, so it is asserted here.
    """
    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_start("MZclear"))
        session = FakeSession.instances[0]
        session.outbound.put_nowait(("audio", b"\x00\x00" * 480))
        session.outbound.put_nowait(("interrupted", None))

        events = []
        for _ in range(2):
            events.append(json.loads(ws.receive_text()))
        ws.send_text(json.dumps({"event": "stop"}))

    kinds = [e["event"] for e in events]
    assert "media" in kinds
    assert "clear" in kinds
    clear = next(e for e in events if e["event"] == "clear")
    assert clear["streamSid"] == "MZclear"


def test_agent_audio_reaches_caller_as_ulaw(bridge_client):
    """Gemini's PCM16 @24k must arrive at Twilio as mu-law @8k, 3:1 downsampled."""
    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_start())
        session = FakeSession.instances[0]
        # 20ms at 24kHz = 480 samples = 960 bytes -> 160 mu-law bytes
        session.outbound.put_nowait(("audio", b"\x00\x01" * 480))

        reply = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"event": "stop"}))

    assert reply["event"] == "media"
    out = base64.b64decode(reply["media"]["payload"])
    assert len(out) <= TWILIO_FRAME_BYTES + 8  # ~160 bytes, allowing filter warm-up


def test_outbound_audio_always_carries_stream_sid(bridge_client):
    """Twilio silently discards media without a streamSid."""
    with bridge_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_start("MZsid"))
        FakeSession.instances[0].outbound.put_nowait(("audio", b"\x00\x01" * 480))
        reply = json.loads(ws.receive_text())
        ws.send_text(json.dumps({"event": "stop"}))

    assert reply["streamSid"] == "MZsid"
