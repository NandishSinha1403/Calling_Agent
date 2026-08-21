"""Per-sender conversation state for the WhatsApp channel.

Two things are tracked, and the second is the reason this file exists:

1. CHAT HISTORY. A voice call is one long-lived connection holding its own
   context. WhatsApp is a series of independent webhook requests, so the
   conversation only exists if we keep it.

2. THE 24-HOUR WINDOW. Twilio's WhatsApp Sandbox only permits free-form replies
   inside a 24-hour window that the USER opens by messaging us ("join <code>"
   counts). Outside it, only three fixed pre-approved templates are allowed, and
   custom templates are not supported in the Sandbox at all. So we track when
   the window opened and check before replying -- otherwise we fire requests
   Twilio rejects and the failure looks like a bug in our code.

Storage is in memory, per PRD section 6 (no database before hackathon day). The
trade-off is stated rather than hidden: restarting the server forgets every
conversation. Acceptable for a demo; the fix on hackathon day is to back
`_SESSIONS` with Redis or a table and change nothing else.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

WINDOW_SECONDS = 24 * 60 * 60

# Twilio's Sandbox throttles to one message per three seconds. Exceeding it
# silently drops messages, so replies to a sender are spaced by at least this.
MIN_SEND_INTERVAL = 3.0

# Keep history bounded. An unbounded transcript grows the prompt on every turn,
# which costs latency and tokens forever. Keeps the most recent entries.
MAX_HISTORY_ENTRIES = 40


@dataclass
class Session:
    """One WhatsApp conversation, keyed by the sender's address."""

    sender: str
    history: list[Any] = field(default_factory=list)
    window_opened_at: float = field(default_factory=time.monotonic)
    last_sent_at: float = 0.0
    messages_sent: int = 0

    def touch_window(self) -> None:
        """An inbound message from the user reopens the 24-hour window."""
        self.window_opened_at = time.monotonic()

    def window_is_open(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return (now - self.window_opened_at) < WINDOW_SECONDS

    def window_remaining(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, WINDOW_SECONDS - (now - self.window_opened_at))

    def seconds_until_can_send(self, now: float | None = None) -> float:
        """How long to wait to respect the 1-message-per-3-seconds cap."""
        now = time.monotonic() if now is None else now
        if self.last_sent_at == 0.0:
            return 0.0
        return max(0.0, MIN_SEND_INTERVAL - (now - self.last_sent_at))

    def record_send(self, now: float | None = None) -> None:
        self.last_sent_at = time.monotonic() if now is None else now
        self.messages_sent += 1

    def trim_history(self) -> None:
        if len(self.history) > MAX_HISTORY_ENTRIES:
            self.history = self.history[-MAX_HISTORY_ENTRIES:]


_SESSIONS: dict[str, Session] = {}

# The Sandbox includes 100 WhatsApp messages as trial units, shared with SMS.
# That is easy to exhaust in one debugging session without noticing, so every
# send is counted and the remaining budget is logged.
TRIAL_MESSAGE_BUDGET = 100
_total_sent = 0


def get_session(sender: str) -> Session:
    """Fetch or create the session for a sender ('whatsapp:+1555...')."""
    session = _SESSIONS.get(sender)
    if session is None:
        session = Session(sender=sender)
        _SESSIONS[sender] = session
        log.info("New WhatsApp session: %s", sender)
    return session


def record_global_send() -> int:
    """Count a sent message and return the running total."""
    global _total_sent
    _total_sent += 1
    remaining = TRIAL_MESSAGE_BUDGET - _total_sent
    if remaining in (50, 25, 10, 5, 1) or remaining <= 0:
        log.warning(
            "WhatsApp trial budget: %d of %d used, %d left",
            _total_sent, TRIAL_MESSAGE_BUDGET, max(0, remaining),
        )
    return _total_sent


def total_sent() -> int:
    return _total_sent


def reset() -> None:
    """Clear all state. Tests only."""
    global _total_sent
    _SESSIONS.clear()
    _total_sent = 0
