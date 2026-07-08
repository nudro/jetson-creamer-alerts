from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = FastAPI()

LOG_PATH = Path("/tmp/uvicorn_5000.log")
STATE_PATH = Path("/tmp/creamer_last_event.json")


def _expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("creamer")
    logger.setLevel(logging.DEBUG)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()


def _write_event(phase: str, success: bool | None = None, detail: str = "") -> None:
    payload = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase,
        "success": success,
        "detail": detail[:500],
    }
    STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    log.info("EVENT %s success=%s detail=%s", phase, success, detail[:200])

# OpenClaw's gateway on :18789 is a WebSocket gateway + control dashboard, NOT a REST
# API — there is no /api/v1/alerts/household route (it returns 405 for any POST).
# Outbound messages go through the `openclaw message send` CLI instead.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")

# WhatsApp delivery target — set HOUSEHOLD_GROUP_JID in .env (see .env.example).
# Also configure ~/.openclaw/openclaw.json -> channels.whatsapp.groups
ALERT_CHANNEL = "whatsapp"
HOUSEHOLD_GROUP_JID = _require_env("HOUSEHOLD_GROUP_JID")
ALERT_TARGET = HOUSEHOLD_GROUP_JID
OPENCLAW_BIN = shutil.which("openclaw") or os.environ.get("OPENCLAW_BIN", "openclaw")
OPENCLAW_WHATSAPP_AUTH_DIR = _expand_path(
    os.environ.get(
        "OPENCLAW_WHATSAPP_AUTH_DIR",
        "~/.openclaw/credentials/whatsapp/default",
    )
)

@app.get("/creamer", response_class=HTMLResponse)
async def creamer_page():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Household Alert</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-900 text-white flex flex-col items-center justify-center min-h-screen p-6">
        <div class="bg-slate-800 p-8 rounded-2xl shadow-xl max-w-sm w-full text-center border border-slate-700">
            <div class="text-5xl mb-4">☕🥛</div>
            <h1 class="text-xl font-bold mb-2">Out of Coffee Creamer?</h1>
            <p class="text-slate-400 text-sm mb-6">Tap confirm to have the Orin alert the house via OpenClaw.</p>
            
            <button id="confirmBtn" onclick="sendAlert()" class="w-full bg-amber-600 hover:bg-amber-700 active:scale-95 transition-all text-white font-semibold py-4 px-6 rounded-xl text-lg shadow-lg">
                Confirm & Send Alert
            </button>
            
            <p id="status" class="mt-4 text-sm font-medium text-amber-400 hidden">Sending alert...</p>
        </div>

        <script>
            async function sendAlert() {
                const btn = document.getElementById('confirmBtn');
                const status = document.getElementById('status');
                
                btn.disabled = true;
                status.classList.remove('hidden');
                
                try {
                    const response = await fetch('/api/trigger-claw', { method: 'POST' });
                    const data = await response.json().catch(() => ({}));
                    if (response.ok && data.status === 'success') {
                        status.innerText = "✅ Alert sent to WhatsApp via OpenClaw!";
                        status.className = "mt-4 text-sm font-medium text-emerald-400";
                    } else {
                        throw new Error(data.detail || 'Request failed');
                    }
                } catch (error) {
                    status.innerText = "❌ Failed to send alert.";
                    status.className = "mt-4 text-sm font-medium text-rose-400";
                    btn.disabled = false;
                }
            }
        </script>
    </body>
    </html>
    """


@app.post("/api/trigger-claw")
async def trigger_claw():
    t0 = time.monotonic()
    log.info("=" * 60)
    log.info("NFC Event: Creamer trigger received")
    _write_event("dispatching", detail="Trigger received — calling Ollama")

    prompt = {
        # Use qwen-house (Modelfile.qwen-house): same weights as qwen2.5:3b but
        # num_gpu=8 baked in so cold loads fit the Orin's ~2GB GPU ceiling.
        # Do NOT pass num_gpu here — mismatched options force a reload that OOMs.
        "model": "qwen-house",
        "messages": [
            {
                "role": "system",
                "content": "You are a precise household assistant tool. Output a single short message suitable for a WhatsApp group alert.",
            },
            {
                "role": "user",
                "content": "The kitchen is out of coffee creamer. Draft an urgent but casual reminder to send to the household WhatsApp group.",
            },
        ],
        "stream": False,
        "keep_alive": -1,
    }

    log.info("Ollama POST %s model=%s stream=%s keep_alive=%s",
             OLLAMA_URL, prompt["model"], prompt["stream"], prompt["keep_alive"])
    log.debug("Ollama messages: %s", json.dumps(prompt["messages"], ensure_ascii=False))

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(OLLAMA_URL, json=prompt, timeout=60.0)
        except Exception as e:
            err = f"Inference bridge failure: {e}"
            log.error(err)
            _write_event("failed", success=False, detail=err)
            raise HTTPException(status_code=502, detail="Failed to reach local Ollama inference daemon.")

    elapsed = time.monotonic() - t0
    log.info("Ollama response HTTP %s (%.2fs)", response.status_code, elapsed)

    if response.status_code != 200:
        body = response.text[:500]
        err = f"Ollama HTTP {response.status_code}: {body}"
        log.error(err)
        _write_event("failed", success=False, detail=err)
        raise HTTPException(
            status_code=502,
            detail=f"Ollama inference failed (HTTP {response.status_code}). No message was sent to my whatsapp group",
        )

    data = response.json()
    llm_output = data["message"]["content"]
    eval_count = data.get("eval_count")
    eval_duration = data.get("eval_duration")
    load_duration = data.get("load_duration")
    log.info("Ollama inference OK eval_count=%s eval_duration=%s load_duration=%s",
             eval_count, eval_duration, load_duration)
    log.info("Qwen Generation: %s", llm_output)
    _write_event("inference_ok", detail=llm_output)

    openclaw_cmd = [
        OPENCLAW_BIN, "message", "send",
        "--channel", ALERT_CHANNEL,
        "--target", ALERT_TARGET,
        "--message", llm_output,
    ]
    log.info("OpenClaw exec: %s", " ".join(openclaw_cmd[:6]) + " <message>")

    try:
        proc = await asyncio.create_subprocess_exec(
            *openclaw_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=45.0)
    except FileNotFoundError:
        err = "OpenClaw CLI not found on host"
        log.critical(err)
        _write_event("failed", success=False, detail=err)
        raise HTTPException(
            status_code=503,
            detail="Trigger received, OpenClaw endpoint unreachable. No message was sent to my whatsapp group",
        )
    except asyncio.TimeoutError:
        err = "OpenClaw send timed out after 45s"
        log.critical(err)
        _write_event("failed", success=False, detail=err)
        raise HTTPException(
            status_code=503,
            detail="Trigger received, OpenClaw endpoint unreachable. No message was sent to my whatsapp group",
        )

    output = stdout.decode("utf-8", errors="replace").strip()
    log.info("OpenClaw stdout (exit %s): %s", proc.returncode, output or "<empty>")
    if proc.returncode != 0:
        err = f"OpenClaw send failed (exit {proc.returncode}): {output}"
        log.error(err)
        _write_event("failed", success=False, detail=err)
        raise HTTPException(status_code=502, detail="No message was sent to my whatsapp group")

    total = time.monotonic() - t0
    log.info("OpenClaw dispatch OK (total %.2fs)", total)
    _write_event("success", success=True, detail=llm_output)
    return {"status": "success", "message": "Dispatched via Ollama & OpenClaw"}