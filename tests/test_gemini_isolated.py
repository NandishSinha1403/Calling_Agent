"""Isolated Gemini Live check -- NO Twilio, NO FastAPI, NO ngrok.

Run this FIRST, and do not start Twilio work until it passes. If audio and tool
calling do not work here, they will not work with Twilio in the loop either, and
this narrows the fault to Gemini config/auth rather than audio format or
networking. That separation is the whole reason this file exists.

Two modes:

  Mic mode (default) -- talk to the agent through your laptop.
      .venv/bin/python tests/test_gemini_isolated.py

      Tests what a WAV file cannot: server-side VAD turn-detection delay and
      barge-in. That delay is the largest, most variable part of perceived
      latency on a real call.

      USE HEADPHONES. Speaker output bleeding into the mic makes the agent
      interrupt itself, which looks exactly like a barge-in bug.

  WAV mode -- deterministic, no pyaudio, repeatable latency number.
      .venv/bin/python tests/test_gemini_isolated.py --wav question.wav

      Input may be any sample rate/channels; it is converted to PCM16 mono
      16 kHz. The reply is written to reply.wav (PCM16 mono 24 kHz).

Both print time-to-first-audio-byte. Ctrl-C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import logging
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.gemini_live import (  # noqa: E402
    RECV_SAMPLE_RATE,
    SEND_SAMPLE_RATE,
    GeminiLiveSession,
)
from app.core.tools import get_tickets  # noqa: E402

CHUNK_FRAMES = 512  # ~32 ms at 16 kHz; small enough to keep VAD responsive


def _banner(mode: str, model: str) -> None:
    print(f"\n{'=' * 62}\n  Isolated Gemini Live test -- {mode}")
    print(f"  model: {model}\n{'=' * 62}\n")


def _report(first_audio_at: float | None, started: float) -> None:
    print(f"\n{'-' * 62}")
    if first_audio_at is None:
        print("  No audio received. Check the API key and model name.")
    else:
        print(f"  Time to first audio byte: {(first_audio_at - started) * 1000:.0f} ms")
    tickets = get_tickets()
    print(f"  Tool calls recorded: {len(tickets)}")
    for t in tickets:
        print(f"    {t['ticket_id']}  [{t['priority']}]  {t['summary']}")
    if not tickets:
        print("    (none -- ask the agent to log/record something to test tools)")
    print(f"{'-' * 62}\n")


# ---------------------------------------------------------------------------
# WAV mode
# ---------------------------------------------------------------------------

def _read_wav_as_pcm16_16k(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise SystemExit(f"{path}: need 16-bit PCM, got {wf.getsampwidth() * 8}-bit")
        pcm = wf.readframes(wf.getnframes())
        if wf.getnchannels() == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        rate = wf.getframerate()
    if rate != SEND_SAMPLE_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, SEND_SAMPLE_RATE, None)
        print(f"  resampled {rate} Hz -> {SEND_SAMPLE_RATE} Hz")
    return pcm


async def run_wav(path: Path, out_path: Path) -> None:
    pcm = _read_wav_as_pcm16_16k(path)
    seconds = len(pcm) / (SEND_SAMPLE_RATE * 2)

    async with GeminiLiveSession() as session:
        _banner(f"WAV  ({path.name}, {seconds:.1f}s)", session.model)
        started = time.monotonic()

        chunks: list[bytes] = []
        state: dict[str, float | None] = {"first_audio_at": None}
        done = asyncio.Event()

        async def send() -> None:
            # Stream in real time rather than dumping the file at once: server-side
            # VAD decides the turn ended from the silence that follows, and a
            # firehose gives it nothing resembling real speech timing.
            step = CHUNK_FRAMES * 2
            for i in range(0, len(pcm), step):
                await session.send_audio(pcm[i : i + step])
                await asyncio.sleep(CHUNK_FRAMES / SEND_SAMPLE_RATE)
            # A real call keeps streaming silence and VAD hears the pause. A
            # file just stops, so say so explicitly or the model waits forever.
            await session.end_audio_stream()

        async def receive() -> None:
            async for kind, payload in session.events():
                if kind == "audio":
                    if state["first_audio_at"] is None:
                        state["first_audio_at"] = time.monotonic()
                    chunks.append(payload)
                elif kind == "input_transcript":
                    print(f"  you:   {payload}", flush=True)
                elif kind == "transcript":
                    print(f"  agent: {payload}", flush=True)
                elif kind == "turn_complete":
                    done.set()
                    return

        # These MUST run concurrently. Sending several seconds of audio without
        # draining the socket leaves the server's messages (VAD signals,
        # transcription, the start of the reply) with no reader, and the session
        # is closed with a 1008. The real Twilio bridge has the same requirement.
        async with asyncio.TaskGroup() as tg:
            receiver = tg.create_task(receive())
            tg.create_task(send())
            await done.wait()
            receiver.cancel()

    if chunks:
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(RECV_SAMPLE_RATE)
            wf.writeframes(b"".join(chunks))
        print(f"\n  wrote {out_path} ({sum(map(len, chunks)) / (RECV_SAMPLE_RATE * 2):.1f}s)")

    _report(state["first_audio_at"], started)


# ---------------------------------------------------------------------------
# Mic mode
# ---------------------------------------------------------------------------

async def run_mic() -> None:
    try:
        import pyaudio
    except ImportError:
        raise SystemExit(
            "pyaudio is not installed.\n"
            "  brew install portaudio && .venv/bin/pip install pyaudio\n"
            "  Or run WAV mode instead:  --wav yourfile.wav"
        )

    pa = pyaudio.PyAudio()
    mic = pa.open(
        format=pyaudio.paInt16, channels=1, rate=SEND_SAMPLE_RATE,
        input=True, frames_per_buffer=CHUNK_FRAMES,
    )
    speaker = pa.open(
        format=pyaudio.paInt16, channels=1, rate=RECV_SAMPLE_RATE, output=True
    )

    playback: asyncio.Queue[bytes] = asyncio.Queue()
    state = {"first_audio_at": None, "started": time.monotonic()}

    async def capture(session: GeminiLiveSession) -> None:
        while True:
            data = await asyncio.to_thread(
                mic.read, CHUNK_FRAMES, exception_on_overflow=False
            )
            await session.send_audio(data)

    async def play() -> None:
        while True:
            chunk = await playback.get()
            await asyncio.to_thread(speaker.write, chunk)

    async def pump(session: GeminiLiveSession) -> None:
        async for kind, payload in session.events():
            if kind == "audio":
                if state["first_audio_at"] is None:
                    state["first_audio_at"] = time.monotonic()
                await playback.put(payload)
            elif kind == "interrupted":
                # Drop everything queued. Without this the agent keeps talking
                # over the person for as long as the buffer lasts -- the exact
                # failure that makes a demo feel broken.
                dropped = 0
                while not playback.empty():
                    playback.get_nowait()
                    dropped += 1
                print(f"  [barge-in: dropped {dropped} queued chunks]", flush=True)
            elif kind == "input_transcript":
                print(f"  you:   {payload}", flush=True)
            elif kind == "transcript":
                print(f"  agent: {payload}", flush=True)

    try:
        async with GeminiLiveSession() as session:
            _banner("MIC  (use headphones)", session.model)
            print("  Speak when ready. Ctrl-C to stop.")
            print("  Try: talk over the agent to test barge-in, and ask it to")
            print("       record/log something to test tool calling.\n")

            state["started"] = time.monotonic()
            # Make the agent speak first, the way it must on a real call.
            await session.send_text("The call has connected. Greet the person now.")

            async with asyncio.TaskGroup() as tg:
                tg.create_task(capture(session))
                tg.create_task(play())
                tg.create_task(pump(session))
    except* KeyboardInterrupt:
        pass
    finally:
        for stream in (mic, speaker):
            stream.stop_stream()
            stream.close()
        pa.terminate()
        _report(state["first_audio_at"], state["started"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--wav", type=Path, help="WAV file to send instead of using the mic")
    ap.add_argument("--out", type=Path, default=Path("reply.wav"), help="WAV mode output")
    ap.add_argument("-v", "--verbose", action="store_true", help="show library logs")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.wav:
            if not args.wav.exists():
                raise SystemExit(f"No such file: {args.wav}")
            asyncio.run(run_wav(args.wav, args.out))
        else:
            asyncio.run(run_mic())
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
