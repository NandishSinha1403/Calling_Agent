"""Configuration loaded from the environment.

Fails loudly and by name. A missing key raises here, at startup, naming the key
and where to get it -- rather than surfacing three layers down as a `None` in
the middle of a live call, which is not what you want to debug at 2am.

Validation is *grouped*, not global. The isolated Gemini test needs only a
Gemini key; requiring Twilio credentials to run it would defeat the point of
being able to test the two halves of the system separately.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Live API model. See .env.example for known-good alternatives -- these IDs get
# versioned and rotated, so it is an env var rather than a constant in code.
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"

# Text model for the WhatsApp channel. Deliberately a *stable* release rather
# than a preview: unlike the voice path, nothing here needs a preview-only
# feature, so there is no reason to accept preview churn.
DEFAULT_TEXT_MODEL = "gemini-3.7-flash"

# Twilio's shared WhatsApp Sandbox number. Same for every sandbox user.
DEFAULT_WHATSAPP_NUMBER = "whatsapp:+14155238886"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


def _require(name: str, hint: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable: {name}\n"
            f"  -> {hint}\n"
            f"  Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Settings:
    """Reads the environment lazily so partial configs still work."""

    @property
    def gemini_api_key(self) -> str:
        return _require(
            "GEMINI_API_KEY",
            "Create one at https://aistudio.google.com/apikey",
        )

    @property
    def gemini_model(self) -> str:
        return os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL

    @property
    def gemini_text_model(self) -> str:
        return os.getenv("GEMINI_TEXT_MODEL", "").strip() or DEFAULT_TEXT_MODEL

    @property
    def twilio_whatsapp_number(self) -> str:
        """Sender for WhatsApp replies, in Twilio's `whatsapp:+E164` form."""
        value = os.getenv("TWILIO_WHATSAPP_NUMBER", "").strip()
        if not value:
            return DEFAULT_WHATSAPP_NUMBER
        # A bare number here is a common slip and fails at send time with an
        # unhelpful Twilio error, so normalise it now.
        return value if value.startswith("whatsapp:") else f"whatsapp:{value}"

    @property
    def twilio_account_sid(self) -> str:
        return _require(
            "TWILIO_ACCOUNT_SID",
            "Twilio Console dashboard -> Account Info",
        )

    @property
    def twilio_auth_token(self) -> str:
        return _require(
            "TWILIO_AUTH_TOKEN",
            "Twilio Console dashboard -> Account Info (click to reveal)",
        )

    @property
    def twilio_phone_number(self) -> str:
        return _require(
            "TWILIO_PHONE_NUMBER",
            "Your Twilio number in E.164 format, e.g. +15551234567",
        )

    @property
    def base_url(self) -> str:
        """Public HTTPS URL of this server, no trailing slash."""
        value = _require(
            "BASE_URL",
            "Your current ngrok HTTPS URL, e.g. https://abc123.ngrok-free.app",
        )
        return value.rstrip("/")

    @property
    def websocket_url(self) -> str:
        """The wss:// Media Stream URL for TwiML's <Stream url=...>.

        Twilio requires an absolute wss:// URL here. A relative path or an
        https:// URL fails -- sometimes loudly, sometimes silently -- so the
        scheme swap happens in exactly one place instead of at each call site.
        """
        url = self.base_url
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):] + "/ws/media-stream"
        if url.startswith("http://"):
            # ngrok always offers https; http here is almost certainly a typo,
            # and Twilio will reject a ws:// stream URL anyway.
            raise ConfigError(
                f"BASE_URL must be an https:// URL, got: {url}\n"
                "  Twilio requires a secure wss:// Media Stream endpoint."
            )
        raise ConfigError(
            f"BASE_URL must start with https://, got: {url}"
        )

    # --- Telnyx: alternative voice provider ---------------------------
    # Kept alongside Twilio rather than replacing it, so the two can be
    # compared and either can be used without a code change.

    @property
    def telnyx_api_key(self) -> str:
        return _require(
            "TELNYX_API_KEY",
            "Telnyx Console -> Account -> API Keys (starts KEY...)",
        )

    @property
    def telnyx_connection_id(self) -> str:
        return _require(
            "TELNYX_CONNECTION_ID",
            "Telnyx Console -> Voice -> Applications -> your app's Connection ID",
        )

    @property
    def telnyx_phone_number(self) -> str:
        return _require(
            "TELNYX_PHONE_NUMBER",
            "A Voice-capable Telnyx number in E.164 format, e.g. +15551234567",
        )

    def require_telnyx(self) -> None:
        """Fail now if anything needed for a Telnyx call is missing."""
        _ = (
            self.telnyx_api_key,
            self.telnyx_connection_id,
            self.telnyx_phone_number,
            self.websocket_url,
        )

    def require_gemini(self) -> None:
        """Fail now if the Gemini config is incomplete."""
        _ = self.gemini_api_key

    def require_twilio(self) -> None:
        """Fail now if anything needed to place a real call is missing."""
        _ = (
            self.twilio_account_sid,
            self.twilio_auth_token,
            self.twilio_phone_number,
            self.websocket_url,
        )

    def require_whatsapp(self) -> None:
        """Fail now if anything needed to answer a WhatsApp message is missing.

        Separate from require_twilio() even though both use the same Twilio
        account: a missing TWILIO_PHONE_NUMBER should not stop the WhatsApp
        channel, and a missing WhatsApp config should not stop a voice call.
        """
        _ = (
            self.gemini_api_key,
            self.twilio_account_sid,
            self.twilio_auth_token,
            self.twilio_whatsapp_number,
        )


settings = Settings()
