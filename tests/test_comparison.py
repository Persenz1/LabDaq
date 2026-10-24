import csv
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from labdaq.comparison import (ComparisonSource, comparison_bounds, comparison_settings,
                               export_comparison_csv, load_comparison, load_comparison_profile,
                               save_comparison_profile)
from labdaq.processing import ChannelSettings, convert_volts
from labdaq.recording import OperationCancelled, SessionRecorder, load_session


def _recording(root, name="group", *, fs=10, channels=None, raw=None, start_scan=40,
               acquisition_epoch="2026-10-24T01:00:00+00:00", error=None):
    path = root / name
    channels = channels or [ChannelSettings(0, "电压", unit="V"),
                            ChannelSettings(3, "力", sensitivity_mv_per_unit=20, zero_volts=.1)]
    if raw is None:
        raw = np.arange(12, dtype=float).reshape(6, 2) / 100
    with SessionRecorder(path, fs, channels,
                         acquisition_config={"record_start_scan": start_scan,
                                             "acquisition_started_at_utc": acquisition_epoch}) as writer:
        writer.write(raw, source_start_scan=start_scan)
        writer.close(termination_reason="acquisition_error" if error else "user_stop", error=error)
    return path


def _metadata(path, **changes):
    metadata_path = path / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(changes)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")


def test_source_uses_saved_channels_real_alignment_and_allows_acquisition_error(tmp_path):
    path = _recording(tmp_path, start_scan=100, error="设备断连")
    source = ComparisonSource.from_path(path, [3])
    assert source.channel_indices == (3,)
    assert source.channels == (load_session(path).channels[1],)
    assert source.zero_seconds == 0
    assert source.sample_origin == 0
    assert source.shift_seconds == 0
    assert source.has_acquisition_error
    assert source.session.metadata["error"] == "设备断连"
    assert ComparisonSource.from_session(load_session(path)).channel_indices == (0, 3)
    aligned = source.set_time_zero(.26)
    assert aligned.zero_seconds == .3
    assert aligned.sample_origin == 0
    assert aligned.with_shift(-2.5).shift_seconds == -2.5
    assert aligned.set_sample_origin(4).sample_origin == 4
    with pytest.raises(ValueError, match="真实样本"):
        ComparisonSource.from_path(path, zero_seconds=.25)
    with pytest.raises(ValueError, match="真实源样本"):
        ComparisonSource.from_path(path, sample_origin=99)
    with pytest.raises(ValueError, match="不重复"):
        ComparisonSource.from_path(path, [])
    with pytest.raises(ValueError, match="不重复"):
        ComparisonSource.from_path(path, [3, 3])


def test_unfinished_empty_and_nonfinite_inputs(tmp_path):
    unfinished = tmp_path / "unfinished"
    writer = SessionRecorder(unfinished, 10, [ChannelSettings(0, "X")])
    try:
        writer.write([[1.]])
        with pytest.raises(ValueError, match="尚未结束"):
            ComparisonSource.from_path(unfinished)
    finally:
        writer.close()
    recovered = load_session(unfinished)
    (unfinished / "raw.f64").unlink()
    with pytest.raises(ValueError, match="不可读"):
        ComparisonSource.from_session(recovered)
    empty_path = _recording(tmp_path, "empty", raw=np.empty((0, 1)),
                            channels=[ChannelSettings(2, "空")])
    empty = ComparisonSource.from_path(empty_path)
    assert (empty.zero_seconds, empty.sample_origin) == (0, 0)
    assert comparison_bounds([empty], "relative") == (0, 0)
    result = load_comparison([empty], span=1)
    assert result.traces == () and result.frame_count == result.value_count == 0
    assert not result.truncated and result.source_frames == (0,)
    for alignment in ({"zero_seconds": 1}, {"sample_origin": 1}):
        with pytest.raises(ValueError):
            ComparisonSource.from_path(empty_path, **alignment)
    with pytest.raises(ValueError):
        empty.set_time_zero(1)
    assert empty.set_time_zero(0) is empty


