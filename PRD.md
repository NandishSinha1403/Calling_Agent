# PRD — Multi-Channel AI Agent Scaffold (Pluggable MVP)

## 1. Context

Built ahead of a 24-hour hackathon (JPMorgan Code for Good) where the actual problem
statement is unknown until hackathon day. The deliverable for *pre-hackathon* work is
not a finished product — it's a working, well-separated **scaffold** that makes
hackathon day mostly a matter of editing two files (`core/persona.py` +
`core/tools.py`) instead of building infrastructure under time pressure.

Two channels are provided so the scaffold fits whichever shape the problem
statement takes: some problems want a phone call, some want messaging, and
the same brain serves both.

## 2. Goal

A scaffold carrying **one agent brain across two channels**:

**Voice channel** (outbound-initiated):
- Dials a real phone number via Twilio
- Talks to the callee using Gemini Live (native speech-to-speech, no separate STT/TTS)
- Can invoke a function mid-conversation (e.g. to log structured data)

**WhatsApp channel** (inbound-driven — see §9 for why it cannot be outbound):
- Receives messages via a Twilio WhatsApp Sandbox webhook
- Replies using a standard Gemini text model, holding multi-turn context
- Can invoke the **same** functions as the voice channel

**Shared across both:**
- Personality and capabilities swappable in minutes, by editing two files, with
  the change taking effect on both channels — without touching audio,
  networking, or webhook code.

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
| 6 | `.env.example` covering all required secrets/config | Validated in groups so one channel's missing key never blocks the other |
| 7 | README with exact steps: ngrok setup, Twilio trial number verification, run server, trigger test call | Must work for someone tired at 3am on hackathon day |
| 8 | `POST /whatsapp/incoming` webhook, parsing Twilio's form-encoded `From`/`Body` | Mounted on the same FastAPI app and the same ngrok tunnel as voice |
| 9 | Per-sender session store: chat history + 24-hour window state | The window is a hard Twilio constraint, not an optimization — see §9 |
| 10 | WhatsApp replies routed through the same `TOOL_HANDLERS` as voice | One tool code path, so the two channels cannot drift apart |
| 11 | Reply spacing guard (1 msg / 3 sec) and a logged running message count | Sandbox throughput cap; 100 trial messages is a finite budget |

## 5. Non-Functional Requirements

- **Modularity is the actual point of this project.** Persona and tool definitions
  must be swappable in under 5 minutes without touching audio, WebSocket, or
  webhook code — and one swap must adapt **both** channels. `app/core/` holds
  the brain; `app/channels/` holds transport only, with no domain logic and no
  cross-channel imports.
- **Latency**: audio bridge should not introduce more than one resampling pass per
  direction; avoid unnecessary buffering that adds perceptible lag to a phone call.
- **Resilience over completeness**: prefer a demo that reliably completes a call
  over one with more features that occasionally breaks the WebSocket.
- **Legibility under pressure**: code needs to be readable by a tired team at 2am,
  not clever. Prefer explicit over abstracted.

## 6. Explicitly Out of Scope (pre-hackathon)

- Authentication/authorization on `/call`
- Persistent storage / real database for tool calls (console log only, for now)
- Inbound *calls* (voice is outbound only; WhatsApp is the inbound channel)
- Outbound-initiated WhatsApp conversations — not a choice, a Sandbox limit (§9)
- WhatsApp media/attachments (text only)
- Multi-call concurrency handling beyond "don't crash" (single active call is fine)
- Production deployment, scaling, observability
- Handling Twilio production (non-trial) account nuances

## 7. Success Criteria (pre-hackathon build)

1. Isolated script proves Gemini Live audio in/out and function calling work,
   independent of Twilio — **before** any Twilio wiring is attempted.
2. A real outbound call to a verified test number connects, the agent speaks the
   system prompt's opening line, responds to speech, and can trigger
   `log_ticket()` with a visible console log.
