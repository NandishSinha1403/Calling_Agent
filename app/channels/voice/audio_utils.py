"""Audio format conversion between Twilio and Gemini.

Twilio Media Streams speak:  mu-law (G.711), 8 kHz, mono, base64, ~20 ms frames
                             (160 mu-law bytes per frame)
Gemini Live speaks:          PCM16 little-endian, 16 kHz in, 24 kHz out, mono

So there are two independent pipelines per call:

    caller -> Gemini   mu-law 8k  ->  PCM16 8k  ->  PCM16 16k
    Gemini -> caller   PCM16 24k  ->  PCM16 8k  ->  mu-law 8k

THE ONE THING TO GET RIGHT
--------------------------
`audioop.ratecv` is a *stateful streaming* resampler. It returns a state object
that must be fed back on the next call for the same direction of the same call.
Passing None every time restarts the filter at each chunk boundary, which sounds
like a click or crackle every 20 ms -- audible, and easy to misdiagnose as a
network problem. Hence `Resampler`, which owns that state so callers cannot
forget it. Two per call: one per direction, never shared between calls.

Why audioop rather than hand-rolled numpy: it does filtered rate conversion in
C. Naive decimation (keeping every Nth sample) aliases everything above 4 kHz
back into the voice band, which is what gives cheap telephony audio its metallic
edge. This is one of the rare cases where the stdlib is simply better than what
most people write by hand.

Deprecation note: `audioop` was removed in Python 3.13. This project pins 3.11
precisely so it stays in the stdlib -- see requirements.txt.
"""

from __future__ import annotations

import audioop

TWILIO_RATE = 8_000
GEMINI_IN_RATE = 16_000
GEMINI_OUT_RATE = 24_000

SAMPLE_WIDTH = 2  # bytes per PCM16 sample
CHANNELS = 1

# One Twilio frame: 20 ms of mu-law at 8 kHz.
TWILIO_FRAME_BYTES = 160


class Resampler:
    """A one-direction PCM16 resampler that remembers its filter state.

    Create one per direction per call. Never share one between two calls, and
    never between the two directions of the same call -- the states are not
    interchangeable and mixing them produces distortion.
    """

    __slots__ = ("_from_rate", "_to_rate", "_state")

    def __init__(self, from_rate: int, to_rate: int) -> None:
        self._from_rate = from_rate
        self._to_rate = to_rate
        self._state = None

    def __call__(self, pcm: bytes) -> bytes:
        if not pcm:
            return b""
        if self._from_rate == self._to_rate:
            return pcm
        converted, self._state = audioop.ratecv(
            pcm, SAMPLE_WIDTH, CHANNELS, self._from_rate, self._to_rate, self._state
        )
        return converted

    def reset(self) -> None:
        """Drop filter state. Only for starting a new call, not a new chunk."""
        self._state = None


class CallAudioBridge:
    """Both directions of one call, with the two resampler states kept apart.

        bridge = CallAudioBridge()
        pcm16k = bridge.caller_to_gemini(ulaw_bytes)
        ulaw   = bridge.gemini_to_caller(pcm24k_bytes)

    One instance per call. Constructing a fresh one is how a new call starts
    clean; reusing one across calls carries the previous caller's filter state
    into the next call.
    """

    def __init__(self) -> None:
        self._up = Resampler(TWILIO_RATE, GEMINI_IN_RATE)
        self._down = Resampler(GEMINI_OUT_RATE, TWILIO_RATE)

    def caller_to_gemini(self, ulaw: bytes) -> bytes:
        """mu-law 8 kHz from Twilio -> PCM16 16 kHz for Gemini."""
        if not ulaw:
            return b""
        return self._up(audioop.ulaw2lin(ulaw, SAMPLE_WIDTH))

    def gemini_to_caller(self, pcm24k: bytes) -> bytes:
        """PCM16 24 kHz from Gemini -> mu-law 8 kHz for Twilio."""
        if not pcm24k:
            return b""
        return audioop.lin2ulaw(self._down(pcm24k), SAMPLE_WIDTH)


# ---------------------------------------------------------------------------
# Stateless helpers, useful for tests and one-off conversions
# ---------------------------------------------------------------------------

def ulaw_to_pcm16(ulaw: bytes) -> bytes:
    """mu-law -> PCM16 at the same sample rate."""
    return audioop.ulaw2lin(ulaw, SAMPLE_WIDTH)


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """PCM16 -> mu-law at the same sample rate."""
    return audioop.lin2ulaw(pcm, SAMPLE_WIDTH)


def pcm16_rms(pcm: bytes) -> int:
    """Loudness of a PCM16 buffer.

    Worth having: a call that is connected but silent is nearly always a
    sample-rate or byte-order mistake, and logging RMS at each stage finds it
    far faster than staring at the networking.
    """
    return audioop.rms(pcm, SAMPLE_WIDTH) if pcm else 0
