"""Rust/Python numerical comparison and actual GUI-process crash/reconnect."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import asdict

import numpy as np
import pytest

from labdaq.core_client import CORE_EXE, ROOT, CoreClient
from labdaq.processing import ChannelSettings, convert_volts
from labdaq.protocol import AcquisitionConfig, plan_clock, encode_adc, decode_adc
from labdaq.recording import load_session
from labdaq.signals import SignalSpec, SimulationSettings, generate_signals


def until(function, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(.025)
    raise AssertionError("Timed out waiting for the core/GUI")


@pytest.fixture
def core(tmp_path):
    if not CORE_EXE.exists():
        pytest.skip("Build core/Cargo.toml in release mode first")
    client, _ = CoreClient.connect_or_start(tmp_path / "endpoint.json", port=0)
    try:
        yield client
    finally:
        try:
            client.request("stop")
            client.request("shutdown")
        except (OSError, ValueError):
            pass


def profile(settings=None, selected=(0, 1, 2, 3), rate=2000, coupling=("DC",) * 4, ranges=(10.0,) * 4):
    config = AcquisitionConfig(sample_rate=rate, channels=selected, coupling=coupling, ranges=ranges)
    return {"config": asdict(config), "actual_rate": plan_clock(rate).actual_rate,
            "channels": [asdict(ChannelSettings(i, f"输入 {i}", zero_volts=.13, inverted=i == 2)) for i in range(4)],
            "signal_settings": (settings or SimulationSettings()).to_dict(),
            "experiment": "核心测试", "recording_root": str(ROOT / "recordings")}


@pytest.mark.parametrize("waveform", ["dc", "sine", "square", "triangle", "sawtooth", "step", "pulse", "chirp"])
def test_rust_waveforms_and_recording_match_python(core, tmp_path, waveform):
    specs = tuple(SignalSpec(waveform=waveform, amplitude_v=.7 + i * .2, offset_v=.17,
                            phase_deg=-33 + i * 21, frequency_hz=13.7, start_s=.03,
                            duration_s=.075, end_frequency_hz=37, drift_v_per_s=.01) for i in range(4))
    settings = SimulationSettings(channels=specs)
    p = profile(settings, selected=(0, 2, 3), ranges=(10, 10, 1, 10))
    path = tmp_path / waveform
    core.request("start", profile=p, recording_path=str(path))
    until(lambda: core.request("poll")["recorded_scans"] >= 300)
    final = core.request("stop")
    result = load_session(path)
    expected = generate_signals(settings, 0, result.sample_count, result.sample_rate)[:, [0, 2, 3]]
    expected = decode_adc(encode_adc(expected, (10, 1, 10)), (10, 1, 10))
    np.testing.assert_allclose(result.read(), expected, rtol=0, atol=20 / 2**24)
    np.testing.assert_allclose(convert_volts(result.read(), result.channels), convert_volts(expected, result.channels),
                               rtol=0, atol=2e-4)
    assert result.sample_count == final["recorded_scans"] == final["total_scans"]
    assert result.metadata["acquisition_config"]["record_start_scan"] == 0
    assert result.metadata["finalized"] and result.metadata["termination_reason"] == "user_stop"
    assert result.metadata["source"] == "simulation"
    assert [c.index for c in result.channels] == [0, 2, 3]


def test_ac_model_matches_python_transport(core, tmp_path):
    from labdaq.simulator import SimulatedTransport
    from labdaq.transport import DaqSession
    signals = SimulationSettings(channels=(SignalSpec(offset_v=.4),) * 4)
    p = profile(signals, coupling=("AC", "DC", "AC", "DC"))
    path = tmp_path / "ac"
    core.request("start", profile=p, recording_path=str(path))
    until(lambda: core.request("poll")["total_scans"] >= 300)
    core.request("stop")
    recording = load_session(path)
    config = AcquisitionConfig(**p["config"])
    with DaqSession(SimulatedTransport(signal_settings=signals)) as reference:
        reference.configure(config)
        reference.start()
        expected = reference.read(recording.sample_count)
    np.testing.assert_allclose(recording.read(), expected, rtol=0, atol=20 / 2**24)


def test_noise_is_repeatable_across_selected_channels(core, tmp_path):
    signals = SimulationSettings(channels=(SignalSpec(waveform="noise", noise_rms_v=.3),) * 4, seed=77)
    results = []
    for selected in ((0, 1, 2, 3), (0, 2, 3)):
        path = tmp_path / f"noise-{len(selected)}"
        core.request("start", profile=profile(signals, selected=selected, rate=100000), recording_path=str(path))
        until(lambda: core.request("poll")["recorded_scans"] >= 100000)
        core.request("stop")
        results.append(load_session(path).read(count=100000))
    np.testing.assert_array_equal(results[0][:, [0, 2, 3]], results[1])
    assert np.max(np.abs(np.mean(results[0], axis=0))) < .003
    np.testing.assert_allclose(np.std(results[0], axis=0), .3, atol=.003, rtol=0)
    assert abs(np.corrcoef(results[0][:, :2], rowvar=False)[0, 1]) < .02


def test_mid_preview_record_no_overwrite_and_invalid_request(core, tmp_path):
    p = profile(selected=(1, 3))
    core.request("start", profile=p)
    until(lambda: core.request("poll")["total_scans"] >= 200)
    path = tmp_path / "group"
    core.request("start_recording", path=str(path), channels=p["channels"], experiment="组一")
    with pytest.raises(RuntimeError, match="先结束"):
        core.request("shutdown")
    with pytest.raises(RuntimeError, match="不能修改"):
        core.request("set_channels", channels=p["channels"])
    until(lambda: core.request("poll")["recorded_scans"] >= 200)
    core.request("stop_recording")
    first = load_session(path)
    first_bytes = (path / "raw.f64").read_bytes()
    assert first.metadata["acquisition_config"]["record_start_scan"] >= 200
    assert core.request("poll")["running"]
    with pytest.raises(RuntimeError):
        core.request("start_recording", path=str(path), channels=p["channels"], experiment="覆盖")
    assert (path / "raw.f64").read_bytes() == first_bytes
    second = tmp_path / "group2"
    core.request("start_recording", path=str(second), channels=p["channels"], experiment="组二")
    until(lambda: core.request("poll")["recorded_scans"] >= 100)
    core.request("stop")
    assert load_session(second).metadata["experiment"] == "组二"
    bad = profile(selected=(0, 2))
    bad["config"]["channels"] = [2, 0]
    with pytest.raises(RuntimeError, match="配置无效"):
        core.request("start", profile=bad)
    assert not core.request("poll")["running"]


def test_high_rate_ring_is_bounded_and_record_keeps_every_scan(core, tmp_path):
    path = tmp_path / "high-rate"
    core.request("start", profile=profile(rate=105469), recording_path=str(path))
    until(lambda: core.request("poll")["total_scans"] >= 240000)
    state = core.request("poll", seconds=12)
    assert state["capacity"] == state["buffered_scans"] == 200000
    assert len(state["raw"]) == 200000 * 4
    final = core.request("stop")
    result = load_session(path)
    assert result.sample_count == final["total_scans"] > 200000
    np.testing.assert_allclose(result.read(state["snapshot_start"], 200000),
                               np.asarray(state["raw"]).reshape(-1, 4), atol=0, rtol=0)


def test_killed_qt_process_keeps_recording_and_new_gui_rejoins(tmp_path):
    if not CORE_EXE.exists():
        pytest.skip("Build the Rust core first")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    endpoint, ready = tmp_path / "endpoint.json", tmp_path / "ready.json"
    source = '''
import json, os, sys
from pathlib import Path
from dataclasses import replace
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6 import QtWidgets as W
from labdaq.gui import MainWindow, configure_application
from labdaq.core_client import CoreClient
from labdaq.signals import SimulationSettings, SignalSpec
endpoint, ready = map(Path, sys.argv[1:])
CoreClient.connect_or_start(endpoint, port=0)
app = W.QApplication([])
configure_application(app)
w = MainWindow(core_endpoint=endpoint)
w.recording_root = endpoint.parent
w.scenario.setCurrentIndex(w.scenario.findData("rust"))
w.active_channels = (0, 2, 3)
w.channels[2] = replace(w.channels[2], name="重连通道", zero_volts=.123, inverted=True)
w.signal_settings = SimulationSettings(channels=(SignalSpec(frequency_hz=13.7),) * 4)
w.experiment.setText("GUI崩溃续录")
w.toggle_record()
assert w.engine and w.engine.recording, w.notice.text()
w.show()
ready.write_text(json.dumps({"path": str(w.latest_recording)}), encoding="utf-8")
app.exec()
'''
    process = subprocess.Popen([sys.executable, "-c", source, str(endpoint), str(ready)], cwd=ROOT,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    client = None
    window = None
    try:
        until(ready.exists, timeout=15)
        path = Path(json.loads(ready.read_text())["path"])
        client = CoreClient(endpoint)
        before = client.request("poll")
        process.kill()  # TerminateProcess on Windows; closeEvent cannot run.
        process.wait(5)
        until(lambda: client.request("poll")["recorded_scans"] >= before["recorded_scans"] + 600)
        assert (path / "raw.f64").stat().st_size > before["recorded_scans"] * 3 * 8
        from PySide6 import QtCore, QtWidgets as W
        from labdaq.gui import MainWindow, configure_application
        app = W.QApplication.instance() or W.QApplication([])
        configure_application(app)
        window = MainWindow(core_endpoint=endpoint)
        assert window.engine.recording and window.latest_recording == path
        assert window.active_channels == (0, 2, 3)
        assert window.channels[2].name == "重连通道" and window.channels[2].zero_volts == .123
        assert window.channels[2].inverted and window.experiment.text() == "GUI崩溃续录"
        assert window.scenario.currentData() == "rust" and not window.rate.isEnabled()
        window.pause_plot.setChecked(True)
        window.stop_acquisition()
        recording = load_session(path)
        settings = SimulationSettings(channels=(SignalSpec(frequency_hz=13.7),) * 4)
        truth = generate_signals(settings, 0, recording.sample_count, recording.sample_rate)[:, [0, 2, 3]]
        truth = decode_adc(encode_adc(truth, (10, 10, 10)), (10, 10, 10))
        np.testing.assert_allclose(recording.read(), truth, rtol=0, atol=20 / 2**24)
        assert recording.sample_count > before["recorded_scans"] + 600
        assert recording.metadata["termination_reason"] == "user_stop"
        assert len(list(tmp_path.glob("*/metadata.json"))) == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(5)
        if process.stderr:
            errors = process.stderr.read().decode("utf-8", errors="replace")
            process.stderr.close()
            if not ready.exists():
                print(errors)
        if client is None and endpoint.exists():
            client = CoreClient(endpoint)
        if client:
            try:
                client.request("stop")
                client.request("shutdown")
            except (OSError, ValueError):
                pass
        if window:
            window.engine.detach()
            window.engine = None
            window.close()
            window.deleteLater()
            QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
            app.processEvents()


def test_gui_zero_pause_and_normal_close_preserve_core_session(core, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtWidgets as W
    from labdaq.gui import MainWindow, configure_application
    app = W.QApplication.instance() or W.QApplication([])
    configure_application(app)
    first = MainWindow(core_endpoint=core.endpoint)
    second = None
    try:
        first.scenario.setCurrentIndex(first.scenario.findData("rust"))
        first.recording_root = tmp_path
        first.signal_settings = SimulationSettings(channels=(SignalSpec(waveform="dc", amplitude_v=.4),) * 4)
        first.start_preview()
        until(lambda: len(first.engine.snapshot()[0]) >= 200)
        first.zero_channels()
        assert first.channels[0].zero_volts == pytest.approx(.4, abs=20 / 2**24)
        first.toggle_record()
        path = first.latest_recording
        first.pause_plot.setChecked(True)
        initial = core.request("poll")["recorded_scans"]
        first.close()
        until(lambda: core.request("poll")["recorded_scans"] >= initial + 200)
        second = MainWindow(core_endpoint=core.endpoint)
        assert second.engine.recording and second.latest_recording == path
        assert second.channels[0].zero_volts == first.channels[0].zero_volts
        second.stop_acquisition()
        result = load_session(path)
        np.testing.assert_allclose(result.read(), .4, rtol=0, atol=20 / 2**24)
        np.testing.assert_allclose(convert_volts(result.read(), result.channels), 0, rtol=0, atol=1e-12)
    finally:
        for window in (first, second):
            if window:
                if window.engine:
                    window.engine.detach()
                window.engine = None
                window.close()
                window.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        app.processEvents()
