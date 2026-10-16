import csv
import json

import numpy as np
import pytest

from labdaq.processing import ChannelSettings, convert_volts
from labdaq.recording import SessionRecorder, export_csv, load_session


def test_recording_replay_preserves_voltage_order_and_partial_final_chunk(tmp_path):
    path = tmp_path / "拖拽实验"
    channels = [ChannelSettings(0, "力 X"), ChannelSettings(3, "力 Z", inverted=True)]
    data = np.arange(46, dtype=float).reshape(23, 2) * 0.001
    with SessionRecorder(path, 1999.5, channels, acquisition_config={"coupling": ["DC", "DC"]}) as writer:
        writer.write(data[:3])
        writer.write(data[3:17])
        writer.write(data[17:])
    session = load_session(path)
    chunks = list(session.iter_chunks(10))
    assert [len(chunk) for chunk in chunks] == [10, 10, 3]
    np.testing.assert_array_equal(np.vstack(chunks), data)
    assert session.sample_count == 23
    assert session.sample_rate == 1999.5
    assert session.duration == pytest.approx(23 / 1999.5)
    assert session.channels == channels
    assert session.metadata["source"] == "simulation"
    assert session.metadata["finalized"] is True
    assert session.metadata["started_at_utc"].endswith("+00:00")
    assert (path / "raw.f64").stat().st_size == 23 * 2 * 8
    np.testing.assert_array_equal(session.read(start=7, count=5), data[7:12])
    np.testing.assert_array_equal(session.read(start=21, count=5), data[21:])
    assert session.read(start=23, count=5).shape == (0, 2)


def test_recording_takes_snapshot_of_conversion_parameters(tmp_path):
    channel = ChannelSettings(1, "拉力", zero_volts=0.2)
    with SessionRecorder(tmp_path / "session", 2000, [channel]) as writer:
        channel.zero_volts = 0.5
        writer.write([[0.3]])
    assert load_session(tmp_path / "session").channels[0].zero_volts == 0.2


def test_export_preserves_metadata_and_uses_actual_rate_for_time(tmp_path):
    path = tmp_path / "session"
    channels = [ChannelSettings(1, "力", sensitivity_mv_per_unit=20, zero_volts=0.02)]
    raw = np.array([[0.02], [0.22], [-0.02]])
    with SessionRecorder(path, 1999.5, channels) as writer:
        writer.write(raw)
    destination = export_csv(path, tmp_path / "导出.csv", chunk_size=2)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
        metadata = json.loads(first_line.removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle))
    assert metadata["channels"][0]["sensitivity_mv_per_unit"] == 20
    assert metadata["source"] == "simulation"
    assert rows[0] == ["time_s", "力[AI1]_raw_V", "力[AI1]_N"]
    exported = np.array(rows[1:], dtype=float)
    np.testing.assert_allclose(exported[:, 0], np.arange(3) / 1999.5)
    np.testing.assert_array_equal(exported[:, 1], raw[:, 0])
    np.testing.assert_allclose(exported[:, 2], convert_volts(raw, channels)[:, 0])


def test_unfinalized_whole_frames_can_be_recovered(tmp_path):
    path = tmp_path / "interrupted"
    writer = SessionRecorder(path, 2000, [ChannelSettings(0, "力")])
    try:
        writer.write([[0.1], [0.2], [0.3]])
        reader = load_session(path)
        assert reader.metadata["finalized"] is False
        assert reader.sample_count == 3
        np.testing.assert_array_equal(np.vstack(list(reader.iter_chunks(2))), [[0.1], [0.2], [0.3]])
    finally:
        writer.close()


def test_partial_frame_and_finalized_length_mismatch_are_reported(tmp_path):
    path = tmp_path / "damaged"
    with SessionRecorder(path, 2000, [ChannelSettings(0, "力")]) as writer:
        writer.write([[0.1]])
    with (path / "raw.f64").open("ab") as handle:
        handle.write(b"\x00")
    with pytest.raises(ValueError, match="不足一个"):
        load_session(path)
    with (path / "raw.f64").open("ab") as handle:
        handle.write(b"\x00" * 7)
    with pytest.raises(ValueError, match="样本数不一致"):
        load_session(path)


def test_session_creation_and_export_do_not_overwrite_raw_recording(tmp_path):
    path = tmp_path / "session"
    with SessionRecorder(path, 2000, [ChannelSettings(0, "力")]):
        pass
    with pytest.raises(FileExistsError):
        SessionRecorder(path, 2000, [ChannelSettings(0, "力")])
    with pytest.raises(ValueError, match="不能覆盖"):
        export_csv(path, path / "raw.f64")
    assert load_session(path).sample_count == 0


def test_aborted_capture_preserves_first_termination_reason_and_error(tmp_path):
    path = tmp_path / "disconnected"
    writer = SessionRecorder(path, 2000, [ChannelSettings(0, "力")])
    writer.write([[0.1], [0.2]])
    writer.close(termination_reason="acquisition_error", error="DeviceDisconnected: 设备断连")
    writer.close()
    session = load_session(path)
    assert session.metadata["finalized"] is True
    assert session.metadata["termination_reason"] == "acquisition_error"
    assert session.metadata["error"] == "DeviceDisconnected: 设备断连"
    assert session.sample_count == 2
