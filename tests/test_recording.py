import csv
import json
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from labdaq.processing import ChannelSettings, convert_volts
from labdaq.recording import (OperationCancelled, SessionRecorder, export_csv, export_txt, load_session,
                             next_recording_path, session_statistics)


def test_export_progress_reaches_total_without_changing_values(tmp_path):
    session, _, raw = _selected_recording(tmp_path)
    calls = []
    destination = export_csv(session, tmp_path / "progress.csv", chunk_size=3,
                             start=2, stop=10, channel_indices=[2, 3], values="raw",
                             time_format="samples", progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (8, 8)
    assert calls[0] == (0, 8)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        handle.readline()
        rows = list(csv.reader(handle))
    np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float)[:, 0], np.arange(2, 10))
    np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float)[:, 1:], raw[2:10, [2, 0]])


def test_statistics_progress_reaches_total_without_changing_values(tmp_path):
    session, _, raw = _selected_recording(tmp_path)
    calls = []
    result = session_statistics(session, start=2, stop=10, channel_indices=[0, 3],
                                values="raw", chunk_size=3,
                                progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (8, 8)
    assert calls[0] == (0, 8)
    expected = raw[2:10, [1, 0]]
    for column, stat in enumerate(result):
        assert stat["count"] == 8
        assert stat["mean"] == pytest.approx(expected[:, column].mean())
        assert stat["std"] == pytest.approx(expected[:, column].std())
        assert stat["rms"] == pytest.approx(np.sqrt(np.mean(expected[:, column] ** 2)))


def test_export_and_statistics_cancel_after_first_chunk(tmp_path):
    session, _, raw = _selected_recording(tmp_path)
    export_progress = []
    destination = tmp_path / "cancelled.csv"
    with pytest.raises(OperationCancelled):
        export_csv(session, destination, chunk_size=3, start=2, stop=10,
                   channel_indices=[0], values="raw", time_format="samples",
                   progress=lambda done, total: export_progress.append((done, total)),
                   cancelled=lambda: bool(export_progress) and export_progress[-1][0] >= 3)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        handle.readline()
        rows = list(csv.reader(handle))
    assert np.asarray(rows[1:], dtype=float).shape == (3, 2)
    np.testing.assert_array_equal(np.asarray(rows[1:], dtype=float),
                                  np.column_stack((np.arange(2, 5), raw[2:5, 1])))

    statistics_progress = []
    with pytest.raises(OperationCancelled):
        session_statistics(session, start=2, stop=10, channel_indices=[0], values="raw",
                           chunk_size=3,
                           progress=lambda done, total: statistics_progress.append((done, total)),
                           cancelled=lambda: bool(statistics_progress) and statistics_progress[-1][0] >= 3)
    assert statistics_progress == [(0, 8), (0, 8), (3, 8)]


def test_pre_cancelled_export_does_not_create_output(tmp_path):
    session, _, _ = _selected_recording(tmp_path)
    destination = tmp_path / "pre-cancelled.csv"
    with pytest.raises(OperationCancelled):
        export_csv(session, destination, cancelled=lambda: True)
    assert not destination.exists()


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


def test_recording_preserves_experiment_notes_group_and_requested_duration(tmp_path):
    options = {"duration_seconds": .123, "notes": "第二组，改变拉伸方向", "group_number": 2}
    with SessionRecorder(tmp_path / "session", 2000, [ChannelSettings(0, "力")],
                         recording_options=options, experiment="拖拽实验") as writer:
        options["notes"] = "changed after start"
        assert writer.metadata["target_sample_count"] == 246
    metadata = load_session(tmp_path / "session").metadata
    assert metadata["experiment"] == "拖拽实验"
    assert metadata["recording_options"] == {
        "duration_seconds": .123, "notes": "第二组，改变拉伸方向", "group_number": 2}


def test_manual_recording_defaults_do_not_set_duration_target(tmp_path):
    with SessionRecorder(tmp_path / "manual", 2000, [ChannelSettings(0, "力")]):
        pass
    metadata = load_session(tmp_path / "manual").metadata
    assert metadata["recording_options"] == {"duration_seconds": None, "notes": "", "group_number": 0}
    assert metadata["target_sample_count"] is None
    assert metadata["experiment"] == ""


@pytest.mark.parametrize("samples, expected", [(0.5, 1), (1.5, 2), (2.499999, 2), (2.5, 3), (2.500001, 3)])
def test_recording_duration_rounds_half_samples_up(tmp_path, samples, expected):
    with SessionRecorder(tmp_path / "rounded", 2000, [ChannelSettings(0, "力")],
                         recording_options={"duration_seconds": samples / 2000}) as writer:
        assert writer.metadata["target_sample_count"] == expected


@pytest.mark.parametrize("options", [
    {"duration_seconds": 0}, {"duration_seconds": -1}, {"duration_seconds": float("nan")},
    {"duration_seconds": float("inf")}, {"duration_seconds": 1e308},
    {"duration_seconds": "2"}, {"duration_seconds": True},
    {"group_number": -1}, {"group_number": 1.5}, {"notes": None},
])
def test_invalid_recording_options_do_not_create_directory(tmp_path, options):
    path = tmp_path / "invalid"
    with pytest.raises(ValueError):
        SessionRecorder(path, 2000, [ChannelSettings(0, "力")], recording_options=options)
    assert not path.exists()


def test_next_recording_path_uses_local_day_and_next_matching_group(tmp_path):
    now = datetime(2026, 10, 19, 23, 59)
    root = tmp_path / "recordings"
    path, group = next_recording_path(root, "拉伸测试", now=now)
    assert path == root / "20261019_拉伸测试_001" and group == 1
    assert not root.exists()
    path.mkdir(parents=True)
    (root / "20261019_拉伸测试_003").mkdir()
    (root / "20261018_拉伸测试_999").mkdir()
    (root / "20261019_另一实验_999").mkdir()
    (root / "20261019_拉伸测试_123_old").mkdir()
    assert next_recording_path(root, "拉伸测试", now=now) == (root / "20261019_拉伸测试_004", 4)
    (root / "20261019_拉伸测试_999").mkdir()
    assert next_recording_path(root, "拉伸测试", now=now) == (root / "20261019_拉伸测试_1000", 1000)


def test_next_recording_path_sanitizes_windows_experiment_name(tmp_path):
    now = datetime(2026, 10, 19)
    path, group = next_recording_path(tmp_path, ' 力<>:"/\\|?*\x00 .', now=now)
    assert path.name == "20261019_力___________001" and group == 1
    assert next_recording_path(tmp_path, " . ", now=now)[0].name == "20261019_实验_001"


def _selected_recording(tmp_path):
    channels = [ChannelSettings(3, "拉力 Z", sensitivity_mv_per_unit=20,
                                zero_volts=.1, inverted=True),
                ChannelSettings(0, "电压", unit="V", zero_volts=-.2),
                ChannelSettings(2, "拉力 X", sensitivity_mv_per_unit=50, zero_volts=-.3)]
    raw = np.arange(39, dtype=float).reshape(13, 3) / 100 - .2
    path = tmp_path / "selected"
    with SessionRecorder(path, 1999.5, channels) as recorder:
        recorder.write(raw)
    return load_session(path), channels, raw


@pytest.mark.parametrize("time_format, time_column, scale", [
    ("seconds", "time_s", 1), ("milliseconds", "time_ms", 1e3),
    ("microseconds", "time_us", 1e6), ("samples", "sample_index", None),
])
@pytest.mark.parametrize("values", ["raw", "engineering", "both"])
def test_selected_export_preserves_scan_time_channel_order_and_conversion(
        tmp_path, monkeypatch, time_format, time_column, scale, values):
    session, channels, raw = _selected_recording(tmp_path)
    calls = []
    read = session.read

    def traced_read(start, count):
        calls.append((start, count))
        return read(start, count)

    monkeypatch.setattr(session, "read", traced_read)
    destination = export_csv(session, tmp_path / "selected.csv", chunk_size=3,
                             start=2, stop=10, channel_indices=[2, 3],
                             values=values, time_format=time_format)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle))
    assert calls == [(2, 3), (5, 3), (8, 2)]
    assert rows[0][0] == time_column
    selected_raw = raw[2:10, [2, 0]]
    selected_channels = [channels[2], channels[0]]
    expected_headings = [time_column]
    if values in ("raw", "both"):
        expected_headings += ["拉力 X[AI2]_raw_V", "拉力 Z[AI3]_raw_V"]
    if values in ("engineering", "both"):
        expected_headings += ["拉力 X[AI2]_N", "拉力 Z[AI3]_N"]
    assert rows[0] == expected_headings
    numbers = np.array(rows[1:], dtype=float)
    np.testing.assert_allclose(numbers[:, 0], np.arange(2, 10) if scale is None
                               else np.arange(2, 10) / session.sample_rate * scale)
    if values == "raw":
        expected = selected_raw
    elif values == "engineering":
        expected = convert_volts(selected_raw, selected_channels)
    else:
        expected = np.column_stack((selected_raw, convert_volts(selected_raw, selected_channels)))
    np.testing.assert_allclose(numbers[:, 1:], expected)
    assert metadata["sample_count"] == 13
    assert metadata["export"] == {
        "scan_start": 2, "scan_stop": 10, "exported_sample_count": 8,
        "channel_indices": [2, 3], "values": values, "time_format": time_format,
        "delimiter": ",",
    }
    assert "export" not in session.metadata


