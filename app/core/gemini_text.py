"""Text-in / text-out Gemini client -- the WhatsApp channel's model client.

Why this exists separately from gemini_live.py, since it looks like duplication:
the Live client CANNOT be reused for text. It runs a persistent streaming
session with response_modalities=["AUDIO"], and native-audio models restrict
responses to audio -- there is no flag that makes it return a sentence for a
chat message. Voice and text are genuinely different API surfaces.

What keeps them from drifting is that both import the SAME persona and the SAME
tools, and both dispatch through the SAME execute_tool(). The Live API and
generateContent accept the same OpenAPI-subset function declarations, so one
declaration in core/tools.py feeds both.

This file contains ZERO persona- or domain-specific logic.

Note on automatic function calling: the SDK can execute Python callables for you,
but we drive the loop manually so that both channels take the identical path
through TOOL_HANDLERS. One tool code path is worth more than a few saved lines.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types

from app.config import settings
from app.core.persona import system_prompt
from app.core.tools import TOOL_DECLARATIONS, execute_tool

log = logging.getLogger(__name__)

# A model that keeps calling tools without ever speaking would loop forever.
# Real conversations need one or two rounds; this is a safety net, not a limit
# anyone should hit.
MAX_TOOL_ROUNDS = 5


def _config(channel: str = "whatsapp") -> types.GenerateContentConfig:
    return types.GenerateContentConfig(
        system_instruction=system_prompt(channel),
        tools=[{"function_declarations": TOOL_DECLARATIONS}],
        # The SDK would otherwise execute tools itself, bypassing our dispatch
        # and giving the two channels different tool paths.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


class GeminiTextSession:
    """Stateless client over a caller-supplied history.

    History lives in the channel's session store rather than here, because the
    WhatsApp channel needs it keyed per sender and persisted across separate
    webhook requests -- there is no long-lived object per conversation the way a
    phone call has one.
    """

    def __init__(self, model: str | None = None) -> None:
        settings.require_gemini()
        self.model = model or settings.gemini_text_model
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def reply(
        self, history: list[types.Content], message: str
    ) -> tuple[str, list[types.Content]]:
        """Answer `message` given `history`. Returns (reply_text, new_history).

        The new history includes this exchange and any tool traffic, and is what
        the caller should store for the next turn.
        """
        contents = list(history)
        contents.append(
            types.Content(role="user", parts=[types.Part(text=message)])
        )

        for round_number in range(MAX_TOOL_ROUNDS):
            response = await self._client.aio.models.generate_content(
                model=self.model, contents=contents, config=_config()
            )

            candidate = (response.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                log.warning("Model returned no candidate")
                return ("Sorry, I didn't catch that. Could you say it again?", contents)

            contents.append(candidate.content)

            calls = [
                part.function_call
                for part in (candidate.content.parts or [])
                if part.function_call is not None
            ]
            if not calls:
                text = (response.text or "").strip()
                if not text:
                    text = "Sorry, I didn't catch that. Could you say it again?"
                return (text, contents)

            # Same dispatch the voice channel uses. Every call needs a response
            # or the model has nothing to continue from.
            responses = []
            for call in calls:
                args = dict(call.args or {})
                log.info("Tool call: %s(%s)", call.name, args)
                result = await execute_tool(call.name, args)
                responses.append(
                    types.Part.from_function_response(
                        name=call.name, response=result
                    )
                )
            contents.append(types.Content(role="user", parts=responses))
            log.info("Answered %d tool call(s), round %d", len(responses), round_number + 1)

        log.error("Hit MAX_TOOL_ROUNDS without a spoken reply")
        return ("Sorry, something went wrong on my end. Please try again.", contents)
