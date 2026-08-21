# CLAUDE.md — Project Context for Claude Code

This file is read automatically by Claude Code. It exists so you don't have to
re-explain the project every session. Keep it updated as the project evolves —
especially the "current state" section at the bottom.

## What this project is

A **multi-channel AI agent scaffold**, built pre-hackathon as reusable
infrastructure. The actual use case (problem statement) is unknown until
hackathon day (JPMorgan Code for Good, 24 hours).

There are two channels, sharing one brain:

- **Voice** — outbound Twilio calls bridged to Gemini Live (native
  speech-to-speech). Outbound-initiated.
- **WhatsApp** — Twilio WhatsApp Sandbox, text in and text out via a standard
  Gemini text model. **Inbound-driven** (see the WhatsApp section below for why
  it cannot be outbound).

The whole point of this codebase is that on hackathon day, only
`app/core/persona.py` and `app/core/tools.py` should need to change — and that
one edit adapts **both** channels at once. Everything else — audio bridging,
WebSocket handling, Twilio call triggering, WhatsApp webhook handling — is
infrastructure that should not be touched once it works.

Full requirements are in `PRD.md` in this repo. Read it before making structural
changes.

## Stack

- **Python 3.11** — *not* 3.13. `audioop` (the core of the audio bridge) was
  removed from the stdlib in 3.13. Staying on 3.11 keeps it built in rather than
  depending on the `audioop-lts` backport, and avoids pyaudio wheel problems.
- **FastAPI** — one app, mounting a router per channel. Serves the admin
  endpoint, TwiML endpoint, media-stream WebSocket, and WhatsApp webhook.
- **Twilio** (trial account) — outbound PSTN calls + Media Streams for real-time
  bidirectional audio, and the WhatsApp Sandbox for messaging.
- **Gemini Live API** (`google-genai` SDK) — native speech-to-speech for voice,
  no separate STT/TTS step.
- **Gemini Flash (text)** — standard text-in/text-out for WhatsApp. A *separate*
  client from Live; see "Why two Gemini clients" below.
- **ngrok** — local tunneling so Twilio's cloud can reach the local dev server.
  One tunnel serves both channels.

## Target file structure

```
Calling_Agent/
├── app/
│   ├── config.py                  # loads .env; grouped fail-fast validation
│   ├── main.py                    # top-level FastAPI app; mounts both channel routers
│   ├── core/                      # THE BRAIN — channel-agnostic
│   │   ├── persona.py             #   BASE_RULES + SYSTEM_PROMPT, shared verbatim
│   │   ├── tools.py               #   TOOL_DECLARATIONS + TOOL_HANDLERS, shared verbatim
│   │   ├── gemini_live.py         #   voice: audio-native Live session wrapper
│   │   └── gemini_text.py         #   whatsapp: text-in/text-out wrapper
│   └── channels/                  # TRANSPORTS — no domain logic, ever
│       ├── voice/
│       │   ├── main.py            #   APIRouter: POST /call, POST /twiml, WS /ws/media-stream
│       │   ├── audio_utils.py     #   μ-law <-> PCM16 + stateful resampling
│       │   └── twilio_call.py     #   trigger_call(to_number)
│       └── whatsapp/
│           ├── main.py            #   APIRouter: POST /whatsapp/incoming
│           ├── whatsapp_client.py #   sends replies via the Twilio WhatsApp API
│           └── session_store.py   #   per-sender chat history + 24h window state
├── tests/
│   ├── test_config.py
│   ├── test_audio_utils.py
│   └── test_gemini_isolated.py    # mic/speaker + --wav; NO Twilio, NO FastAPI
├── .env.example
├── requirements.txt
├── README.md
└── PRD.md
```

## Hard rule: the modularity boundary

**`app/core/persona.py` and `app/core/tools.py` are the only files that should
ever need to change to adapt to a new problem statement — for either channel.**

- Nothing under `app/channels/` contains persona- or domain-specific logic.
- No channel imports from another channel. They share via `core/`, never
  sideways.
- `core/gemini_live.py` and `core/gemini_text.py` import `SYSTEM_PROMPT` from
  `persona.py` and `TOOL_DECLARATIONS`/`TOOL_HANDLERS` from `tools.py`. That is
  their *only* coupling to the problem domain.
- Both Gemini clients dispatch tools through the **same `TOOL_HANDLERS` dict**,
  so there is one tool code path rather than two that drift apart.

If you find yourself adding an `if persona == "x"` branch, or a channel-specific
special case, anywhere outside those two files — stop. That logic belongs in
`persona.py` or `tools.py`.

## Why two Gemini clients

This trips people up, so it is stated plainly: **the Live client cannot be
reused for WhatsApp.** Gemini Live runs a persistent streaming session with
`response_modalities=["AUDIO"]`, and native-audio models restrict responses to
audio — there is no config flag that makes it return plain text for a chat
message. WhatsApp needs ordinary text in and text out.

So `core/` holds two thin clients:

| | `gemini_live.py` | `gemini_text.py` |
|---|---|---|
| Channel | Voice | WhatsApp |
| API | `client.aio.live.connect()`, persistent | `generateContent` / chat, per-request |
| In / Out | PCM16 audio / PCM16 audio | text / text |
| Model | `gemini-3.1-flash-live-preview` | `gemini-3.7-flash` (stable) |
| Tool calling | Manual loop, required | Manual dispatch via the same handlers |

What makes the shared `tools.py` work across both is that the Live API and
`generateContent` accept the **same OpenAPI-subset function-declaration
schema**. One declaration feeds both.

## The WhatsApp channel — the constraint that shapes it

Twilio's WhatsApp **Sandbox** cannot freely initiate conversations:

- Outbound-initiated messages are limited to **three fixed pre-approved
  templates** (appointment reminders, order notifications, verification codes).
  **Custom templates are not supported in the Sandbox.**
- Free-form messaging works **only inside a 24-hour window**, opened when the
  user messages us first. Sending `join <sandbox code>` counts as opening it.
- Shared sandbox number `+14155238886`, stamped with the Twilio logo.
- Throughput cap: **1 message per 3 seconds**.
- **100 WhatsApp messages** included as trial units, shared with SMS.

Therefore this channel is **inbound-driven — the mirror image of voice.** Do not
mirror the outbound `/call` shape onto it:

- There is deliberately **no** "send to anyone" endpoint. The entry point is the
  inbound webhook, `POST /whatsapp/incoming`.
- `session_store.py` exists *because of* this constraint. It holds, per sender,
  the running chat history **and** `window_opened_at`. Before any reply is sent,
  the window is checked; if it has expired, log clearly that a template would be
  required rather than firing a request Twilio will reject.
- Serialize replies per sender with a spacing guard for the 3-second cap, so a
  burst does not silently drop messages.
- Log a running count of messages sent — 100 is easy to burn through in one
  debugging session without noticing.

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
5. **WhatsApp Sandbox will look broken when it is not.** Three things that are
   expected behaviour, not bugs: a reply refused because the 24-hour window
   closed (the user must message again); messages dropped when sent faster than
   1 per 3 seconds; and the Twilio logo on the shared sandbox number. Also watch
   the 100-message trial budget — it is shared with SMS and is easy to exhaust
   while debugging.
6. **Twilio `<Connect><Stream>` needs an absolute `wss://` URL** — a relative or
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
4. **Voice must be fully working and merged before WhatsApp work starts.** Voice
   is the risky part — real-time audio bridging, resampling, barge-in. WhatsApp
   is a webhook and a text-model call. Do the thing that can eat a day while
   there is still time to fix it, and do not build both at once.
5. WhatsApp's window-expiry and session-keying logic are pure functions —
   unit-test them offline before spending any of the 100 trial messages.

## Environment variables (see `.env.example`)

| Variable | Needed for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | everything | https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | voice | Live model; defaults to `gemini-3.1-flash-live-preview` |
| `GEMINI_TEXT_MODEL` | whatsapp | defaults to `gemini-3.7-flash` (stable) |
| `TWILIO_ACCOUNT_SID` | voice + whatsapp | |
| `TWILIO_AUTH_TOKEN` | voice + whatsapp | |
| `TWILIO_PHONE_NUMBER` | voice | E.164, e.g. `+15551234567` |
| `TWILIO_WHATSAPP_NUMBER` | whatsapp | sandbox default `whatsapp:+14155238886` |
| `BASE_URL` | voice + whatsapp | current ngrok https URL, no trailing slash |

`config.py` validates these in **groups** — `require_gemini()`,
`require_twilio()`, `require_whatsapp()` — so a missing WhatsApp key never
blocks the isolated Gemini test or a voice call, and vice versa.

## Current state

_(Update this section as you go so future sessions pick up where you left off.)_

**Phase A — Voice** (must be complete before Phase B begins)

- [x] Repo initialized; `main` + `develop`; issue-per-feature + Projects board
- [x] Step 0 skeleton — pinned `requirements.txt`, `app/config.py`,
      `.env.example`, `tests/test_config.py` (6 passing)
- [ ] Isolated Gemini Live test passing (mic + `--wav`) — **hard gate**
- [ ] Audio utils written + unit-tested
- [ ] FastAPI skeleton (`/call`, `/twiml`, `/ws/media-stream`) with echo stub
- [ ] Full bridge wired, first successful end-to-end test call
- [ ] Barge-in confirmed (talking over the agent cuts its audio)
- [ ] Example tool call confirmed working over a live call
- [ ] README verified by following it fresh

**Phase B — WhatsApp** (blocked until every Phase A box is ticked)

- [ ] `core/gemini_text.py` sharing `persona.py` + `tools.py`
- [ ] `channels/whatsapp/` — webhook, client, session store
- [ ] Window-expiry + session-keying unit tests passing offline
- [ ] Multi-turn WhatsApp conversation with a tool call firing
- [ ] WhatsApp README section

**The test that matters most:** rewrite `core/persona.py` and `core/tools.py`
for an invented problem statement and confirm both channels adopt it with zero
edits under `channels/`. If that fails, the scaffold has not done its job.