def test_tab_text_export_is_quoted_and_contains_selection_metadata(tmp_path):
    path = tmp_path / "tab-session"
    with SessionRecorder(path, 1000, [ChannelSettings(2, "力\tZ", unit="V")]) as recorder:
        recorder.write([[.25], [.5]])
    destination = export_txt(path, tmp_path / "导出.txt", start=1, values="raw")
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle, delimiter="\t"))
    assert metadata["export"]["delimiter"] == "\t"
    assert metadata["export"]["exported_sample_count"] == 1
    assert rows == [["time_s", "力\tZ[AI2]_raw_V"], ["0.001", "0.5"]]


@pytest.mark.parametrize("values", ["raw", "engineering"])
def test_interval_statistics_match_numpy_and_only_read_selected_interval(tmp_path, monkeypatch, values):
    session, channels, raw = _selected_recording(tmp_path)
    calls = []
    read = session.read

    def traced_read(start, count):
        calls.append((start, count))
        return read(start, count)

    monkeypatch.setattr(session, "read", traced_read)
    result = session_statistics(session, start=2, stop=10, channel_indices=[0, 3, 2],
                                values=values, chunk_size=3)
    assert calls == [(2, 3), (5, 3), (8, 2)]
    selected = [channels[1], channels[0], channels[2]]
    expected = raw[2:10, [1, 0, 2]]
    if values == "engineering":
        expected = convert_volts(expected, selected)
    for column, (stat, channel) in enumerate(zip(result, selected)):
        data = expected[:, column]
        assert stat["index"] == channel.index
        assert stat["name"] == channel.name
        assert stat["unit"] == ("V" if values == "raw" else channel.unit)
        assert stat["count"] == 8
        for key, value in {"min": data.min(), "max": data.max(), "delta": np.ptp(data),
                           "mean": data.mean(), "std": data.std(),
                           "rms": np.sqrt(np.mean(data ** 2))}.items():
            assert stat[key] == pytest.approx(value)


