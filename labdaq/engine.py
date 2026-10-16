"""Acquisition worker: raw recording is independent of GUI refresh.

Inputs are a protocol simulator or a paced, read-only HPF replay session.
The bounded display buffer can roll over without losing recorded samples.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import threading
import time

import numpy as np

from .protocol import AcquisitionConfig
from .processing import ChannelSettings
from .recording import SessionRecorder
from .simulator import SimulatedTransport
from .transport import DaqSession, TransportError


class AcquisitionEngine:
    def __init__(self, config: AcquisitionConfig, channels: list[ChannelSettings],
                 scenario: str = "drag", block_seconds: float = 0.05, *,
                 signal_settings=None, input_session=None):
        self.config = config
        self.channels = [replace(c) for c in channels]
        self.transport = (SimulatedTransport(seed=2026, scenario=scenario, signal_settings=signal_settings)
                          if input_session is None else None)
        self.session = DaqSession(self.transport) if input_session is None else input_session
        self.sample_rate = self.session.configure(config)
        self.source = getattr(self.session, "source", "simulation")
        self.input_metadata = (dict(self.session.input_metadata) if input_session is not None else
                               {"scenario": scenario, "seed": self.transport.seed,
                                "signal_settings": asdict(signal_settings) if signal_settings else None})
        self.block_size = max(1, round(self.sample_rate * block_seconds))
        self.capacity = min(200_000, max(self.block_size, round(self.sample_rate * 12)))
        self._ring = np.empty((self.capacity, len(config.channels)), dtype=np.float64)
        self._position = 0
        self.total_scans = 0
        self._input_scans = 0
        self.error = ""
        self.termination_reason = "user_stop"
        self.running = False
        self.last_recording: Path | None = None
        self.recorded_scans = 0
        self._recorder: SessionRecorder | None = None
        self._lock = threading.Lock()
        self._record_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def recording(self) -> bool:
        with self._record_lock:
            return self._recorder is not None

    def start(self, recording_path: Path | None = None) -> None:
        if self.running:
            return
        self.session.start()
        self.running = True
        if recording_path is not None:
            try:
                # Attach before the first read so a direct record includes sample 0.
                self.start_recording(recording_path, self.channels)
            except Exception:
                self.running = False
                self.session.close()
                raise
        self._thread = threading.Thread(target=self._run, name="labdaq-acquisition", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        deadline = time.monotonic()
        try:
            while not self._stop.is_set():
                volts = self.session.read(self.block_size)
                if not len(volts):
                    self.termination_reason = "end_of_file"
                    break
                with self._record_lock:
                    if self._recorder is not None:
                        self._recorder.write(volts)
                        self.recorded_scans += len(volts)
                    self._input_scans += len(volts)
                with self._lock:
                    block = volts[-self.capacity:]
                    n = len(block)
                    first = min(n, self.capacity - self._position)
                    self._ring[self._position:self._position + first] = block[:first]
                    self._ring[:n - first] = block[first:]
                    self._position = (self._position + n) % self.capacity
                    self.total_scans += len(volts)
                deadline += len(volts) / self.sample_rate
                self._stop.wait(max(0, deadline - time.monotonic()))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.termination_reason = "error"
        finally:
            # Close the state before taking the recorder lock: a UI action
            # must not create a new recording after this worker has exited.
            self.running = False
            try:
                self.session.close()
            except Exception as exc:
                self.error = self.error or f"{type(exc).__name__}: {exc}"
            try:
                self.stop_recording(reason=self.termination_reason)
            except Exception as exc:
                self.error = self.error or f"保存未完成：{exc}"
            self.running = False

    def snapshot(self, seconds: float = 8) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            n = min(self.total_scans, self.capacity, max(1, round(seconds * self.sample_rate)))
            start = (self._position - n) % self.capacity
            if start + n <= self.capacity:
                raw = self._ring[start:start + n].copy()
            else:
                raw = np.concatenate((self._ring[start:], self._ring[:(start + n) % self.capacity]))
            end_scan = self.total_scans
        times = np.arange(end_scan - n, end_scan, dtype=np.float64) / self.sample_rate
        return times, raw

    def start_recording(self, path: Path, channels: list[ChannelSettings]) -> None:
        with self._record_lock:
            if not self.running or self._stop.is_set():
                raise RuntimeError("请先开始预览")
            if self._recorder is not None:
                raise RuntimeError("正在记录")
            self._recorder = SessionRecorder(
                path, self.sample_rate, [replace(c) for c in channels], source=self.source,
                acquisition_config={**asdict(self.config), "sample_rate": self.sample_rate,
                                    "input": self.input_metadata,
                                    "record_start_scan": self._input_scans},
            )
            self.last_recording = Path(path)
            self.recorded_scans = 0

    def stop_recording(self, *, reason="user_stop") -> None:
        with self._record_lock:
            if self._recorder is not None:
                recorder, self._recorder = self._recorder, None
                recorder.close(termination_reason="error" if self.error else reason, error=self.error or None)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise RuntimeError("采集线程尚未停止")
        self.stop_recording()
        try:
            self.session.close()
        except TransportError as exc:
            # A disconnected device cannot acknowledge shutdown. The session
            # still releases its local transport in its finally block.
            self.error = self.error or f"{type(exc).__name__}: {exc}"
        self.running = False
