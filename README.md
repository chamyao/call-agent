# call-agent

A phone agent that calls customer service for you: rescheduling deliveries,
asking about a bill, checking an order, booking a service window. You give it
a task in plain English; it dials, works through the phone menu, waits on
hold, talks to the rep, and sends you a summary with any confirmation numbers.

Built on [Twilio ConversationRelay](https://www.twilio.com/docs/voice/conversationrelay)
(handles the phone line, speech-to-text and text-to-speech) and Claude
(handles the conversation).

```
you ──task──▶ call_agent server ──REST──▶ Twilio ──dials──▶ company
                  ▲                          │
                  └──── WebSocket (text) ────┘
                         Claude decides: speak / press keys / stay silent /
                         hang up / transfer the call to you
```

## What it does on a call

- **Phone menus**: listens to the options and presses keys. Says "representative" or presses 0 when nothing fits.
- **Hold**: stays quiet through hold music and announcements.
- **Talking to a rep**: introduces itself as an assistant calling for you, says the call is being transcribed, and explains the task. It reads back confirmation numbers, dates and amounts.
- **Transfer to you (last resort)**: if `OWNER_PHONE` is set, the agent can ring you and connect you to the rep, but only when the task can't be finished without you (a payment, identity checks it can't pass). It never offers this on its own. When you pick up you hear a one-line reason first; if you don't answer, the rep is told you'll follow up.
- **Afterwards**: writes `calls/<date>_<id>.md` with the summary and full transcript. It can also text you the summary.

Guardrails in the prompt: it only shares facts from your task and
`profile.md`. It never gives out passwords, full SSNs or card numbers. It
won't agree to charges or changes the task didn't authorize, and it always
admits to being an AI when asked.

## Setup

1. **Python 3.10+**

   ```sh
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Twilio**
   - Create an account at twilio.com and buy a phone number with Voice (a dollar or two a month).
   - In the Console, enable the AI features that ConversationRelay needs. Twilio asks you to accept its AI/ML features addendum the first time.
   - On a trial account, Twilio can only call numbers you've verified. Upgrade the account to call companies.

3. **A public URL** so Twilio can reach the server on your machine:

   ```sh
   ngrok http 8000      # copy the https://... forwarding URL
   ```

4. **Config**

   ```sh
   cp .env.example .env                          # fill in keys, number, PUBLIC_URL
   cp examples/profile.example.md profile.md     # facts the agent may share
   ```

## Making a call

Start the server, keep it running, and make sure ngrok is up:

```sh
python -m call_agent serve
```

In another terminal:

```sh
python -m call_agent call --to +18007425877 --task examples/task.example.md
# or inline:
python -m call_agent call --to +18007425877 --task "Ask when my order 12345 will ship."
```

The command prints status updates, then the outcome and summary when the call ends.

While the call runs, the command shows the live transcript. Type a line and
press Enter to send it to the agent (the other side doesn't hear it), e.g.
"a $10 fee is fine" or "ask for a supervisor". To follow a call that's already
running, use `python -m call_agent watch <call id>`.

If the agent transfers the call to you, you can give it back once you're done
(say, after verifying your identity): `python -m call_agent handback <call id>
"verified; get the order number"`. Your phone drops off and the agent rejoins
the same call with your note. Your part of the call is transcribed too, so it
shows up live (as YOU) and the agent knows what was said when it rejoins.

Writing good tasks: say what you want, what you'd accept as a fallback, and
what it must not agree to. Put identifiers like account, order and tracking
numbers either in the task or in `profile.md`.

## Running it on db-east

The bot runs on db-east as the restricted `phonebot` user, behind the existing
nginx at `https://calls.ceruleantokyo.xyz` (proxied to `127.0.0.1:8010`). The
server's admin manages `phonebot.service`; restarts go through them.

1. Add to `~/.ssh/config`:

   ```
   Host east
     HostName 178.156.172.174
     User phonebot
     IdentityFile ~/.ssh/east_ed25519
     IdentitiesOnly yes
     StrictHostKeyChecking ask
   ```

2. Set `PUBLIC_URL=https://calls.ceruleantokyo.xyz` in `.env`, then run `deploy/east/deploy.sh`. It copies the code, `.env` and `profile.md`, and builds the virtualenv.
3. Logs: `ssh east tail -f app/logs/phonebot.log`. The script restarts the service with the one sudo command `phonebot` is allowed.

Every Twilio request, including the `/relay` WebSocket handshake, must carry a
valid `X-Twilio-Signature`; there is no setting to turn that off.

## Running it through your own server (frp)

Instead of ngrok, you can give the bot a permanent address using a VPS you
control. The bot keeps running on your Mac; the VPS only forwards traffic.

```
Twilio ──HTTPS──▶ VPS: Caddy :443 ─▶ frps :8080 ◀── tunnel :7000 ── Mac: frpc ─▶ bot :8000
```

1. **DNS**: add an A record for a subdomain (e.g. `calls.yourdomain.com`) pointing at the VPS's IP.
2. **VPS** (Debian/Ubuntu): copy the script over and run it with your domain and the `FRP_TOKEN` from `.env`:

   ```sh
   scp deploy/vps/setup.sh root@YOUR_VPS_IP:
   ssh root@YOUR_VPS_IP 'bash setup.sh calls.yourdomain.com YOUR_FRP_TOKEN'
   ```

   This installs frps and Caddy (which gets the HTTPS certificate), starts both on boot, and opens ports 22, 80, 443 and 7000.
3. **Mac**: set `PUBLIC_URL=https://calls.yourdomain.com` and `FRP_SERVER_ADDR` in `.env`, then:

   ```sh
   brew install frpc
   deploy/mac/install.sh
   ```

   This runs the bot and the tunnel in the background at login, and checks that `https://calls.yourdomain.com/health` answers. Logs go to `~/Library/Logs/call-agent/`. Stop everything with `deploy/mac/install.sh --uninstall`.

The Mac has to be on and awake for calls to work.

## Costs (rough)

- Twilio voice: a few cents per minute for the call plus a per-minute ConversationRelay fee. Check Twilio's pricing page for current rates.
- Claude: one request per thing the other side says. Long holds with chatty hold recordings add up. The default is `claude-opus-5-5` at `low` effort for faster replies. Set `CLAUDE_MODEL=claude-sonnet-5-5` to cut cost.

## Development

```sh
pip install -r requirements-dev.txt
pytest
```

The tests run full simulated calls (menu, hold, rep, hang-up, interruption,
transfer) against a scripted fake model and a fake Twilio client.

## Status and caveats

- **Not yet tested on a real phone call.** The ConversationRelay message format (`prompt`, `interrupt`, `sendDigits`, `end` with `handoffData`) follows Twilio's documented protocol, but the first live calls are the real test. Try it on your own cell phone first.
- Speech recognition garbles numbers sometimes. The agent reads them back, but double-check confirmation numbers in the transcript.
- Laws on AI calls and recording vary by state. California requires all parties to consent to recording, so the agent says the call is transcribed. This tool is for calling businesses' customer service lines for your own accounts, not for calling people.
- The server keeps call state in memory. Restarting it mid-call drops the call.
