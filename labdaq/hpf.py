"""Read a documented, deliberately limited subset of Data Translation HPF.

Supports HPF 0x10001, one synchronous channel group, RandomDataChannel Double
samples, contiguous data chunks and the QuickDAQ configuration XML chunk.
No vendor code is loaded and the original file is always opened read-only.
``read`` is a voltage contract; mixed engineering units are available through
``read_native`` but must not enter the application's voltage/force conversion.
The observed DT9837C force profile uses stored volts and separate QuickDAQ
engineering conversion. Its auxiliary zoom/summary blocks require an explicit
``waveform_only=True`` opt-in and remain described in the returned metadata.
"""

import csv
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import math
import struct
import xml.etree.ElementTree as ET

import numpy as np

from .processing import ChannelSettings


class HpfError(ValueError):
    """Unsupported or incomplete HPF input, suitable for a user-facing message."""


@dataclass(frozen=True)
class _DataChunk:
    offset: int
    start: int
    count: int
    channel_offsets: tuple[int, ...]


class HpfRecording:
    source = "vendor_hpf"

    def __init__(self, path, metadata, chunks):
        self.path = Path(path)
        self.metadata = metadata
        self._chunks = chunks

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
    def voltage_replay_error(self):
        return self.metadata["voltage_replay_error"]

    @property
    def voltage_compatible(self):
        return not self.voltage_replay_error

    def read(self, start=0, count=None):
        """Return a finite window of unscaled, synchronous volts, never force."""
        if not self.voltage_compatible:
            raise HpfError(self.voltage_replay_error)
        return self.read_native(start, count)

    def read_native(self, start=0, count=None):
        """Return stored Double values in XML channel order, without conversion.

        For non-voltage files consult vendor_channels for units/scales. This
        method intentionally does not apply ambiguous sensor calibration fields.
        A bounded request seeks directly into relevant per-channel data spans;
        it does not read the full HPF or instantiate the vendor application.
        """
        if start < 0 or (count is not None and count < 0):
            raise ValueError("回放起点和采样点数不能为负数")
        available = max(0, self.sample_count - start)
        count = available if count is None else min(count, available)
        data = np.empty((count, len(self.metadata["channels"])), dtype=np.float64)
        end = start + count
        with self.path.open("rb") as handle:
            for chunk in self._chunks:
                first = max(start, chunk.start)
                last = min(end, chunk.start + chunk.count)
                if first >= last:
                    continue
                for column, offset in enumerate(chunk.channel_offsets):
                    handle.seek(offset + (first - chunk.start) * 8)
                    values = np.fromfile(handle, dtype="<f8", count=last - first)
                    if len(values) != last - first:
                        raise HpfError("HPF 数据不完整，文件可能在打开后被截断")
                    data[first - start:last - start, column] = values
        if not np.isfinite(data).all():
            raise HpfError("HPF 所选数据段包含 NaN 或无穷值，不能作为有效信号回放")
        return data

    def iter_chunks(self, chunk_size=4096):
        if chunk_size < 1:
            raise ValueError("分块长度必须大于零")
        for start in range(0, self.sample_count, chunk_size):
            yield self.read(start, chunk_size)


def _read(handle, size):
    data = handle.read(size)
    if len(data) != size:
        raise HpfError("HPF 文件不完整：块或字段被截断")
    return data


def _xml(handle, size, expected_root):
    # Metadata is small in the supported format. Do not allocate an arbitrary
    # attacker-controlled chunk size or resolve an XML DTD from an input file.
    if size > 16 * 1024 * 1024:
        raise HpfError("HPF XML 元数据超过当前读取器支持的 16 MiB")
    raw = _read(handle, size).rstrip(b"\0 \r\n\t")
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise HpfError("HPF XML 不支持 DTD 或实体定义")
    try:
        text = raw.decode("utf-8-sig")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError) as exc:
        raise HpfError("HPF XML 元数据无法解析") from exc
    if root.tag != expected_root:
        raise HpfError(f"HPF XML 根节点不是 {expected_root}")
    return root, text


