"""Gemini Live session wrapper -- the voice channel's model client.

Speech in, speech out, with no separate STT/TTS step.

This file contains ZERO persona- and domain-specific logic. It imports
SYSTEM_PROMPT from persona.py and the tool declarations/dispatch from tools.py,
and that is its only coupling to the problem domain. Anything use-case specific
that ends up here is in the wrong file.

Audio contract (do not guess these, they are the usual cause of a silent call):
  IN  -- raw PCM, 16-bit signed little-endian, 16 kHz, mono
  OUT -- raw PCM, 16-bit signed little-endian, 24 kHz, mono

Callers consume `events()`, which yields typed tuples rather than raw SDK
objects. That keeps the SDK's shape -- which moves between releases -- inside
this one file.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Literal

from google import genai
from google.genai import types

from app.config import settings
from app.core.persona import SYSTEM_PROMPT
from app.core.tools import TOOL_DECLARATIONS, execute_tool

log = logging.getLogger(__name__)

SEND_SAMPLE_RATE = 16_000
RECV_SAMPLE_RATE = 24_000

# What events() yields.
#   ("audio", bytes)        PCM16 @ 24 kHz to play to the listener
#   ("interrupted", None)   the person talked over the agent -- STOP PLAYING NOW
#   ("turn_complete", None) the agent finished speaking
#   ("transcript", str)     what the agent said, for logs
#   ("input_transcript", s) what the person said, for logs
Event = tuple[
    Literal["audio", "interrupted", "turn_complete", "transcript", "input_transcript"],
    bytes | str | None,
]


def _build_config() -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM_PROMPT,
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
        # Transcription is for us, not the caller. Native-audio models return
        # audio only, so without this a confusing call leaves no readable
        # record of what was actually said -- which is miserable to debug at 2am.
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )


class GeminiLiveSession:
    """One live conversation. Async context manager.

        async with GeminiLiveSession() as session:
            await session.send_audio(pcm16_16k)
            async for kind, payload in session.events():
                ...
    """

    def __init__(self, model: str | None = None) -> None:
        settings.require_gemini()
        self.model = model or settings.gemini_model
        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._cm = None
        self._session = None

    async def __aenter__(self) -> "GeminiLiveSession":
        log.info("Connecting to Gemini Live (model=%s)", self.model)
        self._cm = self._client.aio.live.connect(
            model=self.model, config=_build_config()
        )
        self._session = await self._cm.__aenter__()
        log.info("Gemini Live session open")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(exc_type, exc, tb)
            except Exception:  # noqa: BLE001
                # A failure tearing down a finished call is not worth
                # propagating over whatever actually ended the call.
                log.warning("Error closing Gemini session", exc_info=True)
        self._session = None
        self._cm = None

    async def send_audio(self, pcm16_16k: bytes) -> None:
        """Send one chunk of caller audio. PCM16 mono @ 16 kHz."""
        if self._session is None:
            raise RuntimeError("Session is not open")
        await self._session.send_realtime_input(
            audio=types.Blob(
                data=pcm16_16k, mime_type=f"audio/pcm;rate={SEND_SAMPLE_RATE}"
            )
        )

    async def send_text(self, text: str) -> None:
        """Inject a text turn. Used to make the agent speak first."""
        if self._session is None:
            raise RuntimeError("Session is not open")
        await self._session.send_realtime_input(text=text)

    async def events(self) -> AsyncIterator[Event]:
        """Yield events until the session ends.

        Tool calls are handled here rather than surfaced to the caller: the Live
        API does NOT execute tools for you, and every function_call must be
        answered with a FunctionResponse carrying the SAME id. Miss one and the
        model simply stops talking, waiting forever -- which presents as the
        agent going silent mid-call for no visible reason.
        """
        if self._session is None:
            raise RuntimeError("Session is not open")

        async for message in self._session.receive():
            content = message.server_content
            if content is not None:
                # Check interruption FIRST. Everything queued behind it is now
                # stale and must not be played.
                if content.interrupted:
                    log.info("Caller interrupted the agent")
                    yield ("interrupted", None)

                if content.input_transcription and content.input_transcription.text:
                    yield ("input_transcript", content.input_transcription.text)

                if content.output_transcription and content.output_transcription.text:
                    yield ("transcript", content.output_transcription.text)

                if content.model_turn is not None:
                    for part in content.model_turn.parts or []:
                        blob = part.inline_data
                        if blob is not None and blob.data:
                            yield ("audio", blob.data)

                if content.turn_complete:
                    yield ("turn_complete", None)

            if message.tool_call is not None:
                await self._handle_tool_call(message.tool_call)

    async def _handle_tool_call(self, tool_call: types.LiveServerToolCall) -> None:
        responses: list[types.FunctionResponse] = []
        for call in tool_call.function_calls or []:
            args = dict(call.args or {})
            log.info("Tool call: %s(%s)", call.name, args)
            result = await execute_tool(call.name, args)
            responses.append(
                types.FunctionResponse(
                    id=call.id, name=call.name, response=result
                )
            )
        if responses:
            await self._session.send_tool_response(function_responses=responses)
            log.info("Sent %d tool response(s)", len(responses))