def test_relative_absolute_and_sample_axes_use_real_indices_and_preview_offset(tmp_path):
    path = _recording(tmp_path, fs=10, start_scan=50,
                      raw=np.arange(6, dtype=float)[:, None], channels=[ChannelSettings(1, "X", unit="V")])
    source = ComparisonSource.from_path(path).set_time_zero(.2).set_sample_origin(1).with_shift(3)
    relative = load_comparison([source], axis="relative", values="raw", start=0, span=3,
                               chunk_size=2).traces[0]
    np.testing.assert_allclose(relative.times, np.arange(2, 6) / 10)
    np.testing.assert_allclose(relative.x, [0, .1, .2, .3])
    np.testing.assert_array_equal(relative.sample_indices, np.arange(2, 6))
    samples = load_comparison([source], axis="samples", values="raw", start=0, span=2).traces[0]
    np.testing.assert_array_equal(samples.sample_indices, [1, 2])
    np.testing.assert_array_equal(samples.x, [0, 1])
    includes_stop = load_comparison([source], axis="samples", values="raw", start=0, span=3).traces[0]
    np.testing.assert_array_equal(includes_stop.sample_indices, [1, 2, 3])
    epoch = datetime(2026, 10, 24, 1, tzinfo=timezone.utc).timestamp() + 5
    absolute = load_comparison([source], axis="absolute", values="raw",
                               start=epoch + 3.1, span=.2).traces[0]
    np.testing.assert_allclose(absolute.x, epoch + 3 + np.array([.1, .2]))
    assert comparison_bounds([source], "relative") == (-.2, .3)
    assert comparison_bounds([source], "samples") == (-1, 4)
    assert comparison_bounds([source], "absolute") == (epoch + 3, epoch + 3.5)


def test_bounds_and_started_time_fallback_span_different_rates_and_lengths(tmp_path):
    short_path = _recording(tmp_path, "short", fs=10, raw=np.ones((3, 1)),
                            channels=[ChannelSettings(0, "短", unit="V")])
    long_path = _recording(tmp_path, "long", fs=4, raw=np.ones((9, 1)),
                           channels=[ChannelSettings(2, "长", unit="V")], acquisition_epoch=None)
    started = "2026-10-24T02:00:00+00:00"
    _metadata(long_path, started_at_utc=started)
    short = ComparisonSource.from_path(short_path)
    long = ComparisonSource.from_path(long_path)
    assert comparison_bounds([short, long], "relative") == (0, 2)
    epoch = datetime.fromisoformat(started).timestamp()
    assert comparison_bounds([short, long], "absolute")[1] == epoch + 2
    result = load_comparison([short, long], axis="absolute", start=epoch, span=3)
    assert result.source_frames == (0, 9)
    np.testing.assert_allclose(result.traces[0].times, np.arange(9) / 4)
    assert result.traces[0].sample_indices[-1] == 8


def test_missing_epoch_and_invalid_axes_or_windows_are_rejected(tmp_path):
    path = _recording(tmp_path)
    _metadata(path, started_at_utc=None, acquisition_started_at_utc=None)
    source = ComparisonSource.from_path(path)
    for operation in (lambda: comparison_bounds([source], "absolute"),
                      lambda: load_comparison([source], axis="absolute")):
        with pytest.raises(ValueError, match="可靠采集时间"):
            operation()
    with pytest.raises(ValueError):
        comparison_bounds([source], "bad")
    for options in ({"axis": "bad"}, {"values": "both"}, {"start": float("nan")},
                    {"span": 0}, {"span": float("inf")}, {"max_frames_per_source": 0},
                    {"max_values": 1.5}, {"chunk_size": True}):
        with pytest.raises(ValueError):
            load_comparison([source], **options)
    before_zero = load_comparison([source], start=-1, span=2)
    assert before_zero.frame_count == source.session.sample_count
    np.testing.assert_array_equal(before_zero.traces[0].sample_indices, np.arange(6))


