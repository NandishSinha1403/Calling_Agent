"""Fake a Twilio Media Stream call -- no phone, no trial minutes spent.

Twilio's Media Stream protocol is not complicated: a WebSocket carrying JSON
events, with audio as base64 mu-law in ~20ms frames. Nothing stops us speaking
it ourselves. This connects to the same endpoint Twilio would, sends the same
events in the same order, streams real audio, and writes back whatever comes
out.

Why this exists: trial voice minutes are a finite budget, and a real call is a
slow, non-repeatable test that needs a human holding a phone. This is instant,
repeatable, and diffable. Spend real minutes only on what genuinely cannot be
simulated -- PSTN audio quality and true end-to-end latency.

It works against the Step 3 echo stub AND the Step 4 Gemini bridge unchanged.

    # local
    .venv/bin/python tests/simulate_twilio_call.py --wav question.wav

    # through ngrok, exactly as Twilio reaches it
    .venv/bin/python tests/simulate_twilio_call.py --wav question.wav --url wss://<host>/ws/media-stream
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import sys
import time
import wave
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels.voice.audio_utils import (  # noqa: E402
    TWILIO_FRAME_BYTES,
    TWILIO_RATE,
    pcm16_rms,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)

FRAME_SECONDS = TWILIO_FRAME_BYTES / TWILIO_RATE  # 20 ms


def wav_to_ulaw_frames(path: Path) -> list[bytes]:
    """Load any WAV and cut it into 20ms mu-law frames, as Twilio sends."""
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        if wf.getnchannels() == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        rate = wf.getframerate()
    if rate != TWILIO_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, TWILIO_RATE, None)
    ulaw = pcm16_to_ulaw(pcm)
    return [
        ulaw[i : i + TWILIO_FRAME_BYTES]
        for i in range(0, len(ulaw), TWILIO_FRAME_BYTES)
    ]


async def simulate(
    url: str, wav: Path | None, out: Path, listen: float,
    interrupt_wav: Path | None = None,
) -> None:
    frames = wav_to_ulaw_frames(wav) if wav else []
    interrupt_frames = wav_to_ulaw_frames(interrupt_wav) if interrupt_wav else []
    stream_sid = "MZsimulated00000000000000000000000"
    received: list[bytes] = []
    first_reply_at: float | None = None
    cleared = {"count": 0}

    print(f"\n  connecting to {url}")
    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call"}))
        await ws.send(
            json.dumps(
                {
                    "event": "start",
                    "sequenceNumber": "1",
                    "start": {
                        "streamSid": stream_sid,
                        "callSid": "CAsimulated",
                        "tracks": ["inbound"],
                        "mediaFormat": {
                            "encoding": "audio/x-mulaw",
                            "sampleRate": TWILIO_RATE,
                            "channels": 1,
                        },
                    },
                    "streamSid": stream_sid,
                }
            )
        )
        print(f"  start sent (streamSid={stream_sid[:12]}...)")

        stop_reading = asyncio.Event()

        async def read() -> None:
            nonlocal first_reply_at
            while not stop_reading.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    return
                msg = json.loads(raw)
                if msg.get("event") == "media":
                    if first_reply_at is None:
                        first_reply_at = time.monotonic()
                    received.append(base64.b64decode(msg["media"]["payload"]))
                elif msg.get("event") == "clear":
                    # Barge-in: the bridge is telling Twilio to drop queued audio.
                    print(f"  <- CLEAR received (barge-in) after {len(received)} frames")
                    cleared["count"] += 1
                    received.clear()

        reader = asyncio.create_task(read())

        # Stream in real time. Sending faster than 20ms per frame would not
        # resemble a phone call, and server-side VAD reads timing.
        sent_until = time.monotonic()
        for i, frame in enumerate(frames):
            await ws.send(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": base64.b64encode(frame).decode()},
                    }
                )
            )
            await asyncio.sleep(FRAME_SECONDS)
        sent_until = time.monotonic()
        print(f"  sent {len(frames)} frames ({len(frames) * FRAME_SECONDS:.1f}s of audio)")

        if listen:
            # Keep streaming SILENCE, exactly as a real call does. Twilio never
            # stops sending until hangup, and server-side VAD detects
            # end-of-turn from that trailing silence. A simulator that simply
            # stops is not a faithful simulation: the model waits forever for a
            # pause that never arrives.
            print(f"  streaming silence for {listen:.0f}s, listening for a reply...")
            silent_frame = base64.b64encode(pcm16_to_ulaw(b"\x00\x00" * TWILIO_FRAME_BYTES)).decode()
            deadline = time.monotonic() + listen
            interrupted_yet = False
            while time.monotonic() < deadline:
                # Barge-in test: once the agent is a little way into speaking,
                # talk over it. A real caller does this constantly.
                if interrupt_frames and not interrupted_yet and len(received) > 40:
                    print(f"  -> INTERRUPTING after {len(received)} frames of agent audio")
                    interrupted_yet = True
                    for frame in interrupt_frames:
                        await ws.send(
                            json.dumps({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": base64.b64encode(frame).decode()},
                            })
                        )
                        await asyncio.sleep(FRAME_SECONDS)
                    continue
                await ws.send(
                    json.dumps({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": silent_frame},
                    })
                )
                await asyncio.sleep(FRAME_SECONDS)

        stop_reading.set()
        await reader
        await ws.send(json.dumps({"event": "stop", "streamSid": stream_sid}))

    print(f"\n{'-' * 62}")
    print(f"  frames received: {len(received)}")
    if interrupt_frames:
        verdict = "PASS" if cleared["count"] else "FAIL -- agent kept talking over the caller"
        print(f"  barge-in:        {cleared['count']} clear event(s) -- {verdict}")
    if received:
        pcm = ulaw_to_pcm16(b"".join(received))
        seconds = len(pcm) / (TWILIO_RATE * 2)
        print(f"  audio back:      {seconds:.1f}s  (RMS {pcm16_rms(pcm)})")
        if first_reply_at:
            gap = (first_reply_at - sent_until) * 1000
            if gap < 0:
                # An echo replies while we are still sending, so this figure is
                # only meaningful for the Gemini bridge, which waits for a turn.
                print("  first reply:     immediate (echo -- replied mid-stream)")
            else:
                print(f"  first reply:     {gap:.0f} ms after we stopped speaking")
        with wave.open(str(out), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TWILIO_RATE)
            wf.writeframes(pcm)
        print(f"  wrote {out} -- play it to hear what a caller would hear")
    else:
        print("  NOTHING CAME BACK.")
        print("  Check the server log. If the echo stub is running, the likely")
        print("  cause is a missing streamSid on outbound media.")
    print(f"{'-' * 62}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="ws://localhost:8000/ws/media-stream")
    ap.add_argument("--wav", type=Path, help="audio to send as the caller")
    ap.add_argument("--out", type=Path, default=Path("caller_heard.wav"))
    ap.add_argument("--interrupt-wav", type=Path,
                    help="talk over the agent with this audio, to test barge-in")
    ap.add_argument("--listen", type=float, default=0.0,
                    help="seconds to keep listening after sending (use ~15 for the Gemini bridge)")
    args = ap.parse_args()
    if args.wav and not args.wav.exists():
        raise SystemExit(f"No such file: {args.wav}")
    asyncio.run(simulate(args.url, args.wav, args.out, args.listen, args.interrupt_wav))


if __name__ == "__main__":
    main()
