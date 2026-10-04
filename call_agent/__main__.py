"""Command line: `python -m call_agent serve` and `python -m call_agent call ...`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv


def serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=args.host, port=args.port)


def call(args: argparse.Namespace) -> None:
    task_path = Path(args.task)
    task = task_path.read_text() if task_path.is_file() else args.task
    with _client(args.server) as client:
        r = client.post("/calls", json={"to": args.to, "task": task})
        if r.status_code >= 400:
            sys.exit(f"Couldn't place the call: {r.status_code} {r.text}")
        call_id = r.json()["call_id"]
        print(f"Calling {args.to} (call id {call_id})...")
        if args.no_wait:
            return
        _follow(client, call_id, interactive=not args.quiet)


def watch(args: argparse.Namespace) -> None:
    with _client(args.server) as client:
        _follow(client, args.call_id, interactive=True)


def handback(args: argparse.Namespace) -> None:
    with _client(args.server) as client:
        r = client.post(f"/calls/{args.call_id}/handback", json={"note": " ".join(args.note)})
        if r.status_code >= 400:
            sys.exit(f"Couldn't hand the call back: {r.status_code} {r.json().get('detail', r.text)}")
        print("Handed back. Your phone will drop off and the agent will rejoin the call.")
        _follow(client, args.call_id, interactive=True)


def _client(server: str) -> httpx.Client:
    token = os.environ.get("CALL_AGENT_TOKEN")
    if not token:
        sys.exit("Set CALL_AGENT_TOKEN (see .env.example)")
    return httpx.Client(base_url=server, headers={"Authorization": f"Bearer {token}"}, timeout=30)


def _follow(client: httpx.Client, call_id: str, interactive: bool) -> None:
    """Print the live transcript; in interactive mode, each line typed goes to the agent."""
    if interactive:
        print("Live transcript below. Type a message and press Enter to send it to the agent;"
              " the other side won't hear it. Ctrl-C stops watching (the call continues).\n")

        def read_input() -> None:
            for line in sys.stdin:
                text = line.strip()
                if not text:
                    continue
                try:
                    r = client.post(f"/calls/{call_id}/message", json={"text": text})
                    if r.status_code >= 400:
                        print(f"  (not sent: {r.json().get('detail', r.text)})", flush=True)
                except httpx.HTTPError as e:
                    print(f"  (not sent: {e})", flush=True)

        threading.Thread(target=read_input, daemon=True).start()

    seen, last_status = 0, None
    try:
        while True:
            try:
                info = client.get(f"/calls/{call_id}").json()
            except httpx.HTTPError:
                time.sleep(3)
                continue
            if info["status"] != last_status:
                print(f"  [status: {info['status']}]", flush=True)
                last_status = info["status"]
            lines = info.get("transcript") or []
            for who, text in lines[seen:]:
                label = {"them": "THEM ", "agent": "AGENT", "owner": "YOU  ", "note": "  ..."}.get(who, who)
                print(f"{label}  {text}", flush=True)
            seen = len(lines)
            if info["finished"]:
                print(f"\nOutcome: {info['outcome'] or 'unknown'}\n\n{info['summary']}", flush=True)
                if info["outcome"] == "transferred_to_owner":
                    print(
                        "\nYour phone is being connected to them. To give the call back to the agent:\n"
                        f'  python -m call_agent handback {call_id} "what happened, and what to do next"',
                        flush=True,
                    )
                return
            time.sleep(2)
    except KeyboardInterrupt:
        print(f"\nStopped watching. The call continues; reattach with: python -m call_agent watch {call_id}")


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
    p_call.add_argument("--quiet", action="store_true", help="show the transcript but don't read typed messages")
    p_call.set_defaults(func=call)

    p_watch = sub.add_parser("watch", help="follow a call live and send messages to the agent")
    p_watch.add_argument("call_id")
    p_watch.add_argument("--server", default="http://127.0.0.1:8000")
    p_watch.set_defaults(func=watch)

    p_back = sub.add_parser("handback", help="after a transfer, hand the call back to the agent")
    p_back.add_argument("call_id")
    p_back.add_argument("note", nargs="*", help="what happened while you were on, and what to do next")
    p_back.add_argument("--server", default="http://127.0.0.1:8000")
    p_back.set_defaults(func=handback)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
