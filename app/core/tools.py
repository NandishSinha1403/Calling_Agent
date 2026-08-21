"""What the agent can actually DO.

Together with `persona.py`, this is one of only two files that should need to
change for a new problem statement -- and it serves BOTH channels.

Two things are exported:

  TOOL_DECLARATIONS -- what the model is told exists. Plain dicts in the
                       OpenAPI subset Gemini accepts. Deliberately NOT
                       `google.genai.types` objects: keeping the SDK out of this
                       file is what lets the same declarations feed both the
                       Live API (voice) and generateContent (WhatsApp).

  TOOL_HANDLERS     -- {name: callable} actually executed when the model calls a
                       tool. Both channels dispatch through this same dict, so
                       there is one tool code path rather than two that drift.

Handlers may be sync or async; `execute_tool` handles both. Return a JSON-safe
dict -- it is sent straight back to the model as the function response.

ON HACKATHON DAY: replace the example below with real tools. Keep the shapes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------
# Description text is prompt, not documentation -- the model decides whether to
# call a tool based on it. Say WHEN to call it, not just what it does.

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "log_ticket",
        "description": (
            "Record the outcome of this conversation. Call this once you know "
            "what the person needs and before the conversation ends. Do not "
            "call it with placeholder values -- ask for anything missing first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "summary": {
                    "type": "STRING",
                    "description": (
                        "What the person needs, in one or two plain sentences, "
                        "in English regardless of the language spoken."
                    ),
                },
                "priority": {
                    "type": "STRING",
                    "enum": ["low", "normal", "high"],
                    "description": (
                        "high if urgent, distressed, or cancelling; normal for "
                        "a change or question; low for routine confirmation."
                    ),
                },
            },
            "required": ["summary", "priority"],
        },
    },
]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

# In-memory only, per PRD section 6: no database before hackathon day.
# Lost on restart, which is fine for a demo and is stated rather than hidden.
_TICKETS: list[dict[str, Any]] = []


def log_ticket(summary: str, priority: str = "normal") -> dict[str, Any]:
    """Record an outcome and hand back a real reference the agent may speak.

    The returned ticket_id matters more than it looks. BASE_RULES forbids the
    agent from inventing reference numbers, so this is the only place one can
    legitimately come from.
    """
    ticket = {
        "ticket_id": f"TKT-{len(_TICKETS) + 1:04d}",
        "summary": summary,
        "priority": priority,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _TICKETS.append(ticket)
    log.info("TOOL log_ticket -> %s", json.dumps(ticket, ensure_ascii=False))
    # Surface tool activity to anything watching the call live. Imported here
    # rather than at module scope to keep tools.py free of infrastructure
    # imports at the top -- this file is meant to be readable by someone
    # writing a new tool under time pressure.
    from app.event_bus import publish
    publish("tool", name="log_ticket", **ticket)
    print(f"\n  [TOOL] log_ticket: {json.dumps(ticket, ensure_ascii=False)}\n")
    return {"status": "recorded", **ticket}


def get_tickets() -> list[dict[str, Any]]:
    """Test/debug helper. Not exposed to the model."""
    return list(_TICKETS)


TOOL_HANDLERS: dict[str, Callable[..., Any | Awaitable[Any]]] = {
    "log_ticket": log_ticket,
}


# ---------------------------------------------------------------------------
# Dispatch -- shared by both channels
# ---------------------------------------------------------------------------

async def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run a tool by name and always return a dict safe to send to the model.

    Never raises. A tool crashing mid-call must not kill the conversation or
    the WebSocket -- the model is told the tool failed and can apologise and
    carry on, which is a far better outcome than dead air.

    An unknown tool name is reported the same way rather than raising, since
    models occasionally hallucinate a plausible-sounding function.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        log.warning("Model called unknown tool: %s", name)
        return {"status": "error", "error": f"No such tool: {name}"}

    try:
        if inspect.iscoroutinefunction(handler):
            return await handler(**args)
        # Sync handlers run in a thread so a slow one cannot stall the audio
        # bridge -- on a live call, a blocked event loop is audible.
        return await asyncio.to_thread(lambda: handler(**args))
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        log.exception("Tool %s failed", name)
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