def test_window_values_different_rates_and_pause_gaps_without_resampling(tmp_path):
    channels = [ChannelSettings(3, "力", sensitivity_mv_per_unit=20, zero_volts=.1),
                ChannelSettings(0, "位移", unit="mm", sensitivity_mv_per_unit=50, inverted=True)]
    raw = np.column_stack((np.arange(8) / 10, np.arange(8) / 20))
    path = _recording(tmp_path, "paused", fs=4, channels=channels, raw=raw, start_scan=100)
    writer_path = tmp_path / "gapped"
    with SessionRecorder(writer_path, 4, channels,
                         acquisition_config={"record_start_scan": 100}) as writer:
        writer.write(raw[:3], source_start_scan=100)
        writer.write(raw[3:], source_start_scan=108)
    first = ComparisonSource.from_path(path)
    second = ComparisonSource.from_path(writer_path, [3, 0])
    result = load_comparison([first, second], values="engineering", start=0, span=3.1,
                             max_frames_per_source=100, chunk_size=2)
    assert len(result.traces) == 4 and not result.truncated
    assert result.frame_count == 16 and result.value_count == 32
    trace = result.traces[2]
    np.testing.assert_array_equal(trace.sample_indices, [0, 1, 2, 8, 9, 10, 11, 12])
    np.testing.assert_array_equal(trace.gaps, [3])
    np.testing.assert_allclose(trace.times, np.array([0, 1, 2, 8, 9, 10, 11, 12]) / 4)
    np.testing.assert_allclose(trace.raw_values, raw[:, 0])
    np.testing.assert_allclose(trace.engineering_values, convert_volts(raw, channels)[:, 0])
    assert not trace.raw_values.flags.writeable and trace.finite_mask.all()
    assert result.traces[3].unit == "mm"
    assert result.traces[2].label == "gapped/AI3/力"
    assert result.traces[2].record_path == writer_path and result.traces[2].source_index == 1


def test_frame_and_total_value_caps_are_explicit(tmp_path):
    first = ComparisonSource.from_path(_recording(tmp_path, "a"))
    second = ComparisonSource.from_path(_recording(tmp_path, "b"))
    limited = load_comparison([first, second], start=0, span=10,
                              max_frames_per_source=4, max_values=12, chunk_size=2)
    assert limited.truncated
    assert limited.source_frames == (4, 2)
    assert limited.frame_count == 6 and limited.value_count == 12
    assert [len(trace.x) for trace in limited.traces] == [4, 4, 2, 2]
    tiny = load_comparison([first], start=0, span=10, max_values=1)
    assert tiny.truncated and tiny.traces == () and tiny.value_count == 0
    empty_window = load_comparison([first], start=100, span=2)
    assert empty_window.traces == () and not empty_window.truncated


