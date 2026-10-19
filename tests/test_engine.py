import time
import threading

import numpy as np
import pytest

from labdaq.engine import AcquisitionEngine
from labdaq.processing import ChannelSettings
from labdaq.protocol import AcquisitionConfig
from labdaq.recording import load_session


def wait_until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_worker_records_without_display_refresh(tmp_path):
    settings = [ChannelSettings(i, f"force{i}") for i in (0, 1, 3)]
    engine = AcquisitionEngine(AcquisitionConfig(sample_rate=2000, channels=(0, 1, 3)), settings, "sine")
    engine.start()
    try:
        path = tmp_path / "recording"
        engine.start_recording(path, settings)
        wait_until(lambda: engine.recorded_scans >= 800)
        # No snapshot or GUI updates are needed for the recorder to progress.
        engine.stop_recording()
        session = load_session(path)
        assert session.sample_count == engine.recorded_scans
        assert session.metadata["source"] == "simulation"
        assert session.read(count=10).shape == (10, 3)
        t, raw = engine.snapshot(seconds=.1)
        assert raw.shape[1] == 3 and len(raw) == len(t)
        np.testing.assert_allclose(np.diff(t), 1 / engine.sample_rate)
    finally:
        engine.stop()


@pytest.mark.parametrize("fault", ["timeout", "disconnect", "overrun"])
def test_fault_finalizes_recording_and_shutdown(tmp_path, fault):
    settings = [ChannelSettings(i, str(i)) for i in range(4)]
    engine = AcquisitionEngine(AcquisitionConfig(), settings)
    engine.start()
    path = tmp_path / fault
    engine.start_recording(path, settings)
    wait_until(lambda: engine.recorded_scans >= 200)
    engine.transport.inject_fault(fault)
    wait_until(lambda: not engine.running)
    assert engine.error
    assert not engine.recording
    recovered = load_session(path)
    assert recovered.sample_count == engine.recorded_scans
    assert recovered.metadata["termination_reason"] == "error"
    assert recovered.metadata["error"] == engine.error
    engine.stop()  # a disconnected transport must not prevent window shutdown


def test_record_cannot_start_during_worker_shutdown(tmp_path):
    settings = [ChannelSettings(i, str(i)) for i in range(4)]
    engine = AcquisitionEngine(AcquisitionConfig(), settings)
    shutdown_entered, release = threading.Event(), threading.Event()
    original_stop = engine.session.stop

    def delayed_stop():
        shutdown_entered.set()
        assert release.wait(3)
        original_stop()

    engine.session.stop = delayed_stop
    engine.start()
    engine.transport.inject_fault("overrun")
    assert shutdown_entered.wait(3)
    try:
        with pytest.raises(RuntimeError, match="预览"):
            engine.start_recording(tmp_path / "must-not-exist", settings)
        assert not (tmp_path / "must-not-exist").exists()
    finally:
        release.set()
        engine.stop()


def test_hpf_direct_record_keeps_first_sample_tail_and_exact_clock(tmp_path):
    from labdaq.hpf import open_hpf
    from labdaq.inputs import HpfReplaySession
    from test_hpf import _force_hpf

    path = tmp_path / "source.hpf"
    expected = _force_hpf(path)
    reader = open_hpf(path, waveform_only=True)
    config = AcquisitionConfig(channels=(0, 2), sample_rate=3000)
    engine = AcquisitionEngine(config, reader.channels, block_seconds=.002,
                               input_session=HpfReplaySession(reader))
    output = tmp_path / "recorded"
    engine.start(recording_path=output)
    engine._thread.join(3)
    assert not engine.running and not engine.recording and not engine.error
    recording = load_session(output)
    np.testing.assert_array_equal(recording.read(count=100), expected)
    assert recording.sample_count == 6 and recording.sample_rate == reader.sample_rate
    assert recording.metadata["source"] == "file_replay"
    assert recording.metadata["termination_reason"] == "end_of_file"
    assert recording.metadata["acquisition_config"]["record_start_scan"] == 0
    assert recording.channels[1].sensitivity_mv_per_unit == 333.3330078125
    assert engine.session.closed
    engine.stop()


def test_mid_preview_record_has_source_offset_and_parameter_snapshot(tmp_path):
    from labdaq.signals import SignalSpec, SimulationSettings, generate_signals
    from labdaq.protocol import encode_adc, decode_adc

    settings = SimulationSettings(channels=(SignalSpec(waveform="sine", noise_rms_v=.01),) * 4, seed=81)
    channels = [ChannelSettings(i, str(i), unit="V") for i in (0, 2)]
    engine = AcquisitionEngine(AcquisitionConfig(channels=(0, 2)), channels, "custom", signal_settings=settings)
    engine.start()
    output = tmp_path / "partial-preview"
    try:
        wait_until(lambda: engine.total_scans >= 200)
        engine.start_recording(output, channels)
        wait_until(lambda: engine.recorded_scans >= 200)
        engine.stop_recording()
        recording = load_session(output)
        config = recording.metadata["acquisition_config"]
        assert config["record_start_scan"] >= 200
        assert config["input"]["signal_settings"]["seed"] == 81
        truth = generate_signals(settings, config["record_start_scan"], recording.sample_count, engine.sample_rate)[:, [0, 2]]
        truth = decode_adc(encode_adc(truth, (10, 10)), (10, 10))
        np.testing.assert_array_equal(recording.read(count=recording.sample_count), truth)
    finally:
        engine.stop()


