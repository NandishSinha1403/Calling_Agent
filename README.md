# Calling Agent — Multi-Channel AI Agent Scaffold

One agent brain, two channels:

- **Voice** — outbound **Twilio** calls (PSTN + Media Streams) bridged to
  **Gemini Live** (native speech-to-speech) over a real-time WebSocket audio pipe.
- **WhatsApp** — inbound **Twilio WhatsApp Sandbox** messages answered by a
  standard Gemini text model, sharing the same persona and tools. *(Phase B — not built yet.)*

Built as a **scaffold**, not a product. Adapting it to a new problem statement
means editing two files — `app/core/persona.py` and `app/core/tools.py` — and
that one edit adapts both channels.

---

## Quick start (target: a test call in under 15 minutes)

### 1. Prerequisites

- **Python 3.11 or 3.12 — not 3.13.** `audioop` (the core of the audio bridge)
  was removed from the stdlib in 3.13.
- `brew install portaudio` — only needed for the microphone test.
- [ngrok](https://ngrok.com/download), with an authtoken configured.
- A Twilio account (trial is fine) and a Gemini API key.

### 2. Install

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # cp, NOT mv — .env.example is the tracked template
```

### 3. Fill in `.env`

| Variable | Where to get it |
|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |
| `TWILIO_ACCOUNT_SID` | Twilio Console home — the `AC…` string |
| `TWILIO_AUTH_TOKEN` | Twilio Console home → "API keys and Auth tokens" |
| `TWILIO_PHONE_NUMBER` | Console → **Communications → Numbers & senders → Manage → Active numbers** |
| `BASE_URL` | your ngrok https URL (step 5) |

**Trial accounts can only call verified numbers.** Add your own mobile under
**Numbers & senders → Verified Caller IDs** now — it needs a code sent to the
phone, and forgetting produces a call that fails for non-obvious reasons.

### 4. Prove Gemini works before involving Twilio

```bash
.venv/bin/python tests/test_gemini_isolated.py            # mic — use headphones
.venv/bin/python tests/test_gemini_isolated.py --wav q.wav  # or a WAV file
```

Do this first. If speech and tool calling don't work here, they won't work with
Twilio in the loop, and this narrows the fault to Gemini config rather than
audio format or networking.

### 5. Start the tunnel and the server

```bash
ngrok http 8000                       # leave running; copy the https URL
# put that URL in .env as BASE_URL (no trailing slash)
.venv/bin/uvicorn app.main:app --port 8000
```

Check it: `curl localhost:8000/health` — every field should be `true` and
`stream_url` should be a `wss://` URL matching your tunnel.

### 6. Place a call

```bash
curl -X POST localhost:8000/call -d "to=+15551234567"
```

Or open **http://localhost:8000/docs** and use the `/call` form — FastAPI's
generated UI is a serviceable control panel.

---

## Test without spending trial minutes

Trial voice time is finite, and a real call is slow, non-repeatable, and needs a
human holding a phone. Twilio's Media Stream protocol is just a WebSocket
carrying JSON events with base64 μ-law in 20ms frames, so we speak it directly:

```bash
# full pipeline through the real public URL
.venv/bin/python tests/simulate_twilio_call.py \
  --wav question.wav --listen 20 \
  --url wss://YOUR-NGROK-HOST/ws/media-stream

# barge-in: talk over the agent mid-sentence
.venv/bin/python tests/simulate_twilio_call.py \
  --wav question.wav --interrupt-wav interrupt.wav --listen 22 \
  --url wss://YOUR-NGROK-HOST/ws/media-stream
```

It writes `caller_heard.wav` — literally what a caller would hear. Generate test
audio on macOS with:

```bash
say -o question.wav --data-format=LEI16@16000 "I need to cancel my appointment."
```

Reserve real calls for what can't be simulated: PSTN audio quality and true
end-to-end latency.

---

## Adapting it (the whole point)

Edit **only** these two files:

**`app/core/persona.py`** — `BASE_RULES` are domain-free guardrails; leave them.
Rewrite `PERSONA` for your use case.

**`app/core/tools.py`** — replace `log_ticket` with real tools. Declarations are
plain dicts (no SDK import, so the same declaration feeds both channels), and
handlers are plain functions, sync or async.

```python
TOOL_DECLARATIONS = [{
    "name": "check_status",
    "description": "Look up a request's status. Call this once you have the ID.",
    "parameters": {
        "type": "OBJECT",
        "properties": {"request_id": {"type": "STRING", "description": "…"}},
        "required": ["request_id"],
    },
}]

def check_status(request_id: str) -> dict:
    return {"status": "in_progress"}

TOOL_HANDLERS = {"check_status": check_status}
```

Nothing under `app/channels/` should ever need editing. If you're adding an
`if persona == "x"` branch there, it belongs in `core/` instead.

**The test that matters:** rewrite both files and confirm both channels adopt the
change with zero edits under `channels/`.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Call connects, then silence | Stale `BASE_URL` after an ngrok restart. Check `/health`. |
| Call connects, caller hears nothing | Outbound media missing `streamSid` — Twilio discards it silently. |
| "This call is from a trial account" | Expected. Twilio's disclaimer, not a bug. |
| Call fails immediately | Number isn't under Verified Caller IDs. |
| Agent talks over the caller | Barge-in `clear` not reaching Twilio. |
| Model goes silent mid-call | A tool call went unanswered. Every `function_call` needs a `FunctionResponse` with a matching `id`. |
| Clicks/crackle every 20ms | Resampler state not threaded — see `audio_utils.py`. |
| Agent never replies to a WAV | VAD waits for trailing silence; a file that just stops never provides it. |
| `ModuleNotFoundError: audioop` | You're on Python 3.13. Use 3.11. |
| pyaudio won't build | `brew install portaudio` first. |

**Is it Twilio's side or Gemini's side?** Set `ECHO_MODE=1` and restart. The
WebSocket then echoes the caller's own audio back. If you hear yourself, Twilio's
half is fine and the fault is Gemini-side.

Server logs show `caller:` and `agent:` transcripts plus periodic peak-RMS. If
peak RMS stays near zero for a whole call, the caller's audio isn't arriving in
the format we think — that's a rate or byte-order bug, not networking.

---

## Layout

```
app/
├── config.py              # settings; validates per channel group
├── main.py                # FastAPI app, mounts channel routers
├── core/                  # THE BRAIN — edit these two for a new problem
│   ├── persona.py         #   BASE_RULES + swappable PERSONA
│   ├── tools.py           #   declarations + handlers + shared dispatch
│   ├── gemini_live.py     #   voice: audio-native Live session
│   └── gemini_text.py     #   whatsapp: text in/out          (Phase B)
└── channels/              # TRANSPORT — no domain logic, ever
    ├── voice/             #   routes, audio conversion, call trigger
    └── whatsapp/          #                                  (Phase B)
```

## Measured latency

Through the ngrok tunnel, end of speech to first audio byte:

| | |
|---|---|
| Isolated Gemini, simple turn | ~900 ms |
| Isolated Gemini, with a tool call | ~1800 ms |
| Full Twilio bridge | ~2500 ms |

The bridge adds VAD turn-detection plus two resampling hops. A real PSTN call
adds more. If this needs to come down, the levers are VAD sensitivity and
`GEMINI_MODEL` — both are config, not code.

## Ideas parked for hackathon day

All belong in `tools.py`, which is what the boundary is for: severity scoring,
duplicate detection via Jaccard similarity over complaint text, post-call SMS
with a tokenised evidence-upload link, and a database behind the tool handlers
instead of the in-memory list.

## Specs

- [`PRD.md`](PRD.md) — requirements and success criteria
- [`CLAUDE.md`](CLAUDE.md) — architecture and verified API specifics
- [`SETUP_CHECKLIST.md`](SETUP_CHECKLIST.md) — account setup
