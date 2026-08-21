"""Top-level FastAPI app. Mounts one router per channel.

Both channels share this app, this port, and this ngrok tunnel -- which is a
large part of why they live in one repo. Two servers and two tunnels to keep
alive during a demo is a worse trade than a slightly bigger repo.

    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.voice.main import router as voice_router
from app.config import ConfigError, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("Calling Agent starting")
    try:
        log.info("Media Stream URL: %s", settings.websocket_url)
    except ConfigError:
        log.warning(
            "BASE_URL is not set. /twiml will fail until it is. "
            "Start ngrok, then put the https URL in .env"
        )
    yield


app = FastAPI(
    title="Calling Agent",
    description="Multi-channel AI agent scaffold",
    lifespan=lifespan,
)

app.include_router(voice_router)
# The WhatsApp router mounts here in Phase B, after voice is fully working.


@app.get("/health")
async def health() -> dict[str, object]:
    """Config visibility without exposing secrets.

    Deliberately reports what is configured rather than 'ok'. The single most
    common failure on this project is a stale BASE_URL after an ngrok restart,
    and this makes that visible in one request instead of a failed phone call.
    """
    try:
        ws_url = settings.websocket_url
    except ConfigError as exc:
        ws_url = f"NOT CONFIGURED: {exc.args[0].splitlines()[0]}"

    def configured(name: str) -> bool:
        try:
            return bool(getattr(settings, name))
        except ConfigError:
            return False

    return {
        "status": "ok",
        "live_model": settings.gemini_model,
        "text_model": settings.gemini_text_model,
        "gemini_key": configured("gemini_api_key"),
        "twilio": configured("twilio_account_sid"),
        "from_number": configured("twilio_phone_number"),
        "stream_url": ws_url,
    }