def test_statistics_preserve_small_variation_on_large_dc_offset(tmp_path):
    data = 1e12 + np.tile(np.array([-2., -1., 0., 1., 2.]), 1001)
    path = tmp_path / "dc"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "DC", unit="V")]) as recorder:
        recorder.write(data[:, None])
    result = session_statistics(path, values="raw", chunk_size=5)[0]
    assert result["std"] == pytest.approx(np.std(data), rel=1e-12)
    assert result["mean"] == pytest.approx(1e12)
    assert result["rms"] == pytest.approx(1e12)


def test_statistics_constant_large_offset_does_not_square_mean(tmp_path):
    path = tmp_path / "large-dc"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "DC", unit="V")]) as recorder:
        recorder.write(np.full((7, 1), 1e200))
    result = session_statistics(path, values="raw", chunk_size=2)[0]
    assert result["std"] == 0
    assert result["rms"] == 1e200


def test_export_and_statistics_accept_hpf_reader_and_preserve_source(tmp_path, monkeypatch):
    from labdaq.hpf import HpfRecording

    path = tmp_path / "source.hpf"
    raw = np.array([[.2, -.3], [.4, -.5], [.6, -.7], [.8, -.9], [1., -1.1]])
    path.write_bytes(raw[:, 0].astype("<f8").tobytes() + raw[:, 1].astype("<f8").tobytes())
    channels = [ChannelSettings(1, "X", unit="V"), ChannelSettings(3, "Y", unit="V")]
    metadata = {"sample_count": len(raw), "sample_rate": 2000,
                "channels": [vars(channel) for channel in channels], "source": "vendor_hpf",
                "voltage_replay_error": ""}
    session = HpfRecording(path, metadata, [SimpleNamespace(start=0, count=5,
                                                           channel_offsets=(0, 40))])
    destination = export_csv(session, tmp_path / "hpf.csv", start=1, stop=4,
                             channel_indices=[3], values="raw", chunk_size=2)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        exported_metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle))
    assert exported_metadata["source"] == "vendor_hpf"
    np.testing.assert_allclose(np.array(rows[1:], dtype=float)[:, 1], raw[1:4, 1])
    result = session_statistics(session, start=1, stop=4, channel_indices=[3], chunk_size=2)[0]
    assert result["mean"] == pytest.approx(raw[1:4, 1].mean())
    with pytest.raises(ValueError, match="不能覆盖"):
        export_csv(session, path)
    def open_waveform(input_path, *, waveform_only):
        assert input_path == path and waveform_only
        return session
    monkeypatch.setattr("labdaq.hpf.open_hpf", open_waveform)
    assert session_statistics(path, values="raw")[0]["mean"] == pytest.approx(raw[:, 0].mean())
    np.testing.assert_array_equal(session.read(), raw)