def test_load_progress_cancellation_and_nonfinite_raw(tmp_path):
    source = ComparisonSource.from_path(_recording(tmp_path))
    calls = []
    with pytest.raises(OperationCancelled):
        load_comparison([source], span=10, chunk_size=2,
                        cancelled=lambda: bool(calls) and calls[-1][0] >= 2,
                        progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (2, 6)
    path = _recording(tmp_path, "nan", raw=np.array([[.1], [np.nan], [.2]]),
                      channels=[ChannelSettings(0, "X")])
    with pytest.raises(ValueError, match="nan.*NaN"):
        load_comparison([ComparisonSource.from_path(path)], span=1, chunk_size=1)


def test_long_csv_export_roundtrip_metadata_progress_and_source_protection(tmp_path):
    path = _recording(tmp_path, "导出源")
    source = ComparisonSource.from_path(path, [3, 0])
    result = load_comparison([source], values="raw", start=.1, span=.21, chunk_size=2)
    before = (path / "raw.f64").read_bytes()
    destination = tmp_path / "比较.csv"
    calls = []
    export_comparison_csv(result, destination, chunk_size=2,
                          progress=lambda done, total: calls.append((done, total)))
    assert calls[0] == (0, 6) and calls[-1] == (6, 6)
    with destination.open(encoding="utf-8-sig", newline="") as handle:
        metadata = json.loads(handle.readline().removeprefix("# labdaq_metadata: "))
        rows = list(csv.reader(handle))
    assert rows[0] == ["recording", "channel_index", "channel_name", "sample_index",
                       "source_time_s", "comparison_x", "raw_V", "value", "unit"]
    assert len(rows) == 7
    assert metadata["format"] == "labdaq-comparison-csv-v1"
    assert metadata["axis"] == result.axis and metadata["window"] == {"start": .1, "span": .21}
    assert metadata["sources"][0]["path"] == str(path)
    assert metadata["sources"][0]["metadata"]["channels"] == result.sources[0].session.metadata["channels"]
    first = rows[1]
    assert first[:3] == [str(path), "3", "力"]
    assert float(first[3]) == 1 and float(first[4]) == .1 and float(first[5]) == .1
    np.testing.assert_allclose([float(row[6]) for row in rows[1:4]], result.traces[0].raw_values)
    assert rows[4][1] == "0"
    assert (path / "raw.f64").read_bytes() == before
    with pytest.raises(FileExistsError):
        export_comparison_csv(result, destination)
    export_comparison_csv(result, destination, overwrite=True, values="engineering", delimiter="\t")
    for protected in (path / "raw.f64", path / "metadata.json"):
        with pytest.raises(ValueError, match="不能覆盖"):
            export_comparison_csv(result, protected, overwrite=True)


def test_export_cancellation_leaves_no_partial_or_replaced_output(tmp_path):
    source = ComparisonSource.from_path(_recording(tmp_path))
    result = load_comparison([source], span=10)
    destination = tmp_path / "cancel.csv"
    calls = []
    with pytest.raises(OperationCancelled):
        export_comparison_csv(result, destination, chunk_size=2,
                              cancelled=lambda: bool(calls) and calls[-1][0] >= 2,
                              progress=lambda done, total: calls.append((done, total)))
    assert not destination.exists()
    destination.write_text("original", encoding="utf-8")
    with pytest.raises(OperationCancelled):
        export_comparison_csv(result, destination, overwrite=True, cancelled=lambda: True)
    assert destination.read_text(encoding="utf-8") == "original"


def test_comparison_profile_roundtrip_and_strict_validation(tmp_path):
    first = ComparisonSource.from_path(_recording(tmp_path, "one"), [3]).set_time_zero(.2).with_shift(-1)
    second = ComparisonSource.from_path(_recording(tmp_path, "two"), [0, 3])
    settings = comparison_settings([first, second], "samples", 1, 2.5, "engineering",
                                   {"0:3": {"visible": False, "color": "#123456"},
                                    "1:0": {"visible": True, "color": "red"}},
                                   view={"splitter_sizes": [333, 900]})
    destination = tmp_path / "layout.json"
    save_comparison_profile(destination, settings)
    loaded = load_comparison_profile(destination)
    assert loaded.axis == "samples" and loaded.start == 1 and loaded.span == 2.5
    assert loaded.values == "engineering" and loaded.appearance == settings["appearance"]
    assert loaded.view == {"splitter_sizes": [333, 900]}
    assert [source.path for source in loaded.sources] == [first.path, second.path]
    assert loaded.sources[0].channel_indices == (3,)
    assert loaded.sources[0].zero_seconds == .2 and loaded.sources[0].shift_seconds == -1
    assert comparison_settings(loaded.sources, loaded.axis, loaded.start, loaded.span,
                               loaded.values, loaded.appearance, loaded.view) == settings
    with pytest.raises(ValueError, match="分隔条"):
        save_comparison_profile(tmp_path / "invalid-view.json", dict(settings, view={"splitter_sizes": [-1, 2]}))
    invalid = dict(settings, schema="labdaq-comparison-v2")
    with pytest.raises(ValueError, match="版本"):
        save_comparison_profile(tmp_path / "bad.json", invalid)
    missing = dict(settings, appearance={})
    missing["sources"] = [dict(settings["sources"][0], path=str(tmp_path / "missing"))]
    save_comparison_profile(destination, missing)
    with pytest.raises((OSError, ValueError)):
        load_comparison_profile(destination)
    for key, value in (("axis", "bad"), ("values", "both"), ("window", {"start": 0, "span": 0}),
                       ("appearance", {"9:3": {"visible": True, "color": "red"}})):
        bad = dict(settings, **{key: value})
        with pytest.raises(ValueError):
            save_comparison_profile(tmp_path / "invalid.json", bad)
    bad_source = dict(settings)
    bad_source["sources"] = [dict(settings["sources"][0], zero_seconds=.25), settings["sources"][1]]
    save_comparison_profile(destination, bad_source)
    with pytest.raises(ValueError, match="真实样本"):
        load_comparison_profile(destination)
