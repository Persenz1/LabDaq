"""Bounded, source-grounded data loading and export for recording comparison."""

import csv
import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from numbers import Integral, Real
from pathlib import Path

import numpy as np

from .processing import ChannelSettings, convert_volts
from .recording import OperationCancelled, RecordedSession, _task_progress, has_acquisition_error, load_session

_AXES = ("relative", "absolute", "samples")
_VALUES = ("raw", "engineering")
_PROFILE_SCHEMA = "labdaq-comparison-v1"


def _finite_number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name}必须是有限数字")
    return float(value)


def _number(value, name, *, positive=False):
    value = _finite_number(value, name)
    if value < 0 or positive and value <= 0:
        raise ValueError(f"{name}必须{'大于零' if positive else '非负'}")
    return value


def _integer(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 0 or positive and value <= 0:
        raise ValueError(f"{name}必须是{'正' if positive else '非负'}整数")
    return int(value)


def _window(start, span):
    return _finite_number(start, "比较窗口起点"), _number(span, "比较窗口宽度", positive=True)


def _timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value)
        epoch = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return epoch if math.isfinite(epoch) else None


def _readonly(array):
    array = np.asarray(array)
    array.setflags(write=False)
    return array


@dataclass(frozen=True)
class ComparisonSource:
    session: RecordedSession
    channel_indices: tuple[int, ...]
    zero_seconds: float
    sample_origin: int
    shift_seconds: float = 0.0

    def __init__(self, session, channel_indices=None, *, zero_seconds=None,
                 sample_origin=None, shift_seconds=0.0):
        if not isinstance(session, RecordedSession):
            raise TypeError("比较来源必须是 RecordedSession")
        if not session.metadata.get("finalized"):
            raise ValueError(f"记录 {session.path} 尚未结束，不能用于比较")
        channels = session.channels
        try:
            raw_size = (session.path / "raw.f64").stat().st_size
        except OSError as exc:
            raise ValueError(f"记录 {session.path} 的原始数据不可读：{exc}") from exc
        if raw_size != session.sample_count * len(channels) * 8:
            raise ValueError(f"记录 {session.path} 的原始数据长度与元数据不一致")
        by_index = {channel.index: column for column, channel in enumerate(channels)}
        indices = list(by_index) if channel_indices is None else list(channel_indices)
        if (not indices or any(isinstance(index, bool) or not isinstance(index, Integral) for index in indices)
                or len(set(indices)) != len(indices) or any(index not in by_index for index in indices)):
            raise ValueError("请选择记录中存在且不重复的物理 AI 通道")
        first_index = int(session.sample_indices(0, 1)[0]) if session.sample_count else 0
        first_time = first_index / session.sample_rate
        if zero_seconds is None:
            zero_seconds = first_time
        else:
            zero_seconds = _number(zero_seconds, "相对时间零点")
            if session.sample_count:
                nearest = session.sample_index_at_time(zero_seconds, "left")
                candidates = {min(max(nearest, 0), session.sample_count - 1),
                              min(max(nearest - 1, 0), session.sample_count - 1)}
                if not any(float(session.sample_times(frame, 1)[0]) == zero_seconds for frame in candidates):
                    raise ValueError("相对时间零点必须对应一个真实样本")
            elif zero_seconds != 0:
                raise ValueError("空记录的相对时间零点只能为 0")
        if sample_origin is None:
            sample_origin = first_index
        else:
            sample_origin = _integer(sample_origin, "样本零点")
            if session.sample_count:
                origin = session.metadata.get("acquisition_config", {}).get("record_start_scan", 0)
                exists = any(segment["source_start_scan"] - origin <= sample_origin
                             < segment["source_start_scan"] - origin + segment["sample_count"]
                             for segment in session.recording_segments)
                if not exists:
                    raise ValueError("样本零点必须对应一个真实源样本索引")
            elif sample_origin != 0:
                raise ValueError("空记录的样本零点只能为 0")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "channel_indices", tuple(int(index) for index in indices))
        object.__setattr__(self, "zero_seconds", float(zero_seconds))
        object.__setattr__(self, "sample_origin", int(sample_origin))
        object.__setattr__(self, "shift_seconds", _finite_number(shift_seconds, "绝对时间偏移"))

    @classmethod
    def from_path(cls, path, channel_indices=None, **alignment):
        return cls(load_session(path), channel_indices, **alignment)

    @classmethod
    def from_session(cls, session, channel_indices=None, **alignment):
        return cls(session, channel_indices, **alignment)

    @property
    def path(self):
        return self.session.path

    @property
    def name(self):
        return self.path.name

    @property
    def channels(self):
        by_index = {channel.index: channel for channel in self.session.channels}
        return tuple(by_index[index] for index in self.channel_indices)

    @property
    def has_acquisition_error(self):
        return has_acquisition_error(self.session.metadata)

    @property
    def epoch(self):
        metadata = self.session.metadata
        acquisition = _timestamp(metadata.get("acquisition_started_at_utc"))
        if acquisition is not None:
            start = metadata.get("acquisition_config", {}).get("record_start_scan", 0)
            return acquisition + start / self.session.sample_rate
        return _timestamp(metadata.get("started_at_utc"))

    def set_time_zero(self, seconds):
        seconds = _number(seconds, "相对时间零点")
        if not self.session.sample_count:
            if seconds != 0:
                raise ValueError("空记录没有可对齐的真实样本")
            return self
        frame = self.session.sample_index_at_time(seconds, "left")
        candidates = [min(max(frame, 0), self.session.sample_count - 1),
                      min(max(frame - 1, 0), self.session.sample_count - 1)]
        nearest = min(candidates, key=lambda item: abs(float(self.session.sample_times(item, 1)[0]) - seconds))
        return ComparisonSource(self.session, self.channel_indices,
                                zero_seconds=float(self.session.sample_times(nearest, 1)[0]),
                                sample_origin=self.sample_origin, shift_seconds=self.shift_seconds)

    def set_sample_origin(self, sample_index):
        return ComparisonSource(self.session, self.channel_indices, zero_seconds=self.zero_seconds,
                                sample_origin=sample_index, shift_seconds=self.shift_seconds)

    def with_shift(self, shift_seconds):
        return ComparisonSource(self.session, self.channel_indices, zero_seconds=self.zero_seconds,
                                sample_origin=self.sample_origin, shift_seconds=shift_seconds)


