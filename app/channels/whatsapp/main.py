"""WhatsApp channel webhook.

    POST /whatsapp/incoming    Twilio delivers inbound messages here

INBOUND-DRIVEN BY DESIGN. There is deliberately no "send to anyone" endpoint,
because the Sandbox cannot freely initiate conversations: outbound is limited to
three fixed pre-approved templates and custom templates are unsupported. The
conversation starts when the user messages us. That is the mirror image of the
voice channel, and mirroring voice's outbound /call shape onto it would produce
something that cannot work.

Transport only. No persona or domain logic -- see core/ for that.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Form
from fastapi.responses import Response

from xml.sax.saxutils import escape

from app.channels.whatsapp.session_store import get_session
from app.core.gemini_text import GeminiTextSession

log = logging.getLogger(__name__)

router = APIRouter(tags=["whatsapp"])

_session_client: GeminiTextSession | None = None


def _model() -> GeminiTextSession:
    global _session_client
    if _session_client is None:
        _session_client = GeminiTextSession()
    return _session_client


@router.post("/whatsapp/incoming")
async def incoming(From: str = Form(...), Body: str = Form(default="")) -> Response:
    """Handle one inbound WhatsApp message.

    Twilio posts form-encoded fields; `From` looks like 'whatsapp:+15551234567'.

    We reply via the REST API rather than returning TwiML, because the reply
    depends on a model round trip that may include tool calls. Returning empty
    TwiML acknowledges the webhook immediately so Twilio does not retry.
    """
    sender, text = From, Body.strip()
    log.info("WhatsApp from %s: %r", sender, text)

    session = get_session(sender)
    # An inbound message reopens the 24-hour free-form window.
    session.touch_window()

    if not text:
        # Media-only message. We are text-only for now; say so rather than
        # sending the model an empty prompt.
        return Response(
            content="<Response><Message>I can only read text messages at the "
                    "moment.</Message></Response>",
            media_type="application/xml",
        )

    try:
        reply, history = await _model().reply(session.history, text)
        session.history = history
        session.trim_history()
    except Exception:  # noqa: BLE001
        # A model failure must not leave the person with silence.
        log.exception("Model failed for %s", sender)
        reply = "Sorry, something went wrong on my end. Could you try again?"

    # Reply as TwiML rather than via the REST API. The Sandbox rejects
    # free-form outbound messages with "ContentSid Required" -- it wants a
    # pre-approved template -- but a TwiML reply is part of the inbound session
    # and carries no such restriction. It is also one fewer API call.
    log.info("agent -> %s: %r", sender, reply)
    return Response(
        content=f"<Response><Message>{escape(reply)}</Message></Response>",
        media_type="application/xml",
    )
