"""Voice channel route tests -- offline, no Twilio account touched.

The WebSocket echo is exercised with FastAPI's test client, so the framing and
streamSid handling are verified before a real call is ever placed.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://example.ngrok-free.app")
    return TestClient(app)


# ---------------------------------------------------------------------------
# TwiML
# ---------------------------------------------------------------------------

def test_twiml_uses_absolute_wss_url(client):
    """Twilio needs an absolute wss:// URL; https:// or a relative path fails."""
    body = client.post("/twiml").text
    assert 'url="wss://example.ngrok-free.app/ws/media-stream"' in body
    assert "https://" not in body


def test_twiml_is_connect_stream_not_start(client):
    """<Connect><Stream> is bidirectional; <Start><Stream> is one-way only.

    Getting this wrong gives a call where the agent can hear but not speak.
    """
    body = client.post("/twiml").text
    assert "<Connect>" in body and "<Stream" in body
    assert "<Start>" not in body


def test_twiml_fails_loudly_without_base_url(monkeypatch):
    """A stale or missing BASE_URL is the most common breakage on this project."""
    monkeypatch.delenv("BASE_URL", raising=False)
    resp = TestClient(app, raise_server_exceptions=False).post("/twiml")
    assert resp.status_code == 500
    assert "BASE_URL" in resp.text


# ---------------------------------------------------------------------------
# The echo stub
# ---------------------------------------------------------------------------

def _media(payload: bytes) -> str:
    return json.dumps(
        {"event": "media", "media": {"payload": base64.b64encode(payload).decode()}}
    )


@pytest.fixture
def echo_client(monkeypatch):
    """Echo mode is a diagnostic fallback now, not the default."""
    monkeypatch.setenv("BASE_URL", "https://example.ngrok-free.app")
    monkeypatch.setenv("ECHO_MODE", "1")
    return TestClient(app)


def test_echo_returns_audio_with_stream_sid(echo_client):
    """Outbound media must carry the streamSid or Twilio silently drops it.

    That is a classic 'call connects but caller hears nothing' cause, so it is
    asserted here rather than discovered on a phone.
    """
    with echo_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call"}))
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ123"}}))
        ws.send_text(_media(b"\xff" * 160))

        reply = json.loads(ws.receive_text())
        assert reply["event"] == "media"
        assert reply["streamSid"] == "MZ123"
        assert base64.b64decode(reply["media"]["payload"]) == b"\xff" * 160


def test_media_before_start_is_dropped_not_crashed(echo_client):
    """Audio can arrive before the start event; it must not kill the socket."""
    with echo_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(_media(b"\x00" * 160))  # no streamSid known yet
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ9"}}))
        ws.send_text(_media(b"\x7f" * 160))
        assert json.loads(ws.receive_text())["streamSid"] == "MZ9"


def test_stop_event_closes_cleanly(echo_client):
    with echo_client.websocket_connect("/ws/media-stream") as ws:
        ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1"}}))
        ws.send_text(json.dumps({"event": "stop"}))


# ---------------------------------------------------------------------------
# /call
# ---------------------------------------------------------------------------

def test_call_reports_twilio_failure_as_502(client, monkeypatch):
    """A Twilio rejection is an upstream failure, not a malformed request."""
    import app.channels.voice.main as voice

    def boom(_to):
        raise RuntimeError("unverified number")

    monkeypatch.setattr(voice, "trigger_call", boom)
    resp = client.post("/call", data={"to": "+15551234567"})
    assert resp.status_code == 502
    assert "unverified number" in resp.text


def test_call_returns_sid_on_success(client, monkeypatch):
    import app.channels.voice.main as voice

    monkeypatch.setattr(voice, "trigger_call", lambda to: "CA-test-sid")
    body = client.post("/call", data={"to": "+15551234567"}).json()
    assert body == {"status": "calling", "to": "+15551234567", "call_sid": "CA-test-sid"}


def test_call_requires_a_number(client):
    assert client.post("/call", data={}).status_code == 422


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_reports_stream_url_and_hides_secrets(client):
    body = client.get("/health").json()
    assert body["stream_url"].startswith("wss://")
    assert body["gemini_key"] is True  # boolean, never the key itself
    assert all(not isinstance(v, str) or "AIza" not in v for v in body.values())