@pytest.mark.parametrize("selection", [
    {"start": -1}, {"start": 1.5}, {"start": True}, {"stop": 14},
    {"stop": 2.5}, {"stop": False}, {"start": 5, "stop": 3},
    {"chunk_size": 0}, {"chunk_size": 1.5}, {"chunk_size": True},
    {"channel_indices": []}, {"channel_indices": [1]}, {"channel_indices": [3, 3]},
    {"channel_indices": [0.0]}, {"channel_indices": [False]},
])
def test_invalid_selections_are_rejected_before_export_creates_file(tmp_path, selection):
    session, _, _ = _selected_recording(tmp_path)
    destination = tmp_path / "invalid.csv"
    with pytest.raises(ValueError):
        export_csv(session, destination, **selection)
    assert not destination.exists()
    with pytest.raises(ValueError):
        session_statistics(session, **selection)


@pytest.mark.parametrize("selection", [
    {"values": "invalid"}, {"time_format": "minutes"}, {"delimiter": ";"},
])
def test_invalid_export_modes_do_not_create_file(tmp_path, selection):
    session, _, _ = _selected_recording(tmp_path)
    destination = tmp_path / "invalid.csv"
    with pytest.raises(ValueError):
        export_csv(session, destination, **selection)
    assert not destination.exists()


def test_empty_export_and_statistics_boundary_behavior(tmp_path):
    session, _, _ = _selected_recording(tmp_path)
    destination = export_csv(session, tmp_path / "empty.csv", start=13, stop=13)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        assert len(list(csv.reader(handle))) == 1
    assert metadata["export"]["exported_sample_count"] == 0
    with pytest.raises(ValueError, match="至少需要一个"):
        session_statistics(session, start=13)
    with pytest.raises(ValueError, match="统计数值"):
        session_statistics(session, values="both")
    with pytest.raises(ValueError, match="不能覆盖"):
        export_csv(session, session.path / "metadata.json")