@dataclass(frozen=True)
class ComparisonTrace:
    source: ComparisonSource
    source_index: int
    channel: ChannelSettings
    label: str
    unit: str
    times: np.ndarray
    sample_indices: np.ndarray
    x: np.ndarray
    raw_values: np.ndarray
    engineering_values: np.ndarray
    finite_mask: np.ndarray
    gaps: np.ndarray
    axis: str
    window_start: float
    window_span: float

    @property
    def record_path(self):
        return self.source.path


@dataclass(frozen=True)
class ComparisonResult:
    traces: tuple[ComparisonTrace, ...]
    sources: tuple[ComparisonSource, ...]
    axis: str
    start: float
    span: float
    values: str
    truncated: bool
    frame_count: int
    value_count: int
    source_frames: tuple[int, ...]

    @property
    def point_count(self):
        return self.value_count


@dataclass(frozen=True)
class ComparisonProfile:
    sources: tuple[ComparisonSource, ...]
    axis: str
    start: float
    span: float
    values: str
    appearance: dict
    view: dict | None = None


def _axis_x(source, axis, times, indices):
    if axis == "relative":
        return times - source.zero_seconds
    if axis == "samples":
        return indices.astype(np.float64) - source.sample_origin
    epoch = source.epoch
    if epoch is None:
        raise ValueError(f"比较组 {source.name} 缺少可靠采集时间，不能使用绝对时间轴")
    return epoch + times + source.shift_seconds


def comparison_bounds(sources, axis):
    """Return the actual-sample display extent; empty recordings add no extent."""
    if axis not in _AXES:
        raise ValueError("比较轴必须是 relative、absolute 或 samples")
    sources = tuple(sources)
    bounds = []
    for source in sources:
        if not isinstance(source, ComparisonSource):
            raise TypeError("比较来源必须是 ComparisonSource")
        if axis == "absolute" and source.epoch is None:
            raise ValueError(f"比较组 {source.name} 缺少可靠采集时间，不能使用绝对时间轴")
        if not source.session.sample_count:
            continue
        first = source.session.sample_indices(0, 1)[0]
        last = source.session.sample_indices(source.session.sample_count - 1, 1)[0]
        times = np.array([first, last], dtype=np.float64) / source.session.sample_rate
        x = _axis_x(source, axis, times, np.array([first, last]))
        bounds.append((float(x[0]), float(x[1])))
    if not bounds:
        return 0.0, 0.0
    return min(item[0] for item in bounds), max(item[1] for item in bounds)


