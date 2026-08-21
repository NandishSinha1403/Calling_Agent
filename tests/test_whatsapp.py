"""WhatsApp channel tests -- offline, no Twilio, no Gemini, no trial messages.

The 24-hour window and the throttle are pure functions of time, so they are
tested by controlling the clock rather than by waiting or by messaging.
"""

import pytest
from fastapi.testclient import TestClient

from app.channels.whatsapp import session_store
from app.channels.whatsapp.session_store import (
    MIN_SEND_INTERVAL,
    WINDOW_SECONDS,
    Session,
    get_session,
)
from app.main import app


@pytest.fixture(autouse=True)
def clean_store():
    session_store.reset()
    yield
    session_store.reset()


# ---------------------------------------------------------------------------
# The 24-hour window — the constraint this channel is built around
# ---------------------------------------------------------------------------

def test_window_open_immediately_after_a_message():
    s = Session(sender="whatsapp:+15551234567")
    assert s.window_is_open()


def test_window_closes_after_24_hours():
    """Past this point Twilio only accepts pre-approved templates."""
    s = Session(sender="whatsapp:+1")
    later = s.window_opened_at + WINDOW_SECONDS + 1
    assert not s.window_is_open(now=later)


def test_window_still_open_just_before_expiry():
    s = Session(sender="whatsapp:+1")
    almost = s.window_opened_at + WINDOW_SECONDS - 10
    assert s.window_is_open(now=almost)


def test_inbound_message_reopens_the_window():
    """The user messaging us is the only thing that reopens it."""
    s = Session(sender="whatsapp:+1")
    s.window_opened_at -= WINDOW_SECONDS + 100
    assert not s.window_is_open()
    s.touch_window()
    assert s.window_is_open()


# ---------------------------------------------------------------------------
# Throttle — the Sandbox drops excess messages silently
# ---------------------------------------------------------------------------

def test_first_message_sends_immediately():
    assert Session(sender="whatsapp:+1").seconds_until_can_send() == 0.0


def test_second_message_waits_three_seconds():
    s = Session(sender="whatsapp:+1")
    s.record_send()
    assert s.seconds_until_can_send() == pytest.approx(MIN_SEND_INTERVAL, abs=0.1)


def test_throttle_clears_after_the_interval():
    s = Session(sender="whatsapp:+1")
    s.record_send()
    later = s.last_sent_at + MIN_SEND_INTERVAL + 0.1
    assert s.seconds_until_can_send(now=later) == 0.0


# ---------------------------------------------------------------------------
# Session keying and history
# ---------------------------------------------------------------------------

def test_same_sender_gets_the_same_session():
    assert get_session("whatsapp:+1") is get_session("whatsapp:+1")


def test_different_senders_are_isolated():
    """Two people must never see each other's conversation."""
    a, b = get_session("whatsapp:+1"), get_session("whatsapp:+2")
    a.history.append("a's message")
    assert a is not b and b.history == []


def test_history_is_trimmed_to_a_bound():
    """Unbounded history grows the prompt on every turn, forever."""
    s = get_session("whatsapp:+1")
    s.history = list(range(session_store.MAX_HISTORY_ENTRIES + 25))
    s.trim_history()
    assert len(s.history) == session_store.MAX_HISTORY_ENTRIES
    # Keeps the most RECENT turns, not the oldest.
    assert s.history[-1] == session_store.MAX_HISTORY_ENTRIES + 24


def test_trial_budget_is_counted():
    for _ in range(3):
        session_store.record_global_send()
    assert session_store.total_sent() == 3


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.ngrok-free.app")
    return TestClient(app)


def test_webhook_acknowledges_immediately(client, monkeypatch):
    """Twilio retries if we do not acknowledge, so this must return 200 fast."""
    import app.channels.whatsapp.main as wa

    class FakeModel:
        async def reply(self, history, message):
            return ("hello there", list(history) + [message])

    sent = []

    async def fake_send(session, body):
        sent.append((session.sender, body))
        return "SM123"

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())
    monkeypatch.setattr(wa, "send_reply", fake_send)

    resp = client.post(
        "/whatsapp/incoming",
        data={"From": "whatsapp:+15551234567", "Body": "hi"},
    )
    assert resp.status_code == 200
    assert sent == [("whatsapp:+15551234567", "hello there")]


def test_webhook_keeps_history_across_messages(client, monkeypatch):
    """Each webhook is a separate request; the conversation only exists if we keep it."""
    import app.channels.whatsapp.main as wa

    class FakeModel:
        async def reply(self, history, message):
            return (f"turn {len(history) + 1}", list(history) + [message])

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())
    monkeypatch.setattr(wa, "send_reply", lambda s, b: _noop())

    async def _noop():
        return "SM"

    for text in ("one", "two", "three"):
        client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": text})

    assert len(get_session("whatsapp:+1").history) == 3


def test_model_failure_still_replies(client, monkeypatch):
    """A model error must not leave the person staring at silence."""
    import app.channels.whatsapp.main as wa

    class Broken:
        async def reply(self, history, message):
            raise RuntimeError("model exploded")

    sent = []

    async def fake_send(session, body):
        sent.append(body)
        return "SM"

    monkeypatch.setattr(wa, "_model", lambda: Broken())
    monkeypatch.setattr(wa, "send_reply", fake_send)

    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "hi"})
    assert resp.status_code == 200
    assert sent and "went wrong" in sent[0].lower()


def test_closed_window_does_not_crash_the_webhook(client, monkeypatch):
    """Refusing to send is expected behaviour, not an error to propagate."""
    import app.channels.whatsapp.main as wa
    from app.channels.whatsapp.whatsapp_client import WindowClosedError

    class FakeModel:
        async def reply(self, history, message):
            return ("hi", list(history))

    async def refuse(session, body):
        raise WindowClosedError(session.sender)

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())
    monkeypatch.setattr(wa, "send_reply", refuse)

    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "hi"})
    assert resp.status_code == 200


def test_empty_body_is_handled(client, monkeypatch):
    """A media-only message has no Body; do not send the model an empty prompt."""
    import app.channels.whatsapp.main as wa

    sent = []

    async def fake_send(session, body):
        sent.append(body)
        return "SM"

    monkeypatch.setattr(wa, "send_reply", fake_send)
    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "   "})
    assert resp.status_code == 200
    assert sent and "text" in sent[0].lower()


# ---------------------------------------------------------------------------
# The boundary: both channels share one brain
# ---------------------------------------------------------------------------

def test_both_channels_share_persona_and_tools():
    """The whole point of the scaffold. If this fails, the design has broken."""
    from app.core import gemini_live, gemini_text
    from app.core.persona import PERSONA, system_prompt
    from app.core.tools import TOOL_DECLARATIONS

    assert gemini_live.system_prompt is gemini_text.system_prompt
    assert gemini_live.TOOL_DECLARATIONS is gemini_text.TOOL_DECLARATIONS
    assert gemini_live.execute_tool is gemini_text.execute_tool
    # Same persona text reaches both, with only the channel note differing.
    for channel in ("voice", "whatsapp"):
        assert PERSONA.strip() in system_prompt(channel)
    assert system_prompt("voice") != system_prompt("whatsapp")
