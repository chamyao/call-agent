"""End-to-end tests of the server and call loop with a scripted fake model."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from call_agent import session as session_module
from call_agent.config import Settings
from call_agent.server import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_settings(tmp_path: Path, owner_phone: str | None = None) -> Settings:
    profile = tmp_path / "profile.md"
    profile.write_text("Name: Test Owner\nUPS tracking: 1Z999")
    return Settings(
        twilio_account_sid="AC123",
        twilio_auth_token="secret",
        twilio_from_number="+15550000000",
        public_url="https://example.ngrok.app",
        api_token=TOKEN,
        owner_name="Test Owner",
        owner_phone=owner_phone,
        model="test-model",
        effort="low",
        profile_path=profile,
        calls_dir=tmp_path / "calls",
        sms_summary=False,
        max_call_seconds=600,
        validate_twilio_signature=False,
    )


def tool_use(id_: str, name: str, **args):
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


class ScriptedBrain:
    """Replies with canned (text, blocks) turns and records what it was sent."""

    def __init__(self, turns, hang_first=False):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []
        self.hang_first = hang_first  # first turn starts talking, then never finishes

    async def respond(self, system, messages, tools, on_text):
        self.seen.append(copy.deepcopy(messages))
        if self.hang_first:
            self.hang_first = False
            await on_text("Let me explain at length ")
            await asyncio.Event().wait()  # stays here until cancelled
        text, blocks = self.turns.pop(0)
        content = []
        if text:
            for word in text.split(" "):
                await on_text(word + " ")
            content.append({"type": "text", "text": text})
        return content + blocks

    async def complete(self, prompt):
        return "SUMMARY: done"


class FakeCalls:
    def __init__(self, twilio):
        self.twilio = twilio

    def create(self, **kwargs):
        return self.twilio._create(**kwargs)

    def __call__(self, sid):
        return SimpleNamespace(update=lambda **kw: self.twilio.updated.append((sid, kw)))


class FakeTwilio:
    def __init__(self):
        self.created = []
        self.updated = []
        self.calls = FakeCalls(self)
        self.messages = SimpleNamespace(create=lambda **kw: None)

    def _create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(sid="CA_test")


@pytest.fixture(autouse=True)
def no_goodbye_delay(monkeypatch):
    monkeypatch.setattr(session_module, "speech_seconds", lambda text: 0)


def recv_until(ws, kind):
    """Read messages until one of the given type; return everything read."""
    got = []
    while True:
        msg = json.loads(ws.receive_text())
        got.append(msg)
        if msg["type"] == kind:
            return got


def start_call(client: TestClient, twilio: FakeTwilio) -> str:
    r = client.post("/calls", json={"to": "+18005551234", "task": "Reschedule my delivery"}, headers=AUTH)
    assert r.status_code == 200, r.text
    call_id = r.json()["call_id"]
    created = twilio.created[-1]
    assert created["to"] == "+18005551234"
    assert created["url"] == f"https://example.ngrok.app/twiml?call_id={call_id}"
    return call_id


def test_full_call(tmp_path):
    brain = ScriptedBrain(
        [
            ("", [tool_use("t1", "press_keys", digits="2")]),
            ("", [tool_use("t2", "stay_silent", reason="hold music")]),
            ("Hi, I'm an AI assistant calling for Test Owner.", []),
            ("Thanks, goodbye!", [tool_use("t3", "end_call", outcome="completed", reason="rescheduled")]),
        ]
    )
    twilio = FakeTwilio()
    app = create_app(make_settings(tmp_path), brain=brain, twilio=twilio)
    client = TestClient(app)
    call_id = start_call(client, twilio)

    twiml = client.post(f"/twiml?call_id={call_id}", data={"CallSid": "CA_test"})
    assert "<ConversationRelay" in twiml.text
    assert 'url="wss://example.ngrok.app/relay"' in twiml.text
    assert f'value="{call_id}"' in twiml.text

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA_test", "customParameters": {"call_id": call_id}}))

        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "For deliveries, press 2.", "last": True}))
        assert recv_until(ws, "sendDigits")[-1] == {"type": "sendDigits", "digits": "2"}

        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Please hold.", "last": True}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "This is Dana, how can I help?", "last": True}))
        spoken = recv_until(ws, "text")
        while not spoken[-1].get("last"):
            spoken += recv_until(ws, "text")
        said = "".join(m["token"] for m in spoken if m["type"] == "text")
        assert said.strip() == "Hi, I'm an AI assistant calling for Test Owner."

        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Done, confirmation 4471.", "last": True}))
        end = recv_until(ws, "end")[-1]
        assert json.loads(end["handoffData"]) == {"action": "hangup"}

    # The tool result for press_keys leads the next user turn.
    second_user = brain.seen[1][-1]
    assert second_user["role"] == "user"
    assert second_user["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "Pressed 2."}
    assert "[them] Please hold." in second_user["content"][-1]["text"]

    record = app.state.calls[call_id]
    assert record.outcome == "completed"
    assert record.summary == "SUMMARY: done"
    saved = list((tmp_path / "calls").glob("*.md"))
    assert len(saved) == 1 and "confirmation 4471" in saved[0].read_text()

    after = client.post(f"/after?call_id={call_id}", data={"HandoffData": end["handoffData"]})
    assert "<Hangup" in after.text


def test_interrupt_cancels_generation(tmp_path):
    brain = ScriptedBrain([("Okay.", [])], hang_first=True)
    twilio = FakeTwilio()
    app = create_app(make_settings(tmp_path), brain=brain, twilio=twilio)
    client = TestClient(app)
    call_id = start_call(client, twilio)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "customParameters": {"call_id": call_id}}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Hello?", "last": True}))
        first = json.loads(ws.receive_text())
        assert first["token"].startswith("Let me explain")
        ws.send_text(json.dumps({"type": "interrupt", "utteranceUntilInterrupt": "Let me"}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "Sorry, go ahead.", "last": True}))
        spoken = recv_until(ws, "text")
        assert "Okay." in "".join(m.get("token", "") for m in spoken)

    last_user = brain.seen[-1][-1]["content"][-1]["text"]
    assert "cut off" in last_user and 'They heard: "Let me"' in last_user
    # The cancelled turn left no assistant message behind.
    assert [m["role"] for m in brain.seen[-1]] == ["user", "user"]


def test_transfer_dials_owner(tmp_path):
    brain = ScriptedBrain(
        [("One moment, I'll connect Test Owner.", [tool_use("t1", "transfer_to_owner", reason="needs ID check")])]
    )
    twilio = FakeTwilio()
    app = create_app(make_settings(tmp_path, owner_phone="+15551112222"), brain=brain, twilio=twilio)
    client = TestClient(app)
    call_id = start_call(client, twilio)

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "customParameters": {"call_id": call_id}}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "I need to verify the account holder.", "last": True}))
        end = recv_until(ws, "end")[-1]

    after = client.post(f"/after?call_id={call_id}", data={"HandoffData": end["handoffData"]})
    assert "<Dial" in after.text and "+15551112222" in after.text
    assert f"/whisper?call_id={call_id}" in after.text and f"/dial-done?call_id={call_id}" in after.text

    briefing = client.post(f"/whisper?call_id={call_id}", data={})
    assert "needs ID check" in briefing.text and "8 0 0, 5 5 5, 1 2 3 4" in briefing.text

    answered = client.post(f"/dial-done?call_id={call_id}", data={"DialCallStatus": "completed"})
    assert "<Say" not in answered.text and "<Hangup" in answered.text
    missed = client.post(f"/dial-done?call_id={call_id}", data={"DialCallStatus": "no-answer"})
    assert "can't be reached right now" in missed.text

    assert "<Start><Transcription" in after.text and f"/handoff-transcript?call_id={call_id}" in after.text
    def say(track, text, final="true"):
        data = {"TranscriptionEvent": "transcription-content", "Track": track, "Final": final,
                "TranscriptionData": json.dumps({"transcript": text, "confidence": 0.9})}
        assert client.post(f"/handoff-transcript?call_id={call_id}", data=data).status_code == 204
    say("inbound_track", "Can I get the last four of your social?")
    say("outbound_track", "Sure, one two three four.")
    say("outbound_track", "The code is 742934 and my social is 123-45-6789.")
    say("outbound_track", "partial words", final="false")
    lines = client.get(f"/calls/{call_id}", headers=AUTH).json()["transcript"]
    assert ["them", "Can I get the last four of your social?"] in lines
    assert ["owner_on_phone", "Sure, [number withheld]."] in lines
    assert ["owner_on_phone", "The code is [number withheld] and my social is [number withheld]."] in lines
    assert not any("742934" in text or "6789" in text for _, text in lines)
    assert not any(text == "partial words" for _, text in lines)

    client.post(f"/status?call_id={call_id}", data={"CallStatus": "completed"})
    status = client.get(f"/calls/{call_id}", headers=AUTH).json()
    assert status["finished"] and status["outcome"] == "transfer_unanswered"


def test_no_transfer_tool_without_owner_phone(tmp_path):
    from call_agent.prompts import build_tools

    assert "transfer_to_owner" not in [t["name"] for t in build_tools(False)]
    assert "transfer_to_owner" in [t["name"] for t in build_tools(True)]


def test_auth_and_validation(tmp_path):
    settings = make_settings(tmp_path)
    client = TestClient(create_app(settings, brain=ScriptedBrain([]), twilio=FakeTwilio()))
    assert client.post("/calls", json={"to": "+18005551234", "task": "x"}).status_code == 401
    assert client.post("/calls", json={"to": "+18005551234", "task": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.post("/calls", json={"to": "800-555-1234", "task": "x"}, headers=AUTH).status_code == 400


def test_rejects_unsigned_twilio_webhooks(tmp_path):
    from dataclasses import replace

    settings = replace(make_settings(tmp_path), validate_twilio_signature=True)
    twilio = FakeTwilio()
    client = TestClient(create_app(settings, brain=ScriptedBrain([]), twilio=twilio))
    call_id = start_call(client, twilio)
    assert client.post(f"/twiml?call_id={call_id}", data={"CallSid": "CA_test"}).status_code == 403

    from twilio.request_validator import RequestValidator

    url = f"https://example.ngrok.app/twiml?call_id={call_id}"
    sig = RequestValidator("secret").compute_signature(url, {"CallSid": "CA_test"})
    ok = client.post(f"/twiml?call_id={call_id}", data={"CallSid": "CA_test"}, headers={"X-Twilio-Signature": sig})
    assert ok.status_code == 200


def test_unknown_call_id_is_rejected_on_websocket(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path), brain=ScriptedBrain([]), twilio=FakeTwilio()))
    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "customParameters": {"call_id": "nope"}}))
        with pytest.raises(Exception):
            ws.receive_text()


def test_rejects_unsigned_relay_websocket(tmp_path):
    from dataclasses import replace

    from starlette.websockets import WebSocketDisconnect
    from twilio.request_validator import RequestValidator

    settings = replace(make_settings(tmp_path), validate_twilio_signature=True)
    client = TestClient(create_app(settings, brain=ScriptedBrain([]), twilio=FakeTwilio()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/relay"):
            pass
    bad = {"X-Twilio-Signature": "forged"}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/relay", headers=bad):
            pass

    sig = RequestValidator("secret").compute_signature("wss://example.ngrok.app/relay", {})
    with client.websocket_connect("/relay", headers={"X-Twilio-Signature": sig}) as ws:
        ws.send_text(json.dumps({"type": "setup", "customParameters": {"call_id": "nope"}}))
        with pytest.raises(Exception):
            ws.receive_text()  # signed, but still closed for an unknown call


def test_twilio_refusal_is_reported(tmp_path):
    from twilio.base.exceptions import TwilioRestException

    class RefusingTwilio(FakeTwilio):
        def _create(self, **kwargs):
            raise TwilioRestException(400, "https://api.twilio.com", msg="Account not authorized to call +12055550100")

    client = TestClient(create_app(make_settings(tmp_path), brain=ScriptedBrain([]), twilio=RefusingTwilio()))
    r = client.post("/calls", json={"to": "+12055550100", "task": "x"}, headers=AUTH)
    assert r.status_code == 502
    assert "not authorized" in r.json()["detail"]


def test_agent_opens_when_nobody_speaks_after_pickup():
    from call_agent.session import CallRecord, CallSession

    async def scenario():
        sent = []

        async def send(msg):
            sent.append(msg)

        brain = ScriptedBrain([("Hi, I'm an assistant calling on behalf of Test Owner.", [])])
        record = CallRecord(call_id="c1", to="+18005551234", task="x")
        session = CallSession(record, brain, send, "Test Owner", "", False, opening_silence_seconds=0.05)
        await session.on_message({"type": "setup", "callSid": "CA_test"})
        runner = asyncio.create_task(session.run())
        for _ in range(100):
            if brain.seen:
                break
            await asyncio.sleep(0.01)
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
        return brain, record

    brain, record = asyncio.run(scenario())
    first_turn = brain.seen[0][0]["content"][-1]["text"]
    assert "nobody has said anything yet" in first_turn
    assert ("agent", "Hi, I'm an assistant calling on behalf of Test Owner.") in record.transcript


def test_tts_voice_is_configurable(tmp_path):
    from dataclasses import replace

    settings = replace(make_settings(tmp_path), tts_provider="Google", tts_voice="en-US-Neural2-F")
    twilio = FakeTwilio()
    client = TestClient(create_app(settings, brain=ScriptedBrain([]), twilio=twilio))
    call_id = start_call(client, twilio)
    twiml = client.post(f"/twiml?call_id={call_id}", data={"CallSid": "CA_test"}).text
    assert 'ttsProvider="Google"' in twiml and 'voice="en-US-Neural2-F"' in twiml


def test_owner_can_message_the_agent_mid_call(tmp_path):
    brain = ScriptedBrain([("", [tool_use("s1", "stay_silent")])])
    twilio = FakeTwilio()
    client = TestClient(create_app(make_settings(tmp_path), brain=brain, twilio=twilio))
    call_id = start_call(client, twilio)

    assert client.post(f"/calls/{call_id}/message", json={"text": "hi"}, headers=AUTH).status_code == 409

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "customParameters": {"call_id": call_id}}))
        r = client.post(f"/calls/{call_id}/message", json={"text": "A $10 fee is fine."}, headers=AUTH)
        assert r.status_code == 200
        for _ in range(200):
            if brain.seen:
                break
            time.sleep(0.01)

    assert "live message from the owner" in brain.seen[0][-1]["content"][-1]["text"]
    assert "A $10 fee is fine." in brain.seen[0][-1]["content"][-1]["text"]
    status = client.get(f"/calls/{call_id}", headers=AUTH).json()
    assert ["owner", "A $10 fee is fine."] in status["transcript"]
    assert client.post(f"/calls/{call_id}/message", json={"text": "x"}).status_code == 401


def test_owner_can_hand_a_transferred_call_back(tmp_path):
    brain = ScriptedBrain(
        [
            ("One moment, I'll connect Test Owner.", [tool_use("t1", "transfer_to_owner", reason="needs ID check")]),
            ("Thanks for holding, I'm back on the line.", [tool_use("s1", "stay_silent")]),
        ]
    )
    twilio = FakeTwilio()
    client = TestClient(create_app(make_settings(tmp_path, owner_phone="+15551112222"), brain=brain, twilio=twilio))
    call_id = start_call(client, twilio)

    assert client.post(f"/calls/{call_id}/handback", json={"note": "x"}, headers=AUTH).status_code == 409

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA_test", "customParameters": {"call_id": call_id}}))
        ws.send_text(json.dumps({"type": "prompt", "voicePrompt": "I need to verify the account holder.", "last": True}))
        recv_until(ws, "end")
    assert client.get(f"/calls/{call_id}", headers=AUTH).json()["outcome"] == "transferred_to_owner"

    r = client.post(f"/calls/{call_id}/handback", json={"note": "Verified. Get the order number."}, headers=AUTH)
    assert r.status_code == 200
    assert twilio.updated[-1] == ("CA_test", {"url": f"https://example.ngrok.app/resume?call_id={call_id}", "method": "POST"})
    client.post(f"/handoff-transcript?call_id={call_id}", data={
        "TranscriptionEvent": "transcription-content", "Track": "outbound_track", "Final": "true",
        "TranscriptionData": json.dumps({"transcript": "I'm verified, please continue with my assistant."})})
    resumed = client.post(f"/resume?call_id={call_id}", data={}).text
    assert "<ConversationRelay" in resumed
    assert f'<Stop><Transcription name="handoff-{call_id}"' in resumed
    assert "<Say" not in client.post(f"/dial-done?call_id={call_id}", data={"DialCallStatus": "canceled"}).text

    with client.websocket_connect("/relay") as ws:
        ws.send_text(json.dumps({"type": "setup", "callSid": "CA_test", "customParameters": {"call_id": call_id}}))
        tokens = []
        while not (tokens and tokens[-1].get("last")):
            tokens.append(json.loads(ws.receive_text()))
    resume_turn = brain.seen[1][-1]["content"][-1]["text"]
    assert "handed the call back" in resume_turn and "Verified. Get the order number." in resume_turn
    assert "owner_on_phone: I'm verified, please continue with my assistant." in resume_turn
    assert "back on the line" in "".join(m.get("token", "") for m in tokens)
    status = client.get(f"/calls/{call_id}", headers=AUTH).json()
    assert status["finished"] and status["outcome"] is None
