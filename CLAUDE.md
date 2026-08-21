# CLAUDE.md — Project Context for Claude Code

This file is read automatically by Claude Code. It exists so you don't have to
re-explain the project every session. Keep it updated as the project evolves —
especially the "current state" section at the bottom.

## What this project is

An outbound-calling voice AI agent, built pre-hackathon as a **reusable scaffold**.
The actual use case (problem statement) is unknown until hackathon day
(JPMorgan Code for Good, 24 hours). The whole point of this codebase is that on
hackathon day, only `app/persona.py` and `app/tools.py` should need to change.
Everything else — audio bridging, WebSocket handling, Twilio call triggering — is
infrastructure that should not be touched once it works.

Full requirements are in `PRD.md` in this repo. Read it before making structural
changes.

## Stack

- **FastAPI** — backend, serves the admin endpoint, TwiML endpoint, and WebSocket
- **Twilio** (trial account) — outbound PSTN calls + Media Streams for real-time
  bidirectional audio over WebSocket
- **Gemini Live API** (`google-genai` SDK) — native speech-to-speech, no separate
  STT/TTS step
- **ngrok** — local tunneling so Twilio's cloud can reach the local dev server

## Target file structure

```
voice-agent/
├── app/
│   ├── config.py          # loads .env, exposes settings object
│   ├── persona.py          # SYSTEM_PROMPT — the only file to edit for a new persona
│   ├── tools.py              # TOOL_DECLARATIONS + TOOL_HANDLERS — swap for new use case
│   ├── audio_utils.py         # μ-law <-> PCM16 conversion + resampling
│   ├── gemini_client.py        # GeminiLiveSession wrapper: connects, streams audio,
│   │                            #   dispatches tool calls to tools.py, has ZERO
│   │                            #   persona- or tool-specific logic in it
│   ├── twilio_call.py           # trigger_call(to_number) — outbound call via Twilio REST
│   └── main.py                   # FastAPI app: POST /call, POST /twiml, WS /ws/media-stream
├── tests/
│   └── test_gemini_isolated.py    # mic/speaker script, NO Twilio, NO FastAPI —
│                                    #   run this first to confirm Gemini Live works at all
├── .env.example
├── requirements.txt
├── README.md
└── PRD.md
```

## Hard rule: modularity boundary

`gemini_client.py`, `audio_utils.py`, `main.py`, and `twilio_call.py` must never
import anything domain-specific. They import `SYSTEM_PROMPT` from `persona.py` and
`TOOL_DECLARATIONS`/`TOOL_HANDLERS` from `tools.py` as their *only* coupling points.
If you find yourself adding an `if persona == "x"` branch anywhere outside those
two files, stop — that logic belongs in `persona.py` or `tools.py` instead.

## Gemini Live API — verified current specifics (do not assume training-data defaults)

This API's SDK surface changes frequently. These were confirmed via live docs, not
memory — trust these over instinct:

- Package: `google-genai` (import as `from google import genai`, and
  `from google.genai import types`)
- Native-audio model example: `gemini-2.5-flash-native-audio-preview-12-2025`
  (check `https://ai.google.dev/gemini-api/docs/live-api` for the current model
  string before hardcoding — these get versioned/rotated)
- Connect: `client.aio.live.connect(model=MODEL, config=CONFIG)` as an async
  context manager
- **Input audio**: raw 16-bit PCM, 16kHz, little-endian, mono. Sent via:
  ```python
  await session.send_realtime_input(
      audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
  )
  ```
- **Output audio**: raw 16-bit PCM, 24kHz, little-endian, mono — arrives via the
  `session.receive()` async iterator on response parts.
- **Tool calling is NOT automatic** — unlike `generateContent`, Live API requires
  you to manually detect `response.tool_call`, execute the function yourself, and
  respond with:
  ```python
  await session.send_tool_response(function_responses=[
      types.FunctionResponse(id=call_id, name=fn_name, response={...})
  ])
  ```
  Every `function_call` in a `tool_call` message needs a matching response sent
  back, matched by `id`. If you don't respond, the model stalls waiting for it.
