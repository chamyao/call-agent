"""Command line: `python -m call_agent serve` and `python -m call_agent call ...`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import httpx2 as httpx
from dotenv import load_dotenv


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=args.host, port=args.port)


def call(args: argparse.Namespace) -> None:
    token = os.environ.get("CALL_AGENT_TOKEN")
    if not token:
        sys.exit("Set CALL_AGENT_TOKEN (see .env.example)")
    task_path = Path(args.task)
    task = task_path.read_text() if task_path.is_file() else args.task
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=args.server, headers=headers, timeout=30) as client:
        r = client.post("/calls", json={"to": args.to, "task": task})
        if r.status_code >= 400:
            sys.exit(f"Couldn't place the call: {r.status_code} {r.text}")
        call_id = r.json()["call_id"]
        print(f"Calling {args.to} (call id {call_id})...")
        if args.no_wait:
            return

        last_status = None
        while True:
            time.sleep(3)
            info = client.get(f"/calls/{call_id}").json()
            if info["status"] != last_status:
                print(f"  status: {info['status']}")
                last_status = info["status"]
            if info["finished"]:
                print(f"\nOutcome: {info['outcome'] or 'unknown'}\n\n{info['summary']}")
                return


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="call_agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the server Twilio connects to")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=serve)

    p_call = sub.add_parser("call", help="place a call (the server must be running)")
    p_call.add_argument("--to", required=True, help="number to call, e.g. +18005551234")
    p_call.add_argument("--task", required=True, help="path to a task file, or the task text itself")
    p_call.add_argument("--server", default="http://127.0.0.1:8000")
    p_call.add_argument("--no-wait", action="store_true", help="don't wait for the call to finish")
    p_call.set_defaults(func=call)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
