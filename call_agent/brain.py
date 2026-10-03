"""The Claude side of the call: one streamed model turn at a time."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Protocol

import anthropic

log = logging.getLogger(__name__)

OnText = Callable[[str], Awaitable[None]]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Brain(Protocol):
    async def respond(
        self, system: str, messages: list[dict], tools: list[dict], on_text: OnText
    ) -> list[Any]:
        """Run one model turn, calling on_text for each text delta as it streams.

        Returns the assistant content blocks, ready to append to the history.
        """

    async def complete(self, prompt: str) -> str:
        """One-shot text completion (used for the post-call summary)."""


class ClaudeBrain:
    def __init__(self, model: str, effort: str, client: anthropic.AsyncAnthropic | None = None):
        self.model = model
        self.effort = effort
        self.client = client or anthropic.AsyncAnthropic()

    async def respond(self, system, messages, tools, on_text):
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=16000,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            tools=tools,
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            async for event in stream:
                if event.type == "text":
                    await on_text(event.text)
            final = await stream.get_final_message()

        if final.stop_reason == "refusal":
            log.warning("Model refused this turn: %s", final.stop_details)
            return []
        return list(final.content)

    async def complete(self, prompt):
        async with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": self.effort},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            final = await stream.get_final_message()
        return "".join(b.text for b in final.content if b.type == "text").strip()
