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


class FakeTwilio:
    def __init__(self):
        self.created = []
        self.calls = SimpleNamespace(create=self._create)
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