@pytest.mark.parametrize("time_format, scale", [
    ("seconds", .001), ("milliseconds", 1), ("microseconds", 1000), ("samples", 1),
])
def test_recording_segments_keep_pause_gap_in_time_lookup_and_export(tmp_path, time_format, scale):
    path = tmp_path / "paused"
    with SessionRecorder(path, 1000, [ChannelSettings(0, "力")],
                         acquisition_config={"record_start_scan": 100}) as writer:
        writer.write([[1], [2], [3]], source_start_scan=100)
        writer.set_paused(True)
        writer.set_paused(False, 110)
        writer.write([[4], [5]], source_start_scan=110)
    session = load_session(path)
    np.testing.assert_array_equal(session.sample_indices(), [0, 1, 2, 10, 11])
    np.testing.assert_allclose(session.sample_times(1, 3), [.001, .002, .010])
    assert session.duration == .005 and session.elapsed_duration == .012
    assert session.recording_segments == [
        {"file_start_scan": 0, "source_start_scan": 100, "sample_count": 3},
        {"file_start_scan": 3, "source_start_scan": 110, "sample_count": 2},
    ]
    for seconds in [-1, 0, .001, .0015, .002, .003, .009, .010, .011, .012, 1]:
        for side in ("left", "right"):
            assert session.sample_index_at_time(seconds, side) == np.searchsorted(
                session.sample_times(), seconds, side=side)
    destination = export_csv(session, tmp_path / f"{time_format}.csv", time_format=time_format,
                             values="raw", chunk_size=2)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        handle.readline()
        rows = list(csv.reader(handle))
    exported = np.asarray(rows[1:], dtype=float)
    np.testing.assert_allclose(exported[:, 0], np.array([0, 1, 2, 10, 11]) * scale)
    np.testing.assert_array_equal(exported[:, 1], [1, 2, 3, 4, 5])
    assert session_statistics(session, values="raw")[0]["count"] == 5


def test_live_segment_reader_extends_only_the_published_active_source(tmp_path):
    path = tmp_path / "live"
    writer = SessionRecorder(path, 2000, [ChannelSettings(0, "力")],
                             acquisition_config={"record_start_scan": 40})
    try:
        writer.write([[1], [2]], source_start_scan=40)
        np.testing.assert_array_equal(load_session(path).sample_indices(), [0, 1])
        writer.set_paused(True)
        assert load_session(path).metadata["recording_paused"]
        writer.set_paused(False, 70)
        assert load_session(path).sample_count == 2
        writer.write([[3], [4]], source_start_scan=70)
        np.testing.assert_array_equal(load_session(path).sample_indices(), [0, 1, 30, 31])
    finally:
        writer.close()
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("recording_segments")
    metadata.pop("active_segment")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    legacy = load_session(path)
    np.testing.assert_array_equal(legacy.sample_indices(), np.arange(4))
    assert legacy.elapsed_duration == legacy.duration


@pytest.mark.parametrize("sample_rate", [195.313, 1999.5, 48000.0])
def test_segment_time_lookup_matches_searchsorted_at_fractional_clock_boundaries(tmp_path, sample_rate):
    path = tmp_path / "fractional-clock"
    with SessionRecorder(path, sample_rate, [ChannelSettings(0, "力")],
                         acquisition_config={"record_start_scan": 1000000}) as writer:
        writer.write(np.ones((3, 1)), source_start_scan=1000000)
        writer.write(np.ones((7, 1)), source_start_scan=1001000)
        writer.write(np.ones((4, 1)), source_start_scan=1300000)
    session = load_session(path)
    times = session.sample_times()
    probes = np.concatenate((times, np.nextafter(times, -np.inf), np.nextafter(times, np.inf),
                             (times[:-1] + times[1:]) / 2))
    for seconds in probes:
        for side in ("left", "right"):
            assert session.sample_index_at_time(float(seconds), side) == np.searchsorted(times, seconds, side=side)