def test_fixed_duration_records_exact_samples_then_keeps_preview_and_allows_next_group(tmp_path):
    channels = [ChannelSettings(0, "力 X"), ChannelSettings(2, "力 Z")]
    engine = AcquisitionEngine(AcquisitionConfig(sample_rate=2000, channels=(0, 2)), channels, "sine")
    first = tmp_path / "group1"
    engine.start(first, recording_options={"duration_seconds": .123, "notes": "第一组", "group_number": 1},
                 experiment="模拟拉伸")
    try:
        wait_until(lambda: not engine.recording)
        assert engine.running and engine.termination_reason == "duration_complete"
        assert engine.recording_target_scans == engine.recorded_scans == 246
        assert engine.recording_remaining_scans == 0
        recording = load_session(first)
        assert recording.sample_count == 246
        assert recording.metadata["termination_reason"] == "duration_complete"
        assert recording.metadata["target_sample_count"] == 246
        assert recording.metadata["experiment"] == "模拟拉伸"
        assert recording.metadata["recording_options"]["notes"] == "第一组"
        wait_until(lambda: engine.total_scans >= 500)
        assert engine.recorded_scans == 246
        second = tmp_path / "group2"
        engine.start_recording(second, channels)
        assert engine.termination_reason == "user_stop"
        assert engine.recording_target_scans is None and engine.recording_remaining_scans is None
        wait_until(lambda: engine.recorded_scans >= 100)
        engine.stop_recording()
        assert load_session(second).metadata["termination_reason"] == "user_stop"
    finally:
        engine.stop()


def test_subsample_duration_records_one_frame(tmp_path):
    channels = [ChannelSettings(0, "力")]
    engine = AcquisitionEngine(AcquisitionConfig(sample_rate=2000, channels=(0,)), channels)
    output = tmp_path / "single-frame"
    engine.start(output, recording_options={"duration_seconds": 1e-9})
    try:
        wait_until(lambda: not engine.recording)
        assert engine.recorded_scans == engine.recording_target_scans == 1
        assert load_session(output).read(count=10).shape == (1, 1)
        assert engine.running
    finally:
        engine.stop()


@pytest.mark.parametrize("duration, count, reason", [(.00125, 3, "duration_complete"), (.01, 6, "end_of_file")])
def test_hpf_duration_completion_preserves_reason_and_source_reaches_eof(tmp_path, duration, count, reason):
    from labdaq.hpf import open_hpf
    from labdaq.inputs import HpfReplaySession
    from test_hpf import _force_hpf

    source = tmp_path / "source.hpf"
    expected = _force_hpf(source)
    reader = open_hpf(source, waveform_only=True)
    engine = AcquisitionEngine(AcquisitionConfig(channels=(0, 2)), reader.channels,
                               block_seconds=.002, input_session=HpfReplaySession(reader))
    output = tmp_path / "recording"
    engine.start(output, recording_options={"duration_seconds": duration})
    engine._thread.join(3)
    try:
        assert not engine.running and not engine.recording and not engine.error
        assert engine.session.closed and engine.total_scans == len(expected)
        assert engine.termination_reason == reason
        recording = load_session(output)
        assert recording.sample_count == count
        assert recording.metadata["termination_reason"] == reason
        np.testing.assert_array_equal(recording.read(count=100), expected[:count])
    finally:
        engine.stop()


def test_record_pause_keeps_preview_and_duration_counts_only_stored_samples(tmp_path):
    from labdaq.protocol import encode_adc, decode_adc
    from labdaq.signals import SimulationSettings, generate_signals

    channels = [ChannelSettings(0, "力 X"), ChannelSettings(2, "力 Z")]
    settings = SimulationSettings()
    engine = AcquisitionEngine(AcquisitionConfig(sample_rate=2000, channels=(0, 2)), channels,
                               "custom", signal_settings=settings)
    output = tmp_path / "paused-timed"
    engine.start(output, recording_options={"duration_seconds": .3})
    try:
        wait_until(lambda: engine.recorded_scans >= 100)
        engine.pause_recording()
        captured, acquired = engine.recorded_scans, engine.total_scans
        remaining = engine.recording_remaining_scans
        assert engine.recording and engine.recording_paused
        wait_until(lambda: engine.total_scans >= acquired + 1000)
        assert engine.recorded_scans == captured and engine.recording_remaining_scans == remaining
        assert engine.recording and load_session(output).metadata["recording_paused"]
        engine.resume_recording()
        wait_until(lambda: not engine.recording)
        assert not engine.recording_paused and engine.recorded_scans == 600
        session = load_session(output)
        assert session.elapsed_duration > session.duration
        assert session.metadata["acquisition_started_at_utc"] == engine.acquisition_started_at_utc
        scans = session.sample_indices()
        expected = generate_signals(settings, 0, int(scans[-1]) + 1, engine.sample_rate)[scans][:, [0, 2]]
        expected = decode_adc(encode_adc(expected, (10, 10)), (10, 10))
        np.testing.assert_array_equal(session.read(), expected)
        second = tmp_path / "paused-stop"
        engine.start_recording(second, channels)
        assert not engine.recording_paused
        wait_until(lambda: engine.recorded_scans >= 100)
        engine.pause_recording()
        engine.stop_recording()
        assert not engine.recording and not engine.recording_paused and engine.running
        assert load_session(second).metadata["termination_reason"] == "user_stop"
    finally:
        engine.stop()
