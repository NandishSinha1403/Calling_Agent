"""Config tests: the failure modes we care about are 'missing key' and
'BASE_URL in the wrong scheme', both of which otherwise show up as a broken
phone call rather than an error message."""

import pytest

from app.config import ConfigError, DEFAULT_MODEL, Settings


@pytest.fixture
def settings(monkeypatch):
    for key in (
        "GEMINI_API_KEY", "GEMINI_MODEL", "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER", "BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    return Settings()


def test_missing_key_names_itself(settings):
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        settings.require_gemini()


def test_model_falls_back_to_default(settings):
    assert settings.gemini_model == DEFAULT_MODEL


def test_model_override(settings, monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "some-other-model")
    assert settings.gemini_model == "some-other-model"


def test_base_url_becomes_wss_stream_url(settings, monkeypatch):
    monkeypatch.setenv("BASE_URL", "https://abc123.ngrok-free.app/")
    assert settings.websocket_url == "wss://abc123.ngrok-free.app/ws/media-stream"


def test_http_base_url_is_rejected(settings, monkeypatch):
    """Twilio will not accept a ws:// stream; catch it here, not on a live call."""
    monkeypatch.setenv("BASE_URL", "http://abc123.ngrok-free.app")
    with pytest.raises(ConfigError, match="https"):
        settings.websocket_url


def test_gemini_only_config_does_not_require_twilio(settings, monkeypatch):
    """The isolated test must run without any Twilio credentials."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    settings.require_gemini()
    with pytest.raises(ConfigError, match="TWILIO"):
        settings.require_twilio()
