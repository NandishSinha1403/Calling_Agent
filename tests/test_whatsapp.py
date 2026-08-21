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

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())
    resp = client.post(
        "/whatsapp/incoming",
        data={"From": "whatsapp:+15551234567", "Body": "hi"},
    )
    assert resp.status_code == 200
    assert "<Message>hello there</Message>" in resp.text


def test_webhook_keeps_history_across_messages(client, monkeypatch):
    """Each webhook is a separate request; the conversation only exists if we keep it."""
    import app.channels.whatsapp.main as wa

    class FakeModel:
        async def reply(self, history, message):
            return (f"turn {len(history) + 1}", list(history) + [message])

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())

    for text in ("one", "two", "three"):
        client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": text})

    assert len(get_session("whatsapp:+1").history) == 3


def test_model_failure_still_replies(client, monkeypatch):
    """A model error must not leave the person staring at silence."""
    import app.channels.whatsapp.main as wa

    class Broken:
        async def reply(self, history, message):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(wa, "_model", lambda: Broken())
    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "hi"})
    assert resp.status_code == 200
    assert "went wrong" in resp.text.lower()


def test_reply_is_twiml_and_xml_escaped(client, monkeypatch):
    """Replies go back as TwiML, not via the REST API.

    The Sandbox rejects free-form outbound messages with "ContentSid Required" —
    it wants a pre-approved template — but a TwiML reply is part of the inbound
    session and carries no such restriction.

    Escaping matters: a model reply containing & or < would otherwise produce
    malformed XML and Twilio would silently send nothing.
    """
    import app.channels.whatsapp.main as wa

    class FakeModel:
        async def reply(self, history, message):
            return ("Tom & Jerry <3", list(history))

    monkeypatch.setattr(wa, "_model", lambda: FakeModel())
    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "hi"})
    assert resp.status_code == 200
    assert "<Message>Tom &amp; Jerry &lt;3</Message>" in resp.text


def test_empty_body_is_handled(client):
    """A media-only message has no Body; do not send the model an empty prompt."""
    resp = client.post("/whatsapp/incoming", data={"From": "whatsapp:+1", "Body": "   "})
    assert resp.status_code == 200
    assert "text messages" in resp.text


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