def _number(info, field):
    try:
        value = float(info[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise HpfError(f"HPF 缺少有效的 {field}") from exc
    if not math.isfinite(value):
        raise HpfError(f"HPF 的 {field} 必须是有限数值")
    return value


def _quickdaq_force_settings(vendor_channels, configuration_xml, sample_rate):
    """Recognize the observed unfiltered DT9837C voltage-to-force profile.

    QuickDAQ stores Double volts in 0x3000 even when the channel label is N.
    Use the full-precision 0x7000 sensitivity, not rounded 0x2000 SensorScale.
    Other devices/engineering units keep the existing native-only contract.
    """
    if not configuration_xml or any(info["Unit"] != "N" for info in vendor_channels):
        return None
    root = ET.fromstring(configuration_xml)
    devices = root.findall(".//Device")
    if len(devices) != 1 or devices[0].findtext("DeviceFamily") != "DT9837_C":
        return None
    device = devices[0]
    inputs = device.findall("./InputSubsystem/Channels/ListItem/ChannelBase")
    by_name = {f"{device.findtext('DeviceId')}.{channel.findtext('Name')}": channel
               for channel in inputs}
    if root.findtext("./acqParams/FilterType") != "None":
        raise HpfError("DT9837C HPF 采集滤波不是 None，尚不能确认未处理电压")
    settings = []
    for info in vendor_channels:
        channel = by_name.get(info["Name"])
        if channel is None:
            raise HpfError("DT9837C HPF 波形通道与 QuickDAQ 配置不匹配")
        fields = {child.tag: child.text for child in channel}
        if (channel.get("{http://www.w3.org/2001/XMLSchema-instance}type") != "IEPEInputChannel"
                or fields.get("ChannelType") != "IEPE" or fields.get("Enabled") != "true"
                or fields.get("EditUnitLabel") != "N"
                or channel.findtext("./Filter/FilterType") != "None"):
            raise HpfError("仅支持已确认的 DT9837C 未滤波 IEPE 电压输入与 N 换算配置")
        if [_number(info, key) for key in ("DataScale", "DataOffset")] != [1.0, 0.0]:
            raise HpfError("DT9837C HPF 数据缩放非恒等，不能直接作为原始电压")
        physical = int(fields["ChannelNumber"])
        if physical not in range(4) or str(physical) != info.get("PhysicalChannelNumber"):
            raise HpfError("DT9837C HPF 物理通道编号不一致")
        sensitivity = _number(fields, "MilliVoltsPerUnit")
        offset = _number(fields, "Offset")
        if sensitivity == 0:
            raise HpfError("DT9837C HPF 传感器灵敏度不能为零")
        # The rounded channel metadata corroborates the config, but is never
        # used as the actual conversion factor.
        if (not math.isclose(_number(info, "SensorScale"), 1000 / sensitivity, rel_tol=1e-6)
                or not math.isclose(_number(info, "SensorOffset"), offset, abs_tol=1e-12)):
            raise HpfError("DT9837C HPF 通道换算与 QuickDAQ 配置不一致")
        settings.append(ChannelSettings(
            physical, info["Name"], unit="N", sensitivity_mv_per_unit=abs(sensitivity),
            zero_volts=-offset * sensitivity / 1000, inverted=sensitivity < 0,
        ))
    physical_channels = [channel.index for channel in settings]
    if physical_channels != sorted(set(physical_channels)):
        raise HpfError("DT9837C HPF 波形通道需按物理编号升序排列且不重复")
    configured = {int(channel.findtext("ChannelNumber")): channel for channel in inputs}
    if set(configured) != set(range(4)):
        raise HpfError("DT9837C HPF 缺少四个物理通道的采集配置")
    acquisition_config = {
        "sample_rate": sample_rate, "channels": physical_channels,
        "ranges": [float(configured[i].findtext("RangeHigh")) for i in range(4)],
        "coupling": [configured[i].findtext("Coupling") for i in range(4)],
        "iepe": [configured[i].findtext("CurrentSourceEnabled") == "true" for i in range(4)],
    }
    return settings, acquisition_config


def open_hpf(path, *, waveform_only=False):
    """Inspect chunk directories/metadata once; later reads seek into windows.

    Rejects unsupported channel types, multiple groups/rates, sample gaps,
    unequal lengths, events and unknown chunks rather than silently losing data.
    waveform_only permits only the observed QuickDAQ zoom/summary extensions
    (0x8000/0x9000/0xA000); their metadata is retained, their values are not used
    as waveform samples. Other non-voltage profiles remain native-only.
    """
    path = Path(path)
    file_size = path.stat().st_size
    chunks = []
    chunk_directory = {}
    indices = None
    index_offset = None
    group_id = None
    vendor_channels = None
    settings = None
    configuration_xml = None
    auxiliary_chunks = []
    zoom_map = None
    sample_count = 0
    sample_rate = None
    recording_date = None
    voltage_error = ""
    offset = 0
    with path.open("rb") as handle:
        while offset < file_size:
            handle.seek(offset)
            chunk_id, size = struct.unpack("<qq", _read(handle, 16))
            if size < 65536 or size % 65536 or offset + size > file_size:
                raise HpfError("HPF 块长度无效或文件不完整（需要完整的 64 KiB 对齐块）")
            if offset == 0 and chunk_id != 0x1000:
                raise HpfError("HPF 文件必须以头块开始")
            entry = {"chunk_id": chunk_id, "offset": offset, "size": size,
                     "group_id": 0, "start": -1, "count": -1}
            if chunk_id == 0x1000:
                if offset != 0:
                    raise HpfError("HPF 存在重复头块")
                creator, version, index_offset = struct.unpack("<4sqq", _read(handle, 20))
                if creator != b"datx" or version != 0x10001:
                    raise HpfError("仅支持 datx / HPF 0x10001 格式")
                root, _ = _xml(handle, size - 36, "RecordingDate")
                recording_date = root.text
            elif chunk_id == 0x2000:
                if vendor_channels is not None:
                    raise HpfError("当前 HPF 读取器不支持多个通道组，不能当作同步电压回放")
                group_id, channel_count = struct.unpack("<ii", _read(handle, 8))
                if channel_count < 1:
                    raise HpfError("HPF 通道组为空")
                entry["group_id"] = group_id
                root, _ = _xml(handle, size - 24, "ChannelInformationData")
                if len(root) != channel_count or any(c.tag != "ChannelInformation" for c in root):
                    raise HpfError("HPF 通道数量与 XML 描述不一致")
                vendor_channels = [{field.tag: field.text for field in c} for c in root]
                settings = []
                data_indexes = []
                start_times = []
                for position, info in enumerate(vendor_channels):
                    if info.get("ChannelType", "").lower() != "randomdatachannel" or info.get("DataType") != "Double":
                        raise HpfError("当前 HPF 读取器仅支持 RandomDataChannel / Double 数据")
                    if info.get("AssignedTimeChannelIndex") != "-1":
                        raise HpfError("当前 HPF 读取器不支持独立时间通道")
                    try:
                        data_index = int(info["DataIndex"])
                        name, unit = info["Name"], info["Unit"]
                    except (KeyError, TypeError, ValueError) as exc:
                        raise HpfError("HPF 缺少通道名称、单位或 DataIndex") from exc
                    if not name or not unit:
                        raise HpfError("HPF 通道名称和单位不能为空")
                    data_indexes.append(data_index)
                    dt = _number(info, "TimeIncrement")
                    rate = _number(info, "PerChannelSampleRate")
                    if dt <= 0 or rate <= 0 or not math.isclose(dt * rate, 1.0, rel_tol=1e-9):
                        raise HpfError("HPF 采样率与时间间隔不一致，不能当作同步电压回放")
                    if sample_rate is not None and not math.isclose(rate, sample_rate, rel_tol=1e-12):
                        raise HpfError("HPF 各通道采样率不同，不能当作同步电压回放")
                    sample_rate = rate
                    start_times.append(info.get("StartTime"))
                    # Restrict replay to the observed Double-voltage subset.
                    # Scaling non-native sensor values needs separate validation.
                    scales = [_number(info, field) for field in
                              ("DataScale", "DataOffset", "SensorScale", "SensorOffset")]
                    if unit != "V":
                        voltage_error = "HPF 含非电压通道；可读取全部原始值，但不能当作同步电压/力信号回放"
                    elif scales != [1.0, 0.0, 1.0, 0.0] and not voltage_error:
                        voltage_error = "HPF 含非恒等换算参数，当前读取器仅支持原始 Double 电压回放"
                    settings.append(ChannelSettings(position, name, unit=unit,
                                                    sensitivity_mv_per_unit=1000.0))
                if sorted(data_indexes) != list(range(channel_count)):
                    raise HpfError("HPF DataIndex 必须完整且不重复")
                if len(set(start_times)) != 1 or start_times[0] is None:
                    raise HpfError("HPF 通道起始时间不一致，不能当作同步电压回放")
            elif chunk_id == 0x3000 or (waveform_only and chunk_id in (0x8000, 0x9000)):
                if vendor_channels is None:
                    raise HpfError("HPF 数据块之前缺少通道描述")
                data_group, start, count_channels = struct.unpack("<iqi", _read(handle, 16))
                if data_group != group_id or count_channels != len(vendor_channels):
                    raise HpfError("HPF 数据块的通道组或通道数不匹配")
                if 32 + count_channels * 8 > size:
                    raise HpfError("HPF 通道描述表超出数据块")
                descriptors = [struct.unpack("<ii", _read(handle, 8)) for _ in range(count_channels)]
                spans = []
                for relative, length in descriptors:
                    if relative < 32 + count_channels * 8 or length < 0 or length % 8 or relative + length > size:
                        raise HpfError("HPF 通道数据偏移/长度无效或数据不完整")
                    spans.append((relative, relative + length))
                ordered = sorted(spans)
                if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:])):
                    raise HpfError("HPF 通道数据范围相互重叠")
                lengths = {length // 8 for _, length in descriptors}
                if len(lengths) != 1:
                    raise HpfError("HPF 各通道长度不同，不能当作同步电压回放")
                count = lengths.pop()
                channel_offsets = tuple(offset + descriptors[int(info["DataIndex"])][0]
                                        for info in vendor_channels)
                entry.update(group_id=data_group, start=start, count=count)
                if chunk_id == 0x3000:
                    if start != sample_count:
                        raise HpfError("HPF 数据块样本序号不连续，不能补点或静默压缩时间轴")
                    chunks.append(_DataChunk(offset, start, count, channel_offsets))
                    sample_count += count
                else:
                    entry["channel_spans"] = [dict(relative_offset=relative, byte_count=length)
                                              for relative, length in descriptors]
                    if chunk_id == 0x8000:
                        factor, = struct.unpack("<q", _read(handle, 8))
                        if factor != 8:
                            raise HpfError("仅支持已观察的 QuickDAQ 8 倍显示缓存块")
                        entry.update(role="quickdaq_zoom_cache", zoom_factor=factor)
                    else:
                        if start != 0 or count != 10:
                            raise HpfError("仅支持已观察的 QuickDAQ 每通道 10 值汇总块")
                        entry["role"] = "quickdaq_summary_uninterpreted"
                    auxiliary_chunks.append(entry)
            elif chunk_id == 0x6000:
                if indices is not None:
                    raise HpfError("HPF 存在多个索引块")
                if index_offset != offset:
                    raise HpfError("HPF 头部索引位置与实际索引块不一致")
                index_count, = struct.unpack("<q", _read(handle, 8))
                if index_count < 0 or index_count > (size - 24) // 40:
                    raise HpfError("HPF 索引数量超出索引块")
                indices = [struct.unpack("<5q", _read(handle, 40)) for _ in range(index_count)]
            elif chunk_id == 0x7000:
                if configuration_xml is not None:
                    raise HpfError("当前 HPF 读取器不支持多个 QuickDAQ 设置块")
                config_group, = struct.unpack("<i", _read(handle, 4))
                entry["group_id"] = config_group
                _, configuration_xml = _xml(handle, size - 20, "PersitantObjects")
            elif waveform_only and chunk_id == 0xA000:
                if zoom_map is not None:
                    raise HpfError("当前 HPF 读取器不支持多个缩放索引映射")
                config_group, = struct.unpack("<i", _read(handle, 4))
                root, xml = _xml(handle, size - 20, "IndexDescriptorToZoomMap")
                pairs = [(int(item.findtext("./Key/int")), int(item.findtext("./Value/int")))
                         for item in root.findall("Item")]
                zoom_map = dict(pairs)
                if not pairs or len(zoom_map) != len(pairs) or min(zoom_map) < 0:
                    raise HpfError("HPF 缩放索引映射为空或重复")
                entry.update(group_id=config_group, start=min(zoom_map), role="quickdaq_zoom_map",
                             xml=xml, zoom_map=zoom_map)
                auxiliary_chunks.append(entry)
            else:
                raise HpfError(f"当前 HPF 读取器不支持块 0x{chunk_id:X}（含事件/触发/FFT扩展时不静默丢弃）")
            chunk_directory[offset] = entry
            offset += size
    if vendor_channels is None or not chunks:
        raise HpfError("HPF 缺少通道描述或数据块")
    if indices is None and index_offset not in (0, -1):
        raise HpfError("HPF 头部指定的索引块缺失")
    if indices is not None:
        indexed_offsets = set()
        for start, count, chunk_id, index_group, file_offset in indices:
            actual = chunk_directory.get(file_offset)
            if (actual is None or file_offset in indexed_offsets or actual["chunk_id"] != chunk_id
                    or actual["group_id"] != index_group or actual["start"] != start
                    or actual["count"] != count):
                raise HpfError("HPF 索引与实际块描述不一致")
            indexed_offsets.add(file_offset)
        expected_offsets = {pos for pos, entry in chunk_directory.items() if entry["chunk_id"] != 0x6000}
        if indexed_offsets != expected_offsets:
            raise HpfError("HPF 索引未完整覆盖文件块")
    if auxiliary_chunks:
        if indices is None or zoom_map is None:
            raise HpfError("HPF 附加显示块缺少索引或缩放映射")
        mapped_offsets = set()
        for index, factor in zoom_map.items():
            if index >= len(indices):
                raise HpfError("HPF 缩放映射超出索引范围")
            entry = chunk_directory[indices[index][4]]
            if not (entry["chunk_id"] == 0x3000 and factor == 1
                    or entry["chunk_id"] == 0x8000 and factor == entry["zoom_factor"]):
                raise HpfError("HPF 缩放映射与原始/显示块不一致")
            mapped_offsets.add(entry["offset"])
        expected = {entry["offset"] for entry in chunk_directory.values()
                    if entry["chunk_id"] in (0x3000, 0x8000)}
        if mapped_offsets != expected:
            raise HpfError("HPF 缩放映射未覆盖原始/显示块")
    acquisition_config = None
    force_profile = _quickdaq_force_settings(vendor_channels, configuration_xml, sample_rate)
    if force_profile is not None:
        settings, acquisition_config = force_profile
        voltage_error = ""
    metadata = {
        "format": "vendor-hpf", "format_version": 0x10001, "source": "vendor_hpf",
        "source_file": path.name, "dtype": "<f8", "sample_rate": sample_rate,
        "sample_count": sample_count, "channels": [asdict(ch) for ch in settings],
        "vendor_channels": vendor_channels, "recording_date": recording_date,
        "voltage_replay_error": voltage_error,
        "origin_note": "导入的厂商 HPF；型号和时间来自文件，不代表本机 DT9837C 实测。",
        "chunks": list(chunk_directory.values()), "index_entries": indices,
        "vendor_configuration_xml": configuration_xml,
        "acquisition_config": acquisition_config,
        "conversion_source": "quickdaq_configuration" if force_profile else "identity_voltage_or_native",
        "waveform_only": bool(waveform_only), "auxiliary_chunks": auxiliary_chunks,
        "waveform_only_note": ("仅回放 0x3000 原始电压；保留附加显示缓存、缩放映射和汇总块元数据，"
                               "不使用其派生值，也不重现原厂统计或触发/事件功能。"
                               if auxiliary_chunks else ""),
    }
    return HpfRecording(path, metadata, chunks)


def export_hpf_csv(path, output_path, chunk_size=4096, *, waveform_only=False):
    """Export supported voltage HPF windows with relative time and provenance."""
    session = open_hpf(path, waveform_only=waveform_only)
    if not session.voltage_compatible:
        raise HpfError(session.voltage_replay_error)
    if chunk_size < 1:
        raise ValueError("分块长度必须大于零")
    output_path = Path(output_path)
    if output_path.resolve() == session.path.resolve():
        raise ValueError("CSV 输出不能覆盖 HPF 原始文件")
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        handle.write("# labdaq_metadata: " + json.dumps(session.metadata, ensure_ascii=False) + "\n")
        writer = csv.writer(handle)
        writer.writerow(["time_s"] + [f"{ch.name}_raw_V" for ch in session.channels])
        start = 0
        for volts in session.iter_chunks(chunk_size):
            for local, values in enumerate(volts):
                writer.writerow([(start + local) / session.sample_rate, *values])
            start += len(volts)
    return output_path
