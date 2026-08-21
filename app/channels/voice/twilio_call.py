"""Outbound call triggering via the Twilio REST API.

No persona or domain logic here -- it dials a number and points Twilio at our
TwiML endpoint. What the agent then says is decided entirely by core/persona.py.
"""

from __future__ import annotations

import logging

from twilio.rest import Client

from app.config import settings

log = logging.getLogger(__name__)

_client: Client | None = None


def _get_client() -> Client:
    """Built lazily so importing this module never requires credentials.

    Matters for tests and for the isolated Gemini path, which must run with no
    Twilio config at all.
    """
    global _client
    if _client is None:
        settings.require_twilio()
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def trigger_call(to_number: str) -> str:
    """Place an outbound call. Returns the Twilio call SID.

    Twilio fetches TwiML from BASE_URL/twiml when the callee answers, which is
    what opens the Media Stream WebSocket back to us.

    On a trial account this only succeeds for numbers listed under Verified
    Caller IDs, and Twilio plays its trial disclaimer before our TwiML runs.
    Both are expected, not bugs.
    """
    client = _get_client()
    url = f"{settings.base_url}/twiml"

    log.info("Calling %s (twiml=%s)", to_number, url)
    call = client.calls.create(
        to=to_number,
        from_=settings.twilio_phone_number,
        url=url,
        method="POST",
    )
    log.info("Call queued: %s", call.sid)
    return call.sid
