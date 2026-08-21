# Calling Agent — Outbound Voice AI Scaffold

A pluggable outbound-calling voice agent: **Twilio** (PSTN + Media Streams) bridged
to **Gemini Live** (native speech-to-speech) over a real-time WebSocket audio pipe.

Built as a *scaffold*, not a product. The design goal is that adapting it to a new
problem statement means editing two files — `app/persona.py` and `app/tools.py` —
and nothing else.

- Requirements and success criteria: [`PRD.md`](PRD.md)
- Architecture notes and API specifics: [`CLAUDE.md`](CLAUDE.md)
- Pre-flight account/tooling setup: [`SETUP_CHECKLIST.md`](SETUP_CHECKLIST.md)

> **Status:** in progress. Full setup and run instructions land with
> `feature/docs-and-tooling`.
