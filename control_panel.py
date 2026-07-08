#!/usr/bin/env python3.10
"""
Ordun Household AI Server — Cyberpunk HUD system orchestrator for Jetson Orin.
Manages Ollama, NemoClaw Gateway, and Uvicorn without blocking the UI thread.
"""

from __future__ import annotations

import customtkinter as ctk
import math
import queue
import re
import shlex
import socket
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

# ══════════════════════════════════════════════════════════════════════════════
# CYBERPUNK HUD — VISUAL LAYER
# ══════════════════════════════════════════════════════════════════════════════
BG = "#0D0D0D"
GRID_LINE = "#161616"
GRID_LINE_BRIGHT = "#242E38"
CARD_BG = "#0A0A0A"
CARD_INNER = "#111111"
CYAN = "#00F0FF"
CYAN_BRIGHT = "#7DF9FF"
CYAN_DIM = "#0A3040"
CYAN_MUTED = "#5AC4D4"
GLOW_CYAN = "#1A5566"
NEON_GREEN = "#39FF14"
NEON_GREEN_BRIGHT = "#6AFF4A"
MAGENTA = "#FF007F"
AMBER = "#FFB000"
AMBER_BRIGHT = "#FFD060"
TEXT = "#E0E0E0"
TEXT_DIM = "#6A6A6A"
CONSOLE_BG = "#080808"

MONO = "Courier New"
MONO_ALT = "Consolas"

DEFAULT_W, DEFAULT_H = 1680, 1050
MIN_W, MIN_H = 960, 720
CARD_STACK_BREAKPOINT = 1100

FONT_TITLE = 28
FONT_SUB = 13
FONT_CARD = 18
FONT_TACTICAL = 11
FONT_STATUS = 14
FONT_BTN = 13
FONT_OVERDRIVE = 16
FONT_TELEM = 12
FONT_CONSOLE = 12
FONT_CONSOLE_HDR = 13
FONT_MATRIX = 11

PAD = 20
PAD_CARD = 16
BRACKET_SIZE = 18
BRACKET_GAP = 3

TACTICAL_LABELS = {
    "ollama": "[LOC_ADDR // 127.0.0.1:11434] // CORE_STATE: {state}",
    "openclaw": "[GATEWAY // 127.0.0.1:18789] // PROXY_TUNNEL: {state}",
    "uvicorn": "[HTTP_SERVER // 0.0.0.0:5000] // INGRESS_FLOW: {state}",
}

SIDEBAR_WIDTH_RATIO = 0.25

CREAMER_LOG_PATH = Path("/tmp/uvicorn_5000.log")
CREAMER_STATE_PATH = Path("/tmp/creamer_last_event.json")


@dataclass(frozen=True)
class PayloadModule:
    """Register future home-automation edge apps here."""
    key: str
    title: str
    identifier: str
    mapping: str
    default_status: str
    status_color: str
    status_bg: str = "#1A1400"
    flash_status: Optional[str] = None
    flash_color: Optional[str] = None
    flash_duration_ms: int = 3000
    log_triggers: tuple[str, ...] = ()
    result_triggers: tuple[str, ...] = ()


PAYLOAD_MODULES: list[PayloadModule] = [
    PayloadModule(
        key="creamer",
        title="NFC Tag Creamer App",
        identifier="[SUB_SYS // CREAMER_RELOAD]",
        mapping="HOOK -> Ingress (Port 5000) -> OpenClaw (18789)",
        default_status="READY // WAITING_FOR_TAG",
        status_color="#CC8800",
        status_bg="#1A1400",
        flash_status="SIGNAL_INGEST_OK",
        flash_color=NEON_GREEN,
        flash_duration_ms=3000,
        log_triggers=("NFC Event", "Creamer trigger", "/api/trigger-claw", "NTAG213"),
        result_triggers=(
            "OpenClaw dispatch OK",
            "[ERROR] // Ollama",
            "OpenClaw send failed",
            "Inference bridge failure",
            "No message was sent to my whatsapp group",
        ),
    ),
    PayloadModule(
        key="household_alerts",
        title="Household Alerts Core",
        identifier="[OUTBOUND // WHATSAPP_BRIDGE]",
        mapping="Target API Route: /api/v1/alerts/household",
        default_status="CONNECTED",
        status_color=CYAN,
        status_bg="#0A1A1A",
    ),
]

WORKSPACE = Path(__file__).resolve().parent


# ══════════════════════════════════════════════════════════════════════════════
# BACKEND — SERVICE ORCHESTRATION (UNTOUCHED)
# ══════════════════════════════════════════════════════════════════════════════

class ServiceState(str, Enum):
    OFFLINE = "OFFLINE"
    LAUNCHING = "LAUNCHING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


STATE_COLORS = {
    ServiceState.OFFLINE: "#9B2C2C",
    ServiceState.LAUNCHING: "#F59E0B",
    ServiceState.RUNNING: "#10B981",
    ServiceState.STOPPING: "#F59E0B",
    ServiceState.ERROR: "#EF4444",
}


class HealthKind(str, Enum):
    HTTP = "http"
    PORT = "port"


@dataclass
class ServiceConfig:
    key: str
    name: str
    port: int
    command: list[str]
    health: HealthKind
    cwd: Optional[Path] = None
    managed: bool = False
    stop_command: Optional[list[str]] = None
    log_tail_command: Optional[list[str]] = None
    health_url: Optional[str] = None
    health_host: str = "127.0.0.1"
    health_poll_interval: float = 0.75
    startup_timeout: float = 45.0
    bind_error_markers: tuple[str, ...] = (
        "address already in use",
        "bind",
        "eaddrinuse",
        "failed to bind",
        "port is already in use",
    )


@dataclass
class ServiceRuntime:
    config: ServiceConfig
    state: ServiceState = ServiceState.OFFLINE
    process: Optional[subprocess.Popen] = None
    log_process: Optional[subprocess.Popen] = None
    last_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_requested: bool = False


SERVICES: list[ServiceConfig] = [
    ServiceConfig(
        key="ollama",
        name="Ollama Server",
        port=11434,
        command=["ollama", "serve"],
        health=HealthKind.HTTP,
        startup_timeout=60.0,
    ),
    ServiceConfig(
        key="openclaw",
        name="NemoClaw Gateway",
        port=18789,
        command=["openclaw", "gateway", "start"],
        stop_command=["openclaw", "gateway", "stop"],
        health=HealthKind.HTTP,
        health_url="http://127.0.0.1:18789",
        health_host="127.0.0.1",
        health_poll_interval=0.25,
        managed=True,
        log_tail_command=[
            "journalctl",
            "--user",
            "-u",
            "openclaw-gateway.service",
            "-f",
            "-n",
            "20",
            "--no-pager",
        ],
    ),
    ServiceConfig(
        key="uvicorn",
        name="Uvicorn Web Server",
        port=5000,
        command=[
            "/usr/bin/python3.10",
            "-m",
            "uvicorn",
            "app:app",
            "--host",
            "0.0.0.0",
            "--port",
            "5000",
        ],
        health=HealthKind.PORT,
        cwd=WORKSPACE,
    ),
]


