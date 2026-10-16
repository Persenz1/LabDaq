import csv
import json
from pathlib import Path
import struct
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from labdaq.hpf import HpfError, export_hpf_csv, open_hpf
from labdaq.processing import convert_volts


VENDOR = (Path(__file__).resolve().parents[1] / "resources/vendor/extracted/quickdaq"
          / "LocalAppData/Data Translation/QuickDAQ/Data")


def _chunk(kind, payload):
    size = ((16 + len(payload) + 65535) // 65536) * 65536
    return struct.pack("<qq", kind, size) + payload + bytes(size - 16 - len(payload))


def _make_hpf(path, *, dtype="Double", units=("V", "V"), rate2=2000,
              sensor_scale=1, reversed_descriptors=False, unequal=False):
    """Small fixture built from the published chunk structures, no vendor files."""
    channels = ET.Element("ChannelInformationData")
    for column, unit in enumerate(units):
        channel = ET.SubElement(channels, "ChannelInformation")
        fields = {
            "Name": f"测试 {column}", "Unit": unit, "ChannelType": "RandomDataChannel",
            "AssignedTimeChannelIndex": "-1", "DataType": dtype,
            "DataIndex": str(1 - column if reversed_descriptors else column),
            "StartTime": "0", "TimeIncrement": str(1 / (2000 if column == 0 else rate2)),
            "PerChannelSampleRate": str(2000 if column == 0 else rate2),
            "DataScale": "1", "DataOffset": "0", "SensorScale": str(sensor_scale),
            "SensorOffset": "0", "RangeMin": "-10", "RangeMax": "10",
        }
        for key, value in fields.items():
            ET.SubElement(channel, key).text = value
    pieces = [bytes(65536), _chunk(0x2000, struct.pack("<ii", 0, 2) + ET.tostring(channels))]
    index = [(-1, -1, 0x1000, 0, 0), (-1, -1, 0x2000, 0, 65536)]
    values = np.array([[0.125, -1.0], [1.25, -2.0], [2.5, -3.0],
                       [3.75, -4.0], [4.5, -5.0], [5.25, -6.0]])
    for start, stop in ((0, 2), (2, 6)):
        by_column = [values[start:stop, col].astype("<f8").tobytes() for col in range(2)]
        if reversed_descriptors:
            by_column.reverse()
        if unequal:
            by_column[1] = by_column[1][:-8]
        payload_start = 32 + 2 * 8
        descriptors = struct.pack("<4i", payload_start, len(by_column[0]),
                                  payload_start + len(by_column[0]), len(by_column[1]))
        index.append((start, stop - start, 0x3000, 0, sum(map(len, pieces))))
        pieces.append(_chunk(0x3000, struct.pack("<iqi", 0, start, 2) + descriptors + b"".join(by_column)))
    index_offset = sum(map(len, pieces))
    pieces[0] = _chunk(0x1000, struct.pack("<4sqq", b"datx", 0x10001, index_offset)
                       + b"<RecordingDate>2026/10/11 12:00:00</RecordingDate>")
    pieces.append(_chunk(0x6000, struct.pack("<q", len(index))
                         + b"".join(struct.pack("<5q", *entry) for entry in index)))
    path.write_bytes(b"".join(pieces))
    return values


def _vendor_path(name):
    path = VENDOR / name
    if not path.exists():
        pytest.skip("Vendor reference package is intentionally not distributed with source")
    return path


def _force_hpf(path, *, sensitivities=(500, 333.3330078125), offsets=(0, 0),
               filter_type="None", bad_zoom=False):
    """Observed QuickDAQ force profile plus explicitly opt-in auxiliary blocks."""
    expected = _make_hpf(path, units=("N", "N"), reversed_descriptors=True)
    original = path.read_bytes()
    channels = ET.fromstring(original[65536 + 24:2 * 65536].rstrip(b"\0"))
    for info, physical, sensitivity, offset in zip(channels, (0, 2), sensitivities, offsets):
        info.find("Name").text = f"DT9837-C(00).Ain {physical}"
        info.find("SensorScale").text = format(1000 / sensitivity, ".15g")
        info.find("SensorOffset").text = str(offset)
        ET.SubElement(info, "PhysicalChannelNumber").text = str(physical)
    root = ET.Element("PersitantObjects")
    device = ET.SubElement(ET.SubElement(root, "devConfig"), "Device")
    ET.SubElement(device, "DeviceFamily").text = "DT9837_C"
    ET.SubElement(device, "DeviceId").text = "DT9837-C(00)"
    inputs = ET.SubElement(ET.SubElement(device, "InputSubsystem"), "Channels")
    for physical in range(4):
        channel = ET.SubElement(ET.SubElement(inputs, "ListItem"), "ChannelBase",
                               {"{http://www.w3.org/2001/XMLSchema-instance}type": "IEPEInputChannel"})
        selected = physical in (0, 2)
        column = 0 if physical == 0 else 1
        fields = {"Name": f"Ain {physical}", "ChannelNumber": str(physical),
                  "ChannelType": "IEPE", "Enabled": str(selected).lower(), "EditUnitLabel": "N",
                  "MilliVoltsPerUnit": str(sensitivities[column] if selected else 1000),
                  "Offset": str(offsets[column] if selected else 0), "RangeHigh": "10",
                  "RangeLow": "-10", "Coupling": "DC", "CurrentSourceEnabled": "false"}
        for name, value in fields.items():
            ET.SubElement(channel, name).text = value
        ET.SubElement(ET.SubElement(channel, "Filter"), "FilterType").text = filter_type
    ET.SubElement(ET.SubElement(root, "acqParams"), "FilterType").text = "None"
    pieces = [bytes(65536)]
    index = [(-1, -1, 0x1000, 0, 0)]

    def append(kind, payload, start=-1, count=-1):
        index.append((start, count, kind, 0, sum(map(len, pieces))))
        pieces.append(_chunk(kind, payload))

    append(0x2000, struct.pack("<ii", 0, 2) + ET.tostring(channels))
    append(0x7000, struct.pack("<i", 0) + ET.tostring(root))
    for block in (2, 3):
        raw_chunk = original[block * 65536:(block + 1) * 65536]
        start, = struct.unpack_from("<q", raw_chunk, 20)
        count = 2 if block == 2 else 4
        append(0x3000, raw_chunk[16:], start, count)
    # The auxiliary values deliberately differ from the waveform. A reader
    # must neither concatenate them nor apply their apparent N scale again.
    append(0x8000, struct.pack("<iqi4iq", 0, 0, 2, 64, 8, 72, 8, 8)
           + bytes(8) + struct.pack("<2d", 99, 101), 0, 1)
    zoom = ET.Element("IndexDescriptorToZoomMap")
    for key, value in [(3, 1), (4, 1), (5, 1 if bad_zoom else 8)]:
        item = ET.SubElement(zoom, "Item")
        ET.SubElement(ET.SubElement(item, "Key"), "int").text = str(key)
        ET.SubElement(ET.SubElement(item, "Value"), "int").text = str(value)
    append(0xA000, struct.pack("<i", 0) + ET.tostring(zoom), 3)
    append(0x9000, struct.pack("<iqi4i", 0, 0, 2, 48, 80, 128, 80)
           + np.arange(20, dtype="<f8").tobytes(), 0, 10)
    index_offset = sum(map(len, pieces))
    pieces[0] = _chunk(0x1000, struct.pack("<4sqq", b"datx", 0x10001, index_offset)
                       + b"<RecordingDate>2026/09/27</RecordingDate>")
    pieces.append(_chunk(0x6000, struct.pack("<q", len(index))
                         + b"".join(struct.pack("<5q", *entry) for entry in index)))
    path.write_bytes(b"".join(pieces))
    return expected


def test_minimal_hpf_seeks_across_chunks_and_honors_data_indexes(tmp_path):
    path = tmp_path / "小样例.hpf"
    expected = _make_hpf(path, reversed_descriptors=True)
    session = open_hpf(path)
    assert session.source == session.metadata["source"] == "vendor_hpf"
    assert "finalized" not in session.metadata
    assert "termination_reason" not in session.metadata
    assert session.voltage_compatible
    assert session.sample_rate == 2000
    assert session.sample_count == 6
    assert session.duration == pytest.approx(0.003)
    assert [ch.unit for ch in session.channels] == ["V", "V"]
    np.testing.assert_array_equal(session.read(1, 4), expected[1:5])
    np.testing.assert_array_equal(np.vstack(list(session.iter_chunks(4))), expected)
    np.testing.assert_array_equal(session.read(5, 200), expected[5:])
    assert session.read(7, 1).shape == (0, 2)
    with pytest.raises(ValueError, match="不能为负"):
        session.read(-1, 1)


def test_official_fft_metadata_window_and_independent_binary_values():
    path = _vendor_path("QuickDAQ FFT Analyzer Sample Data.hpf")
    session = open_hpf(path)
    assert session.voltage_compatible
    assert session.sample_rate == 15000
    assert session.sample_count == 20480
    assert [ch.name for ch in session.channels] == ["DT9837-B(02).Ain 0", "DT9837-A(00).Ain 0"]
    np.testing.assert_array_equal(session.read(0, 3), [
        [-0.12125015258789062, -0.11894822120666504],
        [1.3139581680297852, 1.2641894817352295],
        [5.408109664916992, 5.227690696716309],
    ])
    # An independent struct oracle at known sample-file offsets, not the reader's
    # own chunk table, verifies finite seek windows at the start/middle/end.
    raw = path.read_bytes()
    data_chunk = 0x30000
    for start in (0, 3277, 20477):
        expected = []
        for column in range(2):
            offset, length = struct.unpack_from("<ii", raw, data_chunk + 32 + column * 8)
            assert length == 20480 * 8
            expected.append(struct.unpack_from("<3d", raw, data_chunk + offset + start * 8))
        np.testing.assert_array_equal(session.read(start, 3), np.array(expected).T)
    assert session.metadata["vendor_configuration_xml"].startswith("<?xml")


def test_official_logger_keeps_all_ten_native_channels_and_refuses_voltage_replay():
    path = _vendor_path("QuickDAQ Data Logger Sample Data.hpf")
    session = open_hpf(path)
    assert session.sample_rate == 10
    assert session.sample_count == 101
    assert len(session.channels) == 10
    assert session.channels[1].unit == "Deg C"
    assert not session.voltage_compatible
    with pytest.raises(HpfError, match="非电压"):
        session.read(0, 3)
    assert session.read_native().shape == (101, 10)
    np.testing.assert_array_equal(session.read_native(0, 1)[0, :2],
                                  [1.6950479745864868, 25.783716201782227])
    raw = path.read_bytes()
    chunk = 0x40000
    for column in range(10):
        offset, length = struct.unpack_from("<ii", raw, chunk + 32 + column * 8)
        assert length == 101 * 8
        expected, = struct.unpack_from("<d", raw, chunk + offset + 100 * 8)
        assert session.read_native(100, 1)[0, column] == expected
    # SensorOffset=-0.5 is retained as provenance, not silently applied to raw.
    assert session.metadata["vendor_channels"][1]["SensorOffset"] == "-0.5"


@pytest.mark.parametrize("removed", [1, 65536])
def test_truncated_block_or_missing_index_is_rejected(tmp_path, removed):
    path = tmp_path / "truncated.hpf"
    _make_hpf(path)
    path.write_bytes(path.read_bytes()[:-removed])
    with pytest.raises(HpfError, match="不完整|索引块缺失"):
        open_hpf(path)


@pytest.mark.parametrize("change,error", [
    ({"dtype": "Float"}, "Double"),
    ({"rate2": 1000}, "采样率不同"),
    ({"unequal": True}, "长度不同"),
])
def test_unsupported_type_or_nonsynchronous_data_rejected(tmp_path, change, error):
    path = tmp_path / "unsupported.hpf"
    _make_hpf(path, **change)
    with pytest.raises(HpfError, match=error):
        open_hpf(path)


@pytest.mark.parametrize("change", [{"units": ("V", "N")}, {"sensor_scale": 2}])
def test_sensor_conversion_is_not_invented(tmp_path, change):
    path = tmp_path / "sensor.hpf"
    values = _make_hpf(path, **change)
    session = open_hpf(path)
    assert not session.voltage_compatible
    np.testing.assert_array_equal(session.read_native(), values)
    with pytest.raises(HpfError):
        session.read()
    with pytest.raises(HpfError):
        export_hpf_csv(path, tmp_path / "rejected.csv")
    assert not (tmp_path / "rejected.csv").exists()


@pytest.mark.parametrize("waveform_only", [False, True])
@pytest.mark.parametrize("position,value,error", [
    (3 * 65536 + 20, 5, "样本序号不连续"),
    (4 * 65536 + 24 + 4 * 8, 65536, "索引与实际"),
    (3 * 65536, 0x4000, "不支持块"),
])
def test_gap_bad_index_and_event_chunk_are_not_silently_skipped(tmp_path, position, value, error, waveform_only):
    path = tmp_path / "bad.hpf"
    _make_hpf(path)
    data = bytearray(path.read_bytes())
    struct.pack_into("<q", data, position, value)
    path.write_bytes(data)
    with pytest.raises(HpfError, match=error):
        open_hpf(path, waveform_only=waveform_only)


def test_force_waveform_opt_in_keeps_volts_precision_and_signed_conversion(tmp_path):
    path = tmp_path / "force.hpf"
    expected = _force_hpf(path, sensitivities=(-500, 333.3330078125), offsets=(1.25, -2))
    with pytest.raises(HpfError, match="不支持块 0x8000"):
        open_hpf(path)
    session = open_hpf(path, waveform_only=True)
    assert session.voltage_compatible
    assert session.sample_count == len(expected)  # auxiliary counts are not samples
    assert [ch.index for ch in session.channels] == [0, 2]
    assert [ch.unit for ch in session.channels] == ["N", "N"]
    assert session.channels[0].inverted
    assert session.channels[1].sensitivity_mv_per_unit == 333.3330078125
    np.testing.assert_array_equal(session.read(), expected)
    force = convert_volts(session.read(), session.channels)
    np.testing.assert_allclose(force[:, 0], expected[:, 0] * -2 + 1.25)
    np.testing.assert_allclose(force[:, 1], expected[:, 1] * (1000 / 333.3330078125) - 2)
    assert session.metadata["source"] == "vendor_hpf"
    assert session.metadata["conversion_source"] == "quickdaq_configuration"
    assert session.metadata["waveform_only_note"]
    assert {chunk["chunk_id"] for chunk in session.metadata["auxiliary_chunks"]} == {0x8000, 0x9000, 0xA000}
    assert session.metadata["acquisition_config"] == {
        "sample_rate": 2000, "channels": [0, 2], "ranges": [10] * 4,
        "coupling": ["DC"] * 4, "iepe": [False] * 4,
    }


@pytest.mark.parametrize("change,error", [
    ({"filter_type": "Butterworth"}, "未滤波"),
    ({"bad_zoom": True}, "缩放映射"),
])
def test_force_profile_does_not_ignore_filter_or_auxiliary_index_mismatch(tmp_path, change, error):
    path = tmp_path / "unsupported_force.hpf"
    _force_hpf(path, **change)
    with pytest.raises(HpfError, match=error):
        open_hpf(path, waveform_only=True)


@pytest.mark.parametrize("repeat", [1, 2])
def test_local_lab_hpf_first_two_repeats_read_raw_voltage_without_csv(repeat):
    path = (Path.home() / "AppData/Local/Data Translation/QuickDAQ/Data"
            / f"150-200-4-{repeat}.hpf")
    if not path.exists():
        pytest.skip("Local historical HPF is not distributed with source")
    session = open_hpf(path, waveform_only=True)
    assert session.sample_count == 20002
    assert session.sample_rate == pytest.approx(1000.11595547295)
    assert [ch.index for ch in session.channels] == [0, 1, 2]
    assert [ch.sensitivity_mv_per_unit for ch in session.channels] == [500, 500, 333.3330078125]
    assert session.channels[2].sensitivity_mv_per_unit != 1000 / float(session.metadata["vendor_channels"][2]["SensorScale"])
    # Read-only, independent fixed-offset oracle for these two observed files.
    # No CSV, cached zoom values, or resaved HPF is used as the reference.
    with path.open("rb") as handle:
        for start in (0, 9999, 20000):
            expected = []
            for column in range(3):
                handle.seek(0x30000 + 32 + 8 * column)
                relative, length = struct.unpack("<ii", handle.read(8))
                assert length == 20002 * 8
                handle.seek(0x30000 + relative + start * 8)
                expected.append(struct.unpack("<2d", handle.read(16)))
            np.testing.assert_array_equal(session.read(start, 2), np.array(expected).T)


def test_csv_preserves_vendor_origin_relative_time_values_and_source_file(tmp_path):
    path = tmp_path / "input.hpf"
    values = _make_hpf(path)
    original = path.read_bytes()
    output = export_hpf_csv(path, tmp_path / "输出.csv", chunk_size=4)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle))
    assert metadata["source"] == "vendor_hpf"
    assert "finalized" not in metadata
    assert rows[0] == ["time_s", "测试 0_raw_V", "测试 1_raw_V"]
    exported = np.array(rows[1:], dtype=float)
    np.testing.assert_array_equal(exported[:, 1:], values)
    np.testing.assert_allclose(exported[:, 0], np.arange(6) / 2000)
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="不能覆盖"):
        export_hpf_csv(path, path)
