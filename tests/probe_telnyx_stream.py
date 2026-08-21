"""Does Telnyx actually open a media stream on this account?

Answers ONE question before any porting work happens, because Twilio's trial
turned out to gate Media Streams and no provider documents whether theirs does.
Finding out in two minutes beats finding out after a two-hour rewrite.

It dials with stream_url pointed at the EXISTING /ws/media-stream endpoint. The
protocols are close enough that a connection should arrive even before any port:
Telnyx uses the same connected/start/media/stop events and the same
{"event":"media","media":{"payload": base64}} shape, and PCMU is mu-law at 8kHz,
which audio_utils already handles.

    .venv/bin/python tests/probe_telnyx_stream.py +919999999999

Watch the server log for "Media stream connected". Telnyx also posts
streaming.failed to /telnyx/webhook WITH A REASON if it refuses -- which is the
diagnostic Twilio's trial kept behind a paywall.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import telnyx  # noqa: E402

from app.config import ConfigError, settings  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: probe_telnyx_stream.py +<E164 number to call>")
    to_number = sys.argv[1]

    try:
        settings.require_telnyx()
    except ConfigError as exc:
        raise SystemExit(str(exc))

    telnyx.api_key = settings.telnyx_api_key
    stream_url = settings.websocket_url

    print(f"\n  dialing {to_number}")
    print(f"  from    {settings.telnyx_phone_number}")
    print(f"  stream  {stream_url}\n")

    try:
        call = telnyx.Call.create(
            connection_id=settings.telnyx_connection_id,
            to=to_number,
            from_=settings.telnyx_phone_number,
            stream_url=stream_url,
            stream_track="both_tracks",
            # Bidirectional is what makes the agent audible to the caller.
            stream_bidirectional_mode="rtp",
            stream_bidirectional_codec="PCMU",
            # Telnyx keeps silence flowing when nobody speaks. Server-side VAD
            # infers end-of-turn from trailing silence, so a stream that goes
            # quiet entirely leaves the model waiting for a pause that never
            # arrives -- exactly the bug that broke the WAV harness earlier.
            send_silence_when_idle=True,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  DIAL FAILED: {type(exc).__name__}: {exc}\n")
        print("  If this mentions permissions or trial limits, Telnyx gates")
        print("  streaming the same way Twilio does, and we stop here.\n")
        raise SystemExit(1)

    print(f"  call queued: {getattr(call, 'call_control_id', '?')[:24]}...")
    print("\n  ANSWER THE PHONE, then watch the server log for:")
    print("    'Media stream connected'   -> streaming works, port it")
    print("    'Telnyx streaming.failed'  -> it does not, with a reason\n")


if __name__ == "__main__":
    main()
