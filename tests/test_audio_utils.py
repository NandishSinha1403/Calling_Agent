"""Audio conversion tests.

All offline -- no Twilio, no Gemini, no network. This is the layer CLAUDE.md
flags as most likely to break, and a silent-but-connected call gives you almost
no diagnostic signal, so the maths is pinned down here instead.
"""

import audioop
import math

import pytest

from app.channels.voice.audio_utils import (
    GEMINI_IN_RATE,
    GEMINI_OUT_RATE,
    TWILIO_FRAME_BYTES,
    TWILIO_RATE,
    CallAudioBridge,
    Resampler,
    pcm16_rms,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)


def sine(rate: int, seconds: float, hz: int = 440, amp: int = 12000) -> bytes:
    """PCM16 mono sine wave. 440 Hz sits inside the 300-3400 Hz phone band."""
    n = int(rate * seconds)
    return b"".join(
        int(amp * math.sin(2 * math.pi * hz * i / rate)).to_bytes(
            2, "little", signed=True
        )
        for i in range(n)
    )


# ---------------------------------------------------------------------------
# Frame arithmetic
# ---------------------------------------------------------------------------

def test_twilio_frame_is_20ms():
    assert TWILIO_FRAME_BYTES / TWILIO_RATE == pytest.approx(0.020)


def test_one_twilio_frame_becomes_16k_pcm():
    """160 mu-law bytes @8k -> ~640 bytes PCM16 @16k (2x samples, 2 bytes each).

    The first chunk is a couple of bytes short because ratecv's filter starts
    cold. That is expected; the next assertion covers the steady state.
    """
    bridge = CallAudioBridge()
    out = bridge.caller_to_gemini(b"\xff" * TWILIO_FRAME_BYTES)
    assert 630 <= len(out) <= 640


def test_steady_state_frame_size_is_exact():
    """After warm-up every 20ms frame must produce exactly 640 bytes.

    Drift here would mean audio slowly desynchronising over a long call.
    """
    bridge = CallAudioBridge()
    bridge.caller_to_gemini(b"\xff" * TWILIO_FRAME_BYTES)  # warm up
    for _ in range(50):
        assert len(bridge.caller_to_gemini(b"\xff" * TWILIO_FRAME_BYTES)) == 640


def test_outbound_frame_arithmetic():
    """24k -> 8k is 3:1, and mu-law halves the byte count again."""
    bridge = CallAudioBridge()
    pcm24k = sine(GEMINI_OUT_RATE, 0.02)      # 480 samples, 960 bytes
    bridge.gemini_to_caller(pcm24k)           # warm up
    out = bridge.gemini_to_caller(pcm24k)
    assert len(out) == 160                    # 480/3 samples, 1 byte each


# ---------------------------------------------------------------------------
# The state threading that CLAUDE.md warns about
# ---------------------------------------------------------------------------

def test_chunked_matches_single_pass():
    """Streaming in 20ms chunks must equal converting the whole buffer at once.

    This is the actual guarantee. If state were not threaded through, the two
    would diverge -- audibly, as a click at every chunk boundary.
    """
    pcm8k = sine(TWILIO_RATE, 1.0)
    one_shot = Resampler(TWILIO_RATE, GEMINI_IN_RATE)(pcm8k)

    streaming = Resampler(TWILIO_RATE, GEMINI_IN_RATE)
    step = TWILIO_FRAME_BYTES * 2  # 160 samples of PCM16
    chunked = b"".join(
        streaming(pcm8k[i : i + step]) for i in range(0, len(pcm8k), step)
    )
    assert chunked == one_shot


def test_stateless_conversion_introduces_discontinuities():
    """Demonstrates the bug the Resampler class exists to prevent.

    Resetting state per chunk produces a different, worse signal. If this ever
    starts passing as 'equal', the state threading has been broken.
    """
    pcm8k = sine(TWILIO_RATE, 0.5)
    step = TWILIO_FRAME_BYTES * 2

    stateful = Resampler(TWILIO_RATE, GEMINI_IN_RATE)
    good = b"".join(stateful(pcm8k[i : i + step]) for i in range(0, len(pcm8k), step))

    bad = b"".join(
        audioop.ratecv(pcm8k[i : i + step], 2, 1, TWILIO_RATE, GEMINI_IN_RATE, None)[0]
        for i in range(0, len(pcm8k), step)
    )
    assert good != bad


def test_reset_clears_state():
    r = Resampler(TWILIO_RATE, GEMINI_IN_RATE)
    pcm = sine(TWILIO_RATE, 0.02)
    first = r(pcm)
    r.reset()
    assert r(pcm) == first


def test_directions_have_independent_state():
    """One shared state for both directions would distort both."""
    bridge = CallAudioBridge()
    bridge.caller_to_gemini(b"\xff" * TWILIO_FRAME_BYTES)

    fresh = CallAudioBridge()
    pcm24k = sine(GEMINI_OUT_RATE, 0.02)
    assert bridge.gemini_to_caller(pcm24k) == fresh.gemini_to_caller(pcm24k)


def test_each_call_starts_clean():
    """A new CallAudioBridge must not inherit the previous caller's state."""
    a, b = CallAudioBridge(), CallAudioBridge()
    frame = b"\x7f" * TWILIO_FRAME_BYTES
    assert a.caller_to_gemini(frame) == b.caller_to_gemini(frame)


# ---------------------------------------------------------------------------
# Signal integrity
# ---------------------------------------------------------------------------

def test_ulaw_round_trip_preserves_signal():
    """mu-law is lossy by design, but must stay recognisably the same audio."""
    pcm = sine(TWILIO_RATE, 0.1)
    back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
    assert len(back) == len(pcm)
    # Within ~5% RMS. Larger drift means a width or byte-order mistake.
    assert pcm16_rms(back) == pytest.approx(pcm16_rms(pcm), rel=0.05)


def test_full_round_trip_survives_the_whole_pipeline():
    """caller -> Gemini rate -> back to caller keeps real audio, not silence.

    A connected-but-silent call is nearly always a rate or byte-order error,
    and this is the assertion that catches it before a phone is involved.
    """
    original_8k = sine(TWILIO_RATE, 0.5)
    ulaw_in = pcm16_to_ulaw(original_8k)

    bridge = CallAudioBridge()
    pcm16k = bridge.caller_to_gemini(ulaw_in)
    assert pcm16_rms(pcm16k) > 1000, "signal died on the way in"

    # Stand in for Gemini: 16k -> 24k, as if it had spoken back.
    pcm24k = Resampler(GEMINI_IN_RATE, GEMINI_OUT_RATE)(pcm16k)
    ulaw_out = bridge.gemini_to_caller(pcm24k)
    assert pcm16_rms(ulaw_to_pcm16(ulaw_out)) > 1000, "signal died on the way out"


def test_silence_stays_silent():
    """Silence must not become noise -- that would be a byte-order bug."""
    bridge = CallAudioBridge()
    silence_ulaw = pcm16_to_ulaw(b"\x00\x00" * 400)
    assert pcm16_rms(bridge.caller_to_gemini(silence_ulaw)) < 100


# ---------------------------------------------------------------------------
# Edge cases the WebSocket will hand us
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["caller_to_gemini", "gemini_to_caller"])
def test_empty_input_returns_empty(method):
    """Twilio sends empty media payloads; they must not raise mid-call."""
    assert getattr(CallAudioBridge(), method)(b"") == b""


def test_rms_of_empty_is_zero():
    assert pcm16_rms(b"") == 0
