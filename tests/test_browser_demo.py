"""Browser demo tests.

The important one is codec equivalence. The page implements G.711 mu-law in
JavaScript, and the bridge decodes it with audioop in C. If those two disagree
even slightly the audio is garbled, and garbled audio is miserable to diagnose
from a browser. So the JS algorithm is ported here verbatim and checked against
audioop across the full sample range.
"""

import audioop
import json
import math

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


# ---------------------------------------------------------------------------
# The capture worklet's resampler
# ---------------------------------------------------------------------------
# Ported from the AudioWorklet in demo.html, for the same reason the codec is:
# a subtle fault here produces audio that measures a healthy RMS while being
# full of gaps, which speech recognition cannot parse. The agent then hears
# "something loud" and never replies -- a symptom that looks like a model
# problem and is really a resampling one.

def worklet_resample(chunks, device_rate: int) -> list[float]:
    """Line-for-line port of CaptureProcessor.process() in demo.html."""
    ratio = device_rate / 8000
    tail: list[float] = []
    frac = 0.0
    out: list[float] = []

    for chunk in chunks:
        buf = tail + list(chunk)
        pos = frac
        while pos + ratio <= len(buf):
            lo, hi = int(pos), int(pos + ratio)
            window = buf[lo:hi]
            out.append(sum(window) / len(window) if window else 0.0)
            pos += ratio
        keep = int(pos)
        tail = buf[keep:]
        frac = pos - keep
    return out


def _sine_blocks(rate: int, seconds: float, hz: int = 440, amp: float = 0.3):
    """One second of audio in 128-sample blocks, as a worklet receives it."""
    total = int(rate * seconds)
    samples = [amp * math.sin(2 * math.pi * hz * i / rate) for i in range(total)]
    return [samples[i : i + 128] for i in range(0, len(samples), 128)]


@pytest.mark.parametrize("device_rate", [48000, 44100, 16000])
def test_worklet_resamples_to_realtime_8khz(device_rate):
    """One second in must be one second out, or audio drifts over a long call."""
    out = worklet_resample(_sine_blocks(device_rate, 1.0), device_rate)
    assert abs(len(out) - 8000) < 130, f"{len(out)} samples from 1s at {device_rate}Hz"


@pytest.mark.parametrize("device_rate", [48000, 44100])
def test_worklet_output_is_continuous(device_rate):
    """No gaps at buffer boundaries.

    An earlier version carried a NEGATIVE offset between buffers, which read
    past the start of the array and wrote zeros into the stream. RMS stayed
    healthy and the audio was unintelligible -- the hardest kind of bug to see.
    A clean 440Hz sine at 8kHz cannot step by more than about 0.11 of full
    scale between samples.
    """
    out = worklet_resample(_sine_blocks(device_rate, 1.0), device_rate)
    largest = max(abs(b - a) for a, b in zip(out, out[1:]))
    assert largest < 0.15, f"discontinuity of {largest:.3f} at {device_rate}Hz"


def test_worklet_preserves_loudness():
    out = worklet_resample(_sine_blocks(48000, 1.0), 48000)
    rms = math.sqrt(sum(s * s for s in out) / len(out))
    assert rms == pytest.approx(0.3 / math.sqrt(2), rel=0.05)
