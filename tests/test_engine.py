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
