# jetson-creamer-alerts

NFC / web-triggered household alerts on a **Jetson Orin Nano**: tap or tap Confirm → **Ollama** drafts a message → **OpenClaw** sends it to a WhatsApp group.

## Architecture

```
NFC tag / phone browser  →  FastAPI (port 5000)  →  Ollama (qwen-house)
                                    ↓
                          OpenClaw CLI (WhatsApp)
                                    ↓
                          Household WhatsApp group
```

## Prerequisites

- Jetson Orin Nano (or similar) with JetPack / CUDA Ollama
- [Ollama](https://ollama.com/) running locally (`127.0.0.1:11434`)
- [OpenClaw](https://www.npmjs.com/package/openclaw) installed globally with WhatsApp linked
- Python 3.10+
- `customtkinter` for the optional control panel GUI

## Setup

### 1. Clone and install Python deps

```bash
git clone https://github.com/nudro/jetson-creamer-alerts.git
cd jetson-creamer-alerts
pip install -r requirements.txt
```

### 2. Create the Ollama model

The Orin's shared GPU memory cannot fully offload `qwen2.5:3b`. Use the tuned model:

```bash
ollama create qwen-house -f Modelfile.qwen-house
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env — set HOUSEHOLD_GROUP_JID to your WhatsApp group JID
```

### 4. Discover your WhatsApp group JID (one-time)

If you only have a group invite link (`https://chat.whatsapp.com/<CODE>`):

```bash
./resolve_group_jid.sh YOUR_INVITE_CODE
```

Copy the printed JID into `.env` as `HOUSEHOLD_GROUP_JID`, and add the same JID to `~/.openclaw/openclaw.json` under `channels.whatsapp.groups`.

WhatsApp session credentials live in `~/.openclaw/credentials/` and are **not** part of this repo.

### 5. OpenClaw gateway

Ensure the OpenClaw gateway is running (systemd service or `openclaw gateway start`).

## Run

**Web / NFC ingress (FastAPI):**

```bash
uvicorn app:app --host 0.0.0.0 --port 5000
```

- Creamer page: `http://<your-orin-ip>:5000/creamer`
- Trigger API: `POST /api/trigger-claw`

**Control panel (optional):**

```bash
python3 control_panel.py
```

Manages Ollama, OpenClaw gateway, and Uvicorn from a cyberpunk HUD. Shows creamer dispatch status and Jetson memory telemetry.

## Files

| File | Purpose |
|---|---|
| `app.py` | FastAPI creamer alert pipeline |
| `control_panel.py` | Service orchestrator GUI |
| `Modelfile.qwen-house` | Ollama model tuned for Orin GPU memory |
| `resolve_group_jid.js` / `.sh` | One-time WhatsApp group JID resolver |

## Logs

Runtime logs (not committed):

- `/tmp/uvicorn_5000.log` — verbose Ollama / OpenClaw trace
- `/tmp/creamer_last_event.json` — last dispatch state for the control panel

## Publish to GitHub (maintainers)

```bash
export PATH="$HOME/bin:$PATH"   # if using user-local gh binary
gh auth login --scopes "repo,read:org"
cd ~/home-automation
./scripts/publish-to-github.sh
```

When pasting a token, use a **classic** PAT from [github.com/settings/tokens/new](https://github.com/settings/tokens/new) with scopes **repo** and **read:org**. Tokens missing `read:org` will fail with `error validating token: missing required scope 'read:org'`.

The publish script runs a secrets scan (JIDs, phones, LAN IPs, home paths) before pushing.

## License

MIT (add a LICENSE file if you choose one)
