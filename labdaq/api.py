"""Local Python API and JSON CLI for the same acquisition core as the GUI."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys

from .core_client import CoreClient, DEFAULT_ENDPOINT
from .paths import RECORDING_ROOT
from .processing import ChannelSettings
from .protocol import AcquisitionConfig, plan_clock
from .signals import SimulationSettings


class LabDAQ:
    """Stateless control client. Client exit never stops acquisition or AO."""

    def __init__(self, endpoint=DEFAULT_ENDPOINT):
        self.endpoint = Path(endpoint)
        self._can_start_core = self.endpoint.resolve() == Path(DEFAULT_ENDPOINT).resolve()

    def _request(self, operation, *, start_core=False, **fields):
        try:
            existing = (CoreClient.connect_or_start(self.endpoint) if start_core and self._can_start_core
                        else CoreClient.existing(self.endpoint))
            if existing is None:
                raise RuntimeError("采集核心未运行，请先连接设备或开始采集")
            return existing[0].request(operation, **fields)
        except TimeoutError as exc:
            raise RuntimeError("调用超时，执行结果未知；请先查询 status，不要直接重发控制命令") from exc

    def status(self):
        """Query without starting a core, acquisition, recording or output."""
        existing = CoreClient.existing(self.endpoint)
        return {"core_running": False} if existing is None else {"core_running": True, **existing[1]}

    def devices(self):
        return self._request("list_devices", start_core=True, source="vendor")["devices"]

    @staticmethod
    def _default_profile():
        config = AcquisitionConfig(sample_rate=5000)
        return {"source": "vendor", "config": asdict(config),
                "actual_rate": plan_clock(config.sample_rate).actual_rate,
                "channels": [asdict(ChannelSettings(i, f"通道{i + 1}", unit="V",
                                                    sensitivity_mv_per_unit=0)) for i in range(4)],
                "signal_settings": SimulationSettings().to_dict(), "experiment": "",
                "recording_root": str(RECORDING_ROOT.resolve()), "recording_options": {}}

    def connect(self):
        state = self._request("poll", start_core=True)
        if state["running"] or state["device_connected"]:
            return state
        return self._request("connect", profile=self._default_profile())

    def start_acquisition(self, sample_rate=None, channels=None, *, channel_settings=None,
                          ranges=None, coupling=None, iepe=None):
        state = self._request("poll", start_core=True)
        if state["running"]:
            raise RuntimeError("核心正在采集，请接回当前会话，或明确停止后重新开始")
        profile = (deepcopy(state["profile"]) if state["profile"]
                   and state["profile"]["source"] == "vendor" else self._default_profile())
        config = profile["config"]
        changes = {"sample_rate": sample_rate, "channels": channels, "ranges": ranges,
                   "coupling": coupling, "iepe": iepe}
        config.update({key: value for key, value in changes.items() if value is not None})
        profile["config"] = asdict(AcquisitionConfig(**config))
        profile["actual_rate"] = plan_clock(config["sample_rate"]).actual_rate
        if channel_settings is not None:
            profile["channels"] = [asdict(c) if isinstance(c, ChannelSettings) else dict(c)
                                   for c in channel_settings]
        return self._request("start", profile=profile, recording_path=None)

    def read(self, seconds=1.0):
        """Latest bounded display snapshot, not a lossless streaming reader."""
        if not math.isfinite(seconds) or not 0 < seconds <= 60:
            raise ValueError("读取窗口需大于 0 且不超过 60 秒，另受核心 200,000 帧缓存限制")
        state = self._request("poll", seconds=seconds)
        if state["profile"] is None:
            raise RuntimeError("尚无采集配置，请先开始采集")
        indices = state["profile"]["config"]["channels"]
        raw = state["raw"]
        width = len(indices)
        return {"sample_rate": state["profile"]["actual_rate"],
                "start_scan": state["snapshot_start"], "channel_indices": indices,
                "volts": [raw[i:i + width] for i in range(0, len(raw), width)]}

    def start_recording(self, path, duration_seconds=None, *, experiment=None):
        state = self._request("poll")
        if not state["running"]:
            raise RuntimeError("请先开始采集，再开始记录")
        profile = state["profile"]
        options = {**profile["recording_options"], "duration_seconds": duration_seconds}
        return self._request("start_recording", path=str(Path(path).resolve()),
                             channels=profile["channels"],
                             experiment=profile["experiment"] if experiment is None else experiment,
                             recording_options=options)

    def pause_recording(self):
        return self._request("pause_recording")

    def resume_recording(self):
        return self._request("resume_recording")

    def stop_recording(self):
        return self._request("stop_recording")

    def start_output(self, waveform="sine", amplitude_v=0.2, offset_v=0.0,
                     frequency_hz=10.0, sample_rate=48000.0):
        if waveform not in ("dc", "sine", "square", "triangle"):
            raise ValueError("AO 支持 dc、sine、square、triangle")
        if sample_rate not in (10000, 20000, 48000, 96000):
            raise ValueError("AO 更新率需为 10000、20000、48000 或 96000 S/s")
        return self._request("start_output", settings=dict(waveform=waveform, amplitude_v=amplitude_v,
                             offset_v=offset_v, frequency_hz=frequency_hz, sample_rate=sample_rate))

    def stop_output(self):
        return self._request("stop_output")

    def stop_acquisition(self):
        return self._request("stop")

    def disconnect(self):
        return self._request("disconnect")

    def export(self, recording_path, output_path, *, values="both"):
        from .recording import export_csv, export_txt
        output_path = Path(output_path).resolve()
        if output_path.suffix.lower() not in (".csv", ".txt"):
            raise ValueError("导出文件需使用 .csv 或 .txt 扩展名")
        export = export_txt if output_path.suffix.lower() == ".txt" else export_csv
        return str(export(recording_path, output_path, values=values, overwrite=False))


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = _Parser(description="LabDAQ 本机 Agent API；控制操作与界面共用同一核心")
    parser.add_argument("--endpoint", type=Path, default=DEFAULT_ENDPOINT,
                        help="指定核心端点；默认使用当前源码或安装版对应的位置")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "devices", "connect", "pause-recording", "resume-recording",
                    "stop-recording", "stop-output", "stop-acquisition", "disconnect"):
        commands.add_parser(command)
    start = commands.add_parser("start-acquisition")
    start.add_argument("--sample-rate", type=float)
    start.add_argument("--channels", type=int, nargs="+", help="物理通道编号 0–3，升序且不重复")
    read = commands.add_parser("read")
    read.add_argument("--seconds", type=float, default=1.0)
    record = commands.add_parser("start-recording")
    record.add_argument("path", type=Path)
    record.add_argument("--duration-seconds", type=float)
    record.add_argument("--experiment")
    output = commands.add_parser("start-output")
    output.add_argument("--waveform", choices=("dc", "sine", "square", "triangle"), default="sine")
    for flag, value in (("amplitude-v", .2), ("offset-v", 0), ("frequency-hz", 10), ("sample-rate", 48000)):
        output.add_argument("--" + flag, type=float, default=value)
    export = commands.add_parser("export")
    export.add_argument("recording_path", type=Path)
    export.add_argument("output_path", type=Path)
    export.add_argument("--values", choices=("raw", "engineering", "both"), default="both")
    try:
        args = vars(parser.parse_args(argv))
        client = LabDAQ(args.pop("endpoint"))
        method = args.pop("command").replace("-", "_")
        result = getattr(client, method)(**args)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
