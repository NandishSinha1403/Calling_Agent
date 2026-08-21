# Calling Agent — Multi-Channel AI Agent Scaffold

One agent brain, two channels:

- **Voice** — outbound **Twilio** calls (PSTN + Media Streams) bridged to
  **Gemini Live** (native speech-to-speech) over a real-time WebSocket audio pipe.
- **WhatsApp** — inbound **Twilio WhatsApp Sandbox** messages answered by a
  standard Gemini text model, sharing the same persona and the same tools.

Built as a *scaffold*, not a product. The design goal is that adapting it to a new
problem statement means editing two files — `app/core/persona.py` and
`app/core/tools.py` — and that one edit adapts both channels.

- Requirements and success criteria: [`PRD.md`](PRD.md)
- Architecture notes and API specifics: [`CLAUDE.md`](CLAUDE.md)
- Pre-flight account/tooling setup: [`SETUP_CHECKLIST.md`](SETUP_CHECKLIST.md)

> **Status:** in progress. Full setup and run instructions land with
> `feature/docs-and-tooling`.