- Tools are declared once at session setup as part of `CONFIG["tools"]`, using a
  subset-of-OpenAPI-schema JSON shape — same general shape as regular Gemini
  function declarations.
- System prompt goes in `CONFIG["system_instruction"]`.
- `response_modalities` should be `["AUDIO"]` for this project.

## Audio bridge — the part most likely to break

Twilio Media Streams send/expect: **μ-law (G.711), 8kHz, mono, base64-encoded,
~20ms frames (160 bytes of μ-law per frame)**, wrapped in a JSON `media` event.

Gemini Live expects/sends: **raw PCM16, 16kHz in / 24kHz out**.

So the pipeline is:

- **Caller → Gemini**: base64 decode → μ-law bytes → decode to PCM16 @ 8kHz →
  resample PCM16 8kHz → 16kHz → send to Gemini
- **Gemini → Caller**: PCM16 @ 24kHz from Gemini → resample to PCM16 @ 8kHz →
  encode to μ-law → base64 → send as a `media` event back to Twilio

Implementation notes:
- `audioop` was **removed in Python 3.13**. Use the `audioop-lts` PyPI package
  (drop-in API-compatible backport) instead of hand-rolling resampling in numpy.
- `audioop.ratecv()` is a **stateful streaming resampler** — its `state` return
  value must be threaded through consecutive calls for the *same direction* of
  the *same call*, or you'll get audible clicks/pops at chunk boundaries. Don't
  reinitialize state per-chunk.
- Keep two independent resampler states per call: one for caller→Gemini
  (8k→16k), one for Gemini→caller (24k→8k).

## Known risks to watch for while building

1. **Barge-in / interruption handling**: Gemini Live does server-side VAD and will
   emit an interruption signal if the caller talks over the agent. If you don't
   forward a `clear` event to Twilio's Media Stream on interruption, stale queued
   agent audio keeps playing over the caller. Check the Live API's turn/interrupt
   signal in `session.receive()` and wire a Twilio `clear` message to it.
2. **Startup race**: Twilio begins streaming caller audio the instant the
   WebSocket upgrades, but Gemini session setup (auth + connect) takes some
   nonzero time. Start the Gemini connection as soon as the WS opens (don't wait
   for Twilio's `start` event) to minimize dropped early audio, and buffer briefly
   if needed.
3. **Twilio trial account**: outbound calls only succeed to phone numbers
   verified in the Twilio console, and Twilio injects an audible trial
   disclaimer before your TwiML plays. This is expected — don't debug it as a bug.
4. **ngrok URL churn**: free-tier ngrok issues a new URL on every restart.
   `BASE_URL` in `.env` must be updated, and the TwiML `<Stream>` URL derived
   from it must use `wss://`, not `https://`.
5. **Twilio `<Connect><Stream>` needs an absolute `wss://` URL** — a relative or
   `https://` URL here fails silently or errors depending on Twilio's mood; don't
   assume it interpolates the scheme for you.

## Testing approach — order matters

1. Run `tests/test_gemini_isolated.py` first (mic in/speaker out, no Twilio, no
   FastAPI, no ngrok). If audio and tool calling don't work here, they won't work
   with Twilio in the loop either, and this isolates the bug to Gemini-side
   config/auth rather than audio-format/networking issues.
2. Only once that passes, wire up FastAPI + Twilio Media Streams.
3. When debugging the full pipeline, temporarily log/dump raw audio chunk sizes
   and sample counts at each conversion step — silent-but-connected calls are
   almost always a sample-rate or byte-order mismatch, not a networking issue.

## Environment variables (see `.env.example`)

`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `GEMINI_API_KEY`,
`BASE_URL` (the current ngrok https URL, no trailing slash).

## Current state

_(Update this section as you go so future Claude Code sessions pick up where you
left off — e.g. "isolated Gemini test passing, starting FastAPI skeleton next.")_

- [ ] Isolated Gemini Live test passing
- [ ] Audio utils written + sanity-checked
- [ ] FastAPI skeleton (`/call`, `/twiml`, `/ws/media-stream`) responding
- [ ] Full bridge wired, first successful end-to-end test call
- [ ] `log_ticket` tool call confirmed working over a live call
- [ ] README verified by following it fresh
