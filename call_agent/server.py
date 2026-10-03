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
from twilio.twiml.voice_response import Connect, VoiceResponse

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

    @app.post("/twiml")
    async def twiml(request: Request, call_id: str):
        await twilio_form(request)
        get_call(call_id)
        response = VoiceResponse()
        connect = Connect(action=f"{settings.public_url}/after?call_id={call_id}", method="POST")
        relay = connect.conversation_relay(url=settings.ws_url)
        relay.parameter(name="call_id", value=call_id)
        response.append(connect)
        return Response(str(response), media_type="application/xml")

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
            response.dial(settings.owner_phone, caller_id=settings.twilio_from_number)
        else:
            response.hangup()
        return Response(str(response), media_type="application/xml")

    @app.post("/status")
    async def status_callback(request: Request, call_id: str):
        form = await twilio_form(request)
        record = get_call(call_id)
        record.status = form.get("CallStatus", record.status)
        if record.status in TERMINAL_STATUSES and not record.connected:
            await finalize(record)  # never reached the AI session
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
        if setup.get("type") != "setup" or record is None or record.connected:
            await ws.close(code=1008)
            return
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
        )
        await session.on_message(setup)
        runner = asyncio.create_task(session.run())
        try:
            while True:
                await session.on_message(json.loads(await ws.receive_text()))
        except WebSocketDisconnect:
            pass
        finally:
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