3. **The boundary test — the one that matters most.** Swapping
   `app/core/persona.py` and `app/core/tools.py` for a new use case requires
   **zero** edits anywhere under `app/channels/`, and both channels pick up the
   change. If this fails, the scaffold has not done its job regardless of
   whether calls connect.
4. README lets a stranger get a test call running in under 15 minutes.
5. A WhatsApp sandbox conversation holds multi-turn context and fires the same
   example tool the voice channel fires.

## 8. Success Criteria (hackathon day, informational — not building this now)

- New persona + 1-3 new domain-specific tools written and wired in under an hour,
  taking effect on both channels from that one edit.
- Live demo call completes without dropped audio or crashed WebSocket.
- Live WhatsApp demo holds context across turns and fires a tool call.

## 9. Key Risks

- **Gemini Live API surface is actively changing** (model names, SDK method names
  have shifted across recent SDK versions) — pin dependency versions, don't assume
  training-data-era API shape is current.
- **Audio format mismatch** between Twilio (μ-law/8kHz) and Gemini (PCM16, split
  rates in/out) is the single most likely source of garbled or silent audio.
- **Twilio trial account constraints** (verified numbers only, trial disclaimer
  audio) will surprise anyone testing for the first time.
- **ngrok free-tier URL rotation** breaks `BASE_URL` and the Twilio-facing webhook
  on every restart if not re-synced. Now affects both channels at once.
- **WhatsApp Sandbox cannot freely initiate conversations.** This is the single
  constraint that shapes the WhatsApp channel's whole design:
  - Outbound-initiated messages are limited to **three fixed pre-approved
    templates** (appointment reminders, order notifications, verification
    codes). **Custom templates are not supported in the Sandbox.**
  - Free-form messaging works **only inside a 24-hour window**, opened when the
    user messages us first (`join <sandbox code>` counts).
  - Throughput cap: **1 message per 3 seconds** — exceed it and messages drop.
  - **100 WhatsApp messages** as trial units, shared with SMS.
  - Shared sandbox number `+14155238886`, stamped with the Twilio logo.

  Consequence: the WhatsApp channel is **inbound-driven**, the mirror image of
  voice. Designing it as an outbound `/send` endpoint would produce something
  that cannot work in the Sandbox at all.
- **Building both channels at once** would put the risky work (real-time audio)
  and the easy work (a webhook) in flight together, and neither would get proper
  attention. Voice ships first, fully tested; WhatsApp starts after.

## 10. Build Order

**Phase A — Voice. Completed and merged before Phase B begins.**

1. Isolated Gemini Live test (mic/speaker + `--wav`, no Twilio, no FastAPI) —
   confirms API connectivity, audio streaming, and tool calling work at all.
   **Hard gate: nothing else starts until this passes.**
2. Audio conversion utilities (μ-law↔PCM, stateful resampling), unit-testable
   without a live call.
3. FastAPI skeleton: `/call`, `/twiml`, `/ws/media-stream` — wire Twilio to a
   stub that just echoes audio, to isolate Twilio-side bugs from Gemini-side
   bugs.
4. Swap the stub for the real Gemini bridge, including barge-in handling.
5. Wire the example tool call end-to-end; README + `.env.example` polish.

**Gate: a live call demonstrably works, all of Phase A merged.**

**Phase B — WhatsApp.**

6. `core/gemini_text.py` — text-in/text-out client sharing `persona.py` and
   `tools.py` with the voice channel.
7. `channels/whatsapp/` — inbound webhook, session store (history + 24h window),
   reply client with the window check, spacing guard, and message counter.
   Window-expiry logic unit-tested offline *before* spending trial messages.
8. WhatsApp README section: sandbox join flow, webhook config, and the
   template/window constraint stated plainly so it is not mistaken for a bug.

**Final: the boundary test (§7.3) — swap the two `core/` files and confirm both
channels adapt with no changes under `channels/`.**