def port_is_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def http_is_healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 500
    except Exception:
        return False


def check_health(config: ServiceConfig) -> bool:
    if config.health == HealthKind.HTTP:
        url = config.health_url or f"http://{config.health_host}:{config.port}"
        return http_is_healthy(url)
    return port_is_open(config.port, host=config.health_host)


class ServiceManager:
    def __init__(self, log_callback: Callable[[str, str], None]) -> None:
        self._log = log_callback
        self._runtimes = {cfg.key: ServiceRuntime(config=cfg) for cfg in SERVICES}
        self._monitor_stop = threading.Event()
        self._monitor_thread = threading.Thread(
            target=self._health_monitor_loop, name="health-monitor", daemon=True
        )
        self._monitor_thread.start()
        self._probe_initial_health()

    def _probe_initial_health(self) -> None:
        for key, runtime in self._runtimes.items():
            cfg = runtime.config
            if check_health(cfg):
                runtime.last_error = ""
                self._set_state(key, ServiceState.RUNNING)
                self._log(
                    key,
                    f"Live probe OK — {cfg.health_url or f'{cfg.health_host}:{cfg.port}'} responding.",
                )

    def get_state(self, key: str) -> ServiceState:
        return self._runtimes[key].state

    def get_error(self, key: str) -> str:
        return self._runtimes[key].last_error

    def all_keys(self) -> list[str]:
        return list(self._runtimes.keys())

    def shutdown(self) -> None:
        self._monitor_stop.set()

    def _set_state(self, key: str, state: ServiceState, error: str = "") -> None:
        runtime = self._runtimes[key]
        with runtime._lock:
            runtime.state = state
            if error:
                runtime.last_error = error
        self._log("system", f"[{runtime.config.name}] → {state.value}" + (f" ({error})" if error else ""))

    def _stream_output(self, key: str, proc: subprocess.Popen, prefix: str) -> None:
        assert proc.stdout is not None
        runtime = self._runtimes[key]
        try:
            for raw in iter(proc.stdout.readline, b""):
                if self._monitor_stop.is_set():
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                self._log(key, f"{prefix}{line}")
                lowered = line.lower()
                if any(marker in lowered for marker in runtime.config.bind_error_markers):
                    runtime.last_error = line
                    if not runtime.config.managed:
                        self._set_state(key, ServiceState.ERROR, "Port bind failure")
        finally:
            proc.stdout.close()

    def _start_log_tail(self, key: str) -> None:
        runtime = self._runtimes[key]
        cmd = runtime.config.log_tail_command
        if not cmd:
            return
        self._stop_log_tail(key)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=runtime.config.cwd,
                text=False,
            )
        except Exception as exc:
            self._log(key, f"[log-tail] failed: {exc}")
            return
        runtime.log_process = proc
        threading.Thread(
            target=self._stream_output,
            args=(key, proc, "[svc] "),
            name=f"log-tail-{key}",
            daemon=True,
        ).start()

    def _stop_log_tail(self, key: str) -> None:
        runtime = self._runtimes[key]
        proc = runtime.log_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        runtime.log_process = None

    def _stop_process(self, key: str) -> None:
        runtime = self._runtimes[key]
        proc = runtime.process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        runtime.process = None
        self._stop_log_tail(key)

    def _run_stop_command(self, key: str) -> None:
        runtime = self._runtimes[key]
        cmd = runtime.config.stop_command
        if not cmd:
            return
        try:
            result = subprocess.run(
                cmd,
                cwd=runtime.config.cwd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.stdout.strip():
                self._log(key, result.stdout.strip())
            if result.stderr.strip():
                self._log(key, result.stderr.strip())
            if result.returncode != 0:
                self._log(key, f"stop command exited {result.returncode}")
        except Exception as exc:
            self._log(key, f"stop command failed: {exc}")

    def start(self, key: str) -> None:
        runtime = self._runtimes[key]
        with runtime._lock:
            if runtime.state in (ServiceState.LAUNCHING, ServiceState.STOPPING):
                return
            runtime._stop_requested = False
            runtime.last_error = ""

        if check_health(runtime.config):
            self._set_state(key, ServiceState.RUNNING)
            self._log(key, "Already healthy on loopback — marked RUNNING.")
            return

        self._set_state(key, ServiceState.LAUNCHING)
        threading.Thread(target=self._start_worker, args=(key,), name=f"start-{key}", daemon=True).start()

    def _start_worker(self, key: str) -> None:
        runtime = self._runtimes[key]
        cfg = runtime.config

        if check_health(cfg):
            self._set_state(key, ServiceState.RUNNING)
            self._log(key, "Gateway already responding on loopback — marked RUNNING.")
            return

        self._log(key, f"Launching: {shlex.join(cfg.command)}")

        try:
            proc = subprocess.Popen(
                cfg.command,
                cwd=cfg.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
        except FileNotFoundError:
            self._set_state(key, ServiceState.ERROR, "Executable not found")
            self._log(key, "ERROR: command not found on PATH.")
            return
        except Exception as exc:
            self._set_state(key, ServiceState.ERROR, str(exc))
            self._log(key, f"ERROR: failed to spawn process — {exc}")
            return

        with runtime._lock:
            runtime.process = proc

        threading.Thread(
            target=self._stream_output,
            args=(key, proc, ""),
            name=f"stdout-{key}",
            daemon=True,
        ).start()

        if cfg.managed:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.25)
            self._start_log_tail(key)
        else:
            time.sleep(0.5)
            if proc.poll() is not None and proc.returncode != 0:
                err = runtime.last_error or f"exit code {proc.returncode}"
                self._set_state(key, ServiceState.ERROR, err)
                return

        self._wait_for_health(key)

    def _wait_for_health(self, key: str) -> None:
        runtime = self._runtimes[key]
        cfg = runtime.config
        deadline = time.monotonic() + cfg.startup_timeout

        while time.monotonic() < deadline:
            if runtime._stop_requested:
                return
            if check_health(cfg):
                self._set_state(key, ServiceState.RUNNING)
                target = cfg.health_url or f"{cfg.health_host}:{cfg.port}"
                self._log(key, f"Healthy — {target} responding.")
                return

            proc = runtime.process
            if proc and proc.poll() is not None and not cfg.managed:
                err = runtime.last_error or f"Process exited ({proc.returncode})"
                self._set_state(key, ServiceState.ERROR, err)
                self._log(key, f"ERROR: process died before {cfg.health_host}:{cfg.port} opened.")
                return

            time.sleep(cfg.health_poll_interval)

        target = cfg.health_url or f"{cfg.health_host}:{cfg.port}"
        err = runtime.last_error or f"Timed out waiting for {target}"
        self._set_state(key, ServiceState.ERROR, err)
        self._log(key, f"ERROR: health check failed — {err}")

    def stop(self, key: str) -> None:
        runtime = self._runtimes[key]
        with runtime._lock:
            runtime._stop_requested = True
        self._set_state(key, ServiceState.STOPPING)
        threading.Thread(target=self._stop_worker, args=(key,), name=f"stop-{key}", daemon=True).start()

    def _stop_worker(self, key: str) -> None:
        runtime = self._runtimes[key]
        cfg = runtime.config
        self._log(key, "Stopping service…")

        self._stop_process(key)
        if cfg.stop_command:
            self._run_stop_command(key)

        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if not check_health(cfg):
                break
            time.sleep(0.5)

        if check_health(cfg):
            if cfg.managed:
                self._log(key, "Gateway still active on loopback (external/managed process).")
                self._set_state(key, ServiceState.RUNNING)
            else:
                self._log(key, f"WARNING: {cfg.health_host}:{cfg.port} still appears active.")
                self._set_state(key, ServiceState.ERROR, f"Port {cfg.port} still bound")
        else:
            self._set_state(key, ServiceState.OFFLINE)
            self._log(key, "Stopped.")

    def restart(self, key: str) -> None:
        runtime = self._runtimes[key]
        with runtime._lock:
            runtime._stop_requested = False
        self._set_state(key, ServiceState.STOPPING)
        threading.Thread(
            target=self._restart_worker, args=(key,), name=f"restart-{key}", daemon=True
        ).start()

    def _restart_worker(self, key: str) -> None:
        self._stop_worker(key)
        time.sleep(1.5)
        runtime = self._runtimes[key]
        with runtime._lock:
            if runtime._stop_requested:
                return
        self._start_worker(key)

    def wake_all(self) -> None:
        threading.Thread(target=self._wake_all_worker, name="wake-all", daemon=True).start()

    def _wake_all_worker(self) -> None:
        self._log("system", "═══ BOOTSWAP / WAKE ALL initiated ═══")
        for idx, cfg in enumerate(SERVICES):
            self.start(cfg.key)
            deadline = time.monotonic() + cfg.startup_timeout + 5.0
            while time.monotonic() < deadline:
                state = self.get_state(cfg.key)
                if state == ServiceState.RUNNING:
                    break
                if state == ServiceState.ERROR:
                    self._log("system", f"Wake sequence halted at {cfg.name}.")
                    return
                time.sleep(0.5)
            if idx < len(SERVICES) - 1:
                self._log("system", "Cooldown 2s before next service…")
                time.sleep(2.0)
        self._log("system", "═══ WAKE ALL complete ═══")


    def _health_monitor_loop(self) -> None:
        while not self._monitor_stop.is_set():
            for key, runtime in self._runtimes.items():
                cfg = runtime.config
                healthy = check_health(cfg)
                with runtime._lock:
                    state = runtime.state

                if state == ServiceState.RUNNING and not healthy:
                    if not cfg.managed:
                        self._set_state(key, ServiceState.OFFLINE, "Health probe failed")
                elif state in (ServiceState.OFFLINE, ServiceState.ERROR, ServiceState.LAUNCHING) and healthy:
                    runtime.last_error = ""
                    self._set_state(key, ServiceState.RUNNING)

                if (
                    not cfg.managed
                    and state in (ServiceState.RUNNING, ServiceState.LAUNCHING)
                    and runtime.process
                    and runtime.process.poll() is not None
                    and not healthy
                ):
                    err = runtime.last_error or f"exit {runtime.process.returncode}"
                    self._set_state(key, ServiceState.ERROR, err)

            time.sleep(2.0)


# ══════════════════════════════════════════════════════════════════════════════
# CYBERPUNK HUD — UI COMPONENTS
# ══════════════════════════════════════════════════════════════════════════════

TACTICAL_STATE_TEXT = {
    ServiceState.RUNNING: "OK",
    ServiceState.LAUNCHING: "SPINUP",
    ServiceState.STOPPING: "HALT",
    ServiceState.ERROR: "FAULT",
    ServiceState.OFFLINE: "STANDBY",
}

HUD_STATE_COLORS = {
    ServiceState.RUNNING: CYAN,
    ServiceState.LAUNCHING: AMBER,
    ServiceState.STOPPING: AMBER,
    ServiceState.OFFLINE: MAGENTA,
    ServiceState.ERROR: MAGENTA,
}

HUD_STATE_GLOW = {
    ServiceState.RUNNING: NEON_GREEN,
    ServiceState.LAUNCHING: AMBER,
    ServiceState.STOPPING: AMBER,
    ServiceState.OFFLINE: MAGENTA,
    ServiceState.ERROR: "#FF3366",
}


def _mono(size: int, bold: bool = False) -> ctk.CTkFont:
    weight = "bold" if bold else "normal"
    try:
        return ctk.CTkFont(family=MONO, size=size, weight=weight)
    except Exception:
        return ctk.CTkFont(family=MONO_ALT, size=size, weight=weight)


def read_system_uptime() -> str:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            seconds = float(fh.read().split()[0])
        hours, rem = divmod(int(seconds), 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    except Exception:
        return "--:--:--"


@dataclass
class TegraMemorySample:
  ram_used_mb: int
  ram_total_mb: int
  lfb_mb: int
  swap_used_mb: int
  swap_total_mb: int
  gr3d_pct: int


_TEGRA_LINE = re.compile(
    r"RAM (\d+)/(\d+)MB \(lfb (\d+)x(\d+)MB\).*?SWAP (\d+)/(\d+)MB.*?GR3D_FREQ (\d+)%"
)


def read_tegrastats_sample() -> Optional[TegraMemorySample]:
    """One-shot tegrastats sample (Jetson unified RAM / shared GPU memory)."""
    try:
        result = subprocess.run(
            ["timeout", "2", "tegrastats", "--interval", "1000"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        for line in reversed(result.stdout.strip().splitlines()):
            match = _TEGRA_LINE.search(line)
            if match:
                lfb_count, lfb_unit = int(match.group(3)), int(match.group(4))
                return TegraMemorySample(
                    ram_used_mb=int(match.group(1)),
                    ram_total_mb=int(match.group(2)),
                    lfb_mb=lfb_count * lfb_unit,
                    swap_used_mb=int(match.group(5)),
                    swap_total_mb=int(match.group(6)),
                    gr3d_pct=int(match.group(7)),
                )
    except Exception:
        pass
    return None


class GridBackground(tk.Canvas):
    """Subtle tactical grid overlay on pitch black."""

    def __init__(self, master, **kwargs) -> None:
        super().__init__(master, bg=BG, highlightthickness=0, **kwargs)
        self._cell = 40
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None) -> None:
        self.delete("grid")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 2 or h < 2:
            return
        for i, x in enumerate(range(0, w, self._cell)):
            color = GRID_LINE_BRIGHT if i % 4 == 0 else GRID_LINE
            self.create_line(x, 0, x, h, fill=color, tags="grid")
        for i, y in enumerate(range(0, h, self._cell)):
            color = GRID_LINE_BRIGHT if i % 4 == 0 else GRID_LINE
            self.create_line(0, y, w, y, fill=color, tags="grid")
        # Corner accent glow
        self.create_line(0, 0, 80, 0, fill=CYAN_DIM, width=2, tags="grid")
        self.create_line(0, 0, 0, 80, fill=CYAN_DIM, width=2, tags="grid")
        self.create_line(w, h, w - 80, h, fill=CYAN_DIM, width=2, tags="grid")
        self.create_line(w, h, w, h - 80, fill=CYAN_DIM, width=2, tags="grid")


class HeaderGlowLine(tk.Canvas):
    """Shimmer accent bar under the main title."""

    def __init__(self, master, height: int = 8, **kwargs) -> None:
        super().__init__(master, height=height, bg=BG, highlightthickness=0, **kwargs)
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None) -> None:
        self.delete("glow")
        w = self.winfo_width()
        if w < 8:
            return
        y = self.winfo_height() // 2
        self.create_line(0, y + 1, w, y + 1, fill=GLOW_CYAN, width=1, tags="glow")
        self.create_line(0, y, int(w * 0.72), y, fill=CYAN, width=2, tags="glow")
        self.create_line(0, y - 1, int(w * 0.38), y - 1, fill=CYAN_BRIGHT, width=1, tags="glow")
        self.create_line(int(w * 0.76), y, w, y, fill=CYAN_DIM, width=1, tags="glow")


class BracketBorder(tk.Canvas):
    """Sci-fi double-lined corner bracket frame with glow accent."""

    def __init__(self, master, accent: str = CYAN, **kwargs) -> None:
        super().__init__(master, bg=CARD_BG, highlightthickness=0, **kwargs)
        self._accent = accent
        self._accent2 = CYAN
        self.bind("<Configure>", self._redraw)

    def set_accent(self, color: str, glow: str) -> None:
        self._accent = color
        self._accent2 = glow
        self._redraw()

    def _corner(self, x1, y1, x2, y2, tag, color, width=2) -> None:
        self.create_line(x1, y1, x2, y1, fill=color, width=width, tags=tag)
        self.create_line(x1, y1, x1, y2, fill=color, width=width, tags=tag)

    def _redraw(self, _event=None) -> None:
        self.delete("bracket")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 4 or h < 4:
            return
        s = BRACKET_SIZE
        g = BRACKET_GAP
        outer = self._accent2
        inner = self._accent

        for ox, oy, dx, dy in [
            (g, g, 1, 1),
            (w - g, g, -1, 1),
            (g, h - g, 1, -1),
            (w - g, h - g, -1, -1),
        ]:
            self._corner(
                ox - dx * 3, oy - dy * 3,
                ox + dx * (s + 3), oy + dy * (s + 3),
                "bracket", GLOW_CYAN, 1,
            )
            self._corner(ox, oy, ox + dx * s, oy + dy * s, "bracket", outer, 2)
            self._corner(ox + dx * 4, oy + dy * 4, ox + dx * (s - 4), oy + dy * (s - 4), "bracket", inner, 1)

        self.create_rectangle(2, 2, w - 2, h - 2, outline=inner, width=1, tags="bracket")
        self.create_rectangle(5, 5, w - 5, h - 5, outline=outer, width=1, tags="bracket")
        self.create_rectangle(1, 1, w - 1, h - 1, outline=GLOW_CYAN, width=1, tags="bracket")


class BootAnimator(tk.Canvas):
    """Rotating ring + scan line activated during OVERDRIVE boot."""

    def __init__(self, master, size: int = 64, **kwargs) -> None:
        super().__init__(master, width=size, height=size, bg=BG, highlightthickness=0, **kwargs)
        self._size = size
        self._angle = 0.0
        self._scan_y = 0
        self._active = False
        self._job: Optional[str] = None
        self._matrix_chars = "01█▓▒░"

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._tick()

    def stop(self) -> None:
        self._active = False
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.delete("all")

    def _tick(self) -> None:
        if not self._active:
            return
        self.delete("all")
        cx = cy = self._size // 2
        r = self._size // 2 - 6

        for i in range(3):
            a0 = math.radians(self._angle + i * 120)
            a1 = a0 + math.radians(70)
            self.create_arc(
                cx - r + i * 2, cy - r + i * 2, cx + r - i * 2, cy + r - i * 2,
                start=math.degrees(a0), extent=70,
                style="arc", outline=[CYAN, NEON_GREEN, AMBER][i], width=2,
            )

        self._scan_y = (self._scan_y + 3) % self._size
        self.create_line(0, self._scan_y, self._size, self._scan_y, fill=CYAN, width=1)

        import random
        for _ in range(6):
            x = random.randint(2, self._size - 10)
            y = random.randint(2, self._size - 10)
            ch = random.choice(self._matrix_chars)
            self.create_text(x, y, text=ch, fill=NEON_GREEN, font=(MONO, 8))

        self._angle = (self._angle + 8) % 360
        self._job = self.after(50, self._tick)


class MatrixBreakSeparator(tk.Canvas):
    """Vertical dotted cyan matrix break between sidebar and main deck."""

    def __init__(self, master, width: int = 8, **kwargs) -> None:
        super().__init__(master, width=width, bg=BG, highlightthickness=0, **kwargs)
        self._dot = 5
        self._gap = 5
        self.bind("<Configure>", self._redraw)

    def _redraw(self, _event=None) -> None:
        self.delete("break")
        h = self.winfo_height()
        w = self.winfo_width()
        if h < 2:
            return
        x = w // 2
        for offset, color in ((-2, GLOW_CYAN), (-1, CYAN_DIM), (1, CYAN_DIM), (2, GLOW_CYAN)):
            self.create_line(x + offset, 0, x + offset, h, fill=color, width=1, tags="break")
        y = 4
        while y < h - 4:
            self.create_line(x, y, x, y + self._dot, fill=CYAN_BRIGHT, width=2, tags="break")
            y += self._dot + self._gap


class PayloadModuleCard(ctk.CTkFrame):
    """Read-only payload application indicator slot."""

    def __init__(self, master, module: PayloadModule, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self._module = module
        self._flashing = False
        self._flash_job: Optional[str] = None
        self._accent = module.status_color
        self._glow = module.status_color

        self._shell = ctk.CTkFrame(
            self, fg_color=CARD_INNER,
            border_color=module.status_color, border_width=2, corner_radius=2,
        )
        self._shell.pack(fill="x", padx=0, pady=(0, 10))
        body = self._shell

        ctk.CTkLabel(
            body, text=module.title.upper(),
            font=_mono(FONT_TACTICAL, bold=True), text_color=CYAN_BRIGHT, anchor="w",
        ).pack(fill="x", padx=10, pady=(10, 2))

        ctk.CTkLabel(
            body, text=module.identifier,
            font=_mono(FONT_MATRIX), text_color=NEON_GREEN_BRIGHT, anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 4))

        self._mapping_lbl = ctk.CTkLabel(
            body, text=module.mapping,
            font=_mono(FONT_MATRIX), text_color=TEXT_DIM, anchor="w", wraplength=260,
        )
        self._mapping_lbl.pack(fill="x", padx=10, pady=(0, 8))

        self._status = ctk.CTkLabel(
            body,
            text=f"◈ {module.default_status}",
            font=_mono(FONT_STATUS, bold=True),
            text_color=module.status_color,
            fg_color=module.status_bg,
            corner_radius=4, height=30,
        )
        self._status.pack(fill="x", padx=10, pady=(0, 6))

        self._last_event = ctk.CTkLabel(
            body,
            text="◈ LAST_EVENT: —",
            font=_mono(FONT_MATRIX),
            text_color=TEXT_DIM,
            anchor="w",
            wraplength=260,
            justify="left",
        )
        self._last_event.pack(fill="x", padx=10, pady=(0, 12))

    def _set_accent(self, color: str, glow: str) -> None:
        self._accent = color
        self._glow = glow
        self._shell.configure(border_color=glow)

    def set_wraplength(self, width: int) -> None:
        self._mapping_lbl.configure(wraplength=max(100, width - 24))
        self._last_event.configure(wraplength=max(100, width - 24))

    def set_dispatching(self, timestamp: Optional[str] = None) -> None:
        ts = timestamp or time.strftime("%Y-%m-%d %H:%M:%S")
        self._status.configure(
            text="◈ DISPATCH // IN_PROGRESS",
            text_color=AMBER,
            fg_color="#1A1400",
        )
        self._last_event.configure(
            text=f"◈ LAST_EVENT: {ts} — TRIGGERED",
            text_color=AMBER,
        )
        self._set_accent(AMBER, AMBER_BRIGHT)

    def set_result(self, success: bool, detail: str, timestamp: Optional[str] = None) -> None:
        ts = timestamp or time.strftime("%Y-%m-%d %H:%M:%S")
        if self._flash_job:
            self.after_cancel(self._flash_job)
            self._flash_job = None
        self._flashing = False
        if success:
            self._status.configure(
                text="◈ WHATSAPP // DELIVERED",
                text_color=NEON_GREEN,
                fg_color="#0A2A0A",
            )
            self._last_event.configure(
                text=f"◈ LAST_EVENT: {ts} — SUCCESS\n{detail[:120]}",
                text_color=NEON_GREEN_BRIGHT,
            )
            self._set_accent(NEON_GREEN_BRIGHT, CYAN_BRIGHT)
        else:
            self._status.configure(
                text="◈ DISPATCH // FAILED",
                text_color="#FF3366",
                fg_color="#1A0A14",
            )
            self._last_event.configure(
                text=f"◈ LAST_EVENT: {ts} — FAILED\n{detail[:120]}",
                text_color="#FF3366",
            )
            self._set_accent("#FF3366", MAGENTA)

    def reset_status(self) -> None:
        m = self._module
        self._status.configure(
            text=f"◈ {m.default_status}",
            text_color=m.status_color,
            fg_color=m.status_bg,
        )
        self._last_event.configure(text="◈ LAST_EVENT: —", text_color=TEXT_DIM)
        self._set_accent(m.status_color, m.status_color)

    def trigger_flash(self) -> None:
        m = self._module
        if not m.flash_status:
            return
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self._flashing = True
        self._status.configure(
            text=f"◈ {m.flash_status}",
            text_color=m.flash_color or NEON_GREEN,
            fg_color="#0A2A0A",
        )
        self._set_accent(m.flash_color or NEON_GREEN_BRIGHT, NEON_GREEN_BRIGHT)
        self._flash_job = self.after(m.flash_duration_ms, self._end_flash)

    def _end_flash(self) -> None:
        self._flashing = False
        self._flash_job = None
        self.reset_status()


class PayloadSidebar(ctk.CTkFrame):
    """Vertical PAYLOAD MODULES dock — 25% viewport."""

    def __init__(self, master, modules: list[PayloadModule], **kwargs) -> None:
        super().__init__(master, fg_color=CARD_BG, corner_radius=0, **kwargs)

        ctk.CTkLabel(
            self, text="PAYLOAD MODULES",
            font=_mono(FONT_SUB, bold=True), text_color=MAGENTA, anchor="w",
        ).pack(fill="x", padx=12, pady=(12, 2))

        ctk.CTkLabel(
            self, text="[MAPPED_APPLICATIONS // TARGETS]",
            font=_mono(FONT_TACTICAL, bold=True), text_color=CYAN, anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 10))

        ctk.CTkFrame(self, height=1, fg_color=CYAN).pack(fill="x", padx=12, pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True, padx=8, pady=(0, 12))

        self.cards: dict[str, PayloadModuleCard] = {}
        for mod in modules:
            card = PayloadModuleCard(scroll, mod)
            card.pack(fill="x", pady=(0, 4))
            self.cards[mod.key] = card

    def set_wraplength(self, width: int) -> None:
        for card in self.cards.values():
            card.set_wraplength(width)


class TacticalCard(ctk.CTkFrame):
    def __init__(self, master, config: ServiceConfig, manager: ServiceManager, **kwargs) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.config = config
        self.manager = manager

        self._bracket = BracketBorder(self, accent=MAGENTA)
        self._bracket.pack(fill="both", expand=True)

        body = ctk.CTkFrame(self._bracket, fg_color=CARD_INNER, corner_radius=0)
        body.place(relx=0.04, rely=0.04, relwidth=0.92, relheight=0.92)

        hdr = ctk.CTkFrame(body, fg_color="transparent")
        hdr.pack(fill="x", padx=PAD_CARD, pady=(PAD_CARD, 4))

        ctk.CTkLabel(
            hdr, text=f"// {config.name.upper()}",
            font=_mono(FONT_CARD, bold=True), text_color=CYAN, anchor="w",
        ).pack(side="left")

        ctk.CTkLabel(
            hdr, text=f"NODE:{config.port}",
            font=_mono(FONT_TACTICAL), text_color=TEXT_DIM,
        ).pack(side="right")

        self._tactical = ctk.CTkLabel(
            body,
            text=TACTICAL_LABELS.get(config.key, "").format(state="STANDBY"),
            font=_mono(FONT_TACTICAL), text_color=NEON_GREEN, anchor="w",
        )
        self._tactical.pack(fill="x", padx=PAD_CARD, pady=(0, 8))

        self._status = ctk.CTkLabel(
            body, text="◈ OFFLINE",
            font=_mono(FONT_STATUS, bold=True),
            text_color=MAGENTA, fg_color="#1A0A14",
            corner_radius=4, height=32,
        )
        self._status.pack(fill="x", padx=PAD_CARD, pady=(0, 6))

        self._error = ctk.CTkLabel(
            body, text="", font=_mono(FONT_TACTICAL),
            text_color=MAGENTA, wraplength=400, anchor="w",
        )
        self._error.pack(fill="x", padx=PAD_CARD, pady=(0, 8))

        btns = ctk.CTkFrame(body, fg_color="transparent")
        btns.pack(fill="x", padx=PAD_CARD, pady=(0, PAD_CARD))
        for col in range(3):
            btns.grid_columnconfigure(col, weight=1, uniform="a")

        bf = _mono(FONT_BTN, bold=True)
        ctk.CTkButton(
            btns, text="▶ ENGAGE", height=36, font=bf,
            fg_color="#0A1A1A", hover_color="#0D2A2A",
            border_color=CYAN, border_width=1, text_color=CYAN,
            command=lambda: manager.start(config.key),
        ).grid(row=0, column=0, padx=(0, 4), sticky="ew")

        ctk.CTkButton(
            btns, text="■ HALT", height=36, font=bf,
            fg_color="#1A0A10", hover_color="#2A0A18",
            border_color=MAGENTA, border_width=1, text_color=MAGENTA,
            command=lambda: manager.stop(config.key),
        ).grid(row=0, column=1, padx=4, sticky="ew")

        ctk.CTkButton(
            btns, text="↻ REBOOT", height=36, font=bf,
            fg_color="#1A1400", hover_color="#2A2000",
            border_color=AMBER, border_width=1, text_color=AMBER,
            command=lambda: manager.restart(config.key),
        ).grid(row=0, column=2, padx=(4, 0), sticky="ew")

    def set_wraplength(self, width: int) -> None:
        self._error.configure(wraplength=max(120, width - PAD_CARD * 4))

    def refresh(self) -> None:
        state = self.manager.get_state(self.config.key)
        color = HUD_STATE_COLORS[state]
        glow = HUD_STATE_GLOW[state]
        tactical_state = TACTICAL_STATE_TEXT.get(state, "STANDBY")
        if state == ServiceState.RUNNING:
            if self.config.key == "openclaw":
                tactical_state = "ACTIVE"
            elif self.config.key == "uvicorn":
                tactical_state = "LISTEN"

        label_tpl = TACTICAL_LABELS.get(self.config.key, "")
        self._tactical.configure(
            text=label_tpl.format(state=tactical_state),
            text_color=glow if state == ServiceState.RUNNING else color,
        )
        self._status.configure(
            text=f"◈ {state.value}",
            text_color=glow,
            fg_color="#0A1A1A" if state == ServiceState.RUNNING else "#1A0A14",
        )
        self._bracket.set_accent(color, glow)
        err = self.manager.get_error(self.config.key)
        self._error.configure(text=err[:140] if state == ServiceState.ERROR else "")


class CyberpunkHUD(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("Ordun Household AI Server")
        self.configure(fg_color=BG)
        self.resizable(True, True)
        self.minsize(MIN_W, MIN_H)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._resize_job: Optional[str] = None
        self._last_layout_width = 0
        self._cards_frame: Optional[ctk.CTkFrame] = None
        self._main_panel: Optional[ctk.CTkFrame] = None
        self._payload_sidebar: Optional[PayloadSidebar] = None
        self._boot_anim: Optional[BootAnimator] = None
        self._hover_flash = False
        self._app_start = time.time()
        self._creamer_pending_detail = ""
        self._creamer_state_mtime: float = 0.0
        self._uvicorn_log_stop = threading.Event()

        self.manager = ServiceManager(log_callback=self._enqueue_log)
        self._build_ui()
        self._start_uvicorn_log_watcher()
        self._center_window()
        self.bind("<Configure>", self._on_configure)
        self._poll_logs()
        self._poll_states()
        self._poll_telemetry()
        self._poll_memory()
        self._poll_creamer_state()
        self._load_creamer_state_on_start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _center_window(self) -> None:
        self.update_idletasks()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(DEFAULT_W, sw - 40), min(DEFAULT_H, sh - 80)
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 2)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self) -> None:
        self._grid = GridBackground(self)
        self._grid.place(x=0, y=0, relwidth=1, relheight=1)

        root = ctk.CTkFrame(self, fg_color="transparent")
        root.place(x=0, y=0, relwidth=1, relheight=1)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = ctk.CTkFrame(root, fg_color="transparent")
        hdr.pack(fill="x", padx=PAD, pady=(PAD, 8))

        ctk.CTkLabel(
            hdr, text="ORDUN HOUSEHOLD AI SERVER",
            font=_mono(FONT_TITLE, bold=True), text_color=CYAN_BRIGHT,
        ).pack(side="left")

        HeaderGlowLine(hdr).pack(fill="x", pady=(8, 0))

        # ── Body: Payload sidebar (25%) + main deck (75%) ────────────────────
        body = ctk.CTkFrame(root, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD, pady=(0, PAD))
        body.grid_columnconfigure(0, weight=1, uniform="deck")
        body.grid_columnconfigure(1, weight=0)
        body.grid_columnconfigure(2, weight=3, uniform="deck")
        body.grid_rowconfigure(0, weight=1)

        self._payload_sidebar = PayloadSidebar(body, PAYLOAD_MODULES)
        self._payload_sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        MatrixBreakSeparator(body, width=8).grid(row=0, column=1, sticky="ns", padx=2)

        self._main_panel = ctk.CTkFrame(body, fg_color="transparent")
        self._main_panel.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        main = self._main_panel
        main.grid_rowconfigure(0, weight=0)
        main.grid_rowconfigure(1, weight=0)
        main.grid_rowconfigure(2, weight=1, minsize=220)
        main.grid_columnconfigure(0, weight=1)

        # ── Top command row ──────────────────────────────────────────────────
        cmd_row = ctk.CTkFrame(main, fg_color="transparent")
        cmd_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        cmd_row.grid_columnconfigure(0, weight=3)
        cmd_row.grid_columnconfigure(1, weight=0)
        cmd_row.grid_columnconfigure(2, weight=2)

        self._overdrive = ctk.CTkButton(
            cmd_row,
            text="⚡ OVERDRIVE INITIALIZE",
            height=52,
            font=_mono(FONT_OVERDRIVE, bold=True),
            fg_color="#0A1A22",
            hover_color="#103848",
            border_color=CYAN_BRIGHT,
            border_width=2,
            text_color=CYAN_BRIGHT,
            corner_radius=2,
            command=self._overdrive_init,
        )
        self._overdrive.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._overdrive.bind("<Enter>", lambda _: self._set_overdrive_hover(True))
        self._overdrive.bind("<Leave>", lambda _: self._set_overdrive_hover(False))

        anim_wrap = ctk.CTkFrame(cmd_row, fg_color="transparent", width=70)
        anim_wrap.grid(row=0, column=1, padx=6)
        self._boot_anim = BootAnimator(anim_wrap, size=64)
        self._boot_anim.pack()

        telem = ctk.CTkFrame(
            cmd_row, fg_color="#0A1520",
            border_color=CYAN_BRIGHT, border_width=2, corner_radius=2,
        )
        telem.grid(row=0, column=2, sticky="ew")

        telem_inner = ctk.CTkFrame(
            telem, fg_color=CARD_INNER,
            border_color=CYAN, border_width=1, corner_radius=2,
        )
        telem_inner.pack(fill="both", expand=True, padx=3, pady=3)

        ctk.CTkLabel(
            telem_inner, text="TELEMETRY_CAPSULE // READ-ONLY",
            font=_mono(FONT_TACTICAL), text_color=CYAN_MUTED,
        ).pack(anchor="w", padx=12, pady=(8, 2))

        self._uptime_lbl = ctk.CTkLabel(
            telem_inner, text="SYS_UPTIME: --:--:--",
            font=_mono(FONT_TELEM), text_color=NEON_GREEN_BRIGHT, anchor="w",
        )
        self._uptime_lbl.pack(fill="x", padx=12, pady=(0, 4))

        ctk.CTkLabel(
            telem_inner, text="UNIFIED_MEM // TEGRASTATS",
            font=_mono(FONT_TACTICAL), text_color=CYAN_MUTED,
        ).pack(anchor="w", padx=12, pady=(2, 0))

        self._mem_lbl = ctk.CTkLabel(
            telem_inner, text="RAM: — / — MB",
            font=_mono(FONT_TELEM), text_color=CYAN_BRIGHT, anchor="w",
        )
        self._mem_lbl.pack(fill="x", padx=12)

        self._mem_bar = ctk.CTkProgressBar(
            telem_inner, height=10, progress_color=NEON_GREEN_BRIGHT,
            fg_color="#1A1A1A", border_color=CYAN_BRIGHT, border_width=1,
        )
        self._mem_bar.pack(fill="x", padx=12, pady=(2, 2))
        self._mem_bar.set(0)

        self._mem_detail_lbl = ctk.CTkLabel(
            telem_inner, text="LFB: — MB | GR3D: —% | SWAP: —",
            font=_mono(FONT_MATRIX), text_color=TEXT_DIM, anchor="w",
        )
        self._mem_detail_lbl.pack(fill="x", padx=12, pady=(0, 8))

        # ── Tactical deck cards ──────────────────────────────────────────────
        self._cards_frame = ctk.CTkFrame(main, fg_color="transparent")
        self._cards_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        self._cards: list[TacticalCard] = []
        for cfg in SERVICES:
            self._cards.append(TacticalCard(self._cards_frame, cfg, self.manager))

        # ── Kernel log stream ────────────────────────────────────────────────
        self._log_shell = ctk.CTkFrame(
            main, fg_color="#120E00",
            border_color=AMBER_BRIGHT, border_width=2, corner_radius=2,
        )
        self._log_shell.grid(row=2, column=0, sticky="nsew")

        log_shell = self._log_shell

        log_hdr = ctk.CTkFrame(log_shell, fg_color="#1A1400")
        log_hdr.pack(fill="x")

        ctk.CTkLabel(
            log_hdr,
            text=">> HOST_KERNEL_LOG_STREAM // UNAUTHORIZED EXTRAPOLATION MINIMIZED",
            font=_mono(FONT_CONSOLE_HDR, bold=True), text_color=AMBER,
        ).pack(side="left", padx=12, pady=8)

        ctk.CTkButton(
            log_hdr, text="PURGE", width=80, height=28,
            font=_mono(FONT_TACTICAL), fg_color="#2A2000",
            hover_color="#3A3000", text_color=AMBER,
            border_color=AMBER, border_width=1,
            command=self._clear_console,
        ).pack(side="right", padx=12, pady=6)

        self._console = ctk.CTkTextbox(
            log_shell, font=_mono(FONT_CONSOLE), height=220,
            fg_color=CONSOLE_BG, text_color=AMBER,
            wrap="word", activate_scrollbars=True, corner_radius=0,
        )
        self._console.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self._console.configure(state="disabled")

        self._log("system", "Ordun Household AI Server online.")
        self.after(120, self._initial_layout)

    def _set_overdrive_hover(self, active: bool) -> None:
        self._hover_flash = active
        if active:
            self._overdrive.configure(
                border_color=NEON_GREEN_BRIGHT, text_color=NEON_GREEN_BRIGHT,
            )
        else:
            self._overdrive.configure(border_color=CYAN_BRIGHT, text_color=CYAN_BRIGHT)

    def _overdrive_init(self) -> None:
        if self._boot_anim:
            self._boot_anim.start()
        self.manager.wake_all()
        self._log("system", "OVERDRIVE INITIALIZE — master boot sequence triggered.")
        self.after(60000, self._stop_boot_anim)

    def _stop_boot_anim(self) -> None:
        if self._boot_anim:
            self._boot_anim.stop()

    def _initial_layout(self) -> None:
        self._apply_resize(self.winfo_width(), self.winfo_height())

    def _on_configure(self, event) -> None:
        if event.widget is not self:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        w, h = event.width, event.height
        self._resize_job = self.after(80, lambda: self._apply_resize(w, h))

    def _apply_resize(self, width: int, height: int) -> None:
        self._resize_job = None
        if width < 200 or height < 200 or not self._cards_frame:
            return

        main_w = self._main_panel.winfo_width() if self._main_panel else int(width * 0.75)
        sidebar_w = self._payload_sidebar.winfo_width() if self._payload_sidebar else int(width * 0.25)
        if self._payload_sidebar and sidebar_w > 80:
            self._payload_sidebar.set_wraplength(sidebar_w)

        stacked = main_w < CARD_STACK_BREAKPOINT
        layout_changed = (main_w < CARD_STACK_BREAKPOINT) != (self._last_layout_width < CARD_STACK_BREAKPOINT)
        minor = abs(main_w - self._last_layout_width) < 20

        if self._last_layout_width and not layout_changed and minor:
            cw = self._card_width(main_w, stacked)
            for card in self._cards:
                card.set_wraplength(cw)
            return

        self._last_layout_width = main_w
        for card in self._cards:
            card.grid_forget()

        frame = self._cards_frame
        if stacked:
            for row, card in enumerate(self._cards):
                card.grid(row=row, column=0, pady=(0, 10), sticky="ew")
                frame.grid_rowconfigure(row, weight=0)
            frame.grid_columnconfigure(0, weight=1)
        else:
            for col, card in enumerate(self._cards):
                card.grid(row=0, column=col, padx=(0 if col == 0 else 6, 0 if col == len(self._cards) - 1 else 6), sticky="nsew")
                frame.grid_columnconfigure(col, weight=1, uniform="c")
            frame.grid_rowconfigure(0, weight=0)

        cw = self._card_width(main_w, stacked)
        for card in self._cards:
            card.set_wraplength(cw)

    def _card_width(self, panel_width: int, stacked: bool) -> int:
        inner = panel_width - 8
        if stacked:
            return max(200, inner - PAD_CARD * 2)
        gaps = 12 * (len(self._cards) - 1)
        return max(200, (inner - gaps) // len(self._cards))

    def _check_payload_triggers(self, line: str) -> None:
        self._handle_creamer_log(line)
        if not self._payload_sidebar:
            return
        upper = line.upper()
        for mod in PAYLOAD_MODULES:
            if mod.key == "creamer":
                continue
            if not mod.log_triggers or not mod.flash_status:
                continue
            card = self._payload_sidebar.cards.get(mod.key)
            if card and any(trigger.upper() in upper for trigger in mod.log_triggers):
                card.trigger_flash()

    def _creamer_card(self) -> Optional[PayloadModuleCard]:
        if not self._payload_sidebar:
            return None
        return self._payload_sidebar.cards.get("creamer")

    def _apply_creamer_state(self, data: dict) -> None:
        card = self._creamer_card()
        if not card:
            return
        phase = data.get("phase", "")
        ts = data.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S")
        detail = data.get("detail", "")
        success = data.get("success")

        if phase == "dispatching":
            card.set_dispatching(ts)
        elif phase == "success" or success is True:
            card.set_result(True, detail, ts)
        elif phase == "failed" or success is False:
            card.set_result(False, detail, ts)
        elif phase == "inference_ok":
            self._creamer_pending_detail = detail

    def _load_creamer_state_on_start(self) -> None:
        if not CREAMER_STATE_PATH.exists():
            return
        try:
            data = json.loads(CREAMER_STATE_PATH.read_text(encoding="utf-8"))
            self._creamer_state_mtime = CREAMER_STATE_PATH.stat().st_mtime
            self._apply_creamer_state(data)
        except Exception:
            pass

    def _poll_creamer_state(self) -> None:
        if CREAMER_STATE_PATH.exists():
            try:
                mtime = CREAMER_STATE_PATH.stat().st_mtime
                if mtime != self._creamer_state_mtime:
                    self._creamer_state_mtime = mtime
                    data = json.loads(CREAMER_STATE_PATH.read_text(encoding="utf-8"))
                    self._apply_creamer_state(data)
            except Exception:
                pass
        self.after(1000, self._poll_creamer_state)

    def _handle_creamer_log(self, line: str) -> None:
        card = self._creamer_card()
        if not card:
            return
        ts = time.strftime("%Y-%m-%d %H:%M:%S")

        if "NFC Event" in line or "Creamer trigger" in line:
            card.set_dispatching(ts)
            self._creamer_pending_detail = ""
            return

        if "Qwen Generation:" in line:
            self._creamer_pending_detail = line.split("Qwen Generation:", 1)[-1].strip()
            return

        if "OpenClaw dispatch OK" in line:
            detail = self._creamer_pending_detail or line
            card.set_result(True, detail, ts)
            return

        failure_markers = (
            "[ERROR]",
            "OpenClaw send failed",
            "Inference bridge failure",
            "No message was sent to my whatsapp group",
            "Ollama inference failed",
            "Ollama HTTP",
            "Failed to reach local Ollama",
        )
        if any(marker in line for marker in failure_markers):
            card.set_result(False, line.strip(), ts)
            return

        if "/api/trigger-claw" in line and "502" in line:
            card.set_result(False, "HTTP 502 — pipeline failed", ts)
        elif "/api/trigger-claw" in line and "503" in line:
            card.set_result(False, "HTTP 503 — OpenClaw unreachable", ts)

    def _start_uvicorn_log_watcher(self) -> None:
        """Tail creamer/uvicorn log file — works even when uvicorn wasn't spawned here."""
        log_path = CREAMER_LOG_PATH

        def _watch() -> None:
            while not self._uvicorn_log_stop.is_set():
                if not log_path.exists():
                    time.sleep(2.0)
                    continue
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as fh:
                        fh.seek(0, 2)
                        while not self._uvicorn_log_stop.is_set():
                            line = fh.readline()
                            if not line:
                                time.sleep(0.4)
                                continue
                            text = line.rstrip()
                            if text:
                                self._enqueue_log("creamer", text)
                except Exception:
                    time.sleep(2.0)

        threading.Thread(target=_watch, name="creamer-log-watcher", daemon=True).start()

    def _enqueue_log(self, source: str, message: str) -> None:
        ts = time.strftime("%H:%M:%S")
        tag = source.upper() if source != "system" else "SYS"
        line = f"> [{ts}] [{tag}] {message}"
        self._log_queue.put(line)
        self._check_payload_triggers(message)

    def _log(self, source: str, message: str) -> None:
        self._enqueue_log(source, message)

    def _poll_logs(self) -> None:
        try:
            while True:
                line = self._log_queue.get_nowait()
                self._console.configure(state="normal")
                self._console.insert("end", line + "\n")
                self._console.see("end")
                self._console.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self._poll_logs)

    def _poll_states(self) -> None:
        for card in self._cards:
            card.refresh()
        launching = any(
            self.manager.get_state(c.config.key) == ServiceState.LAUNCHING
            for c in self._cards
        )
        if launching and self._boot_anim and not self._boot_anim._active:
            self._boot_anim.start()
        elif not launching and self._boot_anim and self._boot_anim._active:
            all_running = all(
                self.manager.get_state(c.config.key) == ServiceState.RUNNING
                for c in self._cards
            )
            if all_running:
                self._boot_anim.stop()
        self.after(600, self._poll_states)

    def _update_memory_display(self, mem: TegraMemorySample) -> None:
        pct = mem.ram_used_mb / mem.ram_total_mb if mem.ram_total_mb else 0
        self._mem_bar.set(min(1.0, max(0.0, pct)))
        bar_color = NEON_GREEN
        if pct > 0.85 or mem.lfb_mb < 128:
            bar_color = "#FF3366"
        elif pct > 0.70:
            bar_color = AMBER
        self._mem_bar.configure(progress_color=bar_color)
        self._mem_lbl.configure(
            text=f"RAM: {mem.ram_used_mb}/{mem.ram_total_mb} MB ({pct * 100:.0f}%)",
            text_color=bar_color,
        )
        self._mem_detail_lbl.configure(
            text=(
                f"LFB: {mem.lfb_mb} MB contiguous | GR3D: {mem.gr3d_pct}% | "
                f"SWAP: {mem.swap_used_mb}/{mem.swap_total_mb} MB"
            ),
            text_color=TEXT_DIM if mem.lfb_mb >= 128 else AMBER,
        )

    def _poll_telemetry(self) -> None:
        uptime = read_system_uptime()
        self._uptime_lbl.configure(text=f"SYS_UPTIME: {uptime}")
        self.after(1000, self._poll_telemetry)

    def _poll_memory(self) -> None:
        mem = read_tegrastats_sample()
        if mem:
            self._update_memory_display(mem)
        self.after(2500, self._poll_memory)

    def _clear_console(self) -> None:
        self._console.configure(state="normal")
        self._console.delete("1.0", "end")
        self._console.configure(state="disabled")

    def _on_close(self) -> None:
        if self._boot_anim:
            self._boot_anim.stop()
        self._uvicorn_log_stop.set()
        self.manager.shutdown()
        self.destroy()


def main() -> None:
    app = CyberpunkHUD()
    app.mainloop()


if __name__ == "__main__":
    main()
