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


def test_text_model_defaults_to_stable(settings):
    from app.config import DEFAULT_TEXT_MODEL
    assert settings.gemini_text_model == DEFAULT_TEXT_MODEL


def test_bare_whatsapp_number_is_normalised(settings, monkeypatch):
    """A bare +E164 here fails at send time with an unhelpful Twilio error."""
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "+14155238886")
    assert settings.twilio_whatsapp_number == "whatsapp:+14155238886"


def test_whatsapp_config_is_independent_of_voice(settings, monkeypatch):
    """A missing voice number must not block WhatsApp, or vice versa."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC-fake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "fake-token")
    settings.require_whatsapp()          # no TWILIO_PHONE_NUMBER, no BASE_URL
    with pytest.raises(ConfigError, match="TWILIO_PHONE_NUMBER"):
        settings.require_twilio()
