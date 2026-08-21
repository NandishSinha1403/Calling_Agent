"""Browser demo tests.

The important one is codec equivalence. The page implements G.711 mu-law in
JavaScript, and the bridge decodes it with audioop in C. If those two disagree
even slightly the audio is garbled, and garbled audio is miserable to diagnose
from a browser. So the JS algorithm is ported here verbatim and checked against
audioop across the full sample range.
"""

import audioop
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

SEG_END = [0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF]
BIAS = 0x84


def js_pcm16_to_ulaw(pcm: int) -> int:
    """Line-for-line port of pcm16ToUlaw() in demo.html."""
    pcm = pcm >> 2  # 16-bit -> 14-bit, the step a textbook version omits
    if pcm < 0:
        pcm = -pcm
        mask = 0x7F
    else:
        mask = 0xFF
    if pcm > 8159:
        pcm = 8159
    pcm += 33
    seg = 8
    for i, end in enumerate(SEG_END):
        if pcm <= end:
            seg = i
            break
    if seg >= 8:
        return 0x7F ^ mask
    return ((seg << 4) | ((pcm >> (seg + 1)) & 0xF)) ^ mask


def js_ulaw_to_pcm16(u: int) -> int:
    """Line-for-line port of ulawToPcm16() in demo.html."""
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = ((mantissa << 3) + BIAS) << exponent
    sample -= BIAS
    return -sample if sign else sample


@pytest.mark.parametrize(
    "sample",
    [0, 1, -1, 100, -100, 1000, -1000, 8000, -8000, 32000, -32000, 32767, -32768],
)
def test_js_encoder_matches_audioop(sample):
    """The browser's encoder must agree with what the bridge decodes."""
    expected = audioop.lin2ulaw(sample.to_bytes(2, "little", signed=True), 2)[0]
    assert js_pcm16_to_ulaw(sample) == expected


def test_js_encoder_matches_audioop_over_every_sample():
    """Sweep ALL 65,536 samples, not a sample of them.

    An earlier version of the browser encoder used the textbook algorithm and
    agreed on 99% of values, differing only on a few negatives. That would have
    been faint intermittent crackle, not an obvious break -- the kind of thing
    that gets blamed on the network for hours.
    """
    mismatches = [
        s
        for s in range(-32768, 32768)
        if js_pcm16_to_ulaw(s)
        != audioop.lin2ulaw(s.to_bytes(2, "little", signed=True), 2)[0]
    ]
    assert not mismatches, f"{len(mismatches)} mismatches, e.g. {mismatches[:5]}"


def test_js_decoder_matches_audioop():
    """And the browser's decoder must agree with what the bridge encodes."""
    for u in range(256):
        expected = int.from_bytes(
            audioop.ulaw2lin(bytes([u]), 2), "little", signed=True
        )
        assert js_ulaw_to_pcm16(u) == expected, f"byte {u}"


def test_round_trip_preserves_loudness():
    """A full encode/decode cycle must not collapse the signal."""
    import math

    pcm = [int(10000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(800)]
    back = [js_ulaw_to_pcm16(js_pcm16_to_ulaw(s)) for s in pcm]
    rms_in = math.sqrt(sum(s * s for s in pcm) / len(pcm))
    rms_out = math.sqrt(sum(s * s for s in back) / len(back))
    assert rms_out == pytest.approx(rms_in, rel=0.05)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.ngrok-free.app")
    return TestClient(app)


def test_demo_page_served(client):
    body = client.get("/demo").text
    assert "pcm16ToUlaw" in body
    # It must speak Twilio's protocol, or the bridge would need changes.
    assert "audio/x-mulaw" in body and "streamSid" in body


def test_event_feed_delivers_published_events(client):
    from app.event_bus import publish

    with client.websocket_connect("/ws/events") as ws:
        publish("transcript", speaker="agent", text="hello")
        event = json.loads(ws.receive_text())
    assert event == {"kind": "transcript", "speaker": "agent", "text": "hello"}


def test_slow_subscriber_never_blocks_publish():
    """Observability must never add latency to a live call.

    A browser tab that stops reading must be dropped, not allowed to back up
    into the audio path.
    """
    import asyncio

    from app.event_bus import QUEUE_SIZE, publish, subscribe

    async def main():
        agen = subscribe()
        pending = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)  # let the subscriber register

        # Flood far past the queue size. None of these may raise or block.
        for i in range(QUEUE_SIZE * 3):
            publish("noise", i=i)

        first = await asyncio.wait_for(pending, 1)
        assert first["kind"] == "noise"
        await agen.aclose()

    asyncio.run(main())
