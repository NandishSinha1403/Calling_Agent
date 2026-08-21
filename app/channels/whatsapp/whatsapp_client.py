"""Sending WhatsApp replies via Twilio.

Transport only -- no persona, no domain logic.

Every send passes three checks that exist because of Sandbox constraints:

1. The 24-hour window must be open. Outside it only three fixed pre-approved
   templates are allowed, and the Sandbox supports no custom ones. We refuse and
   log clearly rather than firing a request Twilio will reject, because a raw
   Twilio error here reads like a bug in our code when it is a policy limit.

2. Replies are spaced at least 3 seconds apart per sender. The Sandbox throttles
   to one message per three seconds and drops the excess silently -- the worst
   kind of failure, because nothing appears wrong.

3. Sends are counted against the 100 trial messages, which are shared with SMS
   and easy to exhaust in one debugging session.
"""

from __future__ import annotations

import asyncio
import logging

from twilio.rest import Client

from app.channels.whatsapp.session_store import Session, record_global_send
from app.config import settings

log = logging.getLogger(__name__)

_client: Client | None = None


def _get_client() -> Client:
    """Lazy, so importing this module never requires credentials."""
    global _client
    if _client is None:
        settings.require_whatsapp()
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


class WindowClosedError(RuntimeError):
    """The 24-hour free-form window has expired for this sender."""


async def send_reply(session: Session, body: str) -> str | None:
    """Send `body` to the session's sender. Returns the message SID.

    Raises WindowClosedError if the window has closed -- the caller decides what
    to do, since silently dropping a reply would be worse.
    """
    if not session.window_is_open():
        log.warning(
            "Cannot reply to %s: 24-hour window closed. A pre-approved template "
            "would be required, and the Sandbox supports no custom templates. "
            "Ask them to message again (or send 'join <code>') to reopen it.",
            session.sender,
        )
        raise WindowClosedError(session.sender)

    wait = session.seconds_until_can_send()
    if wait > 0:
        # Sleep rather than drop. The Sandbox cap is 1 msg / 3 sec and exceeding
        # it loses messages silently.
        log.info("Throttling %.1fs before replying to %s", wait, session.sender)
        await asyncio.sleep(wait)

    client = _get_client()
    message = await asyncio.to_thread(
        lambda: client.messages.create(
            to=session.sender,
            from_=settings.twilio_whatsapp_number,
            body=body,
        )
    )
    session.record_send()
    total = record_global_send()
    log.info("Replied to %s (sid=%s, %d sent this run)", session.sender, message.sid, total)
    return message.sid
