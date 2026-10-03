"""Settings, read from environment variables (or a .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    public_url: str  # e.g. https://abc123.ngrok.app (no trailing slash)
    api_token: str  # shared secret for placing calls through the server
    owner_name: str
    owner_phone: str | None  # where to transfer calls / text summaries
    model: str
    effort: str
    profile_path: Path
    calls_dir: Path
    sms_summary: bool
    max_call_seconds: int
    # Always on in production; only tests turn it off.
    validate_twilio_signature: bool = True

    @property
    def ws_url(self) -> str:
        return "wss://" + self.public_url.split("://", 1)[-1] + "/relay"


def load_settings() -> Settings:
    load_dotenv()

    def required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise SystemExit(f"Missing required environment variable {name} (see .env.example)")
        return value

    return Settings(
        twilio_account_sid=required("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=required("TWILIO_AUTH_TOKEN"),
        twilio_from_number=required("TWILIO_FROM_NUMBER"),
        public_url=required("PUBLIC_URL").rstrip("/"),
        api_token=required("CALL_AGENT_TOKEN"),
        owner_name=required("OWNER_NAME"),
        owner_phone=os.environ.get("OWNER_PHONE", "").strip() or None,
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        effort=os.environ.get("CLAUDE_EFFORT", "low"),
        profile_path=Path(os.environ.get("PROFILE_PATH", "profile.md")),
        calls_dir=Path(os.environ.get("CALLS_DIR", "calls")),
        sms_summary=_bool(os.environ.get("SMS_SUMMARY")),
        max_call_seconds=int(os.environ.get("MAX_CALL_SECONDS", "5400")),
    )
