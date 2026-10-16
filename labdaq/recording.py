"""Streaming raw-voltage sessions with human-readable acquisition metadata."""

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .processing import ChannelSettings, convert_volts


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class SessionRecorder:
    """Write a new session directory without buffering the complete acquisition.

    raw.f64: little-endian float64 V, sample-major, selected-channel order.
    metadata.json: actual sample rate, channel conversion snapshot and origin.
    ``path`` must be a new directory to avoid overwriting existing recordings.
    """

    def __init__(self, path, sample_rate, channels, source="simulation", acquisition_config=None):
        if not channels or not np.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("记录需要至少一个通道和有效采样率")
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=False)
        self.channel_count = len(channels)
        self.sample_count = 0
        self.metadata = {
            "format": "labdaq-raw-voltage",
            "format_version": 1,
            "dtype": "<f8",
            "layout": "sample-major",
            "voltage_unit": "V",
            "sample_rate": float(sample_rate),
            "channels": [asdict(channel) for channel in channels],
            "source": source,
            "acquisition_config": acquisition_config or {},
            "started_at_utc": _utc_now(),
            "sample_count": 0,
            "finalized": False,
        }
        self._write_metadata()
        self._file = (self.path / "raw.f64").open("wb")

    def _write_metadata(self):
        (self.path / "metadata.json").write_text(
            json.dumps(self.metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    def write(self, volts):
        if self._file.closed:
            raise ValueError("记录已结束")
        block = np.asarray(volts, dtype="<f8")
        if block.ndim != 2 or block.shape[1] != self.channel_count:
            raise ValueError("记录输入必须是 (采样点数, 已选通道数)")
        self._file.write(block.tobytes(order="C"))
        self.sample_count += len(block)
        # Keep data visible to the replay reader and limit data held in Python's
        # buffer on an interrupted process; OS power-loss persistence is separate.
        self._file.flush()

    def close(self, *, termination_reason="user_stop", error=None):
        """Finalize once; reason distinguishes user stop from interrupted capture.

        ``finalized`` means the file was closed with a recorded sample count,
        not that acquisition completed without errors. Repeated close calls do
        not overwrite the original reason or error.
        """
        if not self._file.closed:
            self._file.close()
            self.metadata.update(sample_count=self.sample_count, finalized=True,
                                 stopped_at_utc=_utc_now(),
                                 termination_reason=termination_reason,
                                 error=error)
            self._write_metadata()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


@dataclass
class RecordedSession:
    path: Path
    metadata: dict

    @property
    def channels(self):
        return [ChannelSettings(**entry) for entry in self.metadata["channels"]]

    @property
    def sample_rate(self):
        return self.metadata["sample_rate"]

    @property
    def sample_count(self):
        return self.metadata["sample_count"]

    @property
    def duration(self):
        return self.sample_count / self.sample_rate

    def read(self, start=0, count=None):
        """Read a finite sample window using seek; None reads to the session end.

        Interactive replay should always pass count to keep memory bounded.
        Reads extending beyond the session end return the available samples.
        """
        if start < 0 or (count is not None and count < 0):
            raise ValueError("回放起点和采样点数不能为负数")
        available = max(0, self.sample_count - start)
        count = available if count is None else min(count, available)
        channel_count = len(self.metadata["channels"])
        with (self.path / "raw.f64").open("rb") as handle:
            handle.seek(start * channel_count * 8)
            data = np.fromfile(handle, dtype="<f8", count=count * channel_count)
        return data.reshape(-1, channel_count)

    def iter_chunks(self, chunk_size=4096):
        """Yield raw-voltage arrays; the final chunk may be shorter than requested."""
        if chunk_size < 1:
            raise ValueError("分块长度必须大于零")
        channel_count = len(self.metadata["channels"])
        with (self.path / "raw.f64").open("rb") as handle:
            while True:
                data = np.fromfile(handle, dtype="<f8", count=chunk_size * channel_count)
                if data.size == 0:
                    break
                yield data.reshape(-1, channel_count)


def load_session(path):
    """Open metadata and inspect raw length, without reading all voltage data.

    A process interrupted before close may still have valid whole frames. In
    that case the available file length supplies sample_count; finalized stays
    false. Partial frames are reported, never silently truncated.
    """
    path = Path(path)
    metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("format") != "labdaq-raw-voltage" or metadata.get("format_version") != 1:
        raise ValueError("不支持的 LabDAQ 记录格式")
    if metadata.get("dtype") != "<f8" or metadata.get("layout") != "sample-major":
        raise ValueError("记录的数值类型或排列方式不受支持")
    frame_bytes = 8 * len(metadata["channels"])
    sample_count, remainder = divmod((path / "raw.f64").stat().st_size, frame_bytes)
    if remainder:
        raise ValueError("原始记录末尾不完整，存在不足一个采样帧的数据")
    if metadata["finalized"] and sample_count != metadata["sample_count"]:
        raise ValueError("原始记录长度与已结束会话的样本数不一致")
    metadata["sample_count"] = sample_count
    return RecordedSession(path=path, metadata=metadata)


def export_csv(session_path, output_path, chunk_size=4096):
    """Export time, raw V and engineering values without loading the full file.

    The first line is '# labdaq_metadata: {JSON}', preserving source, timing,
    acquisition settings and conversion coefficients in one portable file.
    For pandas use read_csv(path, comment='#', encoding='utf-8-sig').
    """
    session = load_session(session_path)
    channels = session.channels
    output_path = Path(output_path)
    if output_path.resolve() in {(session.path / "raw.f64").resolve(),
                                 (session.path / "metadata.json").resolve()}:
        raise ValueError("CSV 输出不能覆盖会话原始数据或元数据")
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("# labdaq_metadata: " + json.dumps(session.metadata, ensure_ascii=False) + "\n")
        writer = csv.writer(handle)
        writer.writerow(["time_s"] + [f"{ch.name}[AI{ch.index}]_raw_V" for ch in channels]
                        + [f"{ch.name}[AI{ch.index}]_{ch.unit}" for ch in channels])
        start = 0
        for volts in session.iter_chunks(chunk_size):
            engineering = convert_volts(volts, channels)
            for offset, (raw_row, eng_row) in enumerate(zip(volts, engineering)):
                writer.writerow([(start + offset) / session.sample_rate, *raw_row, *eng_row])
            start += len(volts)
    return output_path
