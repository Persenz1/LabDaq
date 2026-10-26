"""Qt-side control and bounded display cache for the independent Rust process.

The background poller belongs to the GUI. Stopping it never stops acquisition.
All acquisition and recording state is owned by the Rust executable.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time

import numpy as np

from .protocol import AcquisitionConfig, plan_clock

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENDPOINT = ROOT / ".runtime" / "core.json"
_CORE_NAME = "labdaq-core.exe" if os.name == "nt" else "labdaq-core"
CORE_EXE = ROOT / "target" / "release" / _CORE_NAME
if not CORE_EXE.is_file():
    CORE_EXE = ROOT / "core" / "target" / "release" / _CORE_NAME


class CoreClient:
    def __init__(self, endpoint=DEFAULT_ENDPOINT):
        self.endpoint = Path(endpoint)
        self.address = json.loads(self.endpoint.read_text(encoding="utf-8"))

    def request(self, op, **fields):
        with socket.create_connection(("127.0.0.1", self.address["port"]), timeout=3) as connection:
            connection.settimeout(30 if op in ("start", "connect") else 5)
            request = {"token": self.address["token"], "op": op, **fields}
            connection.sendall(json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")
            with connection.makefile("rb") as reader:
                response = json.loads(reader.readline(32 * 1024 * 1024))
        if not response["ok"]:
            raise RuntimeError(response["error"])
        return response["state"]

    @classmethod
    def existing(cls, endpoint=DEFAULT_ENDPOINT):
        try:
            client = cls(endpoint)
            return client, client.request("poll")
        except (FileNotFoundError, ConnectionRefusedError):
            return None

    @classmethod
    def connect_or_start(cls, endpoint=DEFAULT_ENDPOINT, port=47638):
        existing = cls.existing(endpoint)
        if existing:
            return existing
        if not CORE_EXE.is_file():
            raise FileNotFoundError("请先执行 cargo build --release --manifest-path core/Cargo.toml 构建采集核心")
        endpoint = Path(endpoint).resolve()
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
        process = subprocess.Popen([str(CORE_EXE), str(endpoint), str(port), secrets.token_hex(32)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=flags, start_new_session=os.name != "nt", cwd=ROOT)
        deadline = time.monotonic() + 5
        # Readiness wait only: never resend a recording/control command.
        while time.monotonic() < deadline:
            try:
                client = cls(endpoint)
            except (FileNotFoundError, PermissionError):
                client = None
            # A restarted core can bind the same port before it replaces the
            # old endpoint file. Do not send the previous process's token.
            if client and client.address["pid"] == process.pid:
                return client, client.request("poll")
            if process.poll() is not None:
                raise RuntimeError(f"采集核心启动失败（退出码 {process.returncode}），检查端口 {port} 是否已占用")
            time.sleep(.025)
        raise TimeoutError("采集核心未在 5 秒内就绪")


def enumerate_vendor_devices(endpoint=DEFAULT_ENDPOINT):
    client, _ = CoreClient.connect_or_start(endpoint)
    return client.request("list_devices")["devices"]


class RemoteEngine:
    transport = None

    def __init__(self, client: CoreClient, state: dict):
        self.client = client
        self._state = state
        self._times = np.empty(0)
        self._raw = np.empty((0, len(state["profile"]["config"]["channels"])))
        self._snapshot_end = None
        self.display_seconds = 8.0
        self.display_revision = 0
        self.connection_error = ""
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="labdaq-display-poll", daemon=True)
        self._thread.start()

    @classmethod
    def start_new(cls, config, channels, signal_settings, experiment, recording_root,
                  recording_path=None, *, endpoint=DEFAULT_ENDPOINT, source="simulation", connect_only=False,
                  recording_options=None):
        client, state = CoreClient.connect_or_start(endpoint)
        if state["running"]:
            raise RuntimeError("核心已有采集会话，请重新打开界面接回，不能覆盖当前实验")
        profile = {"source": "vendor" if source == "usb" else source,
                   "config": asdict(config), "actual_rate": plan_clock(config.sample_rate).actual_rate,
                   "channels": [asdict(c) for c in channels], "signal_settings": signal_settings.to_dict(),
                   "experiment": experiment, "recording_root": str(Path(recording_root).resolve()),
                   "recording_options": recording_options or {}}
        if connect_only:
            state = client.request("connect", profile=profile)
        else:
            state = client.request("start", profile=profile,
                                   recording_path=str(Path(recording_path).resolve()) if recording_path else None)
        return cls(client, state)

    @classmethod
    def reconnect(cls, endpoint=DEFAULT_ENDPOINT):
        existing = CoreClient.existing(endpoint)
        if existing and existing[1]["profile"]:
            return cls(*existing)
        return None

    def _call(self, op, **fields):
        with self._request_lock:
            state = self.client.request(op, **fields)
            with self._lock:
                self._state = state
                self.connection_error = ""
                if op == "poll":
                    width = len(state["profile"]["config"]["channels"])
                    incoming = np.asarray(state["raw"], dtype=np.float64).reshape(-1, width)
                    if state["snapshot_start"] == self._snapshot_end:
                        if len(incoming):
                            limit = min(state["capacity"], max(1, round(fields["seconds"] * self.sample_rate)))
                            self._raw = np.concatenate((self._raw, incoming))[-limit:]
                    else:
                        self._raw = incoming
                    if len(incoming) or state["snapshot_start"] != self._snapshot_end:
                        self._times = (state["total_scans"] - len(self._raw)
                                       + np.arange(len(self._raw))) / self.sample_rate
                        self.display_revision += 1
                    self._snapshot_end = state["total_scans"]
            return state

    def _poll(self):
        previous_seconds = None
        while not self._closed.is_set():
            started = time.monotonic()
            try:
                seconds = self.display_seconds
                self._call("poll", seconds=seconds,
                           after_scan=self._snapshot_end if seconds == previous_seconds else None)
                previous_seconds = seconds
            except (OSError, ValueError, RuntimeError) as exc:
                self.connection_error = f"核心连接中断，采集状态未知：{exc}"
            interval = 1 / 30 if self.running else .1
            self._closed.wait(max(0, interval - (time.monotonic() - started))
                              if not self.connection_error else 1)

    @property
    def source(self):
        source = self.profile.get("source", "simulation")
        return "usb" if source == "vendor" else source

    @property
    def profile(self):
        return self._state["profile"]

    @property
    def config(self):
        return AcquisitionConfig(**self.profile["config"])

    @property
    def sample_rate(self):
        return self.profile["actual_rate"]

    @property
    def running(self):
        return self._state["running"]

    @property
    def device_connected(self):
        return self._state["device_connected"]

    @property
    def output(self):
        return self._state.get("output") or {}

    @property
    def recording(self):
        return self._state["recording"]

    @property
    def recording_paused(self):
        return self._state["recording_paused"]

    @property
    def acquisition_started_at_utc(self):
        return self._state["acquisition_started_at_utc"]

    @property
    def total_scans(self):
        return self._state["total_scans"]

    @property
    def recorded_scans(self):
        return self._state["recorded_scans"]

    @property
    def recording_target_scans(self):
        return self._state["recording_target_scans"]

    @property
    def recording_remaining_scans(self):
        return self._state["recording_remaining_scans"]

    @property
    def termination_reason(self):
        return self._state["termination_reason"]

    @property
    def error(self):
        return self._state["error"]

    @property
    def last_recording(self):
        value = self._state["last_recording"]
        return Path(value) if value else None

    def snapshot(self, seconds=8):
        with self._lock:
            count = max(1, round(seconds * self.sample_rate))
            return self._times[-count:].copy(), self._raw[-count:].copy()

    def start_recording(self, path, channels, experiment="", *, recording_options=None):
        # The GUI supplies all four physical channels; the core selects its map.
        self._call("start_recording", path=str(Path(path).resolve()),
                   channels=[asdict(c) for c in channels], experiment=experiment,
                   recording_options=recording_options or {})

    def set_channels(self, channels):
        self._call("set_channels", channels=[asdict(c) for c in channels])

    def stop_recording(self):
        self._call("stop_recording")

    def pause_recording(self):
        self._call("pause_recording")

    def resume_recording(self):
        self._call("resume_recording")

    def stop(self):
        self._call("stop")

    def start_output(self, settings):
        self._call("start_output", settings=settings)

    def stop_output(self):
        self._call("stop_output")

    def disconnect(self):
        self._call("disconnect")

    def detach(self):
        self._closed.set()
        self._thread.join(timeout=8.5)

    def shutdown(self):
        self._call("shutdown")
        self.detach()

    def shutdown_if_idle(self):
        self._call("shutdown_if_idle")
