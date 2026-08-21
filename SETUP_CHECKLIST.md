# Setup Checklist — Do This Before Opening Claude Code

## Accounts & Access

- [ ] **Twilio account** created (trial is fine) — https://www.twilio.com/try-twilio
- [ ] Twilio **Account SID** and **Auth Token** copied from the console dashboard
- [ ] A **Twilio phone number** purchased/assigned (trial accounts get one free number)
- [ ] Your **own cell number verified** in Twilio Console → Phone Numbers →
      Verified Caller IDs (trial accounts can only call verified numbers — do this
      now, it involves receiving a verification code)
- [ ] **Google AI Studio / Gemini API key** — https://aistudio.google.com/apikey
- [ ] Confirm your Gemini API key has access to a **Live API native-audio model**
      (not just standard `generateContent`) — check current model availability at
      https://ai.google.dev/gemini-api/docs/live-api before assuming a model name works

## Local environment

- [ ] Python 3.11 or 3.12 installed (avoid 3.13 unless you've confirmed
      `audioop-lts` works cleanly, or just sidestep the question with 3.12)
- [ ] `ngrok` installed and an ngrok account created (free tier is fine) —
      https://ngrok.com/download
- [ ] `ngrok` authtoken configured (`ngrok config add-authtoken <token>`)
- [ ] A virtual environment tool ready (`venv`, `uv`, `poetry` — whatever you
      normally use)

## Things to decide before you start coding (saves time mid-build)

- [ ] Which phone number will you test-call during the pre-hackathon build?
      (Must be Twilio-verified per above.)
- [ ] Do you want the isolated Gemini test (`tests/test_gemini_isolated.py`) to
      use your laptop mic/speakers? If so, confirm `pyaudio` installs cleanly on
      your OS *now* — it's a common source of setup pain (may need `portaudio`
      via brew/apt first).
- [ ] Pick a placeholder persona for testing (e.g. "helpful assistant that
      confirms appointment times") so you're not blocked waiting for the real
      hackathon problem statement to test end-to-end.

## During the CLI session, have these open/handy

- [ ] `PRD.md` and `CLAUDE.md` in the project root (Claude Code will read
      `CLAUDE.md` automatically; point it at `PRD.md` if it needs the fuller spec)
- [ ] Twilio console open in a browser tab (you'll copy the ngrok URL into the
      Twilio-facing config, and may want to watch the Twilio Console's call logs /
      debugger while testing — it's the fastest way to see WebSocket-level errors)
- [ ] A terminal tab reserved for `ngrok http <port>` — leave it running, don't
      close it mid-session or your `BASE_URL` goes stale

## Known gotchas to expect (so they don't cost you debugging time)

- Twilio trial calls play an audible "this call is being made from a trial
  account" style disclaimer before your TwiML runs — this is expected, not a bug.
- ngrok free tier gives you a **new URL every time you restart it** — you'll need
  to update `BASE_URL` in `.env` each time, and re-verify the TwiML endpoint is
  reachable at the new URL.
- If Python 3.13: `audioop` was removed from the standard library — the project
  plan uses `audioop-lts` as a drop-in replacement; make sure it's in
  `requirements.txt` before you hit an import error mid-build.
- Gemini Live model names and some SDK method names have changed across recent
  `google-genai` versions — if Claude Code writes code referencing an API shape
  that errors out, check current docs rather than assuming the code is wrong in
  some other way.