def _frame_window(source, axis, start, span):
    if axis == "relative":
        first_time = start + source.zero_seconds
        stop_time = first_time + span
    elif axis == "absolute":
        if source.epoch is None:
            raise ValueError(f"比较组 {source.name} 缺少可靠采集时间，不能使用绝对时间轴")
        first_time = start - source.epoch - source.shift_seconds
        stop_time = first_time + span
        tolerance = 2 * math.ulp(max(abs(start), abs(source.epoch), 1.0))
        first_time -= tolerance
        stop_time -= tolerance
    else:
        first_time = (start + source.sample_origin) / source.session.sample_rate
        stop_time = (start + span + source.sample_origin) / source.session.sample_rate
    first = source.session.sample_index_at_time(first_time, "left")
    stop = source.session.sample_index_at_time(stop_time, "left")
    return first, stop


def load_comparison(sources, *, axis="relative", values="engineering", start=0, span=10,
                    max_frames_per_source=200000, max_values=2000000, chunk_size=65536,
                    progress=None, cancelled=None):
    """Load real stored frames without resampling; empty windows produce no traces.

    ``truncated`` reports either frame/value cap being reached. ``source_frames``
    gives loaded stored-frame counts in source order, including empty sources.
    """
    if axis not in _AXES:
        raise ValueError("比较轴必须是 relative、absolute 或 samples")
    if values not in _VALUES:
        raise ValueError("比较数值必须是 raw 或 engineering")
    start, span = _window(start, span)
    max_frames = _integer(max_frames_per_source, "每组帧数上限", positive=True)
    max_values = _integer(max_values, "比较值数上限", positive=True)
    chunk_size = _integer(chunk_size, "读取分块长度", positive=True)
    sources = tuple(sources)
    if any(not isinstance(source, ComparisonSource) for source in sources):
        raise TypeError("比较来源必须是 ComparisonSource")
    windows = [_frame_window(source, axis, start, span) for source in sources]
    planned = [min(max(0, stop - first), max_frames) for first, stop in windows]
    total = sum(planned)
    completed = 0
    remaining = max_values
    traces = []
    counts = []
    truncated = any(stop - first > count for (first, stop), count in zip(windows, planned))
    _task_progress(progress, cancelled, 0, total)
    for source_index, (source, (first, stop), planned_count) in enumerate(zip(sources, windows, planned)):
        count = min(planned_count, remaining // len(source.channel_indices))
        if count < planned_count:
            truncated = True
        if stop - first > count:
            truncated = True
        raw_parts = []
        try:
            for offset in range(0, count, chunk_size):
                _task_progress(progress, cancelled, completed + offset, total)
                size = min(chunk_size, count - offset)
                raw = source.session.read(first + offset, size)
                if len(raw) != size:
                    raise ValueError(f"原始记录仅返回 {len(raw)}/{size} 帧")
                columns = [list(session_channel.index for session_channel in source.session.channels).index(index)
                           for index in source.channel_indices]
                selected = raw[:, columns].copy()
                if not np.isfinite(selected).all():
                    raise ValueError("原始数据包含 NaN 或无穷值")
                raw_parts.append(selected)
        except OperationCancelled:
            raise
        except Exception as exc:
            raise ValueError(f"比较组 {source.name} 读取失败：{exc}") from exc
        raw_values = np.vstack(raw_parts) if raw_parts else np.empty((0, len(source.channel_indices)))
        try:
            engineering = convert_volts(raw_values, source.channels)
        except Exception as exc:
            raise ValueError(f"比较组 {source.name} 工程量换算失败：{exc}") from exc
        if not np.isfinite(engineering).all():
            raise ValueError(f"比较组 {source.name} 工程量包含 NaN 或无穷值")
        indices = source.session.sample_indices(first, count)
        times = indices.astype(np.float64) / source.session.sample_rate
        x = _axis_x(source, axis, times, indices)
        gaps = np.flatnonzero(np.diff(indices) > 1) + 1
        if count:
            for column, channel in enumerate(source.channels):
                label = f"{source.name}/AI{channel.index}/{channel.name}"
                traces.append(ComparisonTrace(
                    source, source_index, channel, label, "V" if values == "raw" else channel.unit,
                    _readonly(times.copy()), _readonly(indices.copy()), _readonly(x.copy()),
                    _readonly(raw_values[:, column].copy()), _readonly(engineering[:, column].copy()),
                    _readonly(np.ones(count, dtype=bool)), _readonly(gaps.copy()), axis, start, span))
        counts.append(count)
        completed += planned_count
        remaining -= count * len(source.channel_indices)
        _task_progress(progress, cancelled, completed, total)
    return ComparisonResult(tuple(traces), sources, axis, start, span, values, truncated,
                            sum(counts), sum(count * len(source.channel_indices)
                                           for count, source in zip(counts, sources)), tuple(counts))


def _result_parts(result_or_traces):
    if isinstance(result_or_traces, ComparisonResult):
        return result_or_traces.traces, result_or_traces.sources, result_or_traces.axis, result_or_traces.start, result_or_traces.span
    traces = tuple(result_or_traces)
    if not traces or any(not isinstance(trace, ComparisonTrace) for trace in traces):
        raise ValueError("导出需要 ComparisonResult 或非空 ComparisonTrace 序列")
    axes = {trace.axis for trace in traces}
    windows = {(trace.window_start, trace.window_span) for trace in traces}
    if len(axes) != 1 or len(windows) != 1:
        raise ValueError("导出曲线必须来自同一比较轴和窗口")
    sources = []
    for trace in traces:
        if all(trace.source is not source for source in sources):
            sources.append(trace.source)
    return traces, tuple(sources), axes.pop(), *windows.pop()


def export_comparison_csv(result_or_traces, output_path, *, values="both", delimiter=",", overwrite=False,
                          chunk_size=4096, progress=None, cancelled=None):
    """Atomically export loaded real samples as a long CSV table."""
    if values not in ("raw", "engineering", "both"):
        raise ValueError("导出数值必须是 raw、engineering 或 both")
    if delimiter not in (",", "\t"):
        raise ValueError("导出分隔符必须是逗号或制表符")
    chunk_size = _integer(chunk_size, "导出分块长度", positive=True)
    traces, sources, axis, start, span = _result_parts(result_or_traces)
    output_path = Path(output_path)
    protected = {path.resolve() for source in sources
                 for path in (source.path, source.path / "raw.f64", source.path / "metadata.json")}
    if output_path.resolve() in protected:
        raise ValueError("导出输出不能覆盖会话原始数据或元数据")
    metadata = {
        "format": "labdaq-comparison-csv-v1", "axis": axis,
        "window": {"start": start, "span": span}, "values": values,
        "sources": [{"path": str(source.path), "sample_rate": source.session.sample_rate,
                     "channel_indices": list(source.channel_indices), "zero_seconds": source.zero_seconds,
                     "sample_origin": source.sample_origin, "shift_seconds": source.shift_seconds,
                     "metadata": source.session.metadata} for source in sources],
    }
    headings = ["recording", "channel_index", "channel_name", "sample_index", "source_time_s", "comparison_x"]
    if values in ("raw", "both"):
        headings.append("raw_V")
    if values in ("engineering", "both"):
        headings.extend(("value", "unit"))
    total = sum(len(trace.x) for trace in traces)
    completed = 0
    _task_progress(progress, cancelled, 0, total)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", dir=output_path.parent,
                                         prefix=output_path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write("# labdaq_metadata: " + json.dumps(metadata, ensure_ascii=False) + "\n")
            writer = csv.writer(handle, delimiter=delimiter)
            writer.writerow(headings)
            for trace in traces:
                for first in range(0, len(trace.x), chunk_size):
                    _task_progress(progress, cancelled, completed, total)
                    stop = min(first + chunk_size, len(trace.x))
                    for row in range(first, stop):
                        values_out = [str(trace.record_path), trace.channel.index, trace.channel.name,
                                      int(trace.sample_indices[row]), float(trace.times[row]), float(trace.x[row])]
                        if values in ("raw", "both"):
                            values_out.append(float(trace.raw_values[row]))
                        if values in ("engineering", "both"):
                            values_out.extend((float(trace.engineering_values[row]), trace.channel.unit))
                        writer.writerow(values_out)
                    completed += stop - first
                    _task_progress(progress, cancelled, completed, total)
        if not overwrite and output_path.exists():
            raise FileExistsError(output_path)
        os.replace(temporary, output_path)
        temporary = None
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise
    return output_path


def _appearance(source_list, appearance):
    if appearance is None:
        return {}
    if not isinstance(appearance, dict):
        raise ValueError("曲线外观必须是字典")
    valid = {f"{source_index}:{channel}" for source_index, source in enumerate(source_list)
             for channel in source.channel_indices}
    result = {}
    for key, style in appearance.items():
        if not isinstance(key, str) or key not in valid or not isinstance(style, dict):
            raise ValueError("曲线外观键必须为 source index:physical AI，且内容必须是字典")
        if set(style) != {"visible", "color"} or not isinstance(style["visible"], bool):
            raise ValueError("曲线外观必须包含 visible 布尔值和 color")
        if not isinstance(style["color"], str) or not style["color"]:
            raise ValueError("曲线颜色必须是非空文本")
        result[key] = {"visible": style["visible"], "color": style["color"]}
    return result


def _view_settings(view):
    if view is None:
        return {}
    if not isinstance(view, dict):
        raise ValueError("比较界面布局必须是字典")
    if not view:
        return {}
    if set(view) != {"splitter_sizes"}:
        raise ValueError("比较界面布局只支持 splitter_sizes")
    sizes = view["splitter_sizes"]
    if (not isinstance(sizes, list) or len(sizes) != 2
            or any(type(size) is not int or not 0 <= size <= 100000 for size in sizes)):
        raise ValueError("比较分隔条必须包含两个非负整数尺寸")
    return {"splitter_sizes": list(sizes)}


def comparison_settings(sources, axis, start, span, values, appearance=None, view=None):
    """Build a JSON-serializable comparison layout without live session objects."""
    if axis not in _AXES:
        raise ValueError("比较轴必须是 relative、absolute 或 samples")
    if values not in _VALUES:
        raise ValueError("比较数值必须是 raw 或 engineering")
    start, span = _window(start, span)
    sources = tuple(sources)
    if any(not isinstance(source, ComparisonSource) for source in sources):
        raise TypeError("比较来源必须是 ComparisonSource")
    return {
        "schema": _PROFILE_SCHEMA,
        "sources": [{"path": str(source.path), "channel_indices": list(source.channel_indices),
                     "zero_seconds": source.zero_seconds, "sample_origin": source.sample_origin,
                     "shift_seconds": source.shift_seconds} for source in sources],
        "axis": axis, "window": {"start": start, "span": span}, "values": values,
        "appearance": _appearance(sources, appearance), "view": _view_settings(view),
    }


def _validate_profile(settings):
    if not isinstance(settings, dict) or settings.get("schema") != _PROFILE_SCHEMA:
        raise ValueError("比较布局格式版本不受支持")
    required = {"schema", "sources", "axis", "window", "values", "appearance"}
    if not required.issubset(settings) or set(settings) - required - {"view"}:
        raise ValueError("比较布局字段不完整或包含未知字段")
    if settings["axis"] not in _AXES or settings["values"] not in _VALUES:
        raise ValueError("比较布局轴或数值类型无效")
    if not isinstance(settings["window"], dict) or set(settings["window"]) != {"start", "span"}:
        raise ValueError("比较布局窗口无效")
    start, span = _window(settings["window"]["start"], settings["window"]["span"])
    entries = settings["sources"]
    if not isinstance(entries, list):
        raise ValueError("比较布局 sources 必须是列表")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "channel_indices", "zero_seconds",
                                                         "sample_origin", "shift_seconds"}:
            raise ValueError("比较布局来源字段无效")
        if not isinstance(entry["path"], str) or not entry["path"]:
            raise ValueError("比较布局记录路径必须是非空文本")
        if (not isinstance(entry["channel_indices"], list) or not entry["channel_indices"]
                or any(isinstance(index, bool) or not isinstance(index, Integral)
                       for index in entry["channel_indices"])
                or len(set(entry["channel_indices"])) != len(entry["channel_indices"])):
            raise ValueError("比较布局通道必须是非空且不重复的物理 AI 整数列表")
        _number(entry["zero_seconds"], "相对时间零点")
        _integer(entry["sample_origin"], "样本零点")
        _finite_number(entry["shift_seconds"], "绝对时间偏移")
    valid = {f"{source_index}:{channel}" for source_index, entry in enumerate(entries)
             for channel in entry["channel_indices"]}
    appearance = settings["appearance"]
    if not isinstance(appearance, dict):
        raise ValueError("曲线外观必须是字典")
    for key, style in appearance.items():
        if not isinstance(key, str) or key not in valid or not isinstance(style, dict):
            raise ValueError("曲线外观键必须为 source index:physical AI，且内容必须是字典")
        if set(style) != {"visible", "color"} or not isinstance(style["visible"], bool):
            raise ValueError("曲线外观必须包含 visible 布尔值和 color")
        if not isinstance(style["color"], str) or not style["color"]:
            raise ValueError("曲线颜色必须是非空文本")
    return start, span, _view_settings(settings.get("view"))


def save_comparison_profile(path, settings):
    """Validate and save a layout as UTF-8 JSON."""
    _validate_profile(settings)
    path = Path(path)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_comparison_profile(path):
    """Load a layout and reopen every exact finalized recording path."""
    path = Path(path)
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取比较布局 {path}：{exc}") from exc
    start, span, view = _validate_profile(settings)
    sources = tuple(ComparisonSource.from_path(**entry) for entry in settings["sources"])
    appearance = _appearance(sources, settings["appearance"])
    return ComparisonProfile(sources, settings["axis"], start, span, settings["values"], appearance, view)
