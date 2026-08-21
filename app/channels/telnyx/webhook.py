"""Telnyx Call Control webhook.

    POST /telnyx/webhook

Telnyx posts call lifecycle events here as JSON. We acknowledge everything with
200 and log it.

This endpoint earns its place for one reason: Telnyx reports **streaming
failures as webhook events with a reason**, where Twilio's equivalent diagnosis
sits behind a paid Debugger. When a stream does not connect, this says why.
Events worth watching: streaming.started, streaming.stopped, streaming.failed.

Transport only. No persona or domain logic.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter(tags=["telnyx"])

# Loud for the ones that explain a failure, quiet for routine chatter.
NOISY = {"call.initiated", "call.answered", "call.hangup"}


@router.post("/telnyx/webhook")
async def telnyx_webhook(request: Request) -> JSONResponse:
    """Acknowledge and log. Always 200 -- Telnyx retries otherwise."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        log.warning("Telnyx webhook: unparseable body")
        return JSONResponse({"ok": True})

    data = body.get("data", {}) or {}
    event = data.get("event_type", "unknown")
    payload = data.get("payload", {}) or {}

    if "fail" in event or "error" in event:
        # The reason is the whole point of having this endpoint.
        log.error("Telnyx %s: %s", event, payload)
    elif event in NOISY:
        log.info(
            "Telnyx %s (call_control_id=%s, from=%s to=%s)",
            event,
            payload.get("call_control_id", "?")[:16],
            payload.get("from"),
            payload.get("to"),
        )
    elif event.startswith("streaming"):
        log.info("Telnyx %s: %s", event, payload)
    else:
        log.debug("Telnyx %s", event)

    return JSONResponse({"ok": True})
