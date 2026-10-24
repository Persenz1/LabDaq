"""Streaming raw-voltage sessions with human-readable acquisition metadata."""

import csv
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from numbers import Integral, Real
from os import PathLike
from pathlib import Path

import numpy as np

from .processing import ChannelSettings, convert_volts


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class OperationCancelled(Exception):
    pass


def has_acquisition_error(metadata):
    return bool(metadata.get("error") or metadata.get("termination_reason") in ("error", "acquisition_error"))


def _task_progress(progress, cancelled, completed, total):
    if cancelled is not None and cancelled():
        raise OperationCancelled("操作已取消")
    if progress is not None:
        progress(completed, total)


def next_recording_path(root, experiment, *, now=None) -> tuple[Path, int]:
    """Choose the next daily experiment group without creating a directory."""
    root = Path(root)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", experiment).strip(" .") or "实验"
    prefix = f"{(now or datetime.now()):%Y%m%d}_{name}_"
    group = 1
    if root.exists():
        for entry in root.iterdir():
            if entry.name.startswith(prefix):
                suffix = entry.name[len(prefix):]
                if suffix.isascii() and suffix.isdigit():
                    group = max(group, int(suffix) + 1)
    return root / f"{prefix}{group:03d}", group


class SessionRecorder:
    """Write a new session directory without buffering the complete acquisition.

    raw.f64: little-endian float64 V, sample-major, selected-channel order.
    metadata.json: actual sample rate, channel conversion snapshot and origin.
    ``path`` must be a new directory to avoid overwriting existing recordings.
    """

    def __init__(self, path, sample_rate, channels, source="simulation", acquisition_config=None,
                 *, recording_options=None, experiment=""):
        if not channels or not np.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("记录需要至少一个通道和有效采样率")
        options = dict(recording_options or {})
        duration = options.get("duration_seconds")
        notes = options.get("notes", "")
        group = options.get("group_number", 0)
        if not isinstance(notes, str) or not isinstance(experiment, str):
            raise ValueError("实验名称和记录备注必须为文本")
        if isinstance(group, bool) or not isinstance(group, Integral) or group < 0:
            raise ValueError("记录组号必须为非负整数")
        target = None
        if duration is not None:
            if (isinstance(duration, bool) or not isinstance(duration, Real)
                    or not math.isfinite(duration) or duration <= 0):
                raise ValueError("记录时长必须为正的有限秒数")
            samples = sample_rate * duration
            if not math.isfinite(samples) or samples > (2**63 - 1) // (8 * len(channels)):
                raise ValueError("记录时长过大")
            target = max(1, math.floor(samples + 0.5))
            duration = float(duration)
        options = {"duration_seconds": duration, "notes": notes, "group_number": int(group)}
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
            "recording_options": options,
            "target_sample_count": target,
            "experiment": experiment,
            "started_at_utc": _utc_now(),
            "acquisition_started_at_utc": (acquisition_config or {}).get("acquisition_started_at_utc"),
            "sample_count": 0,
            "recording_segments": [],
            "recording_paused": False,
            "active_segment": {"file_start_scan": 0,
                               "source_start_scan": (acquisition_config or {}).get("record_start_scan", 0)},
            "finalized": False,
        }
        self._write_metadata()
        self._file = (self.path / "raw.f64").open("wb")

    def _write_metadata(self):
        temporary = self.path / "metadata.json.tmp"
        temporary.write_text(
            json.dumps(self.metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path / "metadata.json")

    def write(self, volts, source_start_scan=None):
        if self._file.closed:
            raise ValueError("记录已结束")
        block = np.asarray(volts, dtype="<f8")
        if block.ndim != 2 or block.shape[1] != self.channel_count:
            raise ValueError("记录输入必须是 (采样点数, 已选通道数)")
        segments = self.metadata["recording_segments"]
        previous_end = (segments[-1]["source_start_scan"] + segments[-1]["sample_count"]
                        if segments else self.metadata["acquisition_config"].get("record_start_scan", 0))
        if source_start_scan is None:
            source_start_scan = previous_end
        if (isinstance(source_start_scan, bool) or not isinstance(source_start_scan, Integral)
                or source_start_scan < previous_end):
            raise ValueError("记录源采样点必须是连续或向前跳转的非负整数")
        if not len(block):
            return
        if source_start_scan != previous_end:
            self.metadata["active_segment"] = {"file_start_scan": self.sample_count,
                                                "source_start_scan": int(source_start_scan)}
            self.metadata["sample_count"] = self.sample_count
            self._write_metadata()
        self._file.write(block.tobytes(order="C"))
        active = self.metadata["active_segment"]
        if (segments and previous_end == source_start_scan
                and (active is None or active["file_start_scan"] != self.sample_count)):
            segments[-1]["sample_count"] += len(block)
        else:
            segments.append({"file_start_scan": self.sample_count,
                             "source_start_scan": int(source_start_scan), "sample_count": len(block)})
        self.sample_count += len(block)
        # Keep data visible to the replay reader and limit data held in Python's
        # buffer on an interrupted process; OS power-loss persistence is separate.
        self._file.flush()
        self.metadata["sample_count"] = self.sample_count

    def set_paused(self, paused, source_start_scan=None):
        self.metadata["recording_paused"] = bool(paused)
        if paused:
            self.metadata["active_segment"] = None
        else:
            self.metadata["active_segment"] = {"file_start_scan": self.sample_count,
                                                "source_start_scan": int(source_start_scan)}
        self._write_metadata()

    def close(self, *, termination_reason="user_stop", error=None):
        """Finalize once; reason distinguishes user stop from interrupted capture.

        ``finalized`` means the file was closed with a recorded sample count,
        not that acquisition completed without errors. Repeated close calls do
        not overwrite the original reason or error.
        """
        if not self._file.closed:
            self._file.close()
            self.metadata.update(sample_count=self.sample_count, finalized=True,
                                 recording_paused=False,
                                 active_segment=None,
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

    @property
    def recording_segments(self):
        """Map stored frames to acquisition scans; old sessions are continuous."""
        start = self.metadata.get("acquisition_config", {}).get("record_start_scan", 0)
        return self.metadata.get("recording_segments", [
            {"file_start_scan": 0, "source_start_scan": start, "sample_count": self.sample_count}
        ])

    @property
    def elapsed_duration(self):
        """Recorded source span, including gaps between stored segments."""
        if not self.sample_count:
            return 0.0
        return (self.sample_indices(self.sample_count - 1, 1)[0] + 1) / self.sample_rate

    def sample_indices(self, start=0, count=None):
        """Source scan indices relative to this group's acquisition start."""
        if start < 0 or (count is not None and count < 0):
            raise ValueError("回放起点和采样点数不能为负数")
        available = max(0, self.sample_count - start)
        count = available if count is None else min(count, available)
        indices = np.empty(count, dtype=np.int64)
        origin = self.metadata.get("acquisition_config", {}).get("record_start_scan", 0)
        for segment in self.recording_segments:
            first = max(start, segment["file_start_scan"])
            stop = min(start + count, segment["file_start_scan"] + segment["sample_count"])
            if stop > first:
                source_first = segment["source_start_scan"] - origin + first - segment["file_start_scan"]
                indices[first - start:stop - start] = np.arange(source_first, source_first + stop - first)
        return indices

    def sample_times(self, start=0, count=None):
        """Seconds from group start, retaining gaps omitted by record pause."""
        return self.sample_indices(start, count) / self.sample_rate

    def sample_index_at_time(self, seconds, side="left"):
        """Find a stored-frame boundary in O(segments), including pause gaps.

        Like searchsorted, left includes a sample exactly at seconds and right
        follows it. A time within a pause maps to the following segment start.
        """
        if not math.isfinite(seconds) or side not in ("left", "right"):
            raise ValueError("定位时间必须有限，side 必须为 left 或 right")
        origin = self.metadata.get("acquisition_config", {}).get("record_start_scan", 0)
        for segment in self.recording_segments:
            source = segment["source_start_scan"] - origin
            length = segment["sample_count"]
            first_time = source / self.sample_rate
            last_time = (source + length - 1) / self.sample_rate
            if seconds < first_time or (seconds == first_time and side == "left"):
                return segment["file_start_scan"]
            if seconds <= last_time:
                relative = seconds * self.sample_rate - source
                offset = math.ceil(relative) if side == "left" else math.floor(relative) + 1
                offset = max(0, min(length, offset))
                # Compare the timestamp itself to correct multiplication rounding
                # at an exact sample boundary, with no full time-vector read.
                if offset < length and ((source + offset) / self.sample_rate < seconds
                                        or (side == "right" and (source + offset) / self.sample_rate == seconds)):
                    offset += 1
                if offset and ((source + offset - 1) / self.sample_rate > seconds
                               or (side == "left" and (source + offset - 1) / self.sample_rate == seconds)):
                    offset -= 1
                return segment["file_start_scan"] + offset
        return self.sample_count

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
    if "recording_segments" in metadata:
        mapped = 0
        previous_end = metadata.get("acquisition_config", {}).get("record_start_scan", 0)
        for segment in metadata["recording_segments"]:
            if (segment["file_start_scan"] != mapped or segment["sample_count"] <= 0
                    or segment["source_start_scan"] < previous_end):
                raise ValueError("记录分段的采样点映射无效")
            mapped += segment["sample_count"]
            previous_end = segment["source_start_scan"] + segment["sample_count"]
        if metadata["finalized"] and mapped != sample_count:
            raise ValueError("记录分段与已结束会话的样本数不一致")
        active = metadata.get("active_segment")
        if not metadata["finalized"] and active is not None:
            # The boundary is published before recording resumes. All raw tail
            # frames therefore belong to this known source-continuous segment.
            first = active["file_start_scan"]
            segments = [segment for segment in metadata["recording_segments"]
                        if segment["file_start_scan"] < first]
            if sample_count > first:
                segments.append({**active, "sample_count": sample_count - first})
            metadata["recording_segments"] = segments
        elif mapped != sample_count:
            raise ValueError("原始记录与已保存的采样分段不完整")
    metadata["sample_count"] = sample_count
    return RecordedSession(path=path, metadata=metadata)


def _session_selection(session_or_path, start, stop, channel_indices, chunk_size):
    if isinstance(session_or_path, (str, PathLike)):
        path = Path(session_or_path)
        if path.suffix.lower() == ".hpf":
            from .hpf import open_hpf
            session = open_hpf(path, waveform_only=True)
        else:
            session = load_session(path)
    else:
        session = session_or_path
    stop = session.sample_count if stop is None else stop
    if any(isinstance(value, bool) or not isinstance(value, Integral)
           for value in (start, stop, chunk_size)):
        raise ValueError("区间起止点和分块长度必须为整数")
    if not 0 <= start <= stop <= session.sample_count:
        raise ValueError("区间必须满足 0 ≤ 起点 ≤ 终点 ≤ 记录采样点数")
    if chunk_size <= 0:
        raise ValueError("分块长度必须大于零")
    channels = session.channels
    by_index = {channel.index: column for column, channel in enumerate(channels)}
    indices = list(by_index) if channel_indices is None else list(channel_indices)
    if (not indices or any(isinstance(index, bool) or not isinstance(index, Integral)
                           for index in indices)
            or len(set(indices)) != len(indices)
            or any(index not in by_index for index in indices)):
        raise ValueError("请选择记录中存在且不重复的物理 AI 通道编号")
    columns = [by_index[index] for index in indices]
    return session, [channels[column] for column in columns], columns, int(start), int(stop)


def export_csv(session_path, output_path, chunk_size=4096, *, start=0, stop=None,
               channel_indices=None, values="both", time_format="seconds", delimiter=",", overwrite=True,
               progress=None, cancelled=None):
    """Stream a selected interval and physical channels to CSV or tabbed text.

    ``start`` is inclusive and ``stop`` exclusive. Time retains original scan
    indices. ``values`` selects raw volts, engineering units, or both. The first
    '# labdaq_metadata: {JSON}' line retains the source metadata and records the
    selection in ``export``; source ``sample_count`` is never replaced.
    For pandas use read_csv(path, comment='#', encoding='utf-8-sig').
    """
    if values not in ("raw", "engineering", "both"):
        raise ValueError("导出数值必须是 raw、engineering 或 both")
    time_columns = {"seconds": ("time_s", 1.0), "milliseconds": ("time_ms", 1e3),
                    "microseconds": ("time_us", 1e6), "samples": ("sample_index", None)}
    if time_format not in time_columns:
        raise ValueError("时间格式必须是 seconds、milliseconds、microseconds 或 samples")
    if delimiter not in (",", "\t"):
        raise ValueError("导出分隔符必须是逗号或制表符")
    session, channels, columns, start, stop = _session_selection(
        session_path, start, stop, channel_indices, chunk_size)
    output_path = Path(output_path)
    if hasattr(session, "path"):
        source_path = Path(session.path)
        protected = {source_path.resolve(), (source_path / "raw.f64").resolve(),
                     (source_path / "metadata.json").resolve()}
        if output_path.resolve() in protected:
            raise ValueError("导出输出不能覆盖会话原始数据或元数据")
    metadata = dict(session.metadata)
    metadata["export"] = {
        "scan_start": start, "scan_stop": stop, "exported_sample_count": stop - start,
        "channel_indices": [channel.index for channel in channels], "values": values,
        "time_format": time_format, "delimiter": delimiter,
    }
    time_column, time_scale = time_columns[time_format]
    headings = [time_column]
    if values in ("raw", "both"):
        headings += [f"{ch.name}[AI{ch.index}]_raw_V" for ch in channels]
    if values in ("engineering", "both"):
        headings += [f"{ch.name}[AI{ch.index}]_{ch.unit}" for ch in channels]
    total = stop - start
    _task_progress(progress, cancelled, 0, total)
    with output_path.open("w" if overwrite else "x", encoding="utf-8-sig", newline="") as handle:
        handle.write("# labdaq_metadata: " + json.dumps(metadata, ensure_ascii=False) + "\n")
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(headings)
        for first in range(start, stop, chunk_size):
            _task_progress(progress, cancelled, first - start, total)
            raw = session.read(first, min(chunk_size, stop - first))[:, columns]
            indices = (session.sample_indices(first, len(raw)) if hasattr(session, "sample_indices")
                       else np.arange(first, first + len(raw)))
            if values == "raw":
                block = raw
            else:
                engineering = convert_volts(raw, channels)
                block = engineering if values == "engineering" else np.column_stack((raw, engineering))
            for offset, row in enumerate(block):
                scan = int(indices[offset])
                time = scan if time_scale is None else scan / session.sample_rate * time_scale
                writer.writerow([time, *row])
            _task_progress(progress, cancelled, first + len(raw) - start, total)
    return output_path


def export_txt(session_path, output_path, chunk_size=4096, **selection):
    """Export the same selections as export_csv with tab-separated columns."""
    return export_csv(session_path, output_path, chunk_size, delimiter="\t", **selection)


def session_statistics(session_or_path, *, start=0, stop=None, channel_indices=None,
                       values="engineering", chunk_size=4096, progress=None, cancelled=None):
    """Stream per-channel interval statistics; std is population std (ddof=0).

    ``values`` is raw (V) or engineering (the saved channel conversion). Each
    result contains index/name/unit/count/min/max/delta/mean/std/rms. Block
    variances are merged around their means to preserve small fluctuations on
    signals with large DC offsets. Empty intervals have no defined statistics.
    """
    if values not in ("raw", "engineering"):
        raise ValueError("统计数值必须是 raw 或 engineering")
    session, channels, columns, start, stop = _session_selection(
        session_or_path, start, stop, channel_indices, chunk_size)
    if start == stop:
        raise ValueError("统计区间至少需要一个采样点")
    count = 0
    total_frames = stop - start
    mean = np.zeros(len(channels))
    m2 = np.zeros(len(channels))
    minimum = np.full(len(channels), np.inf)
    maximum = np.full(len(channels), -np.inf)
    _task_progress(progress, cancelled, 0, total_frames)
    for first in range(start, stop, chunk_size):
        _task_progress(progress, cancelled, count, total_frames)
        block = session.read(first, min(chunk_size, stop - first))[:, columns]
        if values == "engineering":
            block = convert_volts(block, channels)
        if not np.isfinite(block).all():
            raise ValueError("统计区间包含 NaN 或无穷值")
        size = len(block)
        block_mean = np.mean(block, axis=0)
        block_m2 = np.sum((block - block_mean) ** 2, axis=0)
        total = count + size
        if count:
            difference = block_mean - mean
            m2 += block_m2 + difference ** 2 * (count * size / total)
            mean += difference * (size / total)
        else:
            mean = block_mean
            m2 = block_m2
        count = total
        minimum = np.minimum(minimum, np.min(block, axis=0))
        maximum = np.maximum(maximum, np.max(block, axis=0))
        _task_progress(progress, cancelled, count, total_frames)
    std = np.sqrt(m2 / count)
    rms = np.hypot(mean, std)
    return [{"index": channel.index, "name": channel.name,
             "unit": "V" if values == "raw" else channel.unit, "count": count,
             "min": float(minimum[column]), "max": float(maximum[column]),
             "delta": float(maximum[column] - minimum[column]),
             "mean": float(mean[column]), "std": float(std[column]), "rms": float(rms[column])}
            for column, channel in enumerate(channels)]
