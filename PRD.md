# PRD — Outbound Voice AI Agent (Pluggable MVP)

## 1. Context

Built ahead of a 24-hour hackathon (JPMorgan Code for Good) where the actual problem
statement is unknown until hackathon day. The deliverable for *pre-hackathon* work is
not a finished product — it's a working, well-separated **scaffold** that makes
hackathon day mostly a matter of editing two files (persona + tools) instead of
building infrastructure under time pressure.

## 2. Goal

An outbound-calling voice agent that:
- Dials a real phone number via Twilio
- Talks to the callee using Gemini Live (native speech-to-speech, no separate STT/TTS)
- Can invoke a function mid-conversation (e.g. to log structured data)
- Can have its personality and capabilities swapped in minutes, without touching
  the audio/networking code

## 3. Users

- **Primary user during build:** me, solo, pre-hackathon and during the 24 hours.
- **Primary user at demo time:** whoever the hackathon problem statement identifies
  as the call recipient (could be a citizen, a beneficiary, a volunteer, an internal
  test number — unknown until problem statement is assigned).

## 4. Functional Requirements

| # | Requirement | Notes |
|---|---|---|
| 1 | `POST /call` admin endpoint — takes a phone number, triggers an outbound Twilio call | No auth needed for MVP; single operator |
| 2 | TwiML endpoint returning `<Connect><Stream>` | Opens a bidirectional Media Stream WebSocket |
| 3 | WebSocket bridge: Twilio (μ-law, 8kHz) ↔ Gemini Live (PCM16, 16kHz in / 24kHz out) | Real-time, streaming, stateful resampling |
| 4 | Gemini Live session with swappable system prompt | Prompt lives in one file, imported not hardcoded |
| 5 | One working example function/tool call (`log_ticket(summary, priority)`), printed to console | Proves the tool-calling path end-to-end; DB wiring deferred to hackathon day |
| 6 | `.env.example` covering all required secrets/config | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `GEMINI_API_KEY`, `BASE_URL` |
| 7 | README with exact steps: ngrok setup, Twilio trial number verification, run server, trigger test call | Must work for someone tired at 3am on hackathon day |

## 5. Non-Functional Requirements

- **Modularity is the actual point of this project.** Persona and tool definitions
  must be swappable in under 5 minutes without touching audio/WebSocket code.
- **Latency**: audio bridge should not introduce more than one resampling pass per
  direction; avoid unnecessary buffering that adds perceptible lag to a phone call.
- **Resilience over completeness**: prefer a demo that reliably completes a call
  over one with more features that occasionally breaks the WebSocket.
- **Legibility under pressure**: code needs to be readable by a tired team at 2am,
  not clever. Prefer explicit over abstracted.

## 6. Explicitly Out of Scope (pre-hackathon)

- Authentication/authorization on `/call`
- Persistent storage / real database for tool calls (console log only, for now)
- Inbound calls (outbound only)
- Multi-call concurrency handling beyond "don't crash" (single active call is fine)
- Production deployment, scaling, observability
- Handling Twilio production (non-trial) account nuances

## 7. Success Criteria (pre-hackathon build)

1. Isolated script proves Gemini Live audio in/out and function calling work,
   independent of Twilio — **before** any Twilio wiring is attempted.
2. A real outbound call to a verified test number connects, the agent speaks the
   system prompt's opening line, responds to speech, and can trigger
   `log_ticket()` with a visible console log.
3. Swapping `persona.py` and `tools.py` for a new use case requires no edits to
   `audio_utils.py`, `gemini_client.py`, `main.py`, or `twilio_call.py`.
4. README lets a stranger get a test call running in under 15 minutes.

## 8. Success Criteria (hackathon day, informational — not building this now)

- New persona + 1-3 new domain-specific tools written and wired in under an hour.
- Live demo call completes without dropped audio or crashed WebSocket.

## 9. Key Risks

- **Gemini Live API surface is actively changing** (model names, SDK method names
  have shifted across recent SDK versions) — pin dependency versions, don't assume
  training-data-era API shape is current.
- **Audio format mismatch** between Twilio (μ-law/8kHz) and Gemini (PCM16, split
  rates in/out) is the single most likely source of garbled or silent audio.
- **Twilio trial account constraints** (verified numbers only, trial disclaimer
  audio) will surprise anyone testing for the first time.
- **ngrok free-tier URL rotation** breaks `BASE_URL` and the Twilio-facing webhook
  on every restart if not re-synced.

## 10. Build Order

1. Isolated Gemini Live test (mic/speaker, no Twilio, no FastAPI) — confirms API
   connectivity, audio streaming, and tool calling work at all.
2. Audio conversion utilities (μ-law↔PCM, resampling), unit-testable without a
   live call.
3. FastAPI skeleton: `/call`, `/twiml`, `/ws/media-stream` — wire Twilio to a stub
   that just echoes audio, to isolate Twilio-side bugs from Gemini-side bugs.
4. Swap the stub for the real Gemini bridge.
5. Wire the example tool call end-to-end.
6. README + `.env.example` polish.
