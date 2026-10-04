"""HTTP + WebSocket server that Twilio talks to, and the API for placing calls."""

from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import json
import logging
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Dial, Start, Stop, VoiceResponse

from .brain import Brain, ClaudeBrain
from .config import Settings, load_settings
from .prompts import SUMMARY_PROMPT
from .session import CallRecord, CallSession

log = logging.getLogger(__name__)

E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
TERMINAL_STATUSES = {"completed", "busy", "no-answer", "failed", "canceled"}


class CallRequest(BaseModel):
    to: str
    task: str


class OwnerMessage(BaseModel):
    text: str


class HandbackRequest(BaseModel):
    note: str = ""


def create_app(
    settings: Settings | None = None,
    brain: Brain | None = None,
    twilio: Any | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    brain = brain or ClaudeBrain(settings.model, settings.effort)
    twilio = twilio or TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    validator = RequestValidator(settings.twilio_auth_token)
    calls: dict[str, CallRecord] = {}
    sessions: dict[str, CallSession] = {}  # live calls, for owner messages

    app = FastAPI(title="call-agent")
    app.state.calls = calls

    # --- helpers --------------------------------------------------------------

    def require_token(authorization: str | None) -> None:
        expected = f"Bearer {settings.api_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="bad or missing token")

    async def twilio_form(request: Request) -> dict[str, str]:
        """Parse a Twilio webhook and check that Twilio really sent it."""
        form = {k: str(v) for k, v in (await request.form()).items()}
        if settings.validate_twilio_signature:
            # Twilio signs the public URL it called, not our local one.
            url = settings.public_url + request.url.path
            if request.url.query:
                url += "?" + request.url.query
            signature = request.headers.get("X-Twilio-Signature", "")
            if not validator.validate(url, form, signature):
                raise HTTPException(status_code=403, detail="bad Twilio signature")
        return form

    def get_call(call_id: str) -> CallRecord:
        record = calls.get(call_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown call")
        return record

    def read_profile() -> str:
        try:
            return settings.profile_path.read_text()
        except FileNotFoundError:
            log.warning("No profile at %s; the agent only knows the task text", settings.profile_path)
            return ""

    async def finalize(record: CallRecord) -> None:
        if record.finalizing:
            return
        record.finalizing = True
        transcript = "\n".join(f"{who}: {text}" for who, text in record.transcript)
        if transcript:
            try:
                record.summary = await brain.complete(
                    SUMMARY_PROMPT.format(
                        owner_name=settings.owner_name, task=record.task, transcript=transcript
                    )
                )
            except Exception:  # keep the transcript even if the summary fails
                log.exception("Summary failed for %s", record.call_id)
        else:
            record.summary = f"The call didn't connect (status: {record.status})."
        path = save_call(settings.calls_dir, record, transcript)
        record.finalized = True
        log.info("Call %s finished: %s\n%s", record.call_id, path, record.summary)
        if settings.sms_summary and settings.owner_phone and record.summary:
            try:
                await asyncio.to_thread(
                    twilio.messages.create,
                    to=settings.owner_phone,
                    from_=settings.twilio_from_number,
                    body=f"Call to {record.to}: {record.summary}"[:1500],
                )
            except Exception:
                log.exception("Couldn't text the summary")

    # --- API for placing calls ------------------------------------------------

    @app.get("/health")
    async def health():
        return {"ok": True}


    @app.post("/calls")
    async def place_call(body: CallRequest, authorization: str | None = Header(default=None)):
        require_token(authorization)
        if not E164_RE.match(body.to):
            raise HTTPException(400, "Phone number must be in E.164 format, e.g. +18005551234")
        if not body.task.strip():
            raise HTTPException(400, "Task is empty")

        call_id = secrets.token_urlsafe(16)
        record = CallRecord(call_id=call_id, to=body.to, task=body.task)
        calls[call_id] = record
        base = settings.public_url
        try:
            call = await asyncio.to_thread(
                twilio.calls.create,
                to=body.to,
                from_=settings.twilio_from_number,
                url=f"{base}/twiml?call_id={call_id}",
                method="POST",
                status_callback=f"{base}/status?call_id={call_id}",
                status_callback_method="POST",
                status_callback_event=["completed"],
                time_limit=settings.max_call_seconds,
            )
        except TwilioRestException as e:
            calls.pop(call_id, None)
            raise HTTPException(502, f"Twilio refused the call: {e.msg}") from None
        record.call_sid = call.sid
        return {"call_id": call_id, "call_sid": call.sid}

    @app.post("/calls/{call_id}/message")
    async def owner_message(call_id: str, body: OwnerMessage, authorization: str | None = Header(default=None)):
        require_token(authorization)
        get_call(call_id)
        session = sessions.get(call_id)
        if session is None:
            raise HTTPException(409, "The call isn't connected to the assistant right now")
        if not body.text.strip():
            raise HTTPException(400, "Message is empty")
        await session.owner_message(body.text.strip())
        return {"ok": True}

    @app.post("/calls/{call_id}/handback")
    async def handback(call_id: str, body: HandbackRequest, authorization: str | None = Header(default=None)):
        """Take the call back from the owner after a transfer and reconnect it to the agent."""
        require_token(authorization)
        record = get_call(call_id)
        if record.outcome != "transferred_to_owner" or not record.call_sid:
            raise HTTPException(409, "This call isn't currently transferred to you")
        record.handback_note = body.note.strip()
        record.resuming = True
        try:
            await asyncio.to_thread(
                twilio.calls(record.call_sid).update,
                url=f"{settings.public_url}/resume?call_id={call_id}",
                method="POST",
            )
        except TwilioRestException as e:
            record.resuming = False
            raise HTTPException(502, f"Twilio couldn't hand the call back: {e.msg}") from None
        record.transcript.append(("note", "owner handed the call back to the agent"))
        return {"ok": True}

    @app.get("/calls/{call_id}")
    async def call_status(call_id: str, authorization: str | None = Header(default=None)):
        require_token(authorization)
        record = get_call(call_id)
        return {
            "call_id": record.call_id,
            "to": record.to,
            "status": record.status,
            "outcome": record.outcome,
            "finished": record.finalized,
            "summary": record.summary,
            "transcript": record.transcript,
        }

    # --- Twilio webhooks ------------------------------------------------------

    def handoff_transcription_name(call_id: str) -> str:
        return f"handoff-{call_id}"

    def relay_twiml(call_id: str, stop_handoff_transcription: bool = False) -> Response:
        response = VoiceResponse()
        if stop_handoff_transcription:
            stop = Stop()
            stop.transcription(name=handoff_transcription_name(call_id))
            response.append(stop)
        connect = Connect(action=f"{settings.public_url}/after?call_id={call_id}", method="POST")
        voice = {"tts_provider": settings.tts_provider, "voice": settings.tts_voice}
        relay = connect.conversation_relay(url=settings.ws_url, **{k: v for k, v in voice.items() if v})
        relay.parameter(name="call_id", value=call_id)
        response.append(connect)
        return Response(str(response), media_type="application/xml")

    @app.post("/twiml")
    async def twiml(request: Request, call_id: str):
        await twilio_form(request)
        get_call(call_id)
        return relay_twiml(call_id)

    @app.post("/resume")
    async def resume(request: Request, call_id: str):
        """Twilio fetches this after a hand-back; it reconnects the call to the agent."""
        await twilio_form(request)
        get_call(call_id)
        return relay_twiml(call_id, stop_handoff_transcription=True)

    @app.post("/after")
    async def after_relay(request: Request, call_id: str):
        """Twilio calls this when the AI session ends; decide whether to transfer."""
        form = await twilio_form(request)
        get_call(call_id)
        try:
            handoff = json.loads(form.get("HandoffData") or "{}")
        except json.JSONDecodeError:
            handoff = {}
        response = VoiceResponse()
        if handoff.get("action") == "transfer" and settings.owner_phone:
            base = settings.public_url
            # Transcribe the owner's conversation too, so it shows up live and the
            # agent has it if the call is handed back. The rep was told at the start
            # that the call is transcribed.
            start = Start()
            start.transcription(
                name=handoff_transcription_name(call_id),
                track="both_tracks",
                status_callback_url=f"{base}/handoff-transcript?call_id={call_id}",
                status_callback_method="POST",
                partial_results=False,
            )
            response.append(start)
            dial = Dial(
                caller_id=settings.twilio_from_number,
                timeout=25,
                action=f"{base}/dial-done?call_id={call_id}",
                method="POST",
            )
            # The owner hears a one-line briefing before being connected.
            dial.number(settings.owner_phone, url=f"{base}/whisper?call_id={call_id}", method="POST")
            response.append(dial)
        else:
            response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/whisper")
    async def whisper(request: Request, call_id: str):
        """Played to the owner when they pick up a transfer, before the call connects."""
        await twilio_form(request)
        record = get_call(call_id)
        reason = (record.outcome_reason or "they need you on the line").strip().rstrip(".")
        response = VoiceResponse()
        response.say(f"Transfer from your call assistant, on the call to {_spoken_number(record.to)}: {reason}. Connecting you now.")
        return Response(str(response), media_type="application/xml")

    @app.post("/handoff-transcript")
    async def handoff_transcript(request: Request, call_id: str):
        """Live transcription of the owner's part of the call, after a transfer."""
        form = await twilio_form(request)
        record = get_call(call_id)
        if form.get("TranscriptionEvent") == "transcription-content" and form.get("Final", "true") == "true":
            try:
                text = (json.loads(form.get("TranscriptionData") or "{}").get("transcript") or "").strip()
            except json.JSONDecodeError:
                text = ""
            if text:
                # On the call to the company, inbound audio is their side and outbound is the owner's.
                who = "them" if form.get("Track") == "inbound_track" else "owner_on_phone"
                record.transcript.append((who, text))
        return Response(status_code=204)

    @app.post("/dial-done")
    async def dial_done(request: Request, call_id: str):
        """After the transfer leg ends; if the owner never picked up, tell the other side."""
        form = await twilio_form(request)
        record = get_call(call_id)
        response = VoiceResponse()
        if record.handback_note is not None:
            return Response(str(response), media_type="application/xml")  # call was redirected back to the agent
        if form.get("DialCallStatus") in {"no-answer", "busy", "failed", "canceled"}:
            record.transcript.append(("note", f"owner didn't answer the transfer ({form.get('DialCallStatus')})"))
            record.outcome = "transfer_unanswered"
            response.say(
                f"Sorry, {settings.owner_name_spoken or settings.owner_name} can't be reached right now. "
                "They'll follow up with you. Thank you for your patience. Goodbye."
            )
        response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/status")
    async def status_callback(request: Request, call_id: str):
        form = await twilio_form(request)
        record = get_call(call_id)
        record.status = form.get("CallStatus", record.status)
        if record.status in TERMINAL_STATUSES and not record.connected:
            await finalize(record)  # never reached the AI session
        elif record.status in TERMINAL_STATUSES and record.outcome in {"transferred_to_owner", "transfer_unanswered"}:
            # Rewrite the summary and log to include the owner's part of the call.
            record.finalizing = record.finalized = False
            await finalize(record)
        return Response(status_code=204)

    @app.websocket("/relay")
    async def relay(ws: WebSocket):
        if settings.validate_twilio_signature:
            # Twilio signs the WebSocket handshake with the wss:// URL from the TwiML.
            url = settings.ws_url
            if ws.url.query:
                url += "?" + ws.url.query
            signature = ws.headers.get("X-Twilio-Signature", "")
            if not validator.validate(url, {}, signature):
                await ws.close(code=1008)
                return
        await ws.accept()
        setup = json.loads(await ws.receive_text())
        call_id = (setup.get("customParameters") or {}).get("call_id", "")
        record = calls.get(call_id)
        if setup.get("type") != "setup" or record is None or (record.connected and not record.resuming):
            await ws.close(code=1008)
            return
        resuming = record.resuming
        if resuming:
            # A hand-back: this is a second session on the same call.
            record.resuming = False
            record.outcome = record.outcome_reason = None
            record.finalizing = record.finalized = False
        record.connected = True
        record.status = "in-progress"

        async def send(message: dict) -> None:
            await ws.send_text(json.dumps(message))

        session = CallSession(
            record,
            brain,
            send,
            owner_name=settings.owner_name,
            profile=read_profile(),
            can_transfer=bool(settings.owner_phone),
            spoken_name=settings.owner_name_spoken,
            opening_silence_seconds=0 if resuming else 4.0,
        )
        sessions[call_id] = session
        await session.on_message(setup)
        if resuming:
            await session.resume_after_handback(record.handback_note or "")
        runner = asyncio.create_task(session.run())
        try:
            while True:
                await session.on_message(json.loads(await ws.receive_text()))
        except WebSocketDisconnect:
            pass
        finally:
            sessions.pop(call_id, None)
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Session for %s crashed", call_id)
            await finalize(record)

    return app


def save_call(calls_dir: Path, record: CallRecord, transcript: str) -> Path:
    calls_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.fromtimestamp(record.created_at).strftime("%Y-%m-%d_%H%M%S")
    path = calls_dir / f"{stamp}_{record.call_id[:8]}.md"
    path.write_text(
        f"# Call to {record.to}\n\n"
        f"- Started: {dt.datetime.fromtimestamp(record.created_at):%Y-%m-%d %H:%M}\n"
        f"- Twilio call: {record.call_sid}\n"
        f"- Status: {record.status}\n"
        f"- Outcome: {record.outcome or 'unknown'} {('- ' + record.outcome_reason) if record.outcome_reason else ''}\n\n"
        f"## Task\n\n{record.task.strip()}\n\n"
        f"## Summary\n\n{record.summary or '(none)'}\n\n"
        f"## Transcript\n\n```\n{transcript or '(empty)'}\n```\n"
    )
    return path


def _spoken_number(number: str) -> str:
    """+18007425877 -> "8 0 0, 7 4 2, 5 8 7 7" so text-to-speech reads digits."""
    digits = number[2:] if number.startswith("+1") and len(number) == 12 else number.lstrip("+")
    groups = [digits[:3], digits[3:6], digits[6:]] if len(digits) == 10 else [digits]
    return ", ".join(" ".join(g) for g in groups)
