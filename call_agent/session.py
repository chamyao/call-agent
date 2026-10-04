"""One live phone call: Twilio ConversationRelay events in, speech and keypresses out.

ConversationRelay does the speech-to-text and text-to-speech. Over its
WebSocket we receive what the other side said as text ("prompt" events) and
send back text to speak, digits to press, or "end" to leave the call.

The conversation history is append-only: interruptions and tool results are
reported to the model as notes in the next user turn instead of editing
earlier turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .brain import Brain
from .prompts import build_system_prompt, build_tools

log = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]

DIGITS_RE = re.compile(r"^[0-9*#wW]{1,64}$")
# Rough text-to-speech pace, used to let a goodbye finish before hanging up.
TTS_CHARS_PER_SECOND = 14.0


def speech_seconds(text: str) -> float:
    return 1.0 + len(text) / TTS_CHARS_PER_SECOND


@dataclass
class CallRecord:
    call_id: str
    to: str
    task: str
    created_at: float = field(default_factory=time.time)
    call_sid: str | None = None
    status: str = "queued"
    connected: bool = False
    outcome: str | None = None
    outcome_reason: str | None = None
    transcript: list[tuple[str, str]] = field(default_factory=list)
    summary: str | None = None
    finalizing: bool = False  # summary being written; guards against running twice
    finalized: bool = False  # summary and call log are done


def _get(block: Any, key: str) -> Any:
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


class CallSession:
    def __init__(
        self,
        record: CallRecord,
        brain: Brain,
        send: Send,
        owner_name: str,
        profile: str,
        can_transfer: bool,
        spoken_name: str | None = None,
        opening_silence_seconds: float = 4.0,
    ):
        self.record = record
        # If nobody speaks this long after pickup, open the conversation ourselves.
        self.opening_silence_seconds = opening_silence_seconds
        self.brain = brain
        self.send = send
        self.can_transfer = can_transfer
        self.system = build_system_prompt(owner_name, record.task, profile, can_transfer, spoken_name)
        self.tools = build_tools(can_transfer)
        self.messages: list[dict] = []
        self.pending_results: list[dict] = []
        # Only finished utterances from the other side start a model turn;
        # interruptions and keypad tones ride along with the next one as notes.
        self.events: asyncio.Queue[str] = asyncio.Queue()
        self.notes: list[str] = []
        self.turn: asyncio.Task | None = None
        self.streaming = False  # True only while the model is generating
        self.ending = False
        self.done = asyncio.Event()

    # --- events from Twilio -------------------------------------------------

    async def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "setup":
            self.record.call_sid = msg.get("callSid") or self.record.call_sid
        elif kind == "prompt":
            text = (msg.get("voicePrompt") or "").strip()
            if text:
                self.record.transcript.append(("them", text))
                await self.events.put(text)
        elif kind == "dtmf":
            self.notes.append(f"[keypad tone from their side: {msg.get('digit', '')}]")
        elif kind == "interrupt":
            if self.ending:
                return
            heard = (msg.get("utteranceUntilInterrupt") or "").strip()
            # Stop generating so we don't keep talking over them. Once the turn's
            # output is complete it is in the history, so leave it alone.
            if self.streaming and self.turn and not self.turn.done():
                self.turn.cancel()
            self.record.transcript.append(("note", f"agent interrupted; they heard: {heard!r}"))
            heard_note = f' They heard: "{heard}"' if heard else ""
            self.notes.append(f"[note: they spoke over you and your last reply was cut off.{heard_note}]")
        elif kind == "error":
            log.error("ConversationRelay error: %s", msg.get("description"))
        else:
            log.debug("Ignoring ConversationRelay message: %s", msg)

    # --- main loop ----------------------------------------------------------

    async def run(self) -> None:
        try:
            first = True
            while not self.ending:
                if first and self.opening_silence_seconds:
                    first = False
                    try:
                        batch = [await asyncio.wait_for(self.events.get(), self.opening_silence_seconds)]
                    except asyncio.TimeoutError:
                        batch = []
                        self.record.transcript.append(("note", "silence after pickup; agent opens"))
                        self.notes.append(
                            f"[note: the call connected {self.opening_silence_seconds:g} seconds ago "
                            "and nobody has said anything yet. Give your opening line now.]"
                        )
                else:
                    batch = [await self.events.get()]
                while not self.events.empty():
                    batch.append(self.events.get_nowait())
                self._append_user_turn(batch)

                self.turn = asyncio.create_task(self._take_turn())
                try:
                    await self.turn
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise  # the session itself is being shut down
                    # Interrupted by the other side; the note goes out with their next words.
        finally:
            self.done.set()

    def _append_user_turn(self, batch: list[str]) -> None:
        lines = self.notes + [f"[them] {text}" for text in batch]
        self.notes = []
        content = self.pending_results + [{"type": "text", "text": "\n".join(lines)}]
        self.pending_results = []
        self.messages.append({"role": "user", "content": content})

    async def _take_turn(self) -> None:
        spoken: list[str] = []

        async def on_text(delta: str) -> None:
            spoken.append(delta)
            await self.send({"type": "text", "token": delta, "last": False})

        self.streaming = True
        try:
            content = await self.brain.respond(self.system, self.messages, self.tools, on_text)
        finally:
            self.streaming = False
        if spoken:
            await self.send({"type": "text", "token": "", "last": True})

        text = "".join(spoken).strip()
        if text:
            self.record.transcript.append(("agent", text))

        tool_uses = [b for b in content if _get(b, "type") == "tool_use"]
        if text or tool_uses:
            self.messages.append({"role": "assistant", "content": content})

        for tool_use in tool_uses:
            name, args = _get(tool_use, "name"), _get(tool_use, "input") or {}
            result = await self._run_tool(name, args, spoken_text=text)
            self.pending_results.append(
                {"type": "tool_result", "tool_use_id": _get(tool_use, "id"), "content": result}
            )

    # --- tools --------------------------------------------------------------

    async def _run_tool(self, name: str, args: dict, spoken_text: str) -> str:
        if name == "press_keys":
            digits = str(args.get("digits", ""))
            if not DIGITS_RE.match(digits):
                return f"Not pressed: {digits!r} is not valid. Use 0-9, *, # and w."
            await self.send({"type": "sendDigits", "digits": digits})
            self.record.transcript.append(("agent", f"[pressed {digits}]"))
            return f"Pressed {digits}."
        if name == "stay_silent":
            return "Staying silent."
        if name == "end_call":
            self.record.outcome = args.get("outcome")
            self.record.outcome_reason = args.get("reason")
            await self._leave({"action": "hangup"}, spoken_text)
            return "Call ended."
        if name == "transfer_to_owner" and self.can_transfer:
            self.record.outcome = "transferred_to_owner"
            self.record.outcome_reason = args.get("reason")
            self.record.transcript.append(("note", "transferring to owner"))
            await self._leave({"action": "transfer", "reason": args.get("reason", "")}, spoken_text)
            return "Transferring."
        return f"Unknown tool {name}."

    async def _leave(self, handoff: dict, spoken_text: str) -> None:
        self.ending = True
        # ConversationRelay doesn't tell us when speech finishes playing, so wait
        # roughly as long as the goodbye takes to say before leaving.
        await asyncio.sleep(speech_seconds(spoken_text))
        await self.send({"type": "end", "handoffData": json.dumps(handoff)})
